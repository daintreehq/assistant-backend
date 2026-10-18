#!/usr/bin/env bash
# Regenerate tests/fixtures/daintree_mcp/*.json from a Daintree app commit.
#
#   tests/fixtures/daintree_mcp/capture/capture.sh <commit-ish> [branch-label]
#
# Creates a throwaway DETACHED worktree of the app at <commit-ish>, gives it a
# node_modules made of symlinks into the app checkout's own node_modules (the
# workspace packages are re-pointed at the worktree's packages/), copies
# captureMcpSurfaces.test.ts into it, runs that one vitest file, writes one
# whole <surface>.json per caller (pane-core, pane-full, help-core, help-full,
# external, relayed-core, relayed-full), then runs the app's own tests of the
# direct host-control contract at the same commit, and removes the worktree.
#
# Nothing is written into the app checkout itself: the worktree has a real
# node_modules directory (so vite's cache lands there), and only links are
# placed inside it.
#
# Environment:
#   DAINTREE_APP_REPO  app checkout with installed node_modules
#                      (default: ~/Projects/Daintree/daintree)
#   CAPTURE_WORKTREE   where to put the throwaway worktree (default: mktemp)
#   CAPTURE_OUT_DIR    where to write the JSON (default: the parent of this dir)
#   CAPTURE_AT         date to record (default: today, UTC)
#   KEEP_WORKTREE=1    leave the worktree in place for debugging
set -euo pipefail

if [[ $# -lt 1 ]]; then
  echo "usage: $0 <commit-ish> [branch-label]" >&2
  exit 2
fi

here="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
app="${DAINTREE_APP_REPO:-$HOME/Projects/Daintree/daintree}"
out="${CAPTURE_OUT_DIR:-$(dirname "$here")}"
ref="$1"
branch="${2:-}"

commit="$(git -C "$app" rev-parse --verify "${ref}^{commit}")"
if [[ ! -d "$app/node_modules" ]]; then
  echo "error: $app/node_modules is missing; run npm ci in the app checkout first" >&2
  exit 1
fi

if [[ -n "${CAPTURE_WORKTREE:-}" ]]; then
  wt="$CAPTURE_WORKTREE"
else
  wt="$(mktemp -d "${TMPDIR:-/tmp}/daintree-capture.XXXXXX")/app"
fi

cleanup() {
  if [[ "${KEEP_WORKTREE:-}" == "1" ]]; then
    echo "worktree kept at $wt"
    return
  fi
  if [[ -d "$wt" ]]; then
    # Unlink every symlink first so nothing below can ever follow one into the
    # app checkout's node_modules.
    find "$wt/node_modules" -maxdepth 2 -type l -exec rm -f {} + 2>/dev/null || true
    find "$wt/packages" -maxdepth 2 -name node_modules -type l -exec rm -f {} + 2>/dev/null || true
    git -C "$app" worktree remove --force "$wt" || true
  fi
  git -C "$app" worktree prune || true
}
trap cleanup EXIT

git -C "$app" worktree add --detach "$wt" "$commit" >/dev/null

# node_modules: a real directory of links. vite's caches (.vite, .vite-temp)
# are not linked, so they are created inside the worktree.
mkdir -p "$wt/node_modules"
shopt -s dotglob nullglob
for entry in "$app"/node_modules/*; do
  name="$(basename "$entry")"
  case "$name" in
    .vite | .vite-temp | .cache | @daintreehq) continue ;;
  esac
  ln -s "$entry" "$wt/node_modules/$name"
done
# npm workspace links point at ../../packages/<name>; keep them relative so they
# resolve to THIS commit's packages, not the app checkout's.
if [[ -d "$app/node_modules/@daintreehq" ]]; then
  mkdir -p "$wt/node_modules/@daintreehq"
  for entry in "$app"/node_modules/@daintreehq/*; do
    name="$(basename "$entry")"
    target="$(readlink "$entry" || true)"
    if [[ "$target" == ../../packages/* ]]; then
      ln -s "$target" "$wt/node_modules/@daintreehq/$name"
    else
      ln -s "$entry" "$wt/node_modules/@daintreehq/$name"
    fi
  done
fi
for nm in "$app"/packages/*/node_modules; do
  pkg="$(basename "$(dirname "$nm")")"
  [[ -d "$wt/packages/$pkg" ]] && ln -s "$nm" "$wt/packages/$pkg/node_modules"
done
shopt -u dotglob nullglob

cp "$here/captureMcpSurfaces.test.ts" "$wt/src/services/actions/__tests__/captureMcpSurfaces.test.ts"

mkdir -p "$out"
(
  cd "$wt"
  CAPTURE_OUT_DIR="$out" \
    CAPTURE_APP_COMMIT="$commit" \
    CAPTURE_APP_BRANCH="$branch" \
    CAPTURE_AT="${CAPTURE_AT:-}" \
    ./node_modules/.bin/vitest run src/services/actions/__tests__/captureMcpSurfaces.test.ts
  # The host-control contract a listing cannot show, proven by the app's own
  # tests at the same commit: who the runtime counts as one of this machine's
  # assistants and the per-host switch (HostCallRuntime, install), a call's
  # outcomes and its callId running at most once (HostCallRuntime.call,
  # relayCallLedger), the host admitting each call through a relayed session of
  # its own (hostMcpRelay, httpLifecycle.relay), that session's gates and the
  # drive rule (sessionServer.relay, relayWriteRule), notices typed back into
  # the calling run (HostCallRuntime.notices, relayNoticeOutbox,
  # terminalNotify.relay), confirmations put to the Shell (HostCallRuntime.approval,
  # relayApprovals), the session-side refusals (hostControlTools), and one
  # assistant addressing another (AssistantTerminalControl).
  contract_tests=(
    electron/remote/assistant/__tests__/HostCallRuntime.test.ts
    electron/remote/assistant/__tests__/HostCallRuntime.call.test.ts
    electron/remote/assistant/__tests__/HostCallRuntime.approval.test.ts
    electron/remote/assistant/__tests__/HostCallRuntime.notices.test.ts
    electron/remote/assistant/__tests__/hostMcpRelay.test.ts
    electron/remote/assistant/__tests__/install.test.ts
    electron/remote/assistant/__tests__/relayCallLedger.test.ts
    electron/remote/assistant/__tests__/relayApprovals.test.ts
    electron/remote/assistant/__tests__/relayNoticeOutbox.test.ts
    electron/services/mcp-server/__tests__/hostControlTools.test.ts
    electron/services/mcp-server/__tests__/sessionServer.relay.test.ts
    electron/services/mcp-server/__tests__/httpLifecycle.relay.test.ts
    electron/services/mcp-server/__tests__/relayWriteRule.test.ts
    electron/services/mcp-server/__tests__/terminalNotify.relay.test.ts
    electron/services/assistants/__tests__/AssistantTerminalControl.test.ts
  )
  for t in "${contract_tests[@]}"; do
    if [[ ! -f "$t" ]]; then
      echo "error: contract test $t is not at $commit; update capture.sh's list" >&2
      exit 1
    fi
  done
  ./node_modules/.bin/vitest run "${contract_tests[@]}"
)

node -e '
const fs = require("node:fs");
const path = require("node:path");
for (const file of fs.readdirSync(process.argv[1]).filter((f) => f.endsWith(".json")).sort()) {
  const f = JSON.parse(fs.readFileSync(path.join(process.argv[1], file), "utf8"));
  const variants = Object.entries(f.capture.variants ?? {})
    .map(([key, v]) => `${key}: tools=${v.tool_count} +${v.added.length} -${v.removed.length} ~${v.changed.length}`)
    .join("; ");
  console.log(`${file}  tools=${f.capture.tool_count}  hash=${f.capture.surface_hash}${variants ? `  variants: ${variants}` : ""}`);
}
' "$out"
