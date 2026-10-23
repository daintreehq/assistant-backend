#!/usr/bin/env bash
#
# Watch the deploy-main Cloud Build for one commit and emit one line per state
# change, then exit with the build's verdict. Built to be run under the Monitor
# tool: every stdout line is an event, so it prints transitions, never polls.
#
#   watch-build.sh <commit-sha> <build-region>
#
# Exit: 0 SUCCESS, 1 any other terminal status, 2 no build appeared / gcloud
# kept failing. Silence is never a verdict — every path out prints why.
#
# Polls `gcloud builds describe` rather than trusting `gcloud builds log
# --stream`, whose exit code does not reliably reflect the build outcome.
set -uo pipefail

SHA=${1:?commit sha required}
REGION=${2:?build region required}
PROJECT=${PROJECT:?Set PROJECT to your Google Cloud project ID}
TRIGGER=deploy-main
INTERVAL=20
FIND_DEADLINE=300   # the trigger normally fires within seconds of the push
MAX_ERRORS=5

# stderr goes to a file, not stdout: a gcloud warning inside --format=json
# output would break jq. It is surfaced whenever a call fails.
ERR=$(mktemp); trap 'rm -f "$ERR"' EXIT
gc() { gcloud "$@" --project="$PROJECT" --region="$REGION" 2>"$ERR"; }
lasterr() { grep -m1 'ERROR' "$ERR" || tail -1 "$ERR"; }

TRIGGER_ID=$(gc builds triggers describe "$TRIGGER" --format='value(id)') || {
  echo "ERROR: cannot describe trigger $TRIGGER in $REGION: $(lasterr)"; exit 2; }

# 1. Find the build. Filter on the trigger too, so a manual build of the same
#    commit is never mistaken for the push-fired one.
start=$(date +%s); BUILD_ID=""
while [ -z "$BUILD_ID" ]; do
  out=$(gc builds list --filter="buildTriggerId=$TRIGGER_ID AND substitutions.COMMIT_SHA=$SHA" \
    --format='value(id)' --limit=1) && BUILD_ID=$out
  if [ -z "$BUILD_ID" ]; then
    if [ $(( $(date +%s) - start )) -ge $FIND_DEADLINE ]; then
      echo "ERROR: no $TRIGGER build for $SHA after ${FIND_DEADLINE}s (last gcloud error: $(lasterr))"
      exit 2
    fi
    sleep 5
  fi
done
echo "BUILD $BUILD_ID https://console.cloud.google.com/cloud-build/builds;region=$REGION/$BUILD_ID?project=$PROJECT"

# 2. Poll until terminal, printing each step whose status changed.
prev=""; errors=0
while :; do
  if ! json=$(gc builds describe "$BUILD_ID" --format=json); then
    errors=$((errors + 1))
    if [ $errors -ge $MAX_ERRORS ]; then
      echo "ERROR: gcloud describe failed $errors times in a row: $(lasterr)"
      exit 2
    fi
    sleep "$INTERVAL"; continue
  fi
  errors=0
  cur=$(printf '%s' "$json" | jq -r '.steps[] | "\(.id)=\(.status // "PENDING")"')
  # Transitions only; QUEUED is the resting state for steps not reached yet.
  comm -13 <(printf '%s\n' "$prev" | sort) <(printf '%s\n' "$cur" | sort) \
    | grep -v '=QUEUED$' | grep -v '=PENDING$' | sed 's/^/STEP /'
  prev=$cur
  status=$(printf '%s' "$json" | jq -r '.status')
  case "$status" in
    QUEUED|WORKING|PENDING) sleep "$INTERVAL" ;;
    SUCCESS) echo "DONE SUCCESS $BUILD_ID"; exit 0 ;;
    *)
      detail=$(printf '%s' "$json" | jq -r '.failureInfo.detail // .statusDetail // ""')
      echo "DONE $status $BUILD_ID ${detail}"
      exit 1 ;;
  esac
done
