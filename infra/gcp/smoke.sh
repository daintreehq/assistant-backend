#!/usr/bin/env bash
#
# Basic end-to-end check of a deployed Daintree Assistant Server.
#
#   ./infra/gcp/smoke.sh                     # public endpoints + metadata routes
#   ./infra/gcp/smoke.sh --full              # ... plus two real model turns
#
#   ./infra/gcp/smoke.sh --url https://assistant.daintree.org
#                                            # test a specific host instead of the
#                                            # Cloud Run URL discovered from gcloud
#
# NO BEARER TOKEN IS NEEDED. The server holds its own upstream credential
# (TOGETHER_API_KEY) and funds every turn from it, so the metadata routes answer
# an anonymous request. That is also why --full is opt-in and everything else
# here is not: --full spends DAINTREE's money, not the caller's.
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
    # `shift 2` on a flag with no value shifts once and leaves the flag in place,
    # which without `set -e` spins forever.
    --url)  [ $# -gt 1 ] || { echo "--url requires a value" >&2; exit 2; }
            URL="$2"; shift 2 ;;
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
  pass "/readyz is ready ($(jq_field "$BODY" runbooks) runbooks, catalog $(jq_field "$BODY" catalog_revision))"
else
  # A 503 here is a configuration failure — a missing or weak secret, a
  # malformed prompt template, an unparseable runbook file. Production withholds
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
head_ "Authentication posture"

# The door is open, deliberately and temporarily: the server holds the upstream
# credential, so a request without a bearer has one to spend. Asserted rather
# than assumed — this is the check that must start failing when account
# authentication lands.
code="$(req "$URL/v1/daintree/capabilities")"; load_body
if [ "$code" = "200" ]; then
  pass "a request with no bearer token is served (server holds the credential)"
else
  fail "expected 200 without a bearer token, got $code" "$BODY"
fi

# A header that IS present is ignored too, not merely tolerated. A CLI still
# sending its old OpenRouter key must be served, not have that key forwarded to
# Together and rejected as the caller's own.
code="$(req -H 'Authorization: Bearer sk-or-v1-a-stale-openrouter-key' "$URL/v1/daintree/capabilities")"; load_body
if [ "$code" = "200" ]; then
  pass "a stale OpenRouter bearer is ignored, not forwarded"
else
  fail "expected 200 for a stale bearer, got $code" "$BODY"
fi

# --------------------------------------------------------------------------
if true; then
  # No Authorization header: the server funds the turn. Kept as an array so the
  # call sites below are unchanged, and so a future account credential slots in
  # here rather than being threaded through each one.
  AUTH=()

  head_ "Metadata routes (no upstream model call, no cost)"

  code="$(req "${AUTH[@]}" "$URL/v1/daintree/capabilities")"; load_body
  if [ "$code" = "200" ]; then
    pass "/v1/daintree/capabilities"
  else
    fail "/v1/daintree/capabilities returned $code" "$BODY"
  fi

  code="$(req "${AUTH[@]}" "$URL/v1/daintree/runbooks")"; load_body
  if [ "$code" = "200" ]; then
    n="$(printf '%s' "$BODY" | python3 -c "import json,sys
print(len(json.load(sys.stdin).get('data', [])))" 2>/dev/null)"
    pass "/v1/daintree/runbooks lists ${n:-?} runbooks (catalog metadata only, never bodies)"
  else
    fail "/v1/daintree/runbooks returned $code" "$BODY"
  fi

  # The sign-in probe. Free — a tokenless key introspection against the provider,
  # not a model call — and the one route whose verdict a tester acts on directly,
  # so a deployment where it answers wrongly breaks onboarding for everyone at
  # once. `valid` and `usable` are DIFFERENT questions and the CLI branches on
  # `reason`, so all three are asserted rather than just the status code.
  code="$(req "${AUTH[@]}" -X POST "$URL/v1/daintree/auth/verify")"; load_body
  if [ "$code" = "200" ]; then
    # Checked as a TUPLE against the verdicts the contract defines, not field by
    # field. Any-non-empty-reason would accept `{"reason":"ok"}` with no verdict
    # at all, which is precisely the shape a half-implemented route returns.
    # Types are checked too: `jq_field` prints Python reprs, so a JSON string
    # "True" and a JSON boolean true are indistinguishable downstream.
    verdict="$(printf '%s' "$BODY" | python3 -c "import json,sys
d = json.load(sys.stdin)
v, u, r = d.get('valid'), d.get('usable'), d.get('reason')
if not all(isinstance(x, bool) for x in (v, u)) or not isinstance(r, str):
    print('MALFORMED')
elif (v, u, r) == (True, True, 'ok'):
    print('OK')
elif (v, u, r) == (True, False, 'credits_exhausted'):
    print('EXHAUSTED')
elif (v, u, r) == (False, False, 'provider_rejected'):
    print('REJECTED')
else:
    print('INCONSISTENT')" 2>/dev/null)"
    case "${verdict:-MALFORMED}" in
      OK)        pass "/v1/daintree/auth/verify: valid, usable, reason=ok" ;;
      # A true verdict about the key, not a broken deployment — so not a smoke
      # failure. Surfaced loudly because it explains the --full turns failing.
      EXHAUSTED) pass "/v1/daintree/auth/verify: key recognised, NO CREDIT REMAINING" ;;
      REJECTED)  pass "/v1/daintree/auth/verify: key REJECTED by the provider" ;;
      *)         fail "/v1/daintree/auth/verify returned an inconsistent verdict" "$BODY" ;;
    esac
  else
    fail "/v1/daintree/auth/verify returned $code" "$BODY"
  fi

  # Protocol negotiation, on both content routes. A client one version ahead must
  # get 426 rather than a 400 blaming its fields — see docs/DAINTREE_API.md. This
  # sends a body the current build parses so a 426 can only come from the header.
  for path in respond tasks; do
    case "$path" in
      respond) payload='{"protocol_version":3,"session":{"id":"smoke-426","turn_id":"t"},"startup":{},"input":{"messages":[{"role":"user","content":"hi"}]}}' ;;
      tasks)   payload='{"task":"terminal_judge","input":{"question":"q","tail":"t"}}' ;;
    esac
    code="$(req "${AUTH[@]}" -X POST "$URL/v1/daintree/$path" \
      -H 'content-type: application/json' -H 'X-Daintree-Protocol: 999' -d "$payload")"
    load_body
    if [ "$code" = "426" ]; then
      pass "/v1/daintree/$path rejects an unsupported protocol with 426"
    else
      fail "/v1/daintree/$path returned $code for protocol 999 (expected 426)" "$BODY"
    fi
  done

  # One correlation id, echoed back. This is the primitive a tester quotes in a
  # bug report, so a deployment where it is missing or rewritten costs every
  # support conversation afterwards.
  hdrs="$(curl -sS -o /dev/null -D - --max-time 20 "${AUTH[@]}" \
    -H 'X-Request-Id: smoke-correlation-1' "$URL/v1/daintree/capabilities" 2>/dev/null \
    | tr -d '\r')"
  echoed="$(printf '%s' "$hdrs" | awk 'tolower($1) == "x-request-id:" { print $2 }')"
  if [ "$echoed" = "smoke-correlation-1" ]; then
    pass "X-Request-Id is echoed back unchanged"
  else
    fail "X-Request-Id came back as '${echoed:-<missing>}', expected smoke-correlation-1"
  fi

  # A malformed request must produce the TYPED envelope rather than a bare
  # framework error — that envelope is what the CLI branches on to tell a user
  # whose problem it is. (The request-id check above covers the header; this one
  # is about the body shape.)
  code="$(req "${AUTH[@]}" -X POST "$URL/v1/daintree/tasks" \
    -H 'content-type: application/json' -d '{"task":"no-such-task","input":{}}')"
  load_body
  err_code="$(printf '%s' "$BODY" | python3 -c "import json,sys
print((json.load(sys.stdin).get('error') or {}).get('code',''))" 2>/dev/null)"
  if [ "$code" = "404" ] && [ "$err_code" = "unknown_task" ]; then
    pass "an unknown task returns 404 unknown_task in the typed envelope"
  else
    fail "unknown task returned $code / code='$err_code' (expected 404 / unknown_task)" "$BODY"
  fi

  if [ "$FULL" = "1" ]; then
    head_ "Real model turns (these spend money on your key)"

    read -r -d '' PAYLOAD <<'JSON'
{"protocol_version":3,
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
print(','.join(s.get('id','') for s in (d.get('runbooks') or {}).get('active', [])) or 'none')" 2>/dev/null)"
      pass "POST /v1/daintree/respond -> \"$reply\" (active runbooks: $active)"
    else
      # A 502 here points at the SERVER's credential, not the caller's: an
      # invalid or unfunded TOGETHER_API_KEY surfaces as the upstream's rejection
      # relayed through this service, since there is nothing else to fall back on.
      fail "POST /v1/daintree/respond returned $code" "$BODY"
    fi

    # Streaming matters enough to check separately: the first event must be
    # `meta` (the runbooks block), which the CLI relies on to show a runbook loading
    # while the model is still generating.
    stream_out="$(mktemp)"
    TIMEOUT=180
    curl -sS --max-time 180 "${AUTH[@]}" -X POST "$URL/v1/daintree/respond" \
      -H 'content-type: application/json' \
      -d '{"protocol_version":3,"session":{"id":"smoke-test","turn_id":"turn-2"},"startup":{},"generation":{"stream":true},"input":{"messages":[{"role":"user","content":"Reply with exactly: OK"}]}}' \
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
