---
id: daintree.hosts.place-work
title: Place independent work on the remote hosts that can take it
summary: From this Shell, choose a Remote Host for each of several different, independent pieces of work by its load, signed-in agent CLIs and allowance left, show the placement before sending, then delegate each piece and bring back one verified row per piece.
whenToUse: 'Use when the person has DIFFERENT, independent pieces of work (a list of issues, bugs or tasks, or one piece with no machine chosen) and wants each put on whichever Remote Host this Shell reaches can best take it: "here are eight issues, split them across my machines", "put this on whichever host has room", "which machine should take this?". This Shell weighs each host''s load, the agent CLIs signed in there, allowance left and the right project. The deliverable is a placement table (piece, host, project, CLI, reason), shown before anything is sent unless the person said to just go, then each piece delegated and one row per piece verified by branch, SHA or PR. Do not use for the same work on every host (daintree.hosts.swarm-fan-out), one piece for a host the person named (daintree.hosts.delegate-to-host), one change repeated in several repositories (daintree.hosts.migrate-across-repositories), independent pieces for agents on this machine (daintree.orchestration.fan-out-work), or what became of work already sent (daintree.hosts.recover-and-clean-up).'
risk: external
requiredTools:
  - host.list
  - host.call
hostEligibility: connected
---
Initial placement only: a piece needing another's result is a gap; say so.

Procedure:
1. Pin in your shell the remote URL, the base SHA (`git ls-remote`) and a run id. `host.list`: per connected host with `control`, `load` dated by `sampledAt` (it outlives a disconnect; null is not measured, never idle), `agentsObserved`, and the repository's project in `projects`. No project: a gap, never a clone. Other hosts: gaps.
2. Survey each candidate through `host.call` in that project, one round, host after host:
   - `agent.listAvailable`: only CLIs with `availability:"ready"` count (`availabilityComplete:false`: read again shortly). Never guess CLIs.
   - `agent.readQuota`: Codex's windows (`usedPercent`, `resetsAt`); `unavailable` is unknown. Claude Code's usage is never read: unmeasured, not ample.
   - `worktree.list`: branches and PRs it already holds for these pieces.
   - At Full, `project.runCommand` `git remote get-url origin; uname -sm`. At Core, origin and platform stay unverified until the agent's start check.

```json tool=host.call
{"hostId":"<hostId>","projectId":"<projectId>","tool":"agent.readQuota","args":{}}
```

3. Screen: a piece already in flight (an open PR by `gh pr list` or its branch by `git ls-remote`, both in your shell; a host worktree for it in the survey) is not placed.
4. A host qualifies on matching `origin`, the platform a piece needs, and a ready CLI it allows with allowance left (no Claude Code on a Codex-only host). Then lower load (CPU, memory pressure, thermal, agents working), then more Codex allowance. Count each placed piece against its host and account: one Codex account on two hosts is one allowance (identical windows and resets suggest one). Keep the person's per-host cap, or state yours.
5. Table: piece, host, project, CLI, reason, reading times; gaps apart with what each needs. Start at once only if the request said to; else on their yes ("which machine?" gets the table and stops). A CLI swap or a push they never named is asked either way.
6. Per piece through `host.call` on its host: `worktree.createWithRecipe` (`newBranch` `<run>/<piece>`, `collisionPolicy:"error"`, `focusPolicy:"preserve"`), `worktree.waitUntilReady` until `ready`, then `agent.launch` in it with that CLI, `name` `<run>: <piece>`, `notify:true`, `handback:true`, `focusPolicy:"preserve"`. Brief: the piece and its acceptance; start with `git fetch origin`, `origin` must be <remote> (else stop and say so), `git reset --hard <base SHA>` on this fresh branch; this worktree only; checks by command; push or PR only if the person said; report branch, full SHA, PR URL, each check's command and exit code. Keep per piece `hostId`, `projectId`, `worktreeId`, `worktreePath`, `terminalId`. End the turn.

On notice or resume: a host's notice carries no reply: read `terminal.getStatus` through `host.call` (`includeOutput`), and pieces with no notice the same way, paced to the work. A question in scope: answer with `terminal.sendCommandOwned` there (`notify`, `handback`); else ask the person. Verify each claim: the SHA on the remote by `git ls-remote`, the PR by `gh pr view`; unpushed, at Full `project.runCommand` (`git log -1 --stat`, `git status --porcelain`) in its `worktreePath`. A `disconnected` host keeps its agents running: read again when it is back; placing that piece elsewhere is the person's call, since the first may still finish. A next wave surveys again: readings are not live.

Stop and recovery: one launch per piece, one host per piece. A lost or `expired` launch: `terminal.list` (`owned:true`) there before anything else, never a relaunch on a guess.

Report back: a row per piece: host, project, CLI, branch, SHA and PR claimed vs verified and how, state; gaps and open pieces apart.
