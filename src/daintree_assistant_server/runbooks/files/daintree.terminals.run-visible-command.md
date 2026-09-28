---
id: daintree.terminals.run-visible-command
title: Run a shell command in a visible terminal
summary: Run a plain shell command, dev server, watcher or log tail in a Daintree terminal the user can watch, filed under the right worktree, then read its real result — an exit code for a finite command, ready evidence for a long-lived one — and stop only what you started.
whenToUse: 'Use when the user wants a plain SHELL command run where they can watch it, not an AI agent — "run the build in a terminal so I can see it", "open a terminal in the auth worktree and run npm install", "spin up the dev server for the api worktree", "start the test watcher in a side terminal", "tail the server log in a panel", "is the server you started up, what port?", "stop the server you started". The deliverable is the command running in a visible panel in the named worktree, then its exit code and output tail, or for a server or watcher its ready line or URL; also reading or stopping one you started. Do not use to start an AI agent or give one work (use daintree.edits.spawn-visible-agent), for a saved multi-terminal setup (use daintree.recipe.run-or-create), for a Dev Preview browser panel or its console (use daintree.runtime.debug-web-app), for a pass/fail verdict from the project''s tests, lint or typecheck with no panel wanted (use daintree.ci.run-local-checks), to fix a failing build (use daintree.ci.diagnose-and-fix), to free a port something else holds (use daintree.runtime.free-busy-port), to migrate or reseed a worktree''s database (use daintree.runtime.migrate-worktree-database), or to run it later or be woken when it ends (use daintree.orchestration.watch-outcome).'
risk: terminal
requiredTools:
  - worktree.list
  - terminal.new
  - terminal.sendCommandOwned
  - terminal.getStatus
optionalTools:
  - terminal.list
  - terminal.moveToWorktree
  - terminal.rename
  - terminal.getOutput
  - terminal.closeOwned
  - project.detectRunners
---
A shell sends no Daintree notice: `notify` and `handback` are refused on a shell send, `waitForReply` never settles, an idle-notice request is rejected, and `terminal.waitUntilIdle` returns at once. You read a shell; nothing wakes you.

1. Resolve destination and command. Destination: the worktree the user named (`worktree.list`: keep id and path), else the checkout you work in (match your cwd to a listed path); never whatever is active. Command: the user's words, else a `project.detectRunners` runner whose `command` you have read (it lists publish and deploy scripts too: run none unasked). Never `project.runCheck` here: it is invisible, and blocks a server until timeout.
2. Reuse before opening. Reuse only a shell you opened in this destination (or the user pointed at there), and only when `terminal.getStatus` with `includeOutput` shows it idle at its prompt. Busy, or its directory unknown: open a new one; `terminal.moveToWorktree` never changes a shell's directory. A server to start may already run in that worktree: check `terminal.list` (`worktreeId`) before starting a second on the same port.
3. Open with `cwd` and no `command` (the send in step 4 gets a receipt). `cwd` asks the user to confirm; never dodge that by `cd`-ing a shell opened elsewhere. Keep the returned `terminalId`.

```json tool=terminal.new
{"cwd":"<worktree path>","focusPolicy":"preserve"}
```

   The panel is filed under the user's ACTIVE worktree whatever `cwd` says: if that differs from the destination, `terminal.moveToWorktree` (`terminalId`, `worktreeId`). Never `worktree.setActive` to steer it: that moves the user's view. Several shells side by side: `terminal.rename` each. Identical `terminal.new` calls within two minutes return the same terminal: pass each further one a distinct `requestKey`; check the ids differ.
4. Set a deadline sized to the work (seconds for an install, minutes for a build or a server's start). Note the last output line, then send once with `terminal.sendCommandOwned`, no `notify`, `handback` or `waitForReply`. A finite command gets a sentinel the echoed line cannot fake, with a fresh nonce: `<cmd>; printf '__DT_%s rc=%d\n' <nonce> $?` (bash, zsh; fish uses `$status`). A server, watcher or tail goes as-is.

```json tool=terminal.sendCommandOwned
{"terminalId":"<terminal id>","command":"npm run build; printf '__DT_%s rc=%d\\n' k3f9 $?"}
```

5. Finite command: read `terminal.getStatus` (`terminalIds`, `includeOutput:{"lines":50}`) paced to the work until `__DT_<nonce> rc=` appears, an error, the panel is gone, or the deadline. That rc is the result; the lines above it are the evidence (more: `terminal.getOutput`, `maxLines` up to 1000). The echoed command, `lastCheckResult` or a quiet screen are not results. Past the deadline: report it still running, with the tail. Delivery in doubt: `terminal.getStatus` with the `submissionToken`.
6. Long-lived process: sent is not up. Read, within the deadline, for its ready line (a URL, "listening on", "watching for changes") or an error. For a server, one bounded probe: `curl -sS -o /dev/null -w '%{http_code}' --max-time 5 <url>` and the status you expect; then prove it serves THIS worktree: `lsof -nP -iTCP:<port> -sTCP:LISTEN` for the PID, then `lsof -a -p <pid> -d cwd`. Another checkout on that port is a clash (daintree.runtime.free-busy-port); never invent a `PORT=` the app ignores. A log tail prints no ready line: check it runs and tails the file asked for.
7. Longer than you can pace this turn: say no notice will come; the user watches the panel, or you read it when they next ask. Never have an agent re-run what already runs (a replayed write); daintree.orchestration.watch-outcome is for a run not yet started. Never end the turn "waiting" on a shell.
8. Stop only what you started, only when asked. No Ctrl-C exists (`terminal.sendKeysOwned` has none; `terminal.interruptOwned` is agent-only): `terminal.closeOwned` trashes the panel and kills its process about 20 s later. `closedIds` proves the panel closed (empty: failed, re-list), not the process gone: recheck the port or PID after that window. A user's or other agent's server is theirs: ask. Never `pkill`, `killall`, or kill a PID you did not start.

Recovery: after an ambiguous result re-read the same terminal for the sentinel, never re-send. Back at a prompt without it: report "ended without the marker" with the tail. A lost `terminal.new` reply: `terminal.list` with `owned:true` before opening another.

Report back: terminal id and title, worktree and cwd, the exact command and the deadline you set; for a finite command the rc and a quoted tail; for a server or watcher the ready evidence (line, probe status, port, serving PID's cwd), for a tail the process and file; what still runs and that nothing will notify on it; anything closed, by `closedIds` and the recheck.
