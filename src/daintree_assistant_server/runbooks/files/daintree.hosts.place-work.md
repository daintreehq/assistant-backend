---
id: daintree.hosts.place-work
title: Place independent work on the remote hosts that can take it
summary: From this Shell's hub, choose a Remote Host for each of several different, independent pieces of work by its load, signed-in agent CLIs, allowance left and enrolled assistant, show the placement before sending, then delegate each piece and bring back one verified row per piece.
whenToUse: 'Use when the person has DIFFERENT, independent pieces of work (a list of issues, bugs or tasks, or one piece with no machine chosen) and wants each put on whichever Remote Host this Shell reaches can best take it: "here are eight issues, split them across my machines", "put this on whichever host has room", "which machine should take this?". The hub weighs each host''s load, the agent CLIs signed in there, allowance left and an enrolled assistant in the right project. The deliverable is a placement table (piece, host, project, CLI, reason), shown before anything is sent unless the person said to just go, then each piece delegated and one row per piece verified by branch, SHA or PR. Do not use for the same work on every host (daintree.hosts.swarm-fan-out), one piece for a host the person named (daintree.hosts.delegate-to-host), independent pieces for agents on this machine (daintree.orchestration.fan-out-work), what became of work already sent (daintree.hosts.recover-and-clean-up), or moving a started piece to another host (daintree.hosts.reassign-host-work).'
risk: external
requiredTools:
  - host.list
  - host.listAssistants
  - host.listTasks
  - host.sendMessage
  - host.getTask
optionalTools:
  - host.startAssistant
---
Initial placement only; a piece needing another's result is a gap: say so. Keep each task's role: survey, or which piece.

1. `host.list`; per connected host `host.listAssistants` and `host.listTasks`, paging while `hasMore`; other hosts are gaps. Date `load` by `sampledAt` (it outlives a disconnect); null is not measured, never idle.
2. Candidates: the host's own project for the repository, with an `available` lane holding a `generation`; slots in one project share its CLI and machine. Omitted or missing project, or no lane: a gap, never a clone; start one only if the request covers it (daintree.hosts.start-swarm).
3. Without dated readings here, one message per candidate lane, no `waitMs`: daintree.hosts.check-usage-limits's brief for each signed-in CLI, plus the project's `origin` URL, whether it has the base SHA, its platform, and what it has in flight for these pieces. Never guess CLIs. End the turn.
4. Screen: a piece already in flight (an open PR, or a branch, worktree or exchange in use) is not placed.
5. A host qualifies on matching `origin` and base, the platform a piece needs, and a signed-in CLI it allows with allowance left (no Claude Code on a Codex-only host). Then lower load (CPU, memory pressure, thermal, agents working), then more allowance on that CLI. Count each placed piece against its host and account: one account on two hosts is one allowance. Keep the person's per-host cap, or state yours. Table: piece, host, project, slot, CLI, reason, reading times; gaps apart with the step each needs. Send at once only if the request said to send; else on their yes ("which machine?" gets the table). A CLI swap, start or push they never named is asked either way.
6. Per piece, one `host.sendMessage` with daintree.hosts.delegate-to-host's brief and that host's CLI, no `waitMs`; surveys, starts and sends share 10 per host, 30 a minute. End the turn.

On notice or resume: survey replies go to step 4; work replies get the delegate runbook's checks. A `disconnected` host may still reply; a lane no longer enrolled will not. Either way report the piece: placing it elsewhere, as after `ambiguous`, is the person's call. A next wave surveys again; readings are not live.

Stop and recovery: one send per piece, one host per piece.

Report back: a row per piece: host, project, CLI, branch, SHA and PR claimed vs verified and how, state; gaps and open pieces apart.
