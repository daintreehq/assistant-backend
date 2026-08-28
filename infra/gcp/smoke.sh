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
#   ./infra/gcp/smoke.sh --expect-build-sha 4f1c9ae0...
#                                            # ... and fail unless /version reports
#                                            # exactly that commit. This is what
#                                            # makes a run a check of THIS release
#                                            # rather than of whatever is deployed.
#
#   ./infra/gcp/smoke.sh --expect-posture enforce --expect-entitlement observe
#                                            # ... and fail unless the two rollout
#                                            # axes are in the modes you asked for.
#                                            # Without these, every posture check
#                                            # below ADAPTS to whatever is reported.
#
#   ./infra/gcp/smoke.sh --expect-client-id 6f0d...apps.example
#                                            # ... and fail unless the published
#                                            # OAuth client id is that one.
#
#   ./infra/gcp/smoke.sh --expect-issuer https://abc.supabase.co/auth/v1 \
#                        --expect-authorize-url ... --expect-token-url ... \
#                        --expect-jwks-url ... --expect-scopes openid,email \
#                        --expect-account-url ... --expect-subscribe-url ... \
#                        --expect-access-token-seconds 3600 \
#                        --expect-session-max-age 604800
#                                            # ... and fail unless every other
#                                            # published manifest field is
#                                            # exactly this. A revision carrying
#                                            # another project's endpoints has a
#                                            # perfectly valid manifest.
#
#   ./infra/gcp/smoke.sh --expect-gate-revision beta-3   # or --expect-no-gate
#                                            # ... and fail unless the private
#                                            # staging subject gate came up
#                                            # holding a non-empty membership
#                                            # under that label. Only the safe
#                                            # projection is read: enabled, how
#                                            # many, and the label — never a
#                                            # member and never "is X admitted".
#
# THIS IS THE PRE-PROMOTION GATE, not only a hand-run check. cloudbuild.yaml's
# `smoke` step calls this script against the no-traffic candidate revision and
# refuses to promote it on a non-zero exit, so every assertion below is one the
# release actually has to satisfy. That is deliberate: the step used to inline a
# weaker copy of two of these checks, and a second definition of what "a working
# deployment" means is one that drifts from this one silently, in the direction
# of the copy that nobody runs by hand.
#
# NO CREDENTIAL IS NEEDED AND NONE IS ACCEPTED. The server holds its own upstream
# credential (BASETEN_API_KEY) and funds every turn from it, so the metadata
# routes answer an anonymous request. That is also why --full is opt-in and
# everything else here is not: --full spends DAINTREE's money, not the caller's.
#
# There is deliberately no way to hand this script a user's access token. An
# earlier flag took the NAME of an environment variable holding one, so the
# authenticated half of /v1/daintree/account could be smoked here — and the usage
# it documented obtained that value from a CLI subcommand that printed the access
# token to stdout. The CLI has no such subcommand and must not grow one: access
# tokens live in that process's memory on purpose, and a facility that prints one
# for a shell to capture demotes the whole design to a convention, with a CI log
# the first place the value lands. Authenticated acceptance belongs to the
# operator cutover runbook, driven through the real CLI, where the credential
# never leaves the process that obtained it.
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
# The commit the caller believes is deployed. Optional here and MANDATORY in the
# build, which is the whole reason it exists: /version reporting a build_sha at
# all proves only that the image was tagged, while comparing it against the
# commit that triggered the release is what catches a cached image, a mis-tagged
# push, or a deploy that quietly reused the previous revision.
EXPECT_BUILD_SHA=""
# The posture the operator BELIEVES this deployment is in. Optional, and the
# reason it exists is that every check below derives its expectation from what
# the deployment reports — so a revision that shipped `required: false` when it
# meant `true` adapts to the anonymous branch and passes every one of them. The
# script cannot catch that by reading harder; somebody has to state the intent.
EXPECT_POSTURE=""
# The entitlement axis, stated separately because it IS separate: identity says
# the token is genuine and entitlement says the account behind it has paid, and
# merging the two rollouts is how a billing outage starts presenting as a
# signed-out user. Read from /version, which is the only endpoint that reports
# this mode at all — no client-facing surface exposes it, by design.
EXPECT_ENTITLEMENT=""
# The public OAuth client id this deployment should be publishing. Not a secret
# (the discovery endpoint hands it to anyone) — the reason to assert it is that
# it names a SPECIFIC Supabase project, so a staging revision carrying
# production's id sends its users to sign in against the wrong one, with a
# manifest whose shape is perfectly valid.
EXPECT_CLIENT_ID=""
# The OTHER half of the client-id expectation, and it needs its own flag rather
# than an empty value. An empty `--expect-client-id` means "do not inspect the
# id", which is the correct reading for a hand-run probe and precisely the wrong
# one for a release gate: production ships with no OAuth client configured, so
# passing its empty substitution asserted nothing at all, and a candidate that
# published a real-but-unintended client would have passed every other check.
# "Must be absent" is a claim, so it gets said out loud.
EXPECT_NO_OAUTH=0
# The REST of the manifest, one expectation per published field.
#
# The client id was asserted first because it names a Supabase project, and that
# left every other field of the same manifest checked for shape and not for
# value: a candidate carrying another environment's issuer, token endpoint or
# JWKS URL publishes a body that is typed, coherent and completely wrong, and
# `configured=true` with the right client id is not evidence that the endpoints
# beside it belong to the same project. A wrong `jwks_uri` in particular fails
# LATER than a wrong client id and much less legibly — sign-in completes, and
# then every verification fails against keys from somewhere else.
#
# All optional, all refusing a blank value for the reason the flags above do:
# the natural call passes a deployment substitution, and an unset one there would
# silently switch the assertion off.
EXPECT_ISSUER=""
EXPECT_AUTHORIZE_URL=""
EXPECT_TOKEN_URL=""
EXPECT_JWKS_URL=""
# The two browser links. Their HOST is already checked unconditionally below —
# a link pointing anywhere but Daintree is a phishing page with the product
# vouching for it — but a staging revision publishing production's account page
# passes that check, which is the mix-up a release gate exists to catch.
EXPECT_ACCOUNT_URL=""
EXPECT_SUBSCRIBE_URL=""
# Comma-separated. These are hardcoded in the server rather than configured, so
# the assertion is not catching a mistyped deployment value — it is catching a
# revision built from something other than the source this release believes it
# shipped, and it holds the published set still while the CLI is written against
# it.
EXPECT_SCOPES=""
# The session policy a client schedules its refresh from. A wrong value here is
# invisible until tokens start expiring earlier or later than the client plans
# for, which is the kind of defect that surfaces as "sign-in is flaky".
EXPECT_ACCESS_TOKEN_SECONDS=""
EXPECT_SESSION_MAX_AGE=""
# The private-staging subject gate, as /version's SAFE projection reports it:
# whether the gate decides anything, how many subjects it admits, and the
# operator-chosen label for that membership. Never a member, never a prefix of
# one, and never an answer to "is this subject admitted" — a public
# unauthenticated endpoint that could be asked that would be a membership oracle,
# which is worse than publishing the list because nobody can see it happening.
#
# The label is what an operator compares against the list they just published.
# Asserting it is how "the revision came up holding the membership this release
# meant" stops being a control-plane question.
EXPECT_GATE_REVISION=""
# The other direction, and it needs its own flag for the reason --expect-no-oauth
# does. Every deployment that is not private staging publishes `enabled: false`
# with no entries — production, and every staging revision below `enforce`. An
# empty --expect-gate-revision would read as "no opinion" on exactly the
# deployment where a gate coming up unexpectedly, or failing to come up, is the
# whole question.
EXPECT_NO_GATE=0

while [ $# -gt 0 ]; do
  case "$1" in
    # `shift 2` on a flag with no value shifts once and leaves the flag in place,
    # which without `set -e` spins forever.
    --url)  [ $# -gt 1 ] || { echo "--url requires a value" >&2; exit 2; }
            URL="$2"; shift 2 ;;
    --full) FULL=1; shift ;;
    # Assert the deployment is in the auth posture you think it is. Two
    # vocabularies, on purpose:
    #   anonymous — the DOOR a caller meets: accounts off or optional, requests
    #               served without one. `open` and `observe` are the same door,
    #               which is the entire point of `observe`.
    #   open | observe | enforce — the exact MODE the revision came up in, which
    #               only became answerable when /version began reporting it.
    # The door spelling is kept because README and .claude/skills/release already
    # pass it, and because it is still the right question for a hand-run check of
    # a deployment whose mode you do not know.
    --expect-posture)
            [ $# -gt 1 ] || { echo "--expect-posture requires a value" >&2; exit 2; }
            case "$2" in
              anonymous|open|observe|enforce) EXPECT_POSTURE="$2" ;;
              *) echo "--expect-posture must be anonymous|open|observe|enforce, got '$2'" >&2
                 exit 2 ;;
            esac
            shift 2 ;;
    # The second rollout axis. No client-facing endpoint reports it, so before
    # /version carried it this was unobservable from outside the control plane —
    # a billing rollout could only be confirmed by reading the revision's env.
    --expect-entitlement)
            [ $# -gt 1 ] || { echo "--expect-entitlement requires a value" >&2; exit 2; }
            case "$2" in
              off|observe|enforce) EXPECT_ENTITLEMENT="$2" ;;
              *) echo "--expect-entitlement must be off|observe|enforce, got '$2'" >&2
                 exit 2 ;;
            esac
            shift 2 ;;
    --expect-build-sha)
            [ $# -gt 1 ] || { echo "--expect-build-sha requires a value" >&2; exit 2; }
            # Blank is refused rather than read as "no expectation", for exactly
            # the reason --expect-env refuses it: the natural call passes a build
            # variable, and an unset one there would silently switch off the one
            # check that separates "this release is live" from "something is
            # live". A gate asked for by name must never quietly not run — and
            # this one is the release gate.
            [ -n "$2" ] || { echo "--expect-build-sha requires a non-empty value" >&2; exit 2; }
            EXPECT_BUILD_SHA="$2"; shift 2 ;;
    --expect-client-id)
            [ $# -gt 1 ] || { echo "--expect-client-id requires a value" >&2; exit 2; }
            # Blank refused for the same reason as above: the caller passes a
            # substitution, and an empty one would turn the assertion off rather
            # than fail the deployment that forgot to set it.
            [ -n "$2" ] || { echo "--expect-client-id requires a non-empty value" >&2; exit 2; }
            EXPECT_CLIENT_ID="$2"; shift 2 ;;
    # The deployment must publish NO OAuth client. Mutually exclusive with the
    # flag above: asking for both is a caller contradiction, not something to
    # resolve by precedence.
    --expect-no-oauth)
            EXPECT_NO_OAUTH=1; shift ;;
    # The subject gate's LABEL. Blank refused like every flag above: the release
    # passes a substitution, and an unset one would switch off the assertion on
    # the deployment the gate exists for.
    --expect-gate-revision)
            [ $# -gt 1 ] || { echo "--expect-gate-revision requires a value" >&2; exit 2; }
            [ -n "$2" ] || { echo "--expect-gate-revision requires a non-empty value" >&2; exit 2; }
            EXPECT_GATE_REVISION="$2"; shift 2 ;;
    # The deployment must publish NO subject gate. Mutually exclusive with the
    # flag above, and refused rather than resolved by precedence for the same
    # reason the OAuth pair is: two contradictory claims about one field mean the
    # thing that built the arguments has a bug.
    --expect-no-gate)
            EXPECT_NO_GATE=1; shift ;;
    # The rest of the manifest. One arm rather than nine copies of the same four
    # lines: written out individually these differ only in a variable name, and a
    # blank-value guard that has to be retyped nine times is one that will be
    # forgotten on the tenth. `read -r` into the named variable keeps the value
    # out of `eval`, which is the one construct that could turn a caller's
    # argument into something this script parses rather than stores.
    --expect-issuer|--expect-authorize-url|--expect-token-url|--expect-jwks-url|\
    --expect-account-url|--expect-subscribe-url|--expect-scopes|\
    --expect-access-token-seconds|--expect-session-max-age)
            [ $# -gt 1 ] || { echo "$1 requires a value" >&2; exit 2; }
            [ -n "$2" ] || { echo "$1 requires a non-empty value" >&2; exit 2; }
            case "$1" in
              --expect-issuer)         EXPECT_ISSUER="$2" ;;
              --expect-authorize-url)  EXPECT_AUTHORIZE_URL="$2" ;;
              --expect-token-url)      EXPECT_TOKEN_URL="$2" ;;
              --expect-jwks-url)       EXPECT_JWKS_URL="$2" ;;
              --expect-account-url)    EXPECT_ACCOUNT_URL="$2" ;;
              --expect-subscribe-url)  EXPECT_SUBSCRIBE_URL="$2" ;;
              --expect-scopes)         EXPECT_SCOPES="$2" ;;
              --expect-access-token-seconds)
                                       EXPECT_ACCESS_TOKEN_SECONDS="$2" ;;
              --expect-session-max-age)
                                       EXPECT_SESSION_MAX_AGE="$2" ;;
            esac
            shift 2 ;;
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
    # The USAGE block at the top of this file, printed verbatim. The range ends
    # at the last example, deliberately: everything below it is a rationale a
    # reader wants when editing the script and not when running it. Keep the
    # number in step with the header — a range that has slipped prints half a
    # flag's description and silently omits the newest one.
    -h|--help) sed -n '2,51p' "$0" | sed 's/^# \{0,1\}//'; exit 0 ;;
    *) echo "unknown argument: $1" >&2; exit 2 ;;
  esac
done

# Refused rather than resolved by precedence. Both flags are claims about the same
# field, and a caller who made two contradictory ones has a bug in the thing that
# built the arguments — silently honouring whichever the code checks first would
# hide it behind a green run.
if [ "$EXPECT_NO_OAUTH" -eq 1 ] && [ -n "$EXPECT_CLIENT_ID" ]; then
  echo "--expect-no-oauth and --expect-client-id contradict each other" >&2
  exit 2
fi
if [ "$EXPECT_NO_GATE" -eq 1 ] && [ -n "$EXPECT_GATE_REVISION" ]; then
  echo "--expect-no-gate and --expect-gate-revision contradict each other" >&2
  exit 2
fi

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
# A non-negative integer, and nothing else. `[ x -lt 1 ]` on a non-numeric value
# is a shell ERROR rather than a false, and without `set -e` that error is
# indistinguishable from the comparison simply not holding — so a `/version` that
# reported a string where a count belongs would slide past the branch that was
# supposed to object to it.
is_count() { case "${1:-}" in ''|*[!0-9]*) return 1 ;; *) return 0 ;; esac; }

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
# COMMIT_SHA in and this reports it back. Several things are checked rather than
# one, because "200" was passing for bodies that could not have come from this
# service.
code="$(req "$URL/version")"; load_body
VERSION_CODE="$code"
BUILD_SHA="$(jq_field "$BODY" build_sha)"
PROTO_MIN="$(jq_path "$BODY" protocol.min)"
PROTO_MAX="$(jq_path "$BODY" protocol.max)"
# The two rollout axes, as the REVISION states them. This is the only endpoint
# that reports either: auth/config answers a client's question ("must I hold a
# credential"), which is `configured and mode == enforce` and so cannot tell
# `open` from `observe`, and no surface at all has ever reported the entitlement
# mode. Read here, before the manifest, so the posture section below can compare
# the mode the revision came up in against the door it describes to clients.
VERSION_AUTH_MODE="$(jq_path "$BODY" posture.auth)"
VERSION_ENTITLEMENT_MODE="$(jq_path "$BODY" posture.entitlement)"
# The third axis, and the only one derived from a secret. Exactly three facts are
# published and exactly three are read: whether the gate decides anything, how
# many subjects it admits, and the label naming that membership. `enabled` prints
# the Python repr of a JSON boolean — `True`/`False` — and an EMPTY value means
# the block is absent, which is an older build answering rather than a gate that
# is off. The two are distinguished below because they need different sentences.
VERSION_GATE_ENABLED="$(jq_path "$BODY" posture.subject_gate.enabled)"
VERSION_GATE_ENTRIES="$(jq_path "$BODY" posture.subject_gate.entries)"
VERSION_GATE_REVISION="$(jq_path "$BODY" posture.subject_gate.revision)"
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
elif [ -n "$EXPECT_BUILD_SHA" ] && [ "$BUILD_SHA" != "$EXPECT_BUILD_SHA" ]; then
  # THE release gate. A build_sha that is merely present proves the image carried
  # a tag; only comparing it against the commit that asked for this deployment
  # catches a cached layer, a mis-tagged push, or a `gcloud run deploy` that
  # resolved to the previous image and reported success. Exact string equality —
  # a prefix match would accept the short sha of a DIFFERENT commit that happens
  # to share seven characters, which is a collision this repo will eventually see.
  fail "/version serves build $BUILD_SHA, expected $EXPECT_BUILD_SHA" ""
elif [ "$PROTO_MIN" != "3" ] || [ "$PROTO_MAX" != "3" ]; then
  fail "/version negotiates protocol $PROTO_MIN..$PROTO_MAX, expected 3..3" "$BODY"
else
  pass "/version: $(jq_field "$BODY" server_version) build $BUILD_SHA, protocol $PROTO_MIN..$PROTO_MAX"
fi

# The posture block, checked for PRESENCE separately from the fields above. It is
# additive to a wire shape older clients already decode, so its absence is not a
# malformed /version — it is an older build answering, which is precisely the
# thing a release smoke must not mistake for the release it asked for. Reported
# as its own failure rather than folded into the chain above, so a missing
# posture does not mask a wrong build sha or vice versa.
if [ "$VERSION_CODE" = "200" ]; then
  if [ -z "$VERSION_AUTH_MODE" ] || [ -z "$VERSION_ENTITLEMENT_MODE" ]; then
    fail "/version reports no posture block (auth/entitlement modes)" "$BODY"
  else
    pass "/version posture: auth=$VERSION_AUTH_MODE, entitlement=$VERSION_ENTITLEMENT_MODE"
  fi
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

# Manifest VALUES, not merely its shape. Everything above proves this is A
# manifest — typed, coherent, decodable. These prove it is THIS deployment's
# manifest, which is a different question and the one a release gate has to ask:
# a revision that came up carrying another environment's OAuth configuration
# publishes a perfectly well-formed body, and every type and coherence check
# passes while every user is sent to sign in against the wrong project.
#
# Both fields exist only when `configured` is true, so both are skipped when it
# is not — an unconfigured deployment publishing neither is the documented shape
# and the state production is in today, not a defect.
if [ "$AUTH_CONFIGURED" = "True" ]; then
  # A FIXED contract between this server and the CLI rather than a deployment
  # value: the CLI binds that exact loopback port to receive the authorization
  # code and refuses a manifest naming anything else. A drifted value here is
  # every sign-in failing at the last step, AFTER the user has already approved
  # in the browser — the most expensive place in the flow to fail.
  REDIRECT_URI="$(jq_field "$BODY" redirect_uri)"
  if [ "$REDIRECT_URI" = "http://127.0.0.1:42813/oauth/callback" ]; then
    pass "redirect_uri is the CLI's fixed loopback callback"
  else
    fail "redirect_uri is '$REDIRECT_URI', expected http://127.0.0.1:42813/oauth/callback" ""
  fi
  if [ -n "$EXPECT_CLIENT_ID" ]; then
    CLIENT_ID="$(jq_field "$BODY" client_id)"
    # Printed on a mismatch because it is genuinely not a secret — this endpoint
    # hands it to every anonymous caller — and because the useful half of this
    # failure is seeing WHICH project answered.
    if [ "$CLIENT_ID" = "$EXPECT_CLIENT_ID" ]; then
      pass "client_id is the expected OAuth client"
    else
      fail "auth/config publishes client_id '$CLIENT_ID', expected '$EXPECT_CLIENT_ID'" ""
    fi
  fi
  # The rest of the manifest, VALUE by value.
  #
  # Driven off one table rather than nine near-identical blocks: every row is the
  # same comparison, and nine hand-written copies is nine chances to compare the
  # wrong variable against the right field — a mistake that reads as a passing
  # check. The table is `flag-ish name | manifest field | expected value`, and a
  # row with no expected value is simply not asserted, so a hand-run probe that
  # states nothing still runs everything else.
  #
  # `session_policy` is nested, so it goes through `jq_path`; the rest are
  # top-level and go through `jq_field`. That split is why the table carries a
  # dotted path rather than a bare key.
  while IFS='|' read -r LABEL FIELD WANT; do
    [ -n "$WANT" ] || continue
    case "$FIELD" in
      *.*) GOT="$(jq_path "$BODY" "$FIELD")" ;;
      *)   GOT="$(jq_field "$BODY" "$FIELD")" ;;
    esac
    if [ "$GOT" = "$WANT" ]; then
      pass "$LABEL is the expected value"
    else
      # Printed on a mismatch: none of these is a secret — this endpoint hands
      # every one of them to any anonymous caller — and the useful half of the
      # failure is seeing WHICH project or environment answered.
      fail "auth/config publishes $LABEL '$GOT', expected '$WANT'" ""
    fi
  done <<EOF
issuer|issuer|$EXPECT_ISSUER
authorization_endpoint|authorization_endpoint|$EXPECT_AUTHORIZE_URL
token_endpoint|token_endpoint|$EXPECT_TOKEN_URL
jwks_uri|jwks_uri|$EXPECT_JWKS_URL
account_url|account_url|$EXPECT_ACCOUNT_URL
subscribe_url|subscribe_url|$EXPECT_SUBSCRIBE_URL
access_token_seconds|session_policy.access_token_seconds|$EXPECT_ACCESS_TOKEN_SECONDS
session_max_age_seconds|session_policy.session_max_age_seconds|$EXPECT_SESSION_MAX_AGE
EOF

  # Scopes are a LIST, so they are compared as one rather than through the table
  # above. Order is part of the expectation: the manifest publishes a fixed
  # sequence and a client may reasonably send it back verbatim, so a set
  # comparison would call a reordering equal when the bytes a user consents to
  # have changed.
  if [ -n "$EXPECT_SCOPES" ]; then
    GOT_SCOPES="$(printf '%s' "$BODY" | EXPECT_SCOPES="$EXPECT_SCOPES" python3 -c "
import json,sys
try:
    v = json.load(sys.stdin).get('scopes')
except Exception:
    v = None
print(','.join(v) if isinstance(v, list) and all(isinstance(x, str) for x in v) else '')" 2>/dev/null)"
    if [ "$GOT_SCOPES" = "$EXPECT_SCOPES" ]; then
      pass "scopes are the expected set, in the expected order"
    else
      fail "auth/config publishes scopes '$GOT_SCOPES', expected '$EXPECT_SCOPES'" ""
    fi
  fi
elif [ "$AUTH_CONFIGURED" = "False" ]; then
  # Asked about a manifest this deployment does not publish. Named rather than
  # left to a branch that simply does not run, which would report a pass count
  # short of what the caller asked for and look like success.
  for STATED in "$EXPECT_ISSUER" "$EXPECT_AUTHORIZE_URL" "$EXPECT_TOKEN_URL" \
                "$EXPECT_JWKS_URL" "$EXPECT_ACCOUNT_URL" "$EXPECT_SUBSCRIBE_URL" \
                "$EXPECT_SCOPES" "$EXPECT_ACCESS_TOKEN_SECONDS" "$EXPECT_SESSION_MAX_AGE"; do
    if [ -n "$STATED" ]; then
      fail "a manifest expectation was given but this deployment reports configured=false" ""
      break
    fi
  done
fi

if [ "$AUTH_CONFIGURED" = "False" ] && [ -n "$EXPECT_CLIENT_ID" ]; then
  # Asked for a specific client and got a deployment that publishes none. Named
  # here rather than left to the branch above, which would simply not run and
  # report a pass count one short of what the caller asked for.
  fail "--expect-client-id was given but this deployment reports configured=false" ""
fi

# The reverse claim, asserted rather than assumed from an absent flag.
if [ "$EXPECT_NO_OAUTH" -eq 1 ]; then
  if [ "$AUTH_CONFIGURED" = "True" ]; then
    fail "--expect-no-oauth was given but this deployment publishes an OAuth client" ""
  else
    pass "auth/config publishes no OAuth client, as expected"
  fi
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

# The two answers about the same door, cross-checked against each other. The
# manifest says what a client must DO (`required`); /version says which MODE the
# revision came up in. A revision running `enforce` that publishes
# `required: false` is one where the OAuth configuration did not arrive — a door
# the operator believes is shut, described to every client as open, with nothing
# in either answer wrong on its own. Only having both makes it visible.
if [ "$VERSION_AUTH_MODE" = "enforce" ] && [ "$AUTH_POSTURE" = "anonymous" ]; then
  fail "AUTH_MODE=enforce but auth/config reports required=false" \
       "OAuth is not fully configured on this revision, so the door is described as open"
fi

# The posture the operator asked for, compared against the one reported. Runs
# before the checks that BRANCH on the posture, so a mismatch is reported as
# itself rather than as a downstream 401 nobody expected.
if [ -n "$EXPECT_POSTURE" ]; then
  case "$EXPECT_POSTURE" in
    anonymous)
      # The DOOR, derived from the manifest. `open` and `observe` are the same
      # door and this spelling deliberately does not distinguish them — it is the
      # right question for a hand-run check of a deployment whose mode you do not
      # already know, and README and .claude/skills/release both pass it.
      if [ "$AUTH_POSTURE" = "anonymous" ]; then
        pass "auth posture is anonymous (AUTH_MODE=${VERSION_AUTH_MODE:-unreported}), as expected"
      else
        fail "expected auth posture 'anonymous', deployment reports '$AUTH_POSTURE'" ""
      fi
      ;;
    *)
      # The exact MODE, and the reason this spelling had to exist: `required`
      # collapses `open` and `observe` into one value, so a deployment that
      # shipped `open` when the rollout had moved to `observe` passed every check
      # this script owned. Nothing observable to a client separates them — that
      # is what `observe` IS — so the mode has to be reported rather than probed.
      if [ "$VERSION_AUTH_MODE" = "$EXPECT_POSTURE" ]; then
        pass "AUTH_MODE is $VERSION_AUTH_MODE, as expected"
      else
        fail "expected AUTH_MODE '$EXPECT_POSTURE', /version reports '${VERSION_AUTH_MODE:-<absent>}'" ""
      fi
      ;;
  esac
fi

# The entitlement axis. Separate from the block above rather than folded into it,
# because the axes are independently staged on purpose: identity says the token is
# genuine, entitlement says the account behind it has paid, and a check that
# asserted them together would be a check that cannot express the rollout states
# the deployment actually passes through.
if [ -n "$EXPECT_ENTITLEMENT" ]; then
  if [ "$VERSION_ENTITLEMENT_MODE" = "$EXPECT_ENTITLEMENT" ]; then
    pass "ENTITLEMENT_MODE is $VERSION_ENTITLEMENT_MODE, as expected"
  else
    fail "expected ENTITLEMENT_MODE '$EXPECT_ENTITLEMENT', /version reports '${VERSION_ENTITLEMENT_MODE:-<absent>}'" ""
  fi
fi

# The subject gate, checked from the safe projection and nothing else. Three
# facts, and each one is a different release accident:
#
#   enabled  — a private staging revision that came up ungated is a private beta
#              that admits every verified account, and it looks identical to a
#              working one from the outside.
#   entries  — a gate holding an empty list refuses everybody, which readiness
#              already refuses; a gate reporting entries where none was deployed
#              means a membership arrived from somewhere this release did not.
#   revision — the label. It is what ties the running revision to the list an
#              operator just published, and it is the only one of the three that
#              can tell `beta-3` from `beta-2` when both have four entries.
if [ -n "$EXPECT_GATE_REVISION" ]; then
  if [ -z "$VERSION_GATE_ENABLED" ]; then
    fail "/version publishes no subject_gate block" \
         "this build predates the gate, or the projection was removed"
  elif [ "$VERSION_GATE_ENABLED" != "True" ]; then
    fail "expected the subject gate to be enabled, /version reports enabled=$VERSION_GATE_ENABLED" \
         "a private staging deployment that admits every verified account is not private"
  elif ! is_count "$VERSION_GATE_ENTRIES"; then
    fail "/version reports a non-numeric subject gate entry count '$VERSION_GATE_ENTRIES'" ""
  elif [ "$VERSION_GATE_ENTRIES" -lt 1 ]; then
    fail "the subject gate is enabled with $VERSION_GATE_ENTRIES entries" \
         "an empty membership refuses everybody, including the testers it was published for"
  elif [ "$VERSION_GATE_REVISION" != "$EXPECT_GATE_REVISION" ]; then
    # The label is not a secret and naming both sides is the diagnostic: it is
    # the release's own substitution on one side and an operator-chosen name on
    # the other, and neither narrows who is on the list.
    fail "expected subject gate revision '$EXPECT_GATE_REVISION', /version reports '${VERSION_GATE_REVISION:-<absent>}'" ""
  else
    pass "subject gate: enabled, $VERSION_GATE_ENTRIES entries, revision $VERSION_GATE_REVISION"
  fi
elif [ "$EXPECT_NO_GATE" -eq 1 ]; then
  if [ -z "$VERSION_GATE_ENABLED" ]; then
    fail "/version publishes no subject_gate block" \
         "this build predates the gate, or the projection was removed"
  elif [ "$VERSION_GATE_ENABLED" != "False" ]; then
    fail "a subject gate is enabled on a deployment that should have none" \
         "the membership secret reached a revision this release did not mount it on"
  elif ! is_count "$VERSION_GATE_ENTRIES"; then
    fail "/version reports a non-numeric subject gate entry count '$VERSION_GATE_ENTRIES'" ""
  elif [ "$VERSION_GATE_ENTRIES" -ne 0 ]; then
    # Enabled and populated are separate facts: `allowed_subject_digests()`
    # parses whatever is mounted whether or not the gate decides anything, so a
    # secret attached to the wrong deployment shows up here and nowhere else.
    fail "no subject gate was deployed, but /version reports $VERSION_GATE_ENTRIES entries" \
         "a membership list is mounted on a revision that does not consult it"
  else
    pass "no subject gate, as expected (enabled=False, 0 entries)"
  fi
fi

# A header that IS present. Under `open` it is not read at all — a CLI still
# sending its old OpenRouter key must be SERVED, not have that key forwarded to
# Baseten and rejected as the caller's own — and under `enforce` the same
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
head_ "Account status"

# UNAUTHENTICATED first, and this one runs always: it needs no credential and it
# is the half of the contract CI can actually check. What it asserts differs by
# posture, because the route deliberately requires a real principal — under
# `open` and `observe` every caller is anonymous, so a 401 there is the endpoint
# working rather than failing.
code="$(req "$URL/v1/daintree/account")"; load_body
case "$AUTH_POSTURE" in
  enforce|anonymous)
    if [ "$code" = "401" ]; then
      pass "/v1/daintree/account requires an account credential"
    else
      fail "/v1/daintree/account returned $code without a credential (expected 401)" "$BODY"
    fi
    ;;
  *)
    echo "  SKIP  posture unknown (auth/config did not parse); skipping the account check"
    ;;
esac

# There is deliberately NO authenticated half here — see the note at the top of
# this file. The 200 branch of this route needs a real user access token, and the
# only way a shell could hold one is if something printed it; nothing does, and
# nothing should. That check lives in the operator cutover runbook, driven through
# the real CLI, where the credential never leaves the process that obtained it.

# --------------------------------------------------------------------------
head_ "The funded routes, asked anonymously"

# /respond and /tasks are the two routes that SPEND — they open a provider call
# on Daintree's own credential — and they are the only two the paid-access
# dependency guards. Every other check in this file reads metadata, so until now
# a deployment could enforce authentication on everything cheap and serve the
# expensive pair to anybody, and nothing here would have noticed: the anonymous
# POSTs this script owned all lived inside the `anonymous` branch, which
# `enforce` skips wholesale.
#
# Only under `enforce`, and that is not squeamishness about coverage. Under the
# open door these routes answer 200 by design and the request would BILL a turn —
# `--full` exists precisely so spending is opt-in — so there is no anonymous-mode
# version of this check to write.
#
# THE BODIES ARE VALID ON PURPOSE, and it is the whole difficulty of the check. A
# `{}` body earns a 400 from schema validation, which is not 200 and not 401 and
# proves nothing at all about whether a door exists; a naive "did not succeed"
# assertion would pass against a deployment with no authentication whatever.
# These are the same payloads the protocol probes below use — bodies this build
# parses — so a 401 can only have come from the door.
#
# And the envelope is read as well as the status, for the same reason. A 401 with
# `invalid_request` inside it would mean the schema refused the request and the
# status happened to land on the number this check wanted.
if [ "$AUTH_POSTURE" = "enforce" ]; then
  for path in respond tasks; do
    case "$path" in
      respond) payload='{"protocol_version":3,"session":{"id":"smoke-anon","turn_id":"t"},"startup":{},"input":{"messages":[{"role":"user","content":"hi"}]}}' ;;
      tasks)   payload='{"task":"terminal_judge","input":{"question":"q","tail":"t"}}' ;;
    esac
    code="$(req -X POST "$URL/v1/daintree/$path" \
      -H 'content-type: application/json' -d "$payload")"
    load_body
    err_code="$(printf '%s' "$BODY" | python3 -c "import json,sys
print((json.load(sys.stdin).get('error') or {}).get('code',''))" 2>/dev/null)"
    case "$err_code" in
      # The auth family, named rather than pattern-matched on a prefix: these are
      # the three codes `auth.py` raises for a caller who is not signed in, and a
      # prefix rule would also accept a future `auth_...` code that means
      # something else entirely.
      auth_required|auth_token_invalid|auth_token_expired|auth_session_revoked)
        auth_shaped=1 ;;
      *) auth_shaped=0 ;;
    esac
    if [ "$code" = "401" ] && [ "$auth_shaped" = "1" ]; then
      pass "/v1/daintree/$path refuses an anonymous caller (401 $err_code) on a valid body"
    elif [ "$code" = "401" ]; then
      fail "/v1/daintree/$path returned 401 with code '${err_code:-<none>}', not an auth refusal" \
           "the body was rejected rather than the caller, so this proves nothing about the door"
    else
      fail "/v1/daintree/$path answered $code to an anonymous caller under enforcement" \
           "this route spends Daintree's upstream credential"
    fi
  done
else
  echo "  SKIP  this deployment serves anonymous callers; a POST here would bill a turn"
fi

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
      # invalid or unfunded BASETEN_API_KEY surfaces as the upstream's rejection
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
