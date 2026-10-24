---
id: daintree.hosts.undo-swarm-changes
title: Undo a run's code changes across Remote Hosts
summary: 'From this Shell, back out exactly the commits one named run made in each repository on the Remote Hosts: writers stopped first, dependents reverted before what they build on, a fresh agent on each host reverting while everyone else''s work stays, and every revert checked.'
whenToUse: 'Use when the person wants the CODE one run or swarm changed on their Remote Hosts TAKEN BACK across hosts, in every repository it touched, while everyone else''s work stays: "the remote run broke the build, revert everything it committed on both machines", "back out the API and app commits that job made, the app change depends on the API one", "reverse what my other Macs'' agents committed for that run but keep my own edits there". The deliverable is that run''s attributable commits reverted on each host by a fresh agent, dependents before what they build on, nothing reset, force-pushed or rewritten in shared history, hunks of unclear authorship brought back as one list to decide, and per repository the original and revert SHAs with checks. Do not use for one agent''s changes on this machine, or as a Remote Host''s own assistant reverting in its project (daintree.git.undo-agent-changes), stopping or changing a run without undoing it (daintree.hosts.steer-swarm), removing a run''s panes, worktrees or branches (daintree.hosts.recover-and-clean-up), or a deployed service (daintree.release.rollback-deployment).'
risk: external
requiredTools:
  - host.list
  - host.call
hostEligibility: configured
---
Code only: a deployed service, credentials, data and the run's panes and worktrees are separate; name them, do none. You attribute and verify; on each host a fresh agent (never one of the run's) reverts. Per unit this is daintree.git.undo-agent-changes done on a host: read it (`get_runbook`) for its attribution rules, worker brief and checks.

Procedure:
1. Resolve the named run, per unit: host, project, worktree id and path, repository, branch, base SHA, its commits, what it builds on, its checks; from your rows, a checkpoint or the units' reports. None: ask the person; no host keeps a brief. Titles, authors and times only find candidates.
2. Stop writers first: unless every unit's agent has stopped (`terminal.getStatus` through `host.call`), stop the run (daintree.hosts.steer-swarm) and release nothing more of it. A unit still writing waits.
3. Per unit, its baseline and attribution, as the local runbook says: pushed commits from your shell (a fetch into a scratch clone, `git log <base>..origin/<branch>`); HEAD (H1), local-only commits and dirty files on the host with `project.runCommand`.

```json tool=host.call surface=help-full
{"hostId":"<hostId>","projectId":"<projectId>","tool":"project.runCommand","args":{"projectId":"<projectId>","command":"git rev-parse HEAD && git status --porcelain && git log --format='%H %P %s' <base>..HEAD","cwd":"<worktree path>"}}
```

4. Order: dependents before what they build on, across repositories and hosts; each SHA once per checkout. A dependent the person keeps, or a consumer outside the run (merged elsewhere, deployed): ask; keep both, or stage it.
5. This stage, one undo agent per unit in its worktree, through `host.call`, and end the turn:

```json tool=host.call
{"hostId":"<hostId>","projectId":"<projectId>","tool":"agent.launch","args":{"agentId":"claude","name":"Undo: <run> <unit>","worktreeId":"<worktreeId>","prompt":"<undo brief>","handback":true,"notify":true}}
```

   The brief: the local runbook's worker brief, verbatim and whole, filled with that path, branch, H1, the commits newest first and the panes' uncommitted hunks, the checks; keep all other staged, unstaged and untracked work; no push; anything unattributed or mixed: one list, unchanged.

On notice or resume:
- Questions: this stage's from every host, one list to the person; each answer names its unit.
- A finish is a claim. Verify as the local runbook's resume step says: no operation in progress, each revert the inverse of its original, changed files inside the reverted commits', saved work kept, the checks re-run, the remote branch unchanged; with `project.runCommand` (and `project.runCheck` at Full) through `host.call`. Short of that: one narrow question to that agent (`terminal.sendCommandOwned`, `notify:true`).
- What a stage builds on is reverted only after all its dependents verify; a failed or open one blocks it.
- No notice: `terminal.getStatus` through `host.call`, paced to the work. Agent gone or host restarted: never relaunch blindly; read which reverts exist past H1, then brief only the remainder.

Stop and recovery: one undo agent per unit; a failed one stays as it left it, its saved patches and H1 the rescue point, restored only on the person's word. Never replay a revert. Publishing the reverts is the person's later call.

Report back: per repository: host, branch, H1, original and revert SHAs and how each was verified, kept work, open decisions, checks run by you vs reported; what is left outside code.
