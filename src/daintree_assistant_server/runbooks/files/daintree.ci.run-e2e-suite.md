---
id: daintree.ci.run-e2e-suite
title: Run end-to-end tests against a worktree's own app
summary: Run a browser or API end-to-end suite for one worktree, proving the server it hits is that checkout's and not another's, collect failures and artifacts, and stop only what you started.
whenToUse: 'Use when the user wants END-TO-END, BROWSER or INTEGRATION tests run that need the app (and maybe its database) up: "run the Playwright tests in this worktree", "run e2e against my branch", "run the Cypress suite headless", "run the integration tests that need the server running". The deliverable is the e2e result for THAT worktree''s code: the base URL it actually hit, proof the server was this checkout''s, pass and fail counts, each failure with its artifact paths (traces, screenshots, videos), and what was started and stopped. Do not use for unit, lint, typecheck or build runs that need no running app (use daintree.ci.run-local-checks); to make a failing e2e test pass (use daintree.ci.diagnose-and-fix); to give worktrees their own persistent ports or database (use daintree.worktree.provision-local-environment); to debug a page interactively in a dev preview (use daintree.runtime.debug-web-app); or only to start a dev server and leave it running (use daintree.terminals.run-visible-command).'
risk: terminal
requiredTools:
  - worktree.list
  - worktree.waitUntilReady
  - agent.launch
optionalTools:
  - terminal.getStatus
  - terminal.sendCommandOwned
  - terminal.list
---
The trap is a green run against the wrong code: with several worktrees, `localhost:3000` is often another checkout's server. A visible agent in the worktree starts what it needs and runs the suite; you preflight and check its evidence. Only when the user asks you to run it, follow the brief in your own shell.

Procedure:
1. Worktree id and path from `worktree.list`. `worktree.waitUntilReady` with `timeoutMs:0`: `pending`/`running`: call again; `failed`, `timed-out`, `needs-approval`: stop and report; `unknown`: check dependencies are installed, readiness unverified. Record `HEAD` and, if dirty, a hash of `git diff HEAD` plus untracked files. An agent `working` there (`terminal.getStatus`): ask first; the code is moving.
2. Read the e2e config in that path (`playwright.config.*`, `cypress.config.*`, the manifest's e2e script): base URL, port, whether the runner starts the server itself (Playwright `webServer`), and `reuseExistingServer` (often `!process.env.CI`: it silently tests whatever already listens on that port).
3. Port preflight: `lsof -nP -iTCP:<port> -sTCP:LISTEN`. Free: go on. Held: its directory, `lsof -a -p <pid> -d cwd`. Another checkout's or unknown: never test against it or kill it; if the config reads the port from the environment, pick a free one yourself and brief that URL, else stop and report blocked. This worktree's own: it may serve an older build, so prefer a fresh port or report the server's revision as unproven.
4. Launch one agent: the one the user named (built-in `agentId`), else `claude`, and say which. Pass `name:"E2E: <branch>"`, the `worktreeId`, `notify:true, handback:true`; keep the `terminalId`. The prompt carries the path, `HEAD`, the port finding, a startup deadline, and these lines as written:
   - "Base URL <url on the free port>: pin it (`BASE_URL`, `--base-url`, `CYPRESS_BASE_URL`). Let the runner start its own server with reuse off (`CI=1` or the config's switch), or start this worktree's dev or start command yourself in the background on that port once `lsof` shows it free (held: stop and report; never kill it). Opened is not up: prove it with `curl -sI <url>` before the deadline, else stop and report. Record the listener's pid (`lsof -tiTCP:<port> -sTCP:LISTEN`) and its directory (`lsof -a -p <pid> -d cwd`): it must be this worktree."
   - "Never test against, stop or kill a server you did not start. Browsers (`npx playwright install`), migrations or seed data only as the project's docs say, and only against this worktree's own database."
   - "Run headless; one spec if that was asked. Edit no code or tests; commit and push nothing."
   - "On success, failure or timeout, stop every process you started (only a pid whose directory is this worktree) and confirm the port is free."
   - "Reply with the exact command, the base URL, who started the server (pid), passed, failed, skipped and flaky counts, per failure the spec, test name, first error line and artifact paths (`test-results/`, `playwright-report/`, `cypress/screenshots/`), and anything incomplete."
5. End your turn. On the notice, your report waits until you have run the checks below.

```json tool=agent.launch
{"agentId":"claude","name":"E2E: feature/login","worktreeId":"<worktree id>","prompt":"<path, HEAD, base URL, server rules, scope, cleanup, reply>","notify":true,"handback":true}
```

On notice or resume:
- A question inside the brief: answer once with `terminal.sendCommandOwned`, `notify:true, handback:true`. Migrating a shared database or killing a server goes to the user.
- The notice's screen is the agent's claim, never your evidence: report nothing until you have, yourself:
  1. read the reporter's output (results JSON, `playwright-report/`, `test-results/`) or the run's own output, confirming counts and failure names (traces and videos can hold credentials: give paths only);
  2. checked the agent's pid and directory evidence: the listener was this worktree's, started for this run; else the result is unproven for `HEAD`;
  3. `lsof` on the port: nothing the agent started still listens, and any listener that was there before still is (gone: the agent may have killed it; report it);
  4. `git status`: no code edits (report artifacts are fine); `git rev-parse HEAD`: changed means the result is stale.

Stop and recovery: one corrective round. Past the deadline, `terminal.getStatus` and act on its screen. Reconcile with `terminal.list` before any second launch. Never stop a listener you or the agent did not start.

Report back: worktree, branch and revision; the base URL and the proof the server was this checkout's (or that it is unproven); the suite command; the counts you confirmed; each failure with artifact paths; what was started and stopped; the agent's terminal id; anything incomplete; for red, the offer of daintree.ci.diagnose-and-fix.
