---
id: daintree.runtime.measure-performance
title: Measure performance before and after a change
summary: Compare a baseline and a candidate revision, each in its own worktree and built the same way, with warmups and interleaved repeated runs, and say whether the change is faster, slower or within noise.
whenToUse: 'Use when the user wants a SPEED, MEMORY, SIZE or THROUGHPUT difference measured between two revisions: "is my branch faster than main?", "benchmark this against main", "did that change actually speed it up?", "is there a perf regression?", "how much did the bundle grow?", "check the agent''s claimed 30% speedup". The deliverable is numbers for both sides (median, spread, run count, build mode, machine conditions) and a verdict that accounts for noise: faster, slower, or no measurable difference. Do not use to find WHICH commit in a range caused a slowdown (use daintree.git.bisect-regression); to check an agent''s work when there is no measurable claim (use daintree.orchestration.verify-agent-work); when the machine itself is overloaded by agents and test runs (use daintree.runtime.relieve-resource-contention); or when rival agents each make their own version and a winner is picked (use daintree.orchestration.competition, which may use this measurement to judge).'
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
Both sides are built and measured the same way, on the same machine, alternating, many times. A single timing, a debug build or a dirty baseline is not a measurement. A visible agent builds and measures; you fix both sides, check the machine and recompute from its raw numbers. Only when the user asks you yourself to measure, follow the brief in your own shell.

Procedure:
1. Sides. Candidate: the user's worktree (`worktree.list` for its path), `HEAD`, and a hash of `git diff HEAD` plus untracked files. Baseline: the revision they named; "versus main": `git fetch origin <main>` and the tip SHA of `origin/<main>`; "did my change help": `git merge-base HEAD <main>`. Record both SHAs and which rule chose the baseline.
2. Baseline checkout. Never `git checkout` it in the candidate's worktree: stale build artifacts leak across and it pulls the tree from under its user. Reuse a clean worktree already at that SHA; otherwise `git branch bench/base-<short sha> <sha>`, `worktree.createWithRecipe` with `existingBranch` of that name, then `worktree.waitUntilReady` until `ready` (`failed`, `timed-out` or `needs-approval`: stop and report). Reconcile an ambiguous create with `worktree.list`; never create twice.

```json tool=worktree.createWithRecipe
{"source":{"kind":"existingBranch","branchName":"bench/base-<short sha>"},"focusPolicy":"preserve"}
```

3. Quiet machine: `terminal.getStatus` across the project. `working` agents skew CPU, and one working in either measured tree changes the code mid-run. Report them and ask whether to wait; never interrupt anyone's agent.
4. Terms: the metric exactly as asked (a benchmark script, a CLI run, startup to a ready line, bundle bytes, peak memory), the same inputs on both sides, a material threshold (the user's, else state one, such as 5%), and a budget of runs and wall time.
5. Launch one agent: the one the user named (built-in `agentId`), else `claude`, and say which. Pass `name:"Bench: <slug>"`, the baseline `worktreeId`, `notify:true, handback:true`; keep the `terminalId`. The prompt carries both paths and SHAs, the metric, threshold and budget, and these lines as written:
   - "Change no tracked file and commit or stash nothing in either tree. Build both identically: same dependency install (a lockfile difference is part of the change: say so), release or production mode, same flags. The build stays outside the timed command unless build time is the metric."
   - "Warm up, then alternate sides in ABBA order, saving every raw observation (side, run, value) to a file outside both trees. hyperfine runs each command as a block: if you use it, run alternating rounds and pool the raw exports. Size is deterministic: once per side, confirmed once. Memory: at least 10 runs per side (`/usr/bin/time -l` on macOS, `-v` on Linux)."
   - "Record both trees' HEAD and diff-plus-untracked hash before and after; any change invalidates the runs."
   - "Per side give the median and spread, then the paired deltas and their spread and the delta %. Faster or slower only when the whole uncertainty lies beyond the threshold; no material difference when it lies within; otherwise inconclusive. At the budget, stop: no verdict from partial runs."
   - "Reply with the raw file path, commands, build mode, warmups and run counts, machine notes (battery, thermal), and the verdict."
6. End your turn. On the notice, your report waits until you have run the checks below yourself.

On notice or resume:
- A question inside the brief: answer once with `terminal.sendCommandOwned`, `notify:true, handback:true`.
- The notice's numbers are the agent's claim, never your evidence: report nothing until you have, yourself:
  1. opened the raw file and recomputed each side's median and the delta, and checked the run counts and the alternation;
  2. re-read both trees' `HEAD` and diff-plus-untracked hash: changed means invalid;
  3. `terminal.getStatus`: agents that worked during the runs are a caveat to report;
  4. for a surprising result (large, or far from or opposite to the claim being checked), sent one more measuring round (`terminal.sendCommandOwned`, `notify:true, handback:true`) and ended your turn; report on its notice.
  An agent's claimed speedup stays a claim until these numbers agree.

Stop and recovery: one extra round. Reconcile with `terminal.list` and `worktree.list` before any second launch or create. The baseline worktree stays; removing it is on request.

Report back: baseline and candidate SHAs (with the dirty hash) and the baseline rule; build mode; the command; warmups and run counts; per-side median and spread; the delta, its percentage and the threshold; machine conditions and what else was running; the verdict; the raw results file; the agent's terminal id; the baseline worktree left in place.
