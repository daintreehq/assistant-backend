---
id: daintree.ci.run-local-checks
title: Run the project's checks locally
summary: Find the project's real test, lint, typecheck and build commands, run them in the exact worktree (or across several agents' worktrees), and report a pass, fail or incomplete result per worktree bound to the revision tested, fixing nothing.
whenToUse: 'Use when the user wants the project''s checks RUN and the results reported, with nothing known to be broken yet: "run the tests", "run lint and typecheck in the auth worktree", "does it still build?", "run the checks before I push", "run the test suite in every agent''s worktree and tell me which are green", "what checks does this project even have?". The deliverable is a result per worktree and check (revision tested, command, passed / failed with the failing lines / incomplete), not a fix. Do not use when a check is already failing and the user wants it working (use daintree.ci.diagnose-and-fix); to decide whether one agent''s "done, tests pass" is true (use daintree.orchestration.verify-agent-work); to ready a branch and open a PR (use daintree.workflow.prep-branch-for-review); for end-to-end or browser tests that need the app running (use daintree.ci.run-e2e-suite); to run a command in a terminal the user can watch (use daintree.terminals.run-visible-command); to read a PR''s CI on the forge (use daintree.workflow.follow-up-on-issue); to run them at a later time (use daintree.orchestration.watch-outcome); for a ship or no-ship verdict on a release candidate (use daintree.release.assess-readiness); or to try the built installer, image or packed package in a clean setting (use daintree.release.test-distributable).'
risk: project
requiredTools:
  - worktree.list
  - project.detectRunners
  - project.runCheck
optionalTools:
  - actions.getContext
  - worktree.waitUntilReady
  - terminal.list
  - terminal.getStatus
---
"Run the tests" authorizes running, not fixing, installing, committing or pushing. `project.runCheck` is your own Daintree operation, so you run the checks yourself. A red check is a result to report; fixing it is a new request it goes to an agent (daintree.ci.diagnose-and-fix).

On a host: shell steps via its command runner and checks via its `project.runCheck`, else delegate.

1. Targets from `worktree.list` (id, path, branch): the one named, the one you are in, or for "every agent's worktree" the worktrees of `terminal.list` rows that have an `agentId`. Before any check, record for each `git -C <path> rev-parse HEAD` (every result is bound to it) and, if dirty, a hash of `git diff HEAD` plus untracked files (`git ls-files -o --exclude-standard`).
2. Readiness per worktree, `worktree.waitUntilReady` with `timeoutMs:0`: `pending` or `running` (a tree just created): wait, calling again with a timeout; `failed`, `timed-out` or `needs-approval`: skip and report it; `unknown` (created before a restart or by another host): check its dependencies are installed and mark readiness unverified. An agent `working` there (`terminal.getStatus`): the tree is moving, so ask, or test it and mark the result a moving snapshot.
3. Choose the checks. What the user named; for a bare "run the checks", what CI or the project's docs require (`.github/workflows/`, CONTRIBUTING, CLAUDE.md, AGENTS.md). Nothing defined: list the candidates and ask before running an unfamiliar script. `project.detectRunners` (the work's `projectId`) reads the project root only, lists publish, deploy and server scripts too, and synthesizes some. Classify each runner by what it really executes (the script body in the manifest):
   - a check (test, lint, typecheck, build): eligible;
   - a server or watcher (`dev`, `start`, `serve`, `--watch`): never here, it blocks until the timeout;
   - a side effect (publish, deploy, release, db reset or seed, a `prepare` that writes): never, unless asked for by name.
   "What checks are there?" stops here with that list. Otherwise every runner you leave out goes in the report with its reason.
4. Per worktree, confirm the script exists with the same body in that worktree's own manifest: another branch can lack it or run something else under the same name.
5. Order cheap to expensive: lint and typecheck, unit tests, build. Run everything chosen; stop a worktree at its first red only if the user said fail fast.
6. `project.runCheck` with the work's `projectId`, the `runnerId` and ALWAYS `cwd` set to the worktree path: it defaults to the project root and would silently test the main checkout. Size `timeoutMs` to the suite (default 10 minutes, at most 3600000). It runs non-interactively with `CI=1`, one check per directory at a time. Compare the returned `command` with what you expected. A command no runner covers: a shell step, `cd <path> && <command>`, reading its exit code.

```json tool=project.runCheck
{"projectId":"<project id>","runnerId":"<runner id>","cwd":"<worktree path>","timeoutMs":900000}
```

7. Several worktrees: suites that share a database, port, cache or lock file run one worktree at a time, or they clobber each other (separating them is daintree.worktree.provision-local-environment). Fully isolated suites may run in parallel, one directory each.
8. Read the result, not the call. `passed:false` is a red check, not an error. `timedOut`, `aborted` or `exitCode:null` is INCOMPLETE, neither green nor red. `outputTruncated` keeps only the tail: quote what is there and say the start was cut. "command not found" or missing modules means dependencies are not installed: report it; installing is a new request.
9. Re-read `HEAD` and the hash afterwards. Changed: the results describe a tree that no longer exists; say so.

Report back: per worktree, its id, branch and revision (with the dirty hash, and whether untracked files were in it), readiness where unverified, then each command with passed, failed (key failing lines quoted) or incomplete and why; worktrees and runners skipped or deliberately not run, with reasons; for anything red, the offer of daintree.ci.diagnose-and-fix to put an agent on it.
