---
id: daintree.runtime.measure-performance
title: Measure performance before and after a change
summary: Compare a baseline and a candidate revision, each in its own worktree and built the same way, with warmups and interleaved repeated runs, and say whether the change is faster, slower or within noise.
whenToUse: 'Use when the user wants a SPEED, MEMORY, SIZE or THROUGHPUT difference measured between two revisions: "is my branch faster than main?", "benchmark this against main", "did that change actually speed it up?", "is there a perf regression?", "how much did the bundle grow?", "check the agent''s claimed 30% speedup". The deliverable is numbers for both sides (median, spread, run count, build mode, machine conditions) and a verdict that accounts for noise: faster, slower, or no measurable difference. The same pair measured on SEVERAL machines, the Remote Hosts this Shell reaches or this one at a hub''s request, is this runbook''s too: "compare baseline and candidate on each machine, each one''s delta", "cross-host performance check at pinned revisions", "is the speedup real on every host, one is slower or loaded". Each host measures both revisions itself; the deliverable is one row per host (hardware, runtime, load and thermal readings with times, ratio with its spread, verdict) before any fleet summary, never raw times compared across machines. Do not use for a check matrix across environments (daintree.hosts.run-validation-matrix), a test suite split across hosts (daintree.hosts.shard-test-suite), why the same code differs between hosts (daintree.hosts.diagnose-host-differences), a lopsided swarm''s load (daintree.hosts.rebalance-load), plan allowance per host (daintree.hosts.check-usage-limits), finding WHICH commit in a range caused a slowdown (daintree.git.bisect-regression); checking an agent''s work with no measurable claim (daintree.orchestration.verify-agent-work); a machine overloaded by agents and test runs (daintree.runtime.relieve-resource-contention); or rival agents each making their own version for a winner (daintree.orchestration.competition, which may use this measurement to judge).'
risk: external
requiredTools:
  - worktree.list
  - terminal.getStatus
  - agent.launch
optionalTools:
  - worktree.createWithRecipe
  - worktree.waitUntilReady
  - terminal.sendCommandOwned
  - terminal.list
  - host.list
  - host.listAssistants
  - host.sendMessage
---
Same machine, same build, alternating, many times: one timing is not a measurement. A visible agent builds and measures; you fix both sides, check the machine and recompute from its raw numbers. Across hosts, compare ratios within a host, never times between hosts.

Procedure:
1. Sides. Candidate: the user's worktree (`worktree.list`), `HEAD`, a hash of `git diff HEAD` plus untracked files. Baseline: the revision named; "versus main": the tip SHA of `origin/<main>` after `git fetch`; "did my change help": `git merge-base HEAD <main>`. Record both SHAs and the rule.
2. Baseline checkout, never in the candidate's worktree: reuse a clean one at that SHA, else `git branch bench/base-<short sha> <sha>`, `worktree.createWithRecipe` with that `existingBranch`, `worktree.waitUntilReady` until `ready` (`failed`, `timed-out`, `needs-approval`: stop and report).
3. Quiet machine: `terminal.getStatus` across the project. `working` agents skew CPU, and one in a measured tree changes the code mid-run. Report them and ask whether to wait.
4. Terms: the metric exactly as asked, the same inputs on both sides, a material threshold (the user's, else 5%, stated), a budget of runs and time.
5. Launch one agent: the one the user named (built-in `agentId`), else `claude`, and say which. Pass `name:"Bench: <slug>"`, the baseline `worktreeId`, `notify:true, handback:true`; keep the `terminalId`. The prompt carries both paths and SHAs, the metric, threshold and budget, and these lines as written, then end the turn:
   - "Change no tracked file. Build both identically (same dependency install: a lockfile difference is part of the change, say so; release mode; same flags), outside the timed command unless build time is the metric. Hash each side's built artifact. Unless the user says so: no new tooling, throttling or affinity changes; stay within the repository's benchmark scope and budgets."
   - "Warm up, then alternate sides in ABBA order, saving every raw observation (side, run, value) outside both trees. Size: once per side, confirmed once. Memory: at least 10 runs per side (`/usr/bin/time -l`, Linux `-v`)."
   - "HEAD and diff-plus-untracked hash of both trees before and after: any change invalidates the runs. Record OS, CPU, cores, runtime versions, load average, battery and thermal state, each with its time, before, between rounds and after."
   - "Per side give the median and spread, then the paired deltas, their spread and delta %. Faster or slower only when the whole uncertainty lies beyond the threshold; no material difference when within; else inconclusive. At the budget, stop: no verdict from partial runs. Reply with the raw file path, commands, build mode, run counts, those readings, the verdict."

Hub, several hosts (`host.*`): pin both sides as full SHAs on the shared remote (`git ls-remote`; unpushed work: the person pushes). A host with no `available` lane (`host.listAssistants`) is an unavailable row. One workload, threshold and budget for all. One `host.sendMessage` per host (read daintree.hosts.foundation first): "Follow daintree.runtime.measure-performance for this pair on this machine: remote, SHAs, metric and command, threshold, budget. Reply with the raw pairs, ratio, artifact hashes, readings with times, caveats." End the turn. A lane uses the brief's SHAs, not its own `HEAD`, follows steps 2-5, and replies per daintree.hosts.handle-hub-message with all pairs inline (16000 characters).

On notice or resume:
- A question inside the brief: answer once with `terminal.sendCommandOwned`, `notify:true, handback:true`.
- The notice's numbers are a claim: report nothing until you have, yourself:
  1. opened the raw file (a host's reply: its pairs), recomputed each side's median, the paired deltas and the ratio, and checked run counts and alternation;
  2. re-read both trees' `HEAD` and hash: changed means invalid; a host reports the pinned SHAs and each side's artifact hash;
  3. `terminal.getStatus`: agents that worked during the runs are a caveat; per host, its readings with times beside `host.list` `load` by `sampledAt` (whole machine; null or old is unknown). High or changing load or heat qualifies that host: a verdict only if the whole spread is beyond the threshold;
  4. for a surprising result (large, or opposite), one more round within budget, on that host only (`terminal.sendCommandOwned`, or `host.sendMessage` in its `contextId`); end the turn.

Stop and recovery: one extra round. Reconcile with `terminal.list` and `worktree.list` before any second launch or create. The baseline worktree stays.

Report back: SHAs, baseline rule, build mode, command, run counts, conditions, verdict, raw file. Per host first: hardware, runtime, medians, spread, ratio against the threshold, readings with times, verdict; a fleet line only where every host agrees, never pooled times.
