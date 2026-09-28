---
id: daintree.ci.hunt-flaky-test
title: Hunt down and stabilise a flaky test
summary: Measure a suspected flaky test's failure rate under controlled variation, isolate the trigger and root cause (race, shared state, polluting neighbour, time, randomness), and when asked fix the source and prove it with a consecutive-pass gate.
whenToUse: 'Use when a test passes SOMETIMES and fails other times, and the user wants that measured, explained or stabilised: "this test is flaky", "fails about one run in ten", "passes locally but randomly fails in CI", "run it 50 times and see", "which test is polluting this one?", "stabilise the reconnect test without sleeps or retries". The deliverable is a failure rate per condition, the trigger and root cause with evidence, and, when a fix is asked for, a source fix proven by consecutive passes. Do not use for a test that fails EVERY time (use daintree.ci.diagnose-and-fix); to turn a bug report into a first failing test (use daintree.debug.reproduce-bug); or just to run the suite and report pass or fail (use daintree.ci.run-local-checks).'
risk: project
requiredTools:
  - worktree.list
  - terminal.getStatus
  - agent.launch
optionalTools:
  - worktree.createWithRecipe
  - worktree.waitUntilReady
  - terminal.sendCommandOwned
  - terminal.list
---
One run is not a rate, and 0 failures in N runs is "not reproduced at N", never "not flaky". A visible agent measures and, only if asked, fixes; you fix the target and check its evidence. Only when the user asks you yourself to do it, follow the brief in your own shell.

Procedure:
1. Target: the test, its narrowest command and its worktree (`worktree.list`). From a CI failure, read the exact invocation, seed and worker count yourself: `gh run view <run id> --log-failed`. Record `HEAD` and a hash of `git diff HEAD` plus untracked files.
2. A still tree. `terminal.getStatus` on that worktree's terminals other than your own must show none `working`, or the agent measures a moving tree. Busy, or a fix is asked on a branch someone else is using: a new worktree `flake/<slug>` off that branch (`worktree.createWithRecipe`, `collisionPolicy:"error"`, then `worktree.waitUntilReady` until `ready`; `failed`, `timed-out` or `needs-approval`: stop and report). It lacks uncommitted changes: say so.
3. Budget: the most runs and wall time for measuring and, when a fix is asked, for the gate.
4. Launch one agent: the one the user named (built-in `agentId`), else `claude`, and say which. Pass `name:"Flake: <test>"`, the `worktreeId`, `notify:true, handback:true`; keep the `terminalId`. The prompt carries the command, the CI invocation, the recorded identity, the budget, whether a fix is asked, and these lines as written (no diagnosis: the cause is the agent's to find):
   - "Measure before touching code: 30 to 50 runs (fewer only for a slow test; say so) into a new directory from `mktemp -d`, one log per run with its command and exit code. A setup or harness failure (missing dependency, collection error) is counted apart and stops that condition; it is not the flake."
   - "Vary ONE factor at a time, recording failures/runs for each: alone, with its file, in the full suite; order under a fixed, recorded shuffle seed; worker count; `TZ` and locale; the machine under load."
   - "Fails only after certain tests: halve the predecessors and run each half plus the victim enough times to compare with the baseline rate. Neither half raises it: test combinations. Report inconclusive rather than guess."
   - "Name the root cause (race, shared state, clock, randomness, network, order) at `file:line` from failing versus passing output."
   - Fix asked: "Fix the source where the state leaks (a polluter or shared fixture, not a cleanup in the victim): wait on observable state, inject the clock or seed, isolate the shared resource, await what was fire-and-forget. A change that only makes the failure rarer (more random bits, a wider window) is not a fix. Never sleeps, retries, rerun plugins, `flaky` markers, skips, quarantine, longer timeouts, wider tolerances or looser assertions. Gate: K consecutive passes under the failing condition, K at least three times the runs it took to see a failure and never under 30, then one full-suite run with a recorded seed. A gate failure: one new cause and one re-gate within budget, else report unverified with the rate."
   - "Commit and push nothing. Reply with failures/runs per condition, the trigger or polluter, the root cause at `file:line`, the fix and gate result, the results directory, and the tree's HEAD and diff-plus-untracked hash before and after."
5. End your turn. On the notice, your report waits until you have run the checks below yourself.

On notice or resume:
- A question inside the brief: answer once with `terminal.sendCommandOwned`, `notify:true, handback:true`; quarantine or a retry is the user's call.
- Rates and the gate are the agent's claims: report nothing until you have, yourself:
  1. opened the results directory and recounted failures per condition from the logs;
  2. a fix: read the diff for the bans (`sleep`, retry, `.skip`, `flaky`, changed timeouts or tolerances, weakened assertions), for new limits (an id that can overflow its format) and for a failure made only rarer; a banned change is not a fix;
  3. recomputed the identity: it must match the gated one;
  4. run the narrow command about ten times in a loop under the failing condition, counting failures: any failure contradicts the gate.
  Then report, the fix uncommitted unless the user asked for a commit.
- Budget spent without a cause: report the rates as measured.

Stop and recovery: one corrective round, then report. Reconcile with `terminal.list` and `worktree.list` before any second launch or create. Never skip or quarantine the test yourself.

Report back: command, worktree and revision; failures/runs per condition from the logs you read; the trigger and any polluter; the root cause at `file:line`; the fix, its gate (K/K, full-suite seed) and your own re-runs, or that no fix was asked; the results directory; the agent's terminal id.
