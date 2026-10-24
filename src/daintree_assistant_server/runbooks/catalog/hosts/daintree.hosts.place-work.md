---
id: daintree.hosts.place-work
title: Place independent work on the remote hosts that can take it
summary: From this Shell, choose a Remote Host for each of several different, independent pieces of work by its load, signed-in agent CLIs and allowance left, show the placement before sending, then delegate each piece and bring back one verified row per piece.
whenToUse: 'Use when the person has DIFFERENT, independent pieces of work (a list of issues, bugs or tasks, or one piece with no machine chosen) and wants each put on whichever Remote Host this Shell reaches can best take it: "here are eight issues, split them across my machines", "put this on whichever host has room", "which machine should take this?". This Shell weighs each host''s load, the agent CLIs signed in there, allowance left and the right project. The deliverable is a placement table (piece, host, project, CLI, reason), shown before anything is sent unless the person said to just go, then each piece delegated and one row per piece verified by branch, SHA or PR. Do not use for the same work on every host (daintree.hosts.swarm-fan-out), one piece for a host the person named (daintree.hosts.delegate-to-host), several issues all for ONE machine the person named, whatever they call it (a batch or merge-gated waves there: daintree.orchestration.fan-out-work, daintree.orchestration.work-in-waves, driven there), issues worked through in successive batches on hosts whose shares the person already set, with picks and merges between (daintree.hosts.drain-issue-backlog), only which model each issue gets by difficulty once the hosts are chosen (daintree.agents.choose-agent-model), one change repeated in several repositories (daintree.hosts.migrate-across-repositories), independent pieces for agents on this machine (daintree.orchestration.fan-out-work), or what became of work already sent (daintree.hosts.recover-and-clean-up).'
risk: external
requiredTools:
  - host.list
  - host.call
hostEligibility: connected
---
Initial placement, and each refill of it: a piece needing another's result is a gap; say so.

Procedure:
1. Pin in your shell the remote URL, the base SHA (`git ls-remote`) and a run id. `host.list`: per connected host with `control`, its `load` and the repository's project in `projects` (`frontendAttached:false`, no window: every tool here still runs). No project: a gap, never a clone. Other hosts: gaps.
2. Survey each candidate through `host.call` in that project, one round, host after host:
   - `agent.listAvailable`: only CLIs with `availability:"ready"` count (`availabilityComplete:false`: read again shortly). Never guess CLIs.
   - A model the person named: `agent.listModels`, by its listed `id`; a preset: `agent.listPresets` with `presetsComplete:true`, by `id` (unlisted, it silently launches without). Not listed: that host takes no piece needing it.
   - `agent.readQuota`: Codex's windows (`usedPercent`, `resetsAt`); `unavailable` is unknown. Claude Code's usage is never read: unmeasured, not ample.
   - `worktree.list`: branches and PRs it already holds for these pieces.
   - `project.runCommand` `git remote get-url origin; uname -sm`.

```json tool=host.call
{"hostId":"<hostId>","projectId":"<projectId>","tool":"agent.readQuota","args":{}}
```

3. Screen: a piece in flight or landed (its PR open or merged by `gh pr list --state all`, its branch by `git ls-remote`; a host worktree for it; a launch in your record) is not placed again.
4. A host qualifies on matching `origin`, the platform a piece needs, a ready CLI it allows with allowance left and what was named listed. Capacity is `load` alone. Nothing new on a host unmeasured (no `load`, `stale:true`, null `cpuIdleCores` or `memoryAvailableBytes`: never free) or at `memoryPressure` `warn` or `critical`. Of the rest, one leads only with over 0.05 more `cpuIdleCores` and more `memoryAvailableBytes` than each other and no worse `memoryPressure` (Linux with only `memoryPressureSomeAvg10`: cores and memory alone); one host alone, or an older one among them (`memoryCapacityReported:false`): no lead. No lead: spread within the caps, say so. Never compare `cpuPercent`, add `agentsObserved`, or blend a score. Count each piece against its host and account: one Codex account on two hosts is one allowance; `agent.readQuota` names no account, so which hosts share one is the person's word (matching windows: a hint). The person's hosts, per-host caps, model and preset stand; with no cap given, state yours.
5. Table: piece, host, project, CLI, model, reason, reading times; gaps and held hosts apart with what each needs. Start at once only if the request said to; else on their yes ("which machine?" gets the table and stops). A CLI or model swap, or a push they never named, is asked either way.
6. Per piece through `host.call` on its host, each write under a `newCallId` you record first (no answer: resend under it as `callId`): `worktree.createWithRecipe` (`newBranch` `<run>/<piece>`, `collisionPolicy:"error"`, `focusPolicy:"preserve"`, no `recipeId` where `frontendAttached:false`), `worktree.waitUntilReady` until `ready`, `host.list` again (a reading, not a reservation; now held: keep the worktree, launch later), then `agent.launch` in its `worktreeId` with that CLI, model and `presetId`, `name` `<run>: <piece>`, `notify:true`, `handback:true`, `focusPolicy:"preserve"`. `launched:false` (`spawnStatus` `host-memory`, `panel-limit`): nothing started; it waits in that worktree, never a second create. Brief: the piece and its acceptance; start with `git fetch origin`, `origin` must be <remote> and the platform <platform> (else stop and say so), `git reset --hard <base SHA>` on this fresh branch; this worktree only; checks by command; push or PR only if the person said; report branch, full SHA, PR URL, each check's command and exit code. Keep per piece `hostId`, `projectId`, `worktreeId`, `worktreePath`, `terminalId`. End the turn (only held pieces: say what frees them; no notice comes).

On notice or resume: `terminal.getStatus` through `host.call` (`includeOutput`) for every open piece, notice or not. Verify each claim: the SHA on the remote by `git ls-remote`, the PR by `gh pr view`; unpushed, `project.runCommand` (`git log -1 --stat`, `git status --porcelain`) in its `worktreePath`. A `disconnected` host keeps its agents running; placing that piece elsewhere is the person's call. A refill or held piece: steps 1-4 again.

Stop and recovery: one launch per piece, one host per piece. A lost or `expired` launch: `terminal.list` (unfiltered) and `worktree.list` there before anything else, never a relaunch on a guess.

Report back: a row per piece: host, project, CLI, model, branch, SHA and PR claimed vs verified and how, state; gaps, held hosts with their readings, and open pieces apart.
