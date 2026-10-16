#!/usr/bin/env bash
# Put the selector's model files where the server loads them, exactly as selector.lock names
# them. Replaces `git lfs pull`. Files that already match are left alone; a download goes
# through ~/.cache/daintree-selector, so a fresh worktree is a copy, not a transfer.
# Needs `gcloud` signed in to a Google account that can read the bucket (only on a first
# download). Arguments pass through: `training/fetch_selector.sh --no-cache`.
set -euo pipefail
cd "$(dirname "$0")/.."
PY=python3
[ -x .venv/bin/python ] && PY=.venv/bin/python
exec "$PY" training/selector_store.py fetch "$@"
