---
id: daintree.ci.run-e2e-suite
title: Run end-to-end tests against a worktree's own app
summary: Run a browser or API end-to-end suite for one worktree, here or on a Remote Host, proving the server it hits is that checkout's at that revision, collect failures and artifact references, and stop only what you started.
whenToUse: 'Use when the user wants END-TO-END, BROWSER or INTEGRATION tests run that need the app (and maybe its database) up, and the green result tied to the right server: "run the Playwright tests in this worktree", "run e2e against my branch", "run the Cypress suite headless", "run the integration tests that need the server running", "run the browser tests against the remote worktree''s own app", "check the host-side integration tests did not hit another checkout''s server", "verify the e2e server provenance and keep readable trace references". The deliverable is the e2e result for THAT worktree''s code: where the runner, browser and server ran, the base URL used (a host''s localhost is not a Shell''s forwarded URL), the server''s directory and build revision against the exact HEAD, pass and fail counts, each failure with report and trace references naming host, worktree and revision, and what was started and stopped; a wrong or stale server is rejected, never green. Do not use for unit, lint, typecheck or build runs that need no running app (use daintree.ci.run-local-checks); to make a failing e2e test pass (use daintree.ci.diagnose-and-fix); to give worktrees their own persistent ports or database (use daintree.worktree.provision-local-environment); to debug a page interactively in a dev preview (use daintree.runtime.debug-web-app); only to start a dev server and leave it running (use daintree.terminals.run-visible-command); one commit under several environments across hosts (use daintree.hosts.run-validation-matrix); or one suite split between hosts (use daintree.hosts.shard-test-suite).'
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
The trap is a green run against the wrong code: `localhost:3000` is often another checkout's server, or this one's stale build. A visible agent in the worktree starts what it needs and runs the suite; you preflight and check its evidence. A hub cannot inspect a host: its lane (via daintree.hosts.delegate-to-host) runs this and returns the evidence.

1. Worktree id and path from `worktree.list`; `worktree.waitUntilReady` until `ready` (`timedOut:true`: again, for a few minutes; `failed`, `timed-out`, `needs-approval`: stop and report; `unknown`: dependencies unverified). Record `HEAD` and, if dirty, a hash of `git diff HEAD` plus untracked source files (not build or report output). An agent `working` there (`terminal.getStatus`): ask first.
2. Read the e2e config there (`playwright.config.*`, `cypress.config.*`): base URL, port, whether the runner starts the server, `reuseExistingServer` (it silently tests whatever listens), how the app shows its build (version route, stamp, header). Note where the runner, browser and server each run: a host's `localhost` is not the Shell's, and a Shell forward may use another local port. Server evidence comes from the server's machine; through a forward, the browser's machine checks the marker through that URL too.
3. Port preflight: `lsof -nP -iTCP:<port> -sTCP:LISTEN`. Held: its directory, `lsof -a -p <pid> -d cwd`. Another checkout's or unknown: leave it alone; if the config reads the port from the environment, pick a free one and brief that URL, else stop, blocked. This worktree's own may serve an older build: use a fresh port or leave it unproven.
4. Launch one agent: the user's named one, else `claude`. Pass `name:"E2E: <branch>"`, the `worktreeId`, `notify:true, handback:true`. The prompt carries the path, `HEAD`, the port finding, a startup deadline, the repository's test rules, and these lines as written:
   - "Base URL <url on the free port>: pin it (`BASE_URL`, `--base-url`, `CYPRESS_BASE_URL`). Let the runner start its server with reuse off (`CI=1` or the config's switch), or start this worktree's dev or start command in the background on that port once `lsof` shows it free (held: stop and report). Prove it is up (`curl -sI <url>`) before the deadline, else stop and report."
   - "Once the server is up (before the first test if you start it, during the run if the runner does), save the proof outside the checkout: the listener's pid (`lsof -tiTCP:<port> -sTCP:LISTEN`), directory (this worktree), start time (`ps -o lstart= -p <pid>`) and revision: a build marker equal to `HEAD` on a clean tree, else a build you make now from this checkout with the listener started after it (a dirty tree always needs this). Report any forward or proxy URL you were given without vouching for it."
   - "Never test against, stop or kill a server you did not start. Browsers, migrations and seed data only as the project's docs say, in this worktree's database only."
   - "Run headless, only the scope asked for, never wider. Edit no code or tests; commit and push nothing."
   - "On success, failure or timeout, stop every process you started and confirm the port is free."
   - "Reply with the command, where runner, browser and server ran, the base URL, the saved proof's path, counts, per failure its spec, test name, first error line and artifact paths, and anything incomplete."

On notice or resume:
- A question in the brief: answer once with `terminal.sendCommandOwned`, `notify:true, handback:true`; a shared-database migration or killing a server goes to the user.
- End your turn after the launch. The notice's screen is the agent's claim, never your evidence: report nothing until you have, yourself:
  1. read the reporter's output (results JSON, `playwright-report/`, else the saved run output), confirming counts and failure names (traces can hold credentials: paths only);
  2. read the saved proof: the listener was this worktree's, started for this run, the revision as briefed (a marker also matching through the browser's URL). A miss rejects the run as a wrong or stale server, say so and offer one rerun on a fresh port;
  3. `lsof` on the port: nothing the agent started still listens, any earlier listener still does (gone: report it);
  4. `git status`: no code edits (artifacts are fine); `HEAD` or the dirty hash changed: stale.

Stop and recovery: one corrective round. Past the deadline, `terminal.getStatus` and act on its screen.

Report back: worktree and revision; where runner, browser and server ran; the base URL and the proof it was this checkout's server at that revision (or unproven); confirmed counts; each failure with artifact paths as host, worktree path and revision (a hub cannot open a host path: give counts, names and first errors in text); what was started and stopped; the agent's terminal id; anything incomplete; for red, offer daintree.ci.diagnose-and-fix.
