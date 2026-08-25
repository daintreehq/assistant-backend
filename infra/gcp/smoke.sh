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
#   ./infra/gcp/smoke.sh --url https://assistant.daintree.org --expect-env staging
#                                            # ... and fail if it reports a
#                                            # different ENVIRONMENT
#
# NO BEARER TOKEN IS NEEDED. The server holds its own upstream credential
# (TOGETHER_API_KEY) and funds every turn from it, so the metadata routes answer
# an anonymous request. That is also why --full is opt-in and everything else
# here is not: --full spends DAINTREE's money, not the caller's.
#
# Every check runs; failures are counted and the script exits non-zero at the end,
# so one broken route does not hide the rest. Usable as a gate on the exit code.

set -uo pipefail

PROJECT="${PROJECT:-daintree-research}"
REGION="${REGION:-us-central1}"
SERVICE="${SERVICE:-daintree-assistant}"
URL=""
FULL=0
EXPECT_ENV=""

while [ $# -gt 0 ]; do
  case "$1" in
    # `shift 2` on a flag with no value shifts once and leaves the flag in place,
    # which without `set -e` spins forever.
    --url)  [ $# -gt 1 ] || { echo "--url requires a value" >&2; exit 2; }
            URL="$2"; shift 2 ;;
    --full) FULL=1; shift ;;
    # Assert the deployment reports the environment you think you are pointing
    # at. Optional, because the common use is "check whatever is there" — but a
    # staging URL answering `production` is exactly the mix-up worth catching,
    # and it is invisible without something to compare against.
    --expect-env)
            [ $# -gt 1 ] || { echo "--expect-env requires a value" >&2; exit 2; }
            # A blank value is refused rather than treated as "no expectation":
            # the natural call passes a shell variable, and an unset one there
            # would silently turn the check off — the one thing a check asked for
            # by name must never do. (Written without a dollar sign on purpose:
            # tests/unit/test_shell_scripts.py scans this file for variable reads
            # and does not strip comments, so an example here reads as a real use.)
            case "$2" in
              development|test|staging|production) ;;
              *) echo "--expect-env must be development|test|staging|production, got '$2'" >&2; exit 2 ;;
            esac
            EXPECT_ENV="$2"; shift 2 ;;
    -h|--help) sed -n '2,21p' "$0" | sed 's/^# \{0,1\}//'; exit 0 ;;
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

# A dotted path, for the nested shapes (/version's `protocol.min`). Separate from
# jq_field rather than replacing it so the existing call sites keep their exact
# behaviour, including printing Python reprs for the type checks below.
jq_path() { printf '%s' "$1" | python3 -c "
import json,sys
try:
    v = json.load(sys.stdin)
    for part in '$2'.split('.'):
        v = v[part]
    print(v)
except Exception:
    print('')" 2>/dev/null; }

# Whether a body is JSON at all. The distinction this draws is the point of the
# whole exercise: a route that answers with HTML, or with a redirect to a login
# page, is not a degraded API — it is a different service standing where the API
# should be, and every field check above it silently reads an empty string and
# reports a mismatch that names the wrong problem.
is_json() { printf '%s' "$1" | python3 -c "
import json,sys
try:
    json.load(sys.stdin)
except Exception:
    sys.exit(1)" 2>/dev/null; }

# Status, headers and body from ONE request, into $BODY_FILE and $HDR_FILE.
#
# One request rather than two, and that is a correctness point rather than a
# saving: asking for the status and then separately for the body lets a route
# answer 200 to the first and 302 to the second, and the pair then reads as a
# 200 with an empty, non-HTML body — a PASS assembled from two different
# answers. `req` stays for the callers that only want a body.
HDR_FILE="$(mktemp)"
trap 'rm -f "$BODY_FILE" "$HDR_FILE"' EXIT
probe() {
  curl -sS -o "$BODY_FILE" -D "$HDR_FILE" -w '%{http_code}' \
    --max-time "${TIMEOUT:-30}" "$@" 2>/dev/null
}

# Whether a response is a page rather than an API answer. Checks the declared
# content type AND the body, because either one alone is evadable: a proxy can
# serve HTML as application/octet-stream, and a JSON error page can carry
# text/html. Leading whitespace, a UTF-8 BOM, an XML declaration and a comment are
# all skipped before the tag is looked for, since none of them stops a browser —
# or a client following a redirect — from treating what follows as a document.
looks_like_a_page() {
  if grep -qi '^content-type:[[:space:]]*text/html' "$HDR_FILE" 2>/dev/null; then
    return 0
  fi
  python3 -c "
import re, sys
body = sys.argv[1].lstrip('\ufeff \t\r\n')
while body.startswith('<?') or body.startswith('<!--'):
    end = body.find('?>') + 2 if body.startswith('<?') else body.find('-->') + 3
    if end < 2:
        break
    body = body[end:].lstrip()
sys.exit(0 if re.match(r'<(!doctype\b|html\b)', body, re.I) else 1)" "$1" 2>/dev/null
}

# The `Location` a redirect points at, for the failure message. curl writes CRLF
# line endings, so the carriage return is stripped or it lands in the middle of
# the output.
redirect_target() {
  grep -i '^location:' "$HDR_FILE" 2>/dev/null | head -1 | tr -d '\r'
}

# A published browser link, checked the way the SERVER checks it: by parsing the
# URL and comparing the HOSTNAME.
#
# This replaced a shell glob, and the glob was not merely loose — it passed
# hostile values. `https://*.daintree.org/*` looks like a host pattern and is not:
# `*` spans slashes, so `https://evil.com/.daintree.org/x` matches it. The server
# parses and would refuse that; a smoke check must not be the looser of the two,
# because its whole job is to catch a bad value that reached production.
#
# Deliberately stricter than the glob in the other direction too, matching the
# server: userinfo is refused outright (`https://daintree.org@evil.com/` reaches
# evil.com), the scheme must be https, and one trailing dot is stripped because
# `daintree.org.` and `daintree.org` are the same name to DNS.
link_host_ok() { printf '%s' "$1" | python3 -c "
import sys
from urllib.parse import urlsplit
try:
    p = urlsplit(sys.stdin.read().strip())
except ValueError:
    sys.exit(1)
if p.scheme != 'https' or p.username or p.password:
    sys.exit(1)
host = (p.hostname or '').lower().rstrip('.')
sys.exit(0 if host == 'daintree.org' or host.endswith('.daintree.org') else 1)" 2>/dev/null; }

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

# /version is the round trip that confirms a release: the build step bakes
# COMMIT_SHA in and this reports it back. Three things are checked rather than
# one, because "200" was passing for bodies that could not have come from this
# service.
code="$(req "$URL/version")"; load_body
BUILD_SHA="$(jq_field "$BODY" build_sha)"
PROTO_MIN="$(jq_path "$BODY" protocol.min)"
PROTO_MAX="$(jq_path "$BODY" protocol.max)"
if [ "$code" != "200" ]; then
  fail "/version returned $code" "$BODY"
elif ! is_json "$BODY"; then
  # An HTML body here means something in front of the service answered — a
  # platform error page, or a login redirect that a client with -L would have
  # followed and then tried to parse as JSON.
  fail "/version did not return JSON" "$(printf '%.120s' "$BODY")"
elif [ -z "$BUILD_SHA" ]; then
  # An empty build SHA means the image was built without --build-arg BUILD_SHA,
  # so /version can no longer identify what is deployed and the release gate that
  # compares it against the commit has nothing to compare.
  fail "/version reports an empty build_sha" "$BODY"
elif [ "$PROTO_MIN" != "3" ] || [ "$PROTO_MAX" != "3" ]; then
  fail "/version negotiates protocol $PROTO_MIN..$PROTO_MAX, expected 3..3" "$BODY"
else
  pass "/version: $(jq_field "$BODY" server_version) build $BUILD_SHA, protocol $PROTO_MIN..$PROTO_MAX"
fi

# --------------------------------------------------------------------------
head_ "OAuth discovery (public, and answered even while unready)"

# This is the bootstrap: a client reads it to learn how to obtain a credential,
# so it must answer to a caller holding none. It is deliberately NOT gated on
# readiness — the moment someone most needs to know how to sign in is the moment
# the rest of the service is unhappy — which is also why it is checked here
# rather than folded into the authenticated section below.
# The manifest is read ONCE, with types, rather than field by field as strings.
#
# `jq_field` prints `str(value)`, which makes a JSON `true` and the JSON string
# `"True"` identical to every check downstream — so a manifest declaring
# `"required": "True"` would drive the posture branch below while being a body
# no client can use. The client decodes this into typed Go fields and refuses a
# mismatch, so publishing the wrong TYPE is publishing a broken manifest, and a
# check that cannot see the difference is not checking the thing that breaks.
#
# It prints one line: `ok <environment> <configured> <required>`, or
# `bad <reason>`. The posture below reads the parsed value, never the raw body.
read_manifest() { printf '%s' "$1" | python3 -c "
import json, sys
try:
    m = json.load(sys.stdin)
except Exception:
    print('bad not-JSON'); raise SystemExit
if not isinstance(m, dict):
    print('bad not-a-JSON-object'); raise SystemExit
v = m.get('version')
# \`isinstance(True, int)\` is True in Python, so the bool is excluded explicitly:
# a manifest carrying \`"version": true\` would otherwise compare equal to 1.
if not isinstance(v, int) or isinstance(v, bool) or v != 1:
    print('bad version-is-%r-not-the-integer-1' % (v,)); raise SystemExit
env = m.get('environment')
if not isinstance(env, str) or not env.strip():
    print('bad environment-is-%r-not-a-name' % (env,)); raise SystemExit
for name in ('configured', 'required'):
    if not isinstance(m.get(name), bool):
        print('bad %s-is-%r-not-a-boolean' % (name, m.get(name))); raise SystemExit
if m['required'] and not m['configured']:
    # Telling a client a credential is mandatory while saying sign-in is
    # impossible is a dead end it cannot act on in any direction.
    print('bad required-without-configured'); raise SystemExit
print('ok %s %s %s' % (env, m['configured'], m['required']))" 2>/dev/null; }

code="$(req "$URL/v1/daintree/auth/config")"; load_body
# `unknown` until a well-formed manifest says otherwise. The posture drives what
# every check below EXPECTS, so guessing it from a failed read would assert the
# wrong thing everywhere and bury the one real failure under a cascade of
# consequences.
AUTH_POSTURE="unknown"; AUTH_ENV=""; AUTH_CONFIGURED=""; AUTH_REQUIRED=""
if [ "$code" != "200" ]; then
  fail "/v1/daintree/auth/config returned $code" "$(printf '%.120s' "$BODY")"
else
  MANIFEST="$(read_manifest "$BODY")"
  case "$MANIFEST" in
    ok\ *)
      # shellcheck disable=SC2086
      set -- $MANIFEST
      AUTH_ENV="$2"; AUTH_CONFIGURED="$3"; AUTH_REQUIRED="$4"
      [ "$AUTH_REQUIRED" = "True" ] && AUTH_POSTURE="enforce" || AUTH_POSTURE="anonymous"
      if [ -n "$EXPECT_ENV" ] && [ "$AUTH_ENV" != "$EXPECT_ENV" ]; then
        fail "auth/config reports environment '$AUTH_ENV', expected '$EXPECT_ENV'" "$BODY"
      else
        pass "/v1/daintree/auth/config: version 1, environment $AUTH_ENV, configured=$AUTH_CONFIGURED, required=$AUTH_REQUIRED"
      fi
      ;;
    *)
      # The client checks the version FIRST and throws the whole manifest away on
      # a mismatch, so a malformed one is not a degraded login — it is no login,
      # for every user, with an error naming whatever it managed to decode.
      fail "auth/config is not a usable manifest (${MANIFEST#bad })" "$(printf '%.160s' "$BODY")"
      ;;
  esac
fi

# The browser links, when this deployment publishes any. They are OPTIONAL — a
# deployment with working sign-in and no subscription page still publishes a
# usable manifest — so their ABSENCE is not a failure. Their host is: these open
# in the user's browser under the assistant's own recommendation, so a value
# pointing anywhere but Daintree is a phishing page with the product vouching
# for it.
for FIELD in account_url subscribe_url; do
  LINK="$(jq_field "$BODY" "$FIELD")"
  [ -n "$LINK" ] || continue
  if link_host_ok "$LINK"; then
    pass "$FIELD is on daintree.org ($LINK)"
  else
    fail "$FIELD does not point at a Daintree site" "$LINK"
  fi
done

# --------------------------------------------------------------------------
head_ "Authentication posture"

# What an anonymous request should get depends on the posture, and the posture is
# READ rather than assumed. `required` from the manifest above is the deployment's
# own statement of it, so this section checks that the door a deployment DESCRIBES
# is the door it actually has — which is a stronger and more useful question than
# "is it open", and the one that keeps working through the cutover instead of
# having to be rewritten at it.
#
# The two must agree in both directions. A deployment reporting `required: false`
# while refusing anonymous callers locks out every client that believed it; one
# reporting `required: true` while serving them is an authentication boundary that
# does not exist, which is the worse of the two.
code="$(req "$URL/v1/daintree/capabilities")"; load_body
case "$AUTH_POSTURE" in
  enforce)
    if [ "$code" = "401" ]; then
      pass "enforcing: an anonymous request is refused 401, as auth/config promised"
    else
      fail "auth/config reports required=true but an anonymous request returned $code" "$BODY"
    fi
    ;;
  anonymous)
    if [ "$code" = "200" ]; then
      pass "a request with no bearer token is served (server holds the credential)"
      # The endpoint advertises its OWN protocol range, and it is the one a client
      # negotiates against — /version's range is build metadata and they are read by
      # different code paths. A deployment where they disagree is one where a client
      # is told 3 by one route and refused it by another.
      CAP_MIN="$(jq_path "$BODY" protocol.min)"
      CAP_MAX="$(jq_path "$BODY" protocol.max)"
      if [ "$CAP_MIN" = "3" ] && [ "$CAP_MAX" = "3" ]; then
        pass "/v1/daintree/capabilities advertises protocol $CAP_MIN..$CAP_MAX"
      else
        fail "capabilities advertises protocol '$CAP_MIN'..'$CAP_MAX', expected 3..3" "$BODY"
      fi
    else
      fail "expected 200 without a bearer token, got $code" "$BODY"
    fi
    ;;
  *)
    # Posture unknown: the manifest above did not parse, and that failure is
    # already counted. Asserting a status here would be asserting against a guess,
    # and every consequence of the guess would be reported as its own failure —
    # burying the one thing actually wrong under a page of noise.
    echo "  SKIP  posture unknown (auth/config did not parse); skipping the posture checks"
    ;;
esac

# A header that IS present. Under `open` it is not read at all — a CLI still
# sending its old OpenRouter key must be SERVED, not have that key forwarded to
# Together and rejected as the caller's own — and under `enforce` the same
# request is a 401, because a credential this deployment cannot verify is not a
# credential. `observe` behaves exactly as `open` does here, deliberately: it
# records an outcome and changes nothing a caller can see, so it is indented
# under the same branch rather than given one of its own.
code="$(req -H 'Authorization: Bearer sk-or-v1-a-stale-openrouter-key' "$URL/v1/daintree/capabilities")"; load_body
case "$AUTH_POSTURE" in
  enforce)
    if [ "$code" = "401" ]; then
      pass "enforcing: an unverifiable bearer is refused 401"
    else
      fail "expected 401 for a stale bearer under enforcement, got $code" "$BODY"
    fi
    ;;
  anonymous)
    if [ "$code" = "200" ]; then
      pass "a stale OpenRouter bearer is ignored, not forwarded"
    else
      fail "expected 200 for a stale bearer, got $code" "$BODY"
    fi
    ;;
  *) ;;
esac

# --------------------------------------------------------------------------
head_ "No API route answers with a redirect or a page"

# The failure this catches is a whole-service one rather than a bug in any route:
# a proxy, a platform error page, or a marketing site standing in front of the
# API. Every check above reads JSON fields, and a redirect to a login page or an
# HTML 404 makes every one of them read an empty string — so they fail, but they
# fail describing a missing field instead of the wrong service.
#
# `req` uses curl WITHOUT -L, so a 3xx is observed rather than followed. That is
# the whole mechanism: a client that DID follow would receive 200 and a page of
# HTML, and try to parse it as a manifest.
for ROUTE in /health /readyz /version /v1/daintree/auth/config /v1/daintree/capabilities /v1/daintree/runbooks; do
  code="$(probe "$URL$ROUTE")"; load_body
  case "$code" in
    000)
      fail "$ROUTE could not be reached at all" "curl failed (timeout, DNS or TLS)" ;;
    3*)
      # 3xx to a website or an SSO login is the specific shape worth naming: it
      # means requests are being intercepted before they reach the app.
      fail "$ROUTE answered $code — an API route must not redirect" "$(redirect_target)" ;;
    *)
      if looks_like_a_page "$BODY"; then
        fail "$ROUTE answered with a page, not an API response" "$(printf '%.120s' "$BODY")"
      else
        pass "$ROUTE answers directly, no redirect or page ($code)"
      fi
      ;;
  esac
done

# --------------------------------------------------------------------------
# Everything below reads CONTENT — a runbook count, an echoed request id, a typed
# error envelope — and content needs a credential the moment a deployment
# enforces. So the section is gated on the posture the manifest reported rather
# than assuming the open door: under `enforce` these routes correctly answer 401,
# which the posture section above has already checked, and re-running them here
# would report a working deployment as broken.
#
# `open` and `observe` both fall through to the full checks, because `observe`
# changes nothing a caller can see — which is the entire point of that mode, and
# means the two are genuinely the same case rather than two branches that happen
# to agree today.
if [ "$AUTH_POSTURE" != "anonymous" ]; then
  head_ "Metadata routes"
  if [ "$AUTH_POSTURE" = "enforce" ]; then
    echo "  SKIP  this deployment enforces authentication; the content checks below"
    echo "        need a credential this script cannot mint."
  else
    echo "  SKIP  posture unknown (auth/config did not parse); the content checks"
    echo "        below would be asserted against a guess."
  fi
elif true; then
  # No Authorization header: the server funds the turn. Kept as an array so the
  # call sites below are unchanged, and so a future account credential slots in
  # here rather than being threaded through each one.
  AUTH=()
  # Expanded as ${AUTH[@]+"${AUTH[@]}"} at every call site below, never as a bare
  # "${AUTH[@]}". Under `set -u` bash 3.2 — which is what macOS ships, and so what
  # a developer running this by hand has — treats an EMPTY array's expansion as an
  # unbound variable and aborts. The array is empty on every run today, so the
  # plain form made this script fail on the machine it is most often typed on
  # while passing in CI's bash 5.

  head_ "Metadata routes (no upstream model call, no cost)"

  code="$(req ${AUTH[@]+"${AUTH[@]}"} "$URL/v1/daintree/capabilities")"; load_body
  if [ "$code" = "200" ]; then
    pass "/v1/daintree/capabilities"
  else
    fail "/v1/daintree/capabilities returned $code" "$BODY"
  fi

  code="$(req ${AUTH[@]+"${AUTH[@]}"} "$URL/v1/daintree/runbooks")"; load_body
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
  code="$(req ${AUTH[@]+"${AUTH[@]}"} -X POST "$URL/v1/daintree/auth/verify")"; load_body
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
    code="$(req ${AUTH[@]+"${AUTH[@]}"} -X POST "$URL/v1/daintree/$path" \
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
  hdrs="$(curl -sS -o /dev/null -D - --max-time 20 ${AUTH[@]+"${AUTH[@]}"} \
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
  code="$(req ${AUTH[@]+"${AUTH[@]}"} -X POST "$URL/v1/daintree/tasks" \
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

    TIMEOUT=180 code="$(req ${AUTH[@]+"${AUTH[@]}"} -X POST "$URL/v1/daintree/respond" \
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
    curl -sS --max-time 180 ${AUTH[@]+"${AUTH[@]}"} -X POST "$URL/v1/daintree/respond" \
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
