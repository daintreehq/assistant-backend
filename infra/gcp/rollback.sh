#!/usr/bin/env bash
#
# Send traffic back to the previous Cloud Run revision.
#
#   ./infra/gcp/rollback.sh                   # show the plan, ask, then roll back
#   ./infra/gcp/rollback.sh --dry-run         # show the plan and stop
#   ./infra/gcp/rollback.sh --yes             # no prompt (for an incident, or a drill)
#   ./infra/gcp/rollback.sh --to daintree-assistant-00042-abc
#                                             # a specific revision, not just the previous
#   ./infra/gcp/rollback.sh --list            # what is available to roll back to
#
# WHY THIS EXISTS. `cloudbuild.yaml` promotes a candidate only after it answers
# /readyz and reports the right build_sha, so a revision that fails to START never
# takes traffic. What that cannot catch is a revision that comes up perfectly and
# is WRONG — a prompt change that degrades answers, a runbook that routes badly, a
# latency regression. Those are found by a human minutes later, and the recovery
# was previously "remember the gcloud incantation, under pressure, with a tester
# waiting". This is that incantation, with the checks that make it safe.
#
# Rolling forward again is the same command: --to the revision you left.
#
# Exits non-zero unless the post-rollback checks CONFIRM the target is serving.
# "Could not verify" is a failure here, not a shrug: a traffic change that applied
# while leaving the service unhealthy is the worst outcome of all, because the
# operator believes the incident is over and stops looking.

set -uo pipefail

PROJECT="${PROJECT:-daintree-research}"
REGION="${REGION:-us-central1}"
SERVICE="${SERVICE:-daintree-assistant}"
TARGET=""
ASSUME_YES=0
DRY_RUN=0
LIST_ONLY=0

need_value() {
  # `shift 2` on a flag with no value shifts once, leaves the flag in place, and
  # (without `set -e`) spins forever. During an incident that is a hung terminal
  # and no rollback.
  [ "$2" -gt 1 ] || { echo "$1 requires a value" >&2; exit 2; }
}

while [ $# -gt 0 ]; do
  case "$1" in
    --to)      need_value --to $#; TARGET="$2"; shift 2 ;;
    --yes|-y)  ASSUME_YES=1; shift ;;
    --dry-run) DRY_RUN=1; shift ;;
    --list)    LIST_ONLY=1; shift ;;
    -h|--help) sed -n '2,26p' "$0" | sed 's/^# \{0,1\}//'; exit 0 ;;
    *) echo "unknown argument: $1" >&2; exit 2 ;;
  esac
done

bold() { printf '\033[1m%s\033[0m\n' "$1"; }
fail() { printf '\033[31mFAIL\033[0m  %s\n' "$1" >&2; }
ok()   { printf '\033[32mOK\033[0m    %s\n' "$1"; }

gc() { gcloud run "$@" --region="$REGION" --project="$PROJECT"; }

# Every JSON parse goes through a python file rather than an inline `-c` string.
# Quoting a JSON-handling script inside single-quoted bash forces contortions that
# are unreadable and easy to get subtly wrong, and this is a script people read
# under pressure.
WORK="$(mktemp -d)"
trap 'rm -rf "$WORK"' EXIT

cat > "$WORK/current.py" <<'PYEOF'
import json, sys

d = json.load(sys.stdin)
status = d.get("status", {})
url = status.get("url", "")
serving = [t for t in (status.get("traffic") or []) if (t.get("percent") or 0) > 0]

# Exactly one revision must hold all the traffic. Under a split there is no single
# "current", and taking the largest share is a guess: on a 60/40, "the revision
# behind the current one" means something different depending on which side you
# read from. Refuse, and let the operator name a target.
#
# STATE is printed FIRST and is never empty. `read` treats a tab as IFS
# WHITESPACE, so a leading empty field is silently dropped and every later field
# shifts left by one — which read the split marker as the revision name and sent
# the script down the wrong branch entirely.
if not serving:
    # Distinct from a split: nothing is serving at all, which is a different
    # situation and a different thing to tell the operator.
    print("NONE\t\t{}".format(url))
elif len(serving) != 1 or (serving[0].get("percent") or 0) != 100:
    shares = ", ".join(
        "{}={}%".format(t.get("revisionName", "?"), t.get("percent", 0)) for t in serving
    )
    print("SPLIT\t{}\t{}".format(shares, url))
else:
    print("OK\t{}\t{}".format(serving[0].get("revisionName", ""), url))
PYEOF

cat > "$WORK/plan.py" <<'PYEOF'
import json, sys

current = sys.argv[1]
rows = []
for r in json.load(sys.stdin):
    meta = r.get("metadata", {})
    conditions = (r.get("status") or {}).get("conditions") or []
    # The Ready condition specifically. A gcloud `--filter` over two predicates of
    # a REPEATED field matches a revision that has a Ready condition and,
    # separately, some other condition that is True — which is not the same
    # question. Offering a revision that never started makes an incident worse.
    ready = any(c.get("type") == "Ready" and c.get("status") == "True" for c in conditions)
    if not ready:
        continue
    name = meta.get("name", "")
    rows.append({
        "name": name,
        "sha": (meta.get("labels") or {}).get("commit-sha", "?"),
        "created": meta.get("creationTimestamp", "")[:19].replace("T", " "),
        "current": name == current,
    })
print(json.dumps(rows))
PYEOF

cat > "$WORK/pick.py" <<'PYEOF'
import json, sys

# The revision immediately BEHIND the one serving — not simply the newest one that
# is not serving. After a rollback those differ: the newest revision is the bad
# build we just moved away from, and rolling "back" to it would roll forward into
# it, during an incident, while reporting success.
rows = json.load(sys.stdin)
here = next((i for i, r in enumerate(rows) if r["current"]), None)
print(rows[here + 1]["name"] if here is not None and here + 1 < len(rows) else "")
PYEOF

cat > "$WORK/field.py" <<'PYEOF'
import json, sys

# Exact lookup by name, doubling as the membership check. Deliberately not `grep`:
# a revision name is substring- and regex-matchable, so a typo'd `--to` could
# match a revision the operator did not mean.
rows = json.load(sys.stdin)
name, field = sys.argv[1], sys.argv[2]
row = next((r for r in rows if r["name"] == name), None)
if row is None:
    sys.exit(1)
print(row[field])
PYEOF

cat > "$WORK/build_sha.py" <<'PYEOF'
import json, sys

try:
    print(json.load(sys.stdin).get("build_sha", ""))
except Exception:
    print("")
PYEOF

SERVICE_JSON="$(gc services describe "$SERVICE" --format=json 2>/dev/null)"
if [ -z "$SERVICE_JSON" ]; then
  fail "cannot describe $SERVICE in $REGION/$PROJECT"
  exit 2
fi

# --service is not optional: without it `revisions list` returns EVERY revision in
# the project and region, and the one "behind the current" can belong to another
# service entirely.
REVISIONS_JSON="$(gc revisions list --service="$SERVICE" \
  --sort-by='~metadata.creationTimestamp' --format=json 2>/dev/null)"
if [ -z "$REVISIONS_JSON" ]; then
  fail "cannot list revisions for $SERVICE — refusing to guess a rollback target"
  exit 2
fi

CURRENT_INFO="$(printf '%s' "$SERVICE_JSON" | python3 "$WORK/current.py")" || {
  fail "could not read the traffic split for $SERVICE"
  exit 2
}
# STATE leads so no field that `read` sees can be empty — see current.py.
IFS=$'\t' read -r SPLIT_STATE CURRENT SERVICE_URL <<EOF
$CURRENT_INFO
EOF

if [ "${SPLIT_STATE:-}" = "SPLIT" ]; then
  # $CURRENT holds the share summary in this branch.
  fail "traffic is split across revisions ($CURRENT) — no single revision to roll back FROM"
  echo "      Name the target explicitly:  $0 --to <revision>" >&2
  exit 2
fi
if [ "${SPLIT_STATE:-}" != "OK" ] || [ -z "$CURRENT" ]; then
  fail "no revision is currently serving traffic — nothing to roll back from"
  exit 2
fi

PLAN="$(printf '%s' "$REVISIONS_JSON" | python3 "$WORK/plan.py" "$CURRENT")" || {
  fail "could not parse the revision list"
  exit 2
}

bold "Service: $SERVICE  ($REGION / $PROJECT)"
echo "URL: ${SERVICE_URL:-unknown}"
echo
printf '%-42s %-10s %-20s\n' "REVISION" "COMMIT" "CREATED (UTC)"
printf '%s' "$PLAN" | python3 -c '
import json, sys
for r in json.load(sys.stdin)[:10]:
    mark = "  <-- serving now" if r["current"] else ""
    print("{:<42} {:<10} {:<20}{}".format(r["name"], r["sha"], r["created"], mark))
'
echo

if [ "$LIST_ONLY" = "1" ]; then
  exit 0
fi

if [ -z "$TARGET" ]; then
  TARGET="$(printf '%s' "$PLAN" | python3 "$WORK/pick.py")"
fi

if [ -z "$TARGET" ]; then
  fail "no previous ready revision to roll back to (is this the first deploy?)"
  exit 2
fi
if [ "$TARGET" = "$CURRENT" ]; then
  fail "target $TARGET is already serving — nothing to do"
  exit 2
fi
# Exact match against a READY revision of THIS service. plan.py has already
# dropped the unready ones, so a `--to` naming a revision that never started is
# refused here rather than handed to update-traffic.
if ! EXPECTED_SHA="$(printf '%s' "$PLAN" | python3 "$WORK/field.py" "$TARGET" sha)"; then
  fail "$TARGET is not a ready revision of $SERVICE"
  exit 2
fi

bold "Plan: move 100% of traffic"
echo "  from  $CURRENT"
echo "  to    $TARGET"
echo

if [ "$DRY_RUN" = "1" ]; then
  echo "Dry run — nothing changed. The command this would run:"
  echo "  gcloud run services update-traffic $SERVICE \\"
  echo "    --region=$REGION --project=$PROJECT --to-revisions=$TARGET=100 --quiet"
  exit 0
fi

if [ "$ASSUME_YES" != "1" ]; then
  printf 'Proceed? [y/N] '
  read -r answer
  case "$answer" in
    y|Y|yes|YES) ;;
    *) echo "Aborted."; exit 1 ;;
  esac
fi

echo "Rolling back…"
if ! gc services update-traffic "$SERVICE" --to-revisions="$TARGET=100" --quiet; then
  fail "update-traffic failed — the service is unchanged"
  exit 1
fi

# ---------------------------------------------------------------------------
# Verify, failing CLOSED. Anything short of "the target is demonstrably serving
# and healthy" is a failure.
# ---------------------------------------------------------------------------
FAILED=0
URL="${SERVICE_URL:-}"
if [ -z "$URL" ]; then
  URL="$(gc services describe "$SERVICE" --format='value(status.url)' 2>/dev/null)"
fi

if [ -z "$URL" ]; then
  fail "could not resolve the service URL to verify the rollback"
  FAILED=1
elif [ "$EXPECTED_SHA" = "?" ]; then
  # No commit-sha label to compare against, so /readyz alone cannot say WHICH
  # revision answered. Reported rather than passed quietly.
  fail "$TARGET carries no commit-sha label — the serving revision cannot be confirmed"
  FAILED=1
else
  # Traffic migration is not instantaneous, so one immediate request can still
  # reach the old revision. Poll until the target's build_sha is what answers,
  # rather than reading a single sample and believing it.
  CONFIRMED=0
  SERVING=""
  for _ in 1 2 3 4 5 6 7 8 9 10; do
    SERVING="$(curl -fsS --max-time 20 "$URL/version" 2>/dev/null \
      | python3 "$WORK/build_sha.py" 2>/dev/null)"
    case "${SERVING:-}" in
      # The revision label is the SHORT sha the deploy stamped; /version reports
      # the full one, so the prefix match runs in this direction.
      "$EXPECTED_SHA"*) CONFIRMED=1; break ;;
    esac
    sleep 3
  done
  if [ "$CONFIRMED" = "1" ]; then
    ok "/version reports $SERVING, matching $TARGET"
  else
    fail "after ~30s /version still reports '${SERVING:-<no answer>}', not $TARGET ($EXPECTED_SHA)"
    FAILED=1
  fi

  if curl -fsS --max-time 30 "$URL/readyz" >/dev/null 2>&1; then
    ok "/readyz answers"
  else
    fail "/readyz did not answer after the rollback"
    FAILED=1
  fi
fi

echo
if [ "$FAILED" = "0" ]; then
  bold "Rolled back: $CURRENT -> $TARGET"
  echo "Roll forward with:  $0 --to $CURRENT"
else
  bold "Traffic was moved to $TARGET, but the service could NOT be confirmed healthy."
  echo "Check the logs before assuming the incident is over:"
  echo "  gcloud run services logs read $SERVICE --region=$REGION --project=$PROJECT --limit=50"
fi
exit "$FAILED"
