#!/usr/bin/env bash
#
# End-to-end check of a deployed Daintree runbook search service.
#
#   ./infra/gcp/smoke.sh                     # the Cloud Run URL discovered from gcloud
#   ./infra/gcp/smoke.sh --url https://assistant.daintree.org
#                                            # a specific host instead
#   ./infra/gcp/smoke.sh --expect-build-sha 4f1c9ae0...
#                                            # ... and fail unless /version reports
#                                            # exactly that commit. This is what
#                                            # makes a run a check of THIS release
#                                            # rather than of whatever is deployed.
#   ./infra/gcp/smoke.sh --identity-token    # send a gcloud identity token, for a
#                                            # service that is still IAM-private
#                                            # (bootstrap.sh's public cutover)
#
# THIS IS THE PRE-PROMOTION GATE, not only a hand-run check. cloudbuild.yaml's
# `smoke` step runs this script against the no-traffic candidate revision, and
# a failure here keeps that revision from ever taking traffic.
#
# Free: a search is the local classifier, so nothing here spends money.
#
# Exits non-zero if any check fails.

set -uo pipefail

PROJECT="${PROJECT:-}"
REGION="${REGION:-us-central1}"
SERVICE="${SERVICE:-daintree-assistant}"

URL=""
EXPECT_BUILD_SHA=""
IDENTITY_TOKEN=0

while [ $# -gt 0 ]; do
  case "$1" in
    --url)  [ $# -gt 1 ] || { echo "--url requires a value" >&2; exit 2; }
            # An empty value is refused rather than read as "discover it": a
            # caller that meant a specific host must never end up smoking the
            # production service instead.
            [ -n "$2" ] || { echo "--url requires a non-empty value" >&2; exit 2; }
            URL="$2"; shift 2 ;;
    --expect-build-sha)
            [ $# -gt 1 ] || { echo "--expect-build-sha requires a value" >&2; exit 2; }
            [ -n "$2" ] || { echo "--expect-build-sha requires a non-empty value" >&2; exit 2; }
            EXPECT_BUILD_SHA="$2"; shift 2 ;;
    --identity-token) IDENTITY_TOKEN=1; shift ;;
    -h|--help) sed -n '2,23p' "$0" | sed 's/^# \{0,1\}//'; exit 0 ;;
    *) echo "unknown argument: $1" >&2; exit 2 ;;
  esac
done

if [ -z "$URL" ]; then
  if [ -z "$PROJECT" ]; then
    echo "Pass --url https://host or set PROJECT to your Google Cloud project ID." >&2
    exit 2
  fi
  URL="$(gcloud run services describe "$SERVICE" --region="$REGION" \
          --project="$PROJECT" --format='value(status.url)' 2>/dev/null)"
fi
if [ -z "$URL" ]; then
  echo "Could not determine the service URL. Pass --url https://host" >&2
  exit 2
fi
URL="${URL%/}"
MCP="$URL/v1/daintree/mcp"

AUTH=()
if [ "$IDENTITY_TOKEN" -eq 1 ]; then
  AUTH=(-H "Authorization: Bearer $(gcloud auth print-identity-token)")
fi

PASS=0
FAIL=0
pass() { PASS=$((PASS + 1)); printf '  \033[32mPASS\033[0m  %s\n' "$1"; }
fail() { FAIL=$((FAIL + 1)); printf '  \033[31mFAIL\033[0m  %s\n' "$1"; [ -n "${2:-}" ] && printf '        %s\n' "$2"; }

WORK="$(mktemp -d)"
trap 'rm -rf "$WORK"' EXIT

# get PATH -> "<status>" with the body in $WORK/body
get() {
  curl -sS -o "$WORK/body" -w '%{http_code}' --max-time 30 ${AUTH[@]+"${AUTH[@]}"} "$URL$1" 2>/dev/null || echo 000
}

# rpc JSON [HEADER...] -> "<status>" with the body in $WORK/body. Extra
# arguments are request headers ("Name: value"), as Daintree sends its caller's
# host model and host state.
rpc() {
  local body="$1"
  shift
  local extra=()
  for header in "$@"; do extra+=(-H "$header"); done
  curl -sS -o "$WORK/body" -w '%{http_code}' --max-time 30 ${AUTH[@]+"${AUTH[@]}"} \
    -H 'Content-Type: application/json' \
    -H 'Accept: application/json, text/event-stream' \
    ${extra[@]+"${extra[@]}"} \
    -d "$body" "$MCP" 2>/dev/null || echo 000
}
# What a build that drives its hosts directly sends on every runbooks POST.
HOST_CONTROL='Daintree-Host-Control: 1'

# check PYTHON_EXPR [EXPECTED] -> prints "ok" or the reason, reading $WORK/body
# as `body` and EXPECTED as `expected`. The expression is always a literal in
# this script; anything that came from a response goes in as EXPECTED, as data,
# never spliced into the expression.
check() {
  python3 - "$1" "$WORK/body" "${2:-}" <<'PYEOF'
import json, sys
expr, path, expected = sys.argv[1], sys.argv[2], sys.argv[3]
try:
    with open(path) as handle:
        body = json.load(handle)
except Exception as exc:
    print("body is not JSON: %s" % exc)
    raise SystemExit(0)
try:
    result = eval(expr, {"body": body, "expected": expected})
except Exception as exc:
    print("%s: %s" % (type(exc).__name__, exc))
    raise SystemExit(0)
print("ok" if result is True else (result or "check returned %r" % result))
PYEOF
}

echo "Smoke-testing $URL"

# ---- liveness, readiness, version -----------------------------------------
STATUS="$(get /health)"
[ "$STATUS" = "200" ] && pass "/health answers" || fail "/health answered $STATUS"

STATUS="$(get /readyz)"
if [ "$STATUS" = "200" ]; then
  VERDICT="$(check 'body.get("status") == "ready" and body.get("runbooks", 0) > 0 or body')"
  [ "$VERDICT" = "ok" ] && pass "/readyz is ready with a non-empty catalog" \
                        || fail "/readyz body is wrong" "$VERDICT"
  REVISION="$(python3 -c 'import json,sys;print(json.load(open(sys.argv[1])).get("catalog_revision",""))' "$WORK/body")"
else
  fail "/readyz answered $STATUS" "$(head -c 300 "$WORK/body" 2>/dev/null)"
  REVISION=""
fi

STATUS="$(get /version)"
if [ "$STATUS" = "200" ]; then
  SHA="$(python3 -c 'import json,sys;print(json.load(open(sys.argv[1])).get("build_sha",""))' "$WORK/body")"
  if [ -n "$EXPECT_BUILD_SHA" ]; then
    [ "$SHA" = "$EXPECT_BUILD_SHA" ] && pass "/version reports $SHA" \
      || fail "/version reports '${SHA:-<empty>}', expected $EXPECT_BUILD_SHA"
  else
    pass "/version answers (build ${SHA:-<unstamped>})"
  fi
else
  fail "/version answered $STATUS"
fi

# ---- the MCP endpoint -------------------------------------------------------
STATUS="$(rpc '{"jsonrpc":"2.0","id":1,"method":"initialize","params":{"protocolVersion":"2025-06-18","capabilities":{},"clientInfo":{"name":"smoke","version":"0"}}}')"
if [ "$STATUS" = "200" ]; then
  VERDICT="$(check 'body["result"]["protocolVersion"] == "2025-06-18" and "tools" in body["result"]["capabilities"] or body')"
  [ "$VERDICT" = "ok" ] && pass "MCP initialize" || fail "MCP initialize body is wrong" "$VERDICT"
else
  fail "MCP initialize answered $STATUS" "$(head -c 300 "$WORK/body" 2>/dev/null)"
fi

STATUS="$(rpc '{"jsonrpc":"2.0","id":2,"method":"tools/list"}')"
if [ "$STATUS" = "200" ]; then
  VERDICT="$(check '[t["name"] for t in body["result"]["tools"]] == ["search_runbooks", "get_runbook"] or body')"
  [ "$VERDICT" = "ok" ] && pass "MCP tools/list offers search_runbooks and get_runbook" \
                        || fail "MCP tools/list body is wrong" "$VERDICT"
else
  fail "MCP tools/list answered $STATUS"
fi

# A real search through the shipped classifier: the one check that proves the
# ONNX artifact loaded and ranks, rather than that the process is up.
STATUS="$(rpc '{"jsonrpc":"2.0","id":3,"method":"tools/call","params":{"name":"search_runbooks","arguments":{"query":"rename my terminals so I can tell the agents apart","max_results":3}}}' "$HOST_CONTROL")"
if [ "$STATUS" = "200" ]; then
  # Selected hits carry their procedure; neighbours are named only.
  VERDICT="$(check '(lambda r: r["isError"] is False and 1 <= len(r["structuredContent"]["runbooks"]) <= 3 and all(x.get("instructions") if x["selected"] else "instructions" not in x for x in r["structuredContent"]["runbooks"]) or r)(body["result"])')"
  [ "$VERDICT" = "ok" ] && pass "MCP search returns selected runbooks with instructions" \
                        || fail "MCP search result is wrong" "$VERDICT"
  # Routing, not just availability: a selector that loads but ranks garbage
  # would pass every check above. This query is the catalog's canonical case.
  VERDICT="$(check '(lambda top: top["id"] == expected and top["selected"] is True or top["id"])(body["result"]["structuredContent"]["runbooks"][0])' daintree.terminals.rename)"
  [ "$VERDICT" = "ok" ] && pass "search routes the canonical query to daintree.terminals.rename" \
                        || fail "search ranked the wrong runbook first" "$VERDICT"
  if [ -n "$REVISION" ]; then
    VERDICT="$(check 'body["result"]["structuredContent"]["catalog_revision"] == expected or body["result"]["structuredContent"]["catalog_revision"]' "$REVISION")"
    [ "$VERDICT" = "ok" ] && pass "search and /readyz agree on the current catalog revision" \
                          || fail "search catalog_revision differs from /readyz" "$VERDICT"
  fi
else
  fail "MCP tools/call answered $STATUS" "$(head -c 300 "$WORK/body" 2>/dev/null)"
fi

# A manually configured MCP client sends no host-control header and must receive
# the same current catalog and canonical route.
STATUS="$(rpc '{"jsonrpc":"2.0","id":5,"method":"tools/call","params":{"name":"search_runbooks","arguments":{"query":"rename my terminals so I can tell the agents apart","max_results":3}}}')"
if [ "$STATUS" = "200" ]; then
  VERDICT="$(check '(lambda r: r["isError"] is False and r["structuredContent"]["runbooks"] and r["structuredContent"]["runbooks"][0]["id"] == expected and r["structuredContent"]["runbooks"][0]["selected"] is True or r)(body["result"])' daintree.terminals.rename)"
  [ "$VERDICT" = "ok" ] && pass "a caller without Daintree-Host-Control is answered from a ranked catalog" \
                        || fail "the search without Daintree-Host-Control is wrong" "$VERDICT"
  if [ -n "$REVISION" ]; then
    VERDICT="$(check 'body["result"]["structuredContent"]["catalog_revision"] == expected or body["result"]["structuredContent"]["catalog_revision"]' "$REVISION")"
    [ "$VERDICT" = "ok" ] && pass "a caller without Daintree-Host-Control is served the current catalog" \
                          || fail "a caller without Daintree-Host-Control got another catalog" "$VERDICT"
  else
    fail "/readyz names no catalog revision"
  fi
else
  fail "MCP tools/call without a host model answered $STATUS" "$(head -c 300 "$WORK/body" 2>/dev/null)"
fi

# Exact retrieval, and the dependency delivery both tools share: a specific
# runbook must arrive with its foundation, which search alone never paired.
STATUS="$(rpc '{"jsonrpc":"2.0","id":4,"method":"tools/call","params":{"name":"get_runbook","arguments":{"id":"daintree.terminals.rename"}}}')"
if [ "$STATUS" = "200" ]; then
  VERDICT="$(check '(lambda r: r["isError"] is False and r["structuredContent"]["runbook"]["id"] == expected and r["structuredContent"]["runbook"]["instructions"] and [x["id"] for x in r["structuredContent"]["supporting_runbooks"]] == ["daintree.foundation"] or r)(body["result"])' daintree.terminals.rename)"
  [ "$VERDICT" = "ok" ] && pass "get_runbook returns daintree.terminals.rename with its foundation" \
                        || fail "get_runbook result is wrong" "$VERDICT"
else
  fail "MCP get_runbook answered $STATUS" "$(head -c 300 "$WORK/body" 2>/dev/null)"
fi

# With a host connected, a host-control caller gets the hosts foundation with
# every runbook, not only the hosts ones.
STATUS="$(rpc '{"jsonrpc":"2.0","id":6,"method":"tools/call","params":{"name":"get_runbook","arguments":{"id":"daintree.terminals.rename"}}}' "$HOST_CONTROL" 'Daintree-Hosts: connected')"
if [ "$STATUS" = "200" ]; then
  VERDICT="$(check '(lambda r: r["isError"] is False and [x["id"] for x in r["structuredContent"]["supporting_runbooks"]] == ["daintree.foundation", "daintree.hosts.foundation"] or r)(body["result"])')"
  [ "$VERDICT" = "ok" ] && pass "a connected host-control caller gets the hosts foundation with every runbook" \
                        || fail "the hosts foundation did not come with the runbook" "$VERDICT"
else
  fail "MCP get_runbook with a connected host answered $STATUS" "$(head -c 300 "$WORK/body" 2>/dev/null)"
fi

STATUS="$(get /v1/daintree/mcp)"
[ "$STATUS" = "405" ] && pass "GET on the MCP endpoint is 405 (JSON only, no stream)" \
                      || fail "GET on the MCP endpoint answered $STATUS, expected 405"

echo
echo "$PASS passed, $FAIL failed"
[ "$FAIL" -eq 0 ]
