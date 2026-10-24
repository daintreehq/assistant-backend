---
id: daintree.hosts.drain-issue-backlog
title: Drain an issue backlog across several remote hosts
summary: 'From this Shell, keep working an issue backlog on the Remote Hosts the person names, each with its own share, cap or model, while this machine (or a host they name) picks and merges: every host on origin''s base before each create, one worktree and agent per issue there, slots refilled, PRs proven green and merged, finished worktrees cleared, allowance and load watched per host.'
whenToUse: 'Use when the person wants an issue BACKLOG or batches of issues worked across SEVERAL machines: agents on two or more Remote Hosts they name, each with its own share, cap or model, while this machine (or one host they name) picks the issues and merges: "drain the backlog, 8 issues on one studio and 10 on the other, merge here", "run the work agents on my other Macs and keep this one as the orchestrator", "keep both hosts'' slots full until nothing is left", "tell the hosts to pull develop before each batch", and asked mid-run: "how long will the drain take at this rate", "stop starting new work but let the running ones finish", "run the merges on the second host from now on". The deliverable is the run kept going: every host on origin''s base before each create, a worktree and agent per issue on its host, each PR proven green, merges landed, each finished worktree cleared, allowance and load read per host, and a report per issue with its host. Do not use when every issue runs on this machine or on a single host (daintree.orchestration.work-in-waves, daintree.orchestration.fan-out-work), one first placement of different pieces by load with no picks or merges between (daintree.hosts.place-work), the same work on every host (daintree.hosts.swarm-fan-out), combining hosts'' branches into one (daintree.hosts.integrate-host-branches), only bringing hosts up to date with origin (daintree.hosts.sync-with-origin), or only reading usage left (daintree.agents.check-usage-limits).'
risk: external
requiredTools:
  - host.list
  - host.call
hostEligibility: connected
---
This machine is the hub: it picks, merges, checkpoints and watches; the hosts do the work. `gh -R <repo>` stays in your shell.

Procedure:
1. Restate the contract: the workers (the person's names matched to `host.list`; hosts not named stay out), each one's cap and model, where picks and merges run (default here; merges only if asked), rolling or in waves (a barrier: no refill until the wave is done), the pick (their command, else as daintree.workflow.find-issue-work), the per-issue command, who fixes red CI, teardown (default: each worktree once its unit finishes) and the end (one batch, or a drain until a pick is empty and all landed, or green if not merging). Checkpoint locally, per unit its host.
2. Preflight each worker: its project whose `origin` is the repository; `slashCommands.list` (the command) and `agent.listModels` (each model set, by listed `id`); `worktree.list` and `terminal.list` (report what is there, leave it). Missing: that worker launches nothing.
3. Before each create, every instance on origin's base, as daintree.hosts.sync-with-origin does. A create never fetches: an unsynced host branches stale. One that cannot fast-forward takes no creates until fixed or dropped.
4. Re-check each picked issue on the forge and against units already on any host. Per issue on its worker, one create and one launch at a time per host, hosts in parallel: `worktree.createWithRecipe` (`baseBranch:"origin/<base>"`, `fromRemote:true`, `collisionPolicy:"error"`, `issueNumber`), launch on `setupState:"ready"`, with the returned `worktreeId`, never one built from the branch:

```json tool=host.call surface=help-core
{"hostId":"<host>","projectId":"<project>","tool":"agent.launch","args":{"agentId":"claude","model":"<only if set>","worktreeId":"<worktreeId>","name":"<run> · #<n>","prompt":"<command> <n>","requestedId":"<saved first>","notify":true,"focusPolicy":"preserve"}}
```

   `launched:false` (`host-memory`, `panel-limit`): nothing started; the issue waits for a slot. A slash command takes no `handback` (it would join the arguments). First launch per model per host: read its banner (`terminal.getOutput`) for the model.
5. Refill: once a host's free or nearly free slots reach about a quarter of its cap, pick again, a count per host in words. Beyond the free slots, create worktrees now (claiming the issues) and launch in pick order as slots free, each fast-forwarded to `origin/<base>` first when it holds no changes or commits (`project.runCommand` there, its HEAD checked after).
6. Finished: its command's end marker, then its PR's checks on the forge. Red goes back to its agent (`terminal.sendCommandOwned` there), unless a running merge has taken the PR.
7. If asked to merge: whenever finished PRs wait and none runs, in the chosen machine's main checkout once clean on the base. A merge that rescans takes PRs whose agent still works, and two writers on one branch collide: name those PRs to it as off-limits until their units finish. One taken anyway: tell its agent to fetch before any push and never force-push over the merge; report a collision. Done is its marker and each PR `MERGED`.
8. Per finished unit: tree clean, no pane you did not open, close its pane (`terminal.closeMany` there, `closedIds`), then `worktree.deleteOwned` there.

On notice or resume: every check, one `terminal.getStatus` per worker over its live panes (`includeOutput` a line) and `host.list`; classify each stop as daintree.orchestration.work-in-waves does.
- Load, read as daintree.hosts.rebalance-load does, from `host.list` before every launch, after setup: pressured, unmeasured or gone: no launches there. A drop leaves its agents running; its Daintree restarting kills them (daintree.hosts.recover-and-clean-up).
- Allowance per account, read as daintree.agents.check-usage-limits does before launching there; at the person's threshold nothing new starts there. A limit with a reset time: daintree.agents.resume-after-usage-limit.
- Hold ("stop starting"): no picks or launches; running units, merges and teardown go on; queued units stay recorded. A restart of this machine stops its own panes: say so first.
- "How long": the larger of ready issues over measured throughput (landed per hour, agent time per model) and the longest dependency chain at one agent-plus-merge cycle per wave; add inflow of new issues.

Stop and recovery: never two picks or two merges at once; one launch per issue (a lost reply: `terminal.list` there by `requestedId`). A failed merge or a whole batch failing stops picks; running units finish.

Report back: per issue its host, model, PR and state; per host cap, synced SHA, load and allowance with times; worktrees deleted, pending or kept, and why; issues assigned that never landed.
