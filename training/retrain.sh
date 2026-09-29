#!/usr/bin/env bash
# Retrain the selector on the current corpus and install it — one command.
#
#   training/retrain.sh            # ~1 h on Apple silicon (MPS)
#
# Compiles the corpus, trains stage 1 (retriever) and stage 2 (set-wise head, seed 41),
# exports the two-stage artifact, installs it over the shipped one, and runs the routing
# gate (`bench local`). If the gate fails, the previous artifact is restored and the
# script exits non-zero. On success: commit, push, /release.
set -euo pipefail
cd "$(dirname "$0")/.."

PY=.venv-train/bin/python
ART=src/daintree_assistant_server/runbooks/local/artifacts/selector
WORK=.tmp/retrain
export HF_HUB_DISABLE_PROGRESS_BARS=1 TQDM_DISABLE=1
mkdir -p "$WORK"

echo "== compile corpus"
$PY -m training.compile_corpus | tail -1

echo "== stage 1: retriever (log: $WORK/retriever.log)"
$PY -u -m training.train_retriever --out "$WORK/retriever" > "$WORK/retriever.log" 2>&1
tail -1 "$WORK/retriever.log"

echo "== stage 2: set-wise head (log: $WORK/setwise.log)"
$PY -u -m training.train_setwise --epochs 10 --ordering equivariant --seed 41 --init-seed 41 \
  --retriever "$WORK/retriever" --out "$WORK/setwise" > "$WORK/setwise.log" 2>&1
grep 'epoch 10' "$WORK/setwise.log"

echo "== export"
rm -rf "$WORK/artifact"
$PY -m training.export_two_stage --setwise "$WORK/setwise" --out "$WORK/artifact" > "$WORK/export.log" 2>&1
tail -2 "$WORK/export.log"

echo "== install and gate"
rm -rf "$WORK/previous" && cp -R "$ART" "$WORK/previous"
rsync -a --delete "$WORK/artifact/" "$ART/"
if .venv/bin/python -m benchmarks.runbook_selection.bench local --no-save > "$WORK/bench.txt" 2>&1; then
  grep -E 'recall-pass|QUALITY GATE' "$WORK/bench.txt"
  echo "installed. Commit the artifact (it is Git LFS) and release."
else
  grep -E 'recall-pass|QUALITY GATE|FAIL' "$WORK/bench.txt" || true
  rsync -a --delete "$WORK/previous/" "$ART/"
  echo "gate failed: previous artifact restored; details in $WORK/bench.txt" >&2
  exit 1
fi
