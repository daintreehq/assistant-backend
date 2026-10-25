#!/usr/bin/env bash
# dtrb: Daintree runbook trials, an ISOLATED Daintree + scratch project, a local
# runbook server serving THIS checkout, and agents run in real Daintree panes.
#
#   dtrb.sh build [ref]            clone + build the app for e2e (default: origin/develop)
#   dtrb.sh runbooks serve|start|stop|status   (serve = foreground; prefer it)
#   dtrb.sh up [--hosts N] [run-name]   seed a scratch project and hold an isolated Daintree open;
#         --hosts 1-3: also N isolated Remote Hosts (Host mode, private sshd on 127.0.0.1),
#         each with a clone of the scratch repo at the same base commit, added to the Shell
#         (DTRB_HOST_WITHOUT_PROJECT=N: host N opens an unrelated repo instead)
#         DTRB_GLOBAL_AGENT=claude|codex: also start the installation's global Assistant as
#         that agent (set in the isolated app's own settings, never the user's)
#   dtrb.sh trial [opts] "prompt"  launch one agent pane with the prompt; wait; summarise
#         --agent claude|codex (claude)  --model M (haiku)  --timeout MIN (20)  --id NAME
#         --flag ARG (repeatable, verbatim CLI flag)  --no-wait  -f PROMPT_FILE
#         --background [--name TITLE]  launch a worker as the user would; no trial pane owns it
#         --adopt ID[,ID]              hand those terminals to the trial pane at launch
#         --assistant                  type the prompt into the Shell's own Daintree Assistant
#                                      (the host.* caller) instead of launching a pane
#         --global                     type the prompt into the global Assistant (needs
#                                      DTRB_GLOBAL_AGENT at up); its transcript is the evidence
#   dtrb.sh wait <trial-id>        wait for a trial to settle, then summarise
#   dtrb.sh summary <trial-id> [--full|--json]
#   dtrb.sh send "<text>" [terminal-id]   type a user follow-up into the trial pane mid-trial
#   dtrb.sh adopt <id[,id]> <orchestrator-pane-id>   the user's mid-trial hand-over gesture
#   dtrb.sh restart                quit the Shell and reopen it on the same user data, as a person
#                                  restarting Daintree; hosts stay up, the global Assistant restarts
#   dtrb.sh hold on|off            keep the running trial from settling while you act by hand
#   dtrb.sh reset                  close agent panes, restore the scratch repo to its seed
#   dtrb.sh mcp [--as help] [--on HOST] <action> '<json>'   call an ISOLATED app's MCP:
#         default the Shell with its API key (external surface); --as help the Shell as
#         its own assistant (help-core/-full: host.list, host.call); --on HOST a Remote
#         Host's own MCP with its API key (to check what a host.call did there)
#   dtrb.sh hosts                  the run's Remote Hosts (name, hostId, projectId, ...)
#   dtrb.sh snap                   screenshot + terminal dump of the Shell and every host window,
#                                  and the global Assistant's screen (global.txt)
#   dtrb.sh project                print the running trial project's path
#   dtrb.sh status | down          show state | close the app and stop the holder
#   dtrb.sh purge [run-name]       delete stopped runs and their scratch projects
#
# Parallel runners: set DTRB_RUNNER=<name> and DTRB_N=<1-9> on EVERY call. Each runner
# gets its own run root, isolated-app MCP port (45700+N) and runbook server port (8620+N);
# the app build is shared. One driver per runner: calls are not safe to run concurrently
# against the same run.
#
# Nothing here touches the user's running Daintree (its own user-data dir, its own MCP
# port, a project under ~/.cache/ws) or the user's checkouts (the app is cloned; its
# node_modules are symlinks that are only read).
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# The runbook server serves the checkout this script lives in (a worktree included),
# so a trial always sees the branch under test.
BACKEND="${RUNBOOKS_REPO:-$(git -C "$HERE" rev-parse --show-toplevel)}"
if [ -n "${DTRB_RUNNER:-}" ]; then
  : "${DTRB_N:?set DTRB_N (1-9) with DTRB_RUNNER}"
  E2E_ROOT="${E2E_ROOT:-$HOME/.cache/dtrb-$DTRB_RUNNER}"
  DTRB_MCP_PORT="${DTRB_MCP_PORT:-$((45700 + DTRB_N))}"
  RUNBOOKS_PORT="${RUNBOOKS_PORT:-$((8620 + DTRB_N))}"
fi
E2E_ROOT="${E2E_ROOT:-$HOME/.cache/daintree-runbook-e2e}"
if [ -z "${DAINTREE_APP_REPO:-}" ] && [ ! -d "$HOME/Projects/Daintree/daintree" ] && [ -d "$HOME/Projects/daintree/.git" ]; then
  DAINTREE_APP_REPO="$HOME/Projects/daintree"
fi
APP_SRC="${DAINTREE_APP_REPO:-$HOME/Projects/Daintree/daintree}"
APP="${DTRB_APP:-$HOME/.cache/daintree-runbook-e2e/app}"
RUNBOOKS_PORT="${RUNBOOKS_PORT:-8611}"
RUNBOOKS_URL="http://127.0.0.1:$RUNBOOKS_PORT/v1/daintree/mcp"
MCP_PORT="${DTRB_MCP_PORT:-45600}"
PROJECTS_ROOT="${DTRB_PROJECTS_ROOT:-$HOME/.cache/ws}"
PY="${PYTHON:-python3}"

say() { printf 'dtrb: %s\n' "$*" >&2; }
die() { say "$*"; exit 1; }
current_run() {
  [ -L "$E2E_ROOT/current" ] || die "no run is up (dtrb.sh up)"
  (cd "$E2E_ROOT/current" && pwd -P)
}

# The backend's interpreter, in PY_BACKEND. A worktree has no .venv of its own: borrow
# the main checkout's, and always put this tree's src/ first so the editable install
# never wins. Call it directly, never in $(...): the export would die with the subshell.
resolve_backend_python() {
  export PYTHONPATH="$BACKEND/src${PYTHONPATH:+:$PYTHONPATH}"
  if [ -x "$BACKEND/.venv/bin/python" ]; then
    PY_BACKEND="$BACKEND/.venv/bin/python"; return
  fi
  local common main
  common="$(git -C "$BACKEND" rev-parse --path-format=absolute --git-common-dir)"
  main="$(dirname "$common")"
  [ -x "$main/.venv/bin/python" ] || die "no .venv in $BACKEND or $main"
  PY_BACKEND="$main/.venv/bin/python"
}

# Hand the holder one request file (send.json, adopt.json): one at a time, atomically,
# and only once the previous one has been taken.
drop_request() {
  local run="$1" name="$2" tmp="$3" i
  [ -s "$tmp" ] || die "empty $name request"
  for i in $(seq 1 30); do [ -e "$run/$name" ] || break; sleep 1; done
  if [ -e "$run/$name" ]; then rm -f "$tmp"; die "a previous $name is still pending"; fi
  mv "$tmp" "$run/$name"
  for i in $(seq 1 30); do [ -e "$run/$name" ] || return 0; sleep 1; done
  die "$name was not taken: the holder reads it only while a trial is running"
}

cmd_build() {
  local ref="${1:-origin/develop}" sha
  sha="$(git -C "$APP_SRC" rev-parse --verify "$ref^{commit}")"
  mkdir -p "$(dirname "$APP")"
  if [ ! -d "$APP/.git" ]; then
    say "cloning $APP_SRC (local clone: nothing is written to the source repo)"
    git clone --quiet --no-checkout "$APP_SRC" "$APP"
  else
    git -C "$APP" fetch --quiet origin
  fi
  git -C "$APP" checkout --quiet --force --detach "$sha"
  git -C "$APP" clean -fdxq -e node_modules -e 'packages/*/node_modules' -e e2e/dtrb -e playwright.dtrb.config.ts
  # node_modules: a real directory of links into the source checkout's (read only).
  # Topped up on every build, so a package the checkout installed later gets its link.
  mkdir -p "$APP/node_modules/@daintreehq"
  local entry name target nm pkg
  for entry in "$APP_SRC"/node_modules/* "$APP_SRC"/node_modules/.bin; do
    name="$(basename "$entry")"
    case "$name" in .vite|.vite-temp|.cache|@daintreehq) continue ;; esac
    [ -e "$APP/node_modules/$name" ] || [ -L "$APP/node_modules/$name" ] || ln -s "$entry" "$APP/node_modules/$name"
  done
  for entry in "$APP_SRC"/node_modules/@daintreehq/*; do
    name="$(basename "$entry")"; target="$(readlink "$entry" || true)"
    [ -e "$APP/node_modules/@daintreehq/$name" ] || [ -L "$APP/node_modules/@daintreehq/$name" ] && continue
    if [[ "$target" == ../../packages/* ]]; then ln -s "$target" "$APP/node_modules/@daintreehq/$name"
    else ln -s "$entry" "$APP/node_modules/@daintreehq/$name"; fi
  done
  for nm in "$APP_SRC"/packages/*/node_modules; do
    pkg="$(basename "$(dirname "$nm")")"
    [ -d "$APP/packages/$pkg" ] && [ ! -e "$APP/packages/$pkg/node_modules" ] && [ ! -L "$APP/packages/$pkg/node_modules" ] && ln -s "$nm" "$APP/packages/$pkg/node_modules"
  done
  say "building workspace packages, renderer and main (NODE_ENV=test) at ${sha:0:10}"
  local p
  for p in plugin-sdk svelte-source-model tour plugin-vite daintree-plugin create-daintree-plugin; do
    [ -d "$APP/packages/$p" ] && (cd "$APP/packages/$p" && ../../node_modules/.bin/tsup >/dev/null)
  done
  (cd "$APP" && npx vite build >"$(dirname "$APP")/build.log" 2>&1 \
    && NODE_ENV=test node scripts/build-main.mjs >>"$(dirname "$APP")/build.log" 2>&1) \
    || die "build failed; see $(dirname "$APP")/build.log"
  echo "$sha" > "$APP/.dtrb-built"
  say "built ${sha:0:10} ($(git -C "$APP" log -1 --format=%s))"
}

cmd_runbooks() {
  local pidf="$E2E_ROOT/runbooks-$RUNBOOKS_PORT.pid" logf="$E2E_ROOT/runbooks-$RUNBOOKS_PORT.log"
  mkdir -p "$E2E_ROOT"
  case "${1:-status}" in
    serve)
      # Foreground: run it under a supervisor (a terminal tab, or an agent's background
      # task). A nohup'd server started from an agent's shell call was reaped when that
      # session's process group was cleaned up. Real env beats .env in
      # pydantic-settings, so PORT here wins over the repo's .env.
      # exec keeps this pid, so stop can find a served server too.
      resolve_backend_python
      echo $$ >"$pidf"
      cd "$BACKEND" && PORT="$RUNBOOKS_PORT" HOST=127.0.0.1 DAINTREE_LOG_CONSOLE=1 \
        exec "$PY_BACKEND" -m daintree_assistant_server ;;
    start)
      if curl -fsS -o /dev/null --max-time 2 "http://127.0.0.1:$RUNBOOKS_PORT/readyz" 2>/dev/null; then
        say "already serving on $RUNBOOKS_PORT"; return 0; fi
      resolve_backend_python
      (cd "$BACKEND" && PORT="$RUNBOOKS_PORT" HOST=127.0.0.1 DAINTREE_LOG_CONSOLE=1 \
        nohup "$PY_BACKEND" -m daintree_assistant_server >"$logf" 2>&1 & echo $! >"$pidf")
      local i
      for i in $(seq 1 90); do
        curl -fsS -o /dev/null --max-time 2 "http://127.0.0.1:$RUNBOOKS_PORT/readyz" 2>/dev/null && break
        sleep 1
      done
      curl -fsS "http://127.0.0.1:$RUNBOOKS_PORT/readyz" || die "runbook server not ready; see $logf"
      echo
      say "runbooks MCP: $RUNBOOKS_URL (from $BACKEND @ $(git -C "$BACKEND" rev-parse --short HEAD); log $logf)"
      ;;
    stop)
      [ -f "$pidf" ] && kill "$(cat "$pidf")" 2>/dev/null || true
      rm -f "$pidf"
      # A server this kit did not start (or lost the pid of) keeps the port: say so,
      # or the next start silently talks to the old checkout.
      local i holder
      for i in $(seq 1 15); do
        lsof -nP -iTCP:"$RUNBOOKS_PORT" -sTCP:LISTEN >/dev/null 2>&1 || { say "runbook server stopped"; return 0; }
        sleep 1
      done
      holder="$(lsof -nP -t -iTCP:"$RUNBOOKS_PORT" -sTCP:LISTEN 2>/dev/null | head -1)"
      die "port $RUNBOOKS_PORT is still served (pid ${holder:-?}); stop that process before starting another" ;;
    status)
      curl -fsS "http://127.0.0.1:$RUNBOOKS_PORT/readyz" && echo || say "not serving on $RUNBOOKS_PORT" ;;
  esac
}

seed_project() {
  local dir="$1"
  mkdir -p "$dir/src" "$dir/test"
  cat >"$dir/package.json" <<'EOF'
{
  "name": "tally",
  "version": "0.3.0",
  "description": "A tiny shopping-cart totals library.",
  "type": "module",
  "main": "src/cart.js",
  "scripts": {
    "test": "node --test",
    "start": "node src/cli.js"
  },
  "license": "MIT"
}
EOF
  cat >"$dir/src/cart.js" <<'EOF'
// Cart totals. Prices are integer cents to avoid float drift.
export function subtotal(items) {
  return items.reduce((sum, item) => sum + item.priceCents * item.qty, 0);
}

export function applyDiscount(cents, percent) {
  if (percent < 0 || percent > 100) throw new RangeError("percent must be 0-100");
  return Math.round(cents * (1 - percent / 100));
}

export function withTax(cents, rate = 0.15) {
  return Math.round(cents * (1 + rate));
}

export function total(items, { discountPercent = 0, taxRate = 0.15 } = {}) {
  return withTax(applyDiscount(subtotal(items), discountPercent), taxRate);
}
EOF
  cat >"$dir/src/cli.js" <<'EOF'
import { total } from "./cart.js";

const items = [
  { sku: "apple", priceCents: 120, qty: 3 },
  { sku: "bread", priceCents: 450, qty: 1 },
];
console.log(`total: ${(total(items, { discountPercent: 10 }) / 100).toFixed(2)}`);
EOF
  cat >"$dir/test/cart.test.js" <<'EOF'
import { test } from "node:test";
import assert from "node:assert/strict";
import { subtotal, applyDiscount, withTax, total } from "../src/cart.js";

const items = [
  { sku: "apple", priceCents: 120, qty: 3 },
  { sku: "bread", priceCents: 450, qty: 1 },
];

test("subtotal sums price times quantity", () => {
  assert.equal(subtotal(items), 810);
});

test("applyDiscount rejects out-of-range percentages", () => {
  assert.throws(() => applyDiscount(100, 101), RangeError);
});

test("total applies discount before tax", () => {
  assert.equal(total(items, { discountPercent: 10 }), withTax(729));
});
EOF
  cat >"$dir/README.md" <<'EOF'
# tally

A tiny shopping-cart totals library: subtotal, percentage discounts and tax,
all in integer cents.

## Development

    npm test        # node --test: runs test/*.test.js
    npm start       # prints a sample total

Node 20 or newer; there are no dependencies.
EOF
  printf 'node_modules/\n' >"$dir/.gitignore"
  (cd "$dir" && git init --quiet -b main && git add -A \
    && git -c user.email=dev@example.com -c user.name=Dev commit --quiet -m "Initial tally library" \
    && git init --quiet --bare "$dir/../origin.git" \
    && git remote add origin "$dir/../origin.git" && git push --quiet -u origin main)
  # The runbook server for Claude Code, as a project-scope MCP server; untracked and
  # excluded so the agent sees a clean tree. enableAllProjectMcpServers skips the
  # "new MCP server found" approval an unattended run cannot answer.
  cat >"$dir/.mcp.json" <<EOF
{ "mcpServers": { "daintree-runbooks": { "type": "http", "url": "$RUNBOOKS_URL" } } }
EOF
  mkdir -p "$dir/.claude"
  cat >"$dir/.claude/settings.local.json" <<'EOF'
{
  "enableAllProjectMcpServers": true,
  "permissions": { "allow": ["mcp__daintree", "mcp__daintree-runbooks"] }
}
EOF
  # The host instruction from docs/agents/DAINTREE_RUNBOOK_CLIENT.md, as a user would
  # install it: CLAUDE.local.md is Claude Code's personal, untracked project memory.
  # DTRB_CLIENT_INSTRUCTION=0 leaves it out (the prompt then has to ask for runbooks).
  if [ "${DTRB_CLIENT_INSTRUCTION:-1}" = 1 ]; then
    awk '/^## The client instruction/{f=1} f&&/^```text/{p=1;next} p&&/^```/{exit} p' \
      "$BACKEND/docs/agents/DAINTREE_RUNBOOK_CLIENT.md" >"$dir/CLAUDE.local.md"
    [ -s "$dir/CLAUDE.local.md" ] || die "could not extract the client instruction"
  fi
  printf '.mcp.json\n.claude/\n.daintree/\nCLAUDE.local.md\n' >>"$dir/.git/info/exclude"
}

# A Remote Host's copy of the scratch project: a clone of the same bare origin, so the
# Shell and every host start from one base commit, with the same agent config. With
# a third argument, an unrelated repo instead (no origin, nothing shared): a host that
# does not have the project, for preparing one there (DTRB_HOST_WITHOUT_PROJECT).
seed_host_project() {
  local src="$1" dir="$2" f
  if [ -n "${3:-}" ]; then
    dir="$(dirname "$dir")/notes"
    mkdir -p "$dir" && git -C "$dir" init --quiet -b main
    printf '# notes\n\nScratch notes, kept in git.\n' >"$dir/README.md"
    git -C "$dir" add -A && git -C "$dir" -c user.name=dev -c user.email=dev@example.invalid commit --quiet -m "Start notes"
    echo "$dir"; return
  fi
  git clone --quiet -b main "$src/../origin.git" "$dir"
  for f in .mcp.json .claude CLAUDE.local.md; do [ -e "$src/$f" ] && cp -R "$src/$f" "$dir/$f"; done
  printf '.mcp.json\n.claude/\n.daintree/\nCLAUDE.local.md\n' >>"$dir/.git/info/exclude"
}

cmd_up() {
  local nhosts="${DTRB_HOSTS:-0}"
  if [ "${1:-}" = --hosts ]; then nhosts="${2:?--hosts N}"; shift 2; fi
  case "$nhosts" in 0|1|2|3) ;; *) die "--hosts takes 0-3" ;; esac
  [ "$nhosts" = 0 ] || [ -x /usr/sbin/sshd ] || die "--hosts needs /usr/sbin/sshd (macOS)"
  [ -f "$APP/.dtrb-built" ] || die "no app build; run: dtrb.sh build"
  [ -f "$APP/dist-electron/electron/main.js" ] || die "app build incomplete; run: dtrb.sh build"
  local name="${1:-$(date +%Y%m%d-%H%M%S)}"
  local run="$E2E_ROOT/runs/$name"
  [ -e "$run" ] && die "run $name exists"
  if [ -L "$E2E_ROOT/current" ] && [ -f "$E2E_ROOT/current/holder.pid" ] \
     && kill -0 "$(cat "$E2E_ROOT/current/holder.pid")" 2>/dev/null; then
    die "a run is already up ($(current_run)); dtrb.sh down first"
  fi
  local port i
  # The Shell's MCP port, and each host's at +51, +52, ...
  for port in "$MCP_PORT" $(for i in $(seq 1 "$nhosts"); do echo $((MCP_PORT + 50 + i)); done); do
    if lsof -nP -iTCP:"$port" -sTCP:LISTEN >/dev/null 2>&1; then
      die "port $port is taken; set DTRB_MCP_PORT (or DTRB_RUNNER/DTRB_N)"
    fi
  done
  curl -fsS -o /dev/null --max-time 2 "http://127.0.0.1:$RUNBOOKS_PORT/readyz" 2>/dev/null \
    || say "warning: no runbook server on $RUNBOOKS_PORT yet (dtrb.sh runbooks serve)"
  mkdir -p "$run"
  # The project lives OUTSIDE the run dir, under a path that says nothing about
  # testing: agents read their cwd, and a pilot run under .../runbook-e2e/runs/pilot4
  # searched for "e2e test runbook pilot trial".
  local proj; proj="$PROJECTS_ROOT/$(openssl rand -hex 2)/tally"
  echo "$proj" >"$run/project-path"
  seed_project "$proj"
  local hproj hprojs=""
  : >"$run/host-projects"
  for i in $(seq 1 "$nhosts"); do
    hproj="$PROJECTS_ROOT/$(openssl rand -hex 2)/tally"
    if [ "${DTRB_HOST_WITHOUT_PROJECT:-}" = "$i" ]; then
      hproj="$(seed_host_project "$proj" "$hproj" bare)"
    else
      seed_host_project "$proj" "$hproj"
    fi
    echo "$hproj" >>"$run/host-projects"
    hprojs="${hprojs:+$hprojs:}$hproj"
  done
  mkdir -p "$APP/e2e/dtrb"
  cp "$HERE/harness/hold.spec.ts" "$APP/e2e/dtrb/hold.spec.ts"
  cp "$HERE/harness/hosts.ts" "$APP/e2e/dtrb/hosts.ts"
  cp "$HERE/harness/playwright.dtrb.config.ts" "$APP/playwright.dtrb.config.ts"
  ln -sfn "$run" "$E2E_ROOT/current"
  say "starting isolated Daintree (user data $run/userdata, MCP port $MCP_PORT)"
  # env -u: a harness started from inside Claude Code must not hand its markers on.
  (cd "$APP" && env -u CLAUDECODE -u CLAUDE_CODE_ENTRYPOINT -u CLAUDE_CODE_SSE_PORT \
    DTRB_RUN_DIR="$run" DTRB_PROJECT="$proj" DTRB_MCP_PORT="$MCP_PORT" \
    DTRB_TIER="${DTRB_TIER:-full}" DTRB_SKIP_PERMISSIONS="${DTRB_SKIP_PERMISSIONS:-1}" \
    DTRB_SKIP_CONFIRMATIONS="${DTRB_SKIP_CONFIRMATIONS:-1}" DTRB_APPROVE_DIALOGS="${DTRB_APPROVE_DIALOGS:-0}" \
    DTRB_HOLD_HOURS="${DTRB_HOLD_HOURS:-8}" DTRB_QUIET_MS="${DTRB_QUIET_MS:-45000}" \
    DTRB_HOST_PROJECTS="$hprojs" DTRB_HELP_TIER="${DTRB_HELP_TIER:-core}" DTRB_RUNBOOKS_URL="$RUNBOOKS_URL" \
    nohup npx playwright test --config playwright.dtrb.config.ts >"$run/playwright.log" 2>&1 & echo $! >"$run/holder.pid")
  for i in $(seq 1 $((120 + 120 * nhosts))); do
    [ -f "$run/state.json" ] && break
    kill -0 "$(cat "$run/holder.pid")" 2>/dev/null || { tail -30 "$run/playwright.log" >&2; die "holder exited"; }
    sleep 1
  done
  [ -f "$run/state.json" ] || die "app never became ready; see $run/playwright.log"
  cat "$run/state.json"; echo
  say "up: $run  (screenshot $run/ready.png)"
}

cmd_trial() {
  local agent=claude model=haiku timeout=20 wait=1 id="" flags=() prompt bg=0 adopt="" pname="" assistant=0
  while [ $# -gt 0 ]; do
    case "$1" in
      --agent) agent="$2"; shift 2 ;;
      --model) model="$2"; shift 2 ;;
      --timeout) timeout="$2"; shift 2 ;;
      --flag) flags+=("$2"); shift 2 ;;
      --no-wait) wait=0; shift ;;
      --id) id="$2"; shift 2 ;;
      --background) bg=1; wait=0; shift ;;
      --adopt) adopt="$2"; shift 2 ;;
      --name) pname="$2"; shift 2 ;;
      --assistant) assistant=1; shift ;;
      --global) assistant=global; shift ;;
      -f) prompt="$(cat "$2")"; shift 2 ;;
      *) prompt="$1"; shift ;;
    esac
  done
  [ -n "${prompt:-}" ] || die "no prompt"
  local run; run="$(current_run)"
  [ -f "$run/state.json" ] || die "run not ready"
  id="${id:-t-$(date +%H%M%S)-$(openssl rand -hex 2)}"
  if [ -e "$run/inbox/$id.json" ] || [ -e "$run/inbox/.taken/$id.json" ] || [ -e "$run/trials/$id" ]; then
    die "trial id $id is already used in this run; pick another --id"
  fi
  # Codex has no project-scope MCP file; hand it the runbook server per launch.
  if [ "$agent" = codex ]; then
    flags+=("-c" "mcp_servers.daintree-runbooks.url=$RUNBOOKS_URL")
  fi
  "$PY" - "$run/inbox/$id.json" "$agent" "$model" "$timeout" "$prompt" "$bg" "$adopt" "$pname" "$assistant" "${flags[@]+"${flags[@]}"}" <<'PY'
import json, sys
out, agent, model, timeout, prompt, bg, adopt, pname, assistant, *flags = sys.argv[1:]
req = {"agentId": agent, "prompt": prompt, "timeoutMin": float(timeout), "flags": flags}
if assistant == "1": req["assistant"] = True
if assistant == "global": req["global"] = True
if bg == "1": req["background"] = True
if adopt: req["adopt"] = adopt.split(",")
if pname: req["name"] = pname
if model and model != "default":
    req["model"] = model
json.dump(req, open(out + ".tmp", "w"), indent=2)
PY
  mv "$run/inbox/$id.json.tmp" "$run/inbox/$id.json"
  say "trial $id queued ($agent${model:+ / $model})"
  if [ "$bg" = 1 ]; then
    until [ -f "$run/trials/$id/done.json" ]; do
      kill -0 "$(cat "$run/holder.pid")" 2>/dev/null || die "holder exited; see $run/playwright.log"
      sleep 2
    done
    local tid
    tid="$("$PY" -c "import json,sys;print(json.load(open(sys.argv[1])).get('trialTerminalId') or '')" "$run/trials/$id/done.json")"
    [ -n "$tid" ] || die "background launch failed; see $run/trials/$id/events.jsonl"
    echo "$tid"
    return
  fi
  if [ "$wait" = 1 ]; then cmd_wait "$id"; else echo "$id"; fi
}

cmd_wait() {
  local id="$1" run; run="$(current_run)"
  until [ -f "$run/trials/$id/done.json" ]; do
    kill -0 "$(cat "$run/holder.pid")" 2>/dev/null || die "holder exited; see $run/playwright.log"
    sleep 5
  done
  cmd_summary "$id"
}

cmd_summary() {
  local id="$1"; shift; local run; run="$(current_run)"
  "$PY" "$HERE/summarise.py" "$run/trials/$id" "$@"
}

cmd_send() {
  local text="${1:?text}" target="${2:-}" run; run="$(current_run)"
  local tmp; tmp="$(mktemp "$run/.send.XXXXXX")"
  "$PY" -c 'import json,sys
req = {"text": sys.argv[2]}
if sys.argv[3]: req["terminalId"] = sys.argv[3]
json.dump(req, open(sys.argv[1], "w"))' "$tmp" "$text" "$target"
  drop_request "$run" send.json "$tmp"
  say "follow-up typed into ${target:-the trial pane}"
}

cmd_adopt() {
  local ids="${1:?terminal id(s)}" pane="${2:?orchestrator pane id}" run; run="$(current_run)"
  local tmp; tmp="$(mktemp "$run/.adopt.XXXXXX")"
  "$PY" -c 'import json,sys
json.dump({"terminalIds": sys.argv[2].split(","), "orchestratorPaneId": sys.argv[3]}, open(sys.argv[1], "w"))' \
    "$tmp" "$ids" "$pane"
  drop_request "$run" adopt.json "$tmp"
  say "hand-over of $ids to $pane requested; check the trial's events.jsonl for the result"
}

cmd_hold() {
  local run; run="$(current_run)"
  case "${1:-on}" in
    on) touch "$run/hold"; say "holding: the running trial will not settle until 'hold off'" ;;
    off) rm -f "$run/hold"; say "released" ;;
    *) die "hold on|off" ;;
  esac
}

cmd_reset() {
  local run proj real root i; run="$(current_run)"
  proj="$(cat "$run/project-path" 2>/dev/null || true)"
  # Destructive git below: only ever inside this run's own scratch project, checked on
  # the resolved path (no ../ or symlink escapes) with a real .git directory (not a
  # gitfile pointing elsewhere).
  real="$(cd "$proj" 2>/dev/null && pwd -P)" || die "no scratch project at '$proj'"
  root="$(cd "$PROJECTS_ROOT" 2>/dev/null && pwd -P)" || die "no projects root $PROJECTS_ROOT"
  case "$real" in "$root"/*/tally) ;; *) die "refusing to reset '$real': not a scratch project" ;; esac
  [ -d "$real/.git" ] && [ ! -L "$real/.git" ] || die "no scratch repo at $real"
  proj="$real"
  touch "$run/close-all"
  # The holder removes close-all only after every agent pane is closed.
  for i in $(seq 1 60); do [ -e "$run/close-all" ] || break; sleep 1; done
  [ -e "$run/close-all" ] && die "the holder did not confirm the panes closed; nothing was reset"
  (
    cd "$proj" || exit 1
    git worktree list --porcelain | sed -n 's/^worktree //p' | tail -n +2 \
      | while IFS= read -r wt; do git worktree remove --force --force "$wt" || echo "dtrb: could not remove worktree $wt" >&2; done
    git worktree prune || exit 1
    [ "$(git worktree list --porcelain | grep -c '^worktree ')" = 1 ] || exit 1
    git checkout --quiet -f main || exit 1
    git reset --quiet --hard origin/main || exit 1
    git clean -fdq || exit 1
    git for-each-ref --format='%(refname:short)' refs/heads | { grep -vx main || true; } \
      | xargs -r git branch -D -q || exit 1
  ) || die "resetting $proj failed"
  say "panes closed; scratch repo back at its seed"
}

json_field() { "$PY" -c 'import json,sys;print(json.load(open(sys.argv[1])).get(sys.argv[2], ""))' "$1" "$2"; }

# One field of the run's state.json: the Shell's, or a host's by name, index or hostId.
state_field() {
  "$PY" - "$1/state.json" "$2" "${3:-}" <<'PY'
import json, sys
state, field, host = json.load(open(sys.argv[1])), sys.argv[2], sys.argv[3]
if not host:
    print(state[field]); sys.exit(0)
hosts = state.get("hosts") or []
for i, h in enumerate(hosts, 1):
    if host in (h["name"], str(i), h.get("hostId")):
        print(h[field]); sys.exit(0)
sys.exit(f"no host {host!r} in this run (hosts: {[h['name'] for h in hosts]})")
PY
}

cmd_mcp() {
  local run as="" on="" i; run="$(current_run)"
  while [ $# -gt 0 ]; do
    case "$1" in
      --as) as="$2"; shift 2 ;;
      --on) on="$2"; shift 2 ;;
      *) break ;;
    esac
  done
  if [ -n "$on" ]; then
    "$PY" "$HERE/mcp_call.py" "$(state_field "$run" mcpUrl "$on")" "$(state_field "$run" mcpApiKeyFile "$on")" "$@"
    return
  fi
  case "$as" in
    "") "$PY" "$HERE/mcp_call.py" "$(state_field "$run" mcpUrl)" "$run/mcp-api-key" "$@" ;;
    help)
      # The Shell's own assistant's bearer, live while its PTY runs. None yet (or the
      # assistant was closed): ask the holder to start one.
      if [ ! -f "$run/help-session.json" ] || [ -n "${DTRB_RESTART_ASSISTANT:-}" ]; then
        touch "$run/provision-help"
        for i in $(seq 1 90); do [ -e "$run/provision-help" ] || break; sleep 1; done
        [ -e "$run/provision-help" ] && die "the holder did not start the assistant"
        [ -f "$run/help-session.json" ] || die "no assistant started; see $run/holder.log"
      fi
      "$PY" "$HERE/mcp_call.py" "$(state_field "$run" mcpUrl)" "$run/help-session.json" "$@" ;;
    *) die "--as help is the only other caller" ;;
  esac
}

cmd_snap() {
  local run i; run="$(current_run)"
  touch "$run/snap"
  for i in $(seq 1 60); do [ -e "$run/snap" ] || break; sleep 1; done
  [ -e "$run/snap" ] && die "the holder did not take the snap"
  ls -d "$run"/snaps/* | tail -1
}

cmd_restart() {
  local run i; run="$(current_run)"
  touch "$run/restart-app"
  for i in $(seq 1 300); do [ -e "$run/restart-app" ] || break; sleep 1; done
  [ -e "$run/restart-app" ] && die "the holder did not restart the Shell"
  grep "restart" "$run/holder.log" | tail -2
}

cmd_hosts() {
  local run; run="$(current_run)"
  "$PY" -c 'import json,sys
for h in json.load(open(sys.argv[1])).get("hosts") or []:
    print(json.dumps(h))' "$run/state.json"
}



cmd_project() {
  local run; run="$(current_run)"
  cat "$run/project-path"
}

cmd_status() {
  local run; run="$(current_run)"
  echo "run: $run"
  cat "$run/state.json" 2>/dev/null; echo
  kill -0 "$(cat "$run/holder.pid")" 2>/dev/null && echo "holder: running" || echo "holder: stopped"
  ls "$run/trials" 2>/dev/null | sed 's/^/trial: /'
  cmd_runbooks status || true
}

cmd_down() {
  local run; run="$(current_run)"
  touch "$run/stop"
  local i pid; pid="$(cat "$run/holder.pid" 2>/dev/null || true)"
  # Never signal 0 or 1 (0 is this shell's own process group).
  case "$pid" in ''|*[!0-9]*) pid="" ;; esac
  if [ -n "$pid" ] && [ "$pid" -gt 1 ]; then
    for i in $(seq 1 60); do kill -0 "$pid" 2>/dev/null || break; sleep 1; done
    kill "$pid" 2>/dev/null || true
  fi
  # Belt and braces: only processes carrying THIS run's user-data dirs, and its sshds.
  pkill -f "user-data-dir=$run/userdata" 2>/dev/null || true
  local hj ud sp
  for hj in "$run"/host*/host.json; do
    [ -f "$hj" ] || continue
    ud="$(json_field "$hj" userData)"; sp="$(json_field "$hj" sshdPid)"
    [ -n "$ud" ] && pkill -f "user-data-dir=$ud" 2>/dev/null || true
    case "$sp" in ''|*[!0-9]*) ;; *)
      if [ "$sp" -gt 1 ] && ps -p "$sp" -o command= 2>/dev/null | grep -q '/usr/sbin/sshd'; then kill "$sp" 2>/dev/null || true; fi ;;
    esac
  done
  say "down: $run (kept for inspection; dtrb.sh purge removes it)"
}

cmd_purge() {
  # Removes stopped runs (all, or the one named) with their scratch projects,
  # including any worktrees the agents created beside them. Transcripts stay in
  # ~/.claude/projects and ~/.codex/sessions.
  local run proj
  for run in "$E2E_ROOT"/runs/${1:-*}; do
    [ -d "$run" ] || continue
    if [ -f "$run/holder.pid" ] && kill -0 "$(cat "$run/holder.pid")" 2>/dev/null; then
      say "skipping $run: still up"; continue; fi
    proj="$(cat "$run/project-path" 2>/dev/null || true)"
    [ -n "$proj" ] && rm -rf "$(dirname "$proj")"
    # Each host's project directory, and its root (sshd keys, HOME, userData) under /tmp.
    local hp hdir hj hroot
    if [ -f "$run/host-projects" ]; then
      while IFS= read -r hp; do
        hdir="${hp%/tally}"
        case "$hdir" in "$PROJECTS_ROOT"/?*) rm -rf "$hdir" ;; esac
      done <"$run/host-projects"
    fi
    for hj in "$run"/host*/host.json; do
      [ -f "$hj" ] || continue
      hroot="$(json_field "$hj" root)"
      case "$hroot" in /private/tmp/dtrb-h?*|/tmp/dtrb-h?*) rm -rf "$hroot" ;; esac
    done
    rm -rf "$run"; say "purged $run${proj:+ and $(dirname "$proj")}"
  done
  [ -L "$E2E_ROOT/current" ] && [ ! -e "$E2E_ROOT/current" ] && rm -f "$E2E_ROOT/current"
  return 0
}

case "${1:-}" in
  build) shift; cmd_build "$@" ;;
  runbooks) shift; cmd_runbooks "$@" ;;
  up) shift; cmd_up "$@" ;;
  hosts) shift; cmd_hosts ;;
  snap) shift; cmd_snap ;;
  restart) shift; cmd_restart ;;
  trial) shift; cmd_trial "$@" ;;
  wait) shift; cmd_wait "$@" ;;
  summary) shift; cmd_summary "$@" ;;
  send) shift; cmd_send "$@" ;;
  adopt) shift; cmd_adopt "$@" ;;
  hold) shift; cmd_hold "$@" ;;
  reset) shift; cmd_reset ;;
  mcp) shift; cmd_mcp "$@" ;;
  project) shift; cmd_project ;;
  status) shift; cmd_status ;;
  down) shift; cmd_down ;;
  purge) shift; cmd_purge "$@" ;;
  *) sed -n '2,40p' "$0"; exit 2 ;;
esac
