#!/usr/bin/env bash
#
# Basic end-to-end check of a deployed Daintree Assistant Server.
#
#   ./infra/gcp/smoke.sh                     # public endpoints + the auth gate
#   OPENROUTER_API_KEY=sk-or-... ./infra/gcp/smoke.sh
#                                            # ... plus the authenticated metadata routes
#   OPENROUTER_API_KEY=sk-or-... ./infra/gcp/smoke.sh --full
#                                            # ... plus two real model turns
#
#   ./infra/gcp/smoke.sh --url https://assistant.daintree.org
#                                            # test a specific host instead of the
#                                            # Cloud Run URL discovered from gcloud
#
# Note what the bearer token is: this server has NO API key of its own. The
# Authorization header carries the CALLER's OpenRouter key, which both admits the
# request and pays for its upstream calls. That is why --full is opt-in — it
# spends real money on whichever key you export, and nothing else here does.
#
# Exits non-zero on the first failed check so it is usable as a gate.

set -uo pipefail

PROJECT="${PROJECT:-daintree-research}"
REGION="${REGION:-us-central1}"
SERVICE="${SERVICE:-daintree-assistant}"
URL=""
FULL=0

while [ $# -gt 0 ]; do
  case "$1" in
    --url)  URL="${2:-}"; shift 2 ;;
    --full) FULL=1; shift ;;
    -h|--help) sed -n '2,22p' "$0" | sed 's/^# \{0,1\}//'; exit 0 ;;
    *) echo "unknown argument: $1" >&2; exit 2 ;;
  esac
done

if [ -z "$URL" ]; then
  URL="$(gcloud run services describe "$SERVICE" --region="$REGION" \
          --project="$PROJECT" --format='value(status.url)' 2>/dev/null)"
fi
if [ -z "$URL" ]; then
  echo "Could not determine the service URL. Pass --url https://host" >&2
  exit 2
fi
URL="${URL%/}"

PASS=0
FAIL=0
pass() { PASS=$((PASS + 1)); printf '  \033[32mPASS\033[0m  %s\n' "$1"; }
fail() { FAIL=$((FAIL + 1)); printf '  \033[31mFAIL\033[0m  %s\n' "$1"; [ -n "${2:-}" ] && printf '        %s\n' "$2"; }
head_() { printf '\n\033[1m%s\033[0m\n' "$1"; }

# Prints the HTTP status code and leaves the response body in $BODY.
#
# The body goes through a file rather than being assigned to BODY directly:
# callers use `code="$(req ...)"`, and command substitution runs in a subshell,
# so any variable this function set would be discarded on return. The file
# survives; BODY is then reloaded in the caller's own shell.
BODY=""
BODY_FILE="$(mktemp)"
trap 'rm -f "$BODY_FILE"' EXIT
req() {
  curl -sS -o "$BODY_FILE" -w '%{http_code}' --max-time "${TIMEOUT:-30}" "$@" 2>/dev/null
}
load_body() { BODY="$(cat "$BODY_FILE" 2>/dev/null)"; }

jq_field() { printf '%s' "$1" | python3 -c "import json,sys
try: print(json.load(sys.stdin).get('$2',''))
except Exception: print('')" 2>/dev/null; }

echo "Target: $URL"

# --------------------------------------------------------------------------
head_ "Public endpoints (no authentication required)"

# /health, not /healthz. Google's frontend reserves the /healthz path and
# answers it itself with an HTML 404 — the request never reaches the container
# (its response carries no `server: Google Frontend` header, unlike every route
# that does reach the app). /healthz still serves Cloud Run's own liveness
# probe, which connects to the container directly; /health is the alias that is
# reachable from outside.
code="$(req "$URL/health")"; load_body
if [ "$code" = "200" ] && [ "$(jq_field "$BODY" status)" = "ok" ]; then
  pass "/health is alive"
else
  fail "/health returned $code" "$BODY"
fi

code="$(req "$URL/readyz")"; load_body
if [ "$code" = "200" ] && [ "$(jq_field "$BODY" status)" = "ready" ]; then
  pass "/readyz is ready ($(jq_field "$BODY" skills) skills, catalog $(jq_field "$BODY" catalog_revision))"
else
  # A 503 here is a configuration failure — a missing or weak secret, a
  # malformed prompt template, an unparseable skill file. Production withholds
  # the reason, so the detail is in the Cloud Run logs.
  fail "/readyz returned $code" "$BODY"
  echo "        gcloud run services logs read $SERVICE --region=$REGION --project=$PROJECT --limit=50"
fi

code="$(req "$URL/version")"; load_body
if [ "$code" = "200" ]; then
  pass "/version: $(jq_field "$BODY" server_version) build $(jq_field "$BODY" build_sha)"
else
  fail "/version returned $code" "$BODY"
fi

# --------------------------------------------------------------------------
head_ "Authentication gate"

code="$(req "$URL/v1/daintree/capabilities")"; load_body
if [ "$code" = "401" ]; then
  pass "an unauthenticated request is rejected with 401"
else
  fail "expected 401 without a bearer token, got $code" "$BODY"
fi

code="$(req -H 'Authorization: Bearer' "$URL/v1/daintree/capabilities")"; load_body
if [ "$code" = "401" ]; then
  pass "a malformed bearer header is rejected with 401"
else
  fail "expected 401 for a malformed header, got $code" "$BODY"
fi

# --------------------------------------------------------------------------
KEY="${OPENROUTER_API_KEY:-}"
if [ -z "$KEY" ]; then
  head_ "Authenticated routes"
  echo "  SKIP  set OPENROUTER_API_KEY to exercise these"
else
  AUTH=(-H "Authorization: Bearer $KEY")

  head_ "Authenticated routes (metadata only — no upstream model call, no cost)"

  code="$(req "${AUTH[@]}" "$URL/v1/daintree/capabilities")"; load_body
  if [ "$code" = "200" ]; then
    pass "/v1/daintree/capabilities"
  else
    fail "/v1/daintree/capabilities returned $code" "$BODY"
  fi

  code="$(req "${AUTH[@]}" "$URL/v1/daintree/skills")"; load_body
  if [ "$code" = "200" ]; then
    n="$(printf '%s' "$BODY" | python3 -c "import json,sys
print(len(json.load(sys.stdin).get('data', [])))" 2>/dev/null)"
    pass "/v1/daintree/skills lists ${n:-?} skills (catalog metadata only, never bodies)"
  else
    fail "/v1/daintree/skills returned $code" "$BODY"
  fi

  if [ "$FULL" = "1" ]; then
    head_ "Real model turns (these spend money on your key)"

    read -r -d '' PAYLOAD <<'JSON'
{"protocol_version":2,
 "session":{"id":"smoke-test","turn_id":"turn-1"},
 "startup":{},
 "input":{"messages":[{"role":"user","content":"Reply with exactly: OK"}]}}
JSON

    TIMEOUT=180 code="$(req "${AUTH[@]}" -X POST "$URL/v1/daintree/respond" \
      -H 'content-type: application/json' -d "$PAYLOAD")"; load_body
    if [ "$code" = "200" ]; then
      reply="$(printf '%s' "$BODY" | python3 -c "import json,sys
d=json.load(sys.stdin)
m=d.get('message') or {}
print((m.get('content') or '')[:60].replace(chr(10),' '))" 2>/dev/null)"
      active="$(printf '%s' "$BODY" | python3 -c "import json,sys
d=json.load(sys.stdin)
print(','.join(s.get('id','') for s in (d.get('skills') or {}).get('active', [])) or 'none')" 2>/dev/null)"
      pass "POST /v1/daintree/respond -> \"$reply\" (active skills: $active)"
    else
      # 502 upstream_error is the shape an invalid or unfunded caller key takes:
      # the server has no key to fall back on, so OpenRouter's rejection surfaces
      # here rather than as a 401 from this service.
      fail "POST /v1/daintree/respond returned $code" "$BODY"
    fi

    # Streaming matters enough to check separately: the first event must be
    # `meta` (the skills block), which the CLI relies on to show a skill loading
    # while the model is still generating.
    stream_out="$(mktemp)"
    TIMEOUT=180
    curl -sS --max-time 180 "${AUTH[@]}" -X POST "$URL/v1/daintree/respond" \
      -H 'content-type: application/json' \
      -d '{"protocol_version":2,"session":{"id":"smoke-test","turn_id":"turn-2"},"startup":{},"generation":{"stream":true},"input":{"messages":[{"role":"user","content":"Reply with exactly: OK"}]}}' \
      > "$stream_out" 2>/dev/null
    first_event="$(grep -m1 '^event:' "$stream_out" | sed 's/^event: *//')"
    if [ "$first_event" = "meta" ] && grep -q '^event: *done' "$stream_out"; then
      pass "streaming turn: first event is 'meta', stream ends with 'done'"
    else
      fail "streaming turn: first event was '${first_event:-none}'" "$(head -5 "$stream_out")"
    fi
    rm -f "$stream_out"
  else
    head_ "Real model turns"
    echo "  SKIP  pass --full to send two turns (spends money on your key)"
  fi
fi

# --------------------------------------------------------------------------
printf '\n\033[1m%d passed, %d failed\033[0m\n' "$PASS" "$FAIL"
[ "$FAIL" -eq 0 ]
