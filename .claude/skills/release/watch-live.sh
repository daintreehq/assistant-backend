#!/usr/bin/env bash
#
# Watch the public service until it serves one commit, with no gcloud login:
# polls /version and emits one line per change, then exits with the verdict.
# The release skill's public mode; run it under the Monitor tool.
#
#   watch-live.sh <commit-sha> <pushed-at-epoch> [url]
#
# The deadline is absolute: the push time plus 65 minutes, the most a
# deploy-main build can take to start (5 min) and run (its 3600s timeout). Past
# it, no build for that push can still promote. A Monitor expiry before then is
# not a verdict: re-run with the same arguments and it picks up where it left off.
#
# Exit: 0 LIVE, 1 DEADLINE (the commit never went live). Silence is never a
# verdict — every path out prints why.
set -uo pipefail

SHA=${1:?commit sha required}
PUSHED_AT=${2:?push time (epoch seconds) required}
URL=${3:-https://assistant.daintree.org}
URL=${URL%/}
INTERVAL=20
DEADLINE=$((PUSHED_AT + 3900))

prev=""
while :; do
  cur=$(curl -fsS --max-time 15 "$URL/version" 2>/dev/null | jq -r '.build_sha // empty' 2>/dev/null)
  [ -n "$cur" ] || cur=unreachable
  if [ "$cur" = "$SHA" ]; then echo "LIVE $SHA"; exit 0; fi
  if [ "$cur" != "$prev" ]; then echo "SERVING $cur"; prev=$cur; fi
  if [ "$(date +%s)" -ge "$DEADLINE" ]; then
    echo "DEADLINE $SHA never went live; still serving $cur"
    exit 1
  fi
  sleep "$INTERVAL"
done
