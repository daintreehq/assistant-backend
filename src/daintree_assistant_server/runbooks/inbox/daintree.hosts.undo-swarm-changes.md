---
id: daintree.hosts.undo-swarm-changes
title: Undo a run's code changes across Remote Hosts
summary: 'From this Shell''s hub, back out exactly the commits one named run made in each repository on the Remote Hosts: writers stopped first, dependents reverted before what they build on, a fresh agent on each host reverting while everyone else''s work stays, and every revert checked.'
whenToUse: 'Use on the hub when the person wants the CODE one run or swarm changed on their Remote Hosts TAKEN BACK across hosts, in every repository it touched, while everyone else''s work stays: "the remote run broke the build, revert everything it committed on both machines", "back out the API and app commits that job made, the app change depends on the API one", "reverse what my other Macs'' agents committed for that run but keep my own edits there". The deliverable is that run''s attributable commits reverted on each host by a fresh agent, dependents before what they build on, nothing reset, force-pushed or rewritten in shared history, hunks of unclear authorship brought back as one list to decide, and per repository the original and revert SHAs with checks. Do not use for one agent''s changes on this machine, or as a Remote Host''s own assistant reverting in its project (daintree.git.undo-agent-changes), stopping or changing a run without undoing it (daintree.hosts.steer-swarm), removing a run''s panes, worktrees or branches (daintree.hosts.recover-and-clean-up), or a deployed service (daintree.release.rollback-deployment).'
risk: external
requiredTools:
  - host.list
  - host.listAssistants
  - host.listTasks
  - host.getTask
  - host.sendMessage
hostEligibility: configured
---
Code only: a deployed service, credentials, data and the run's panes and worktrees are separate; name them, do none.

1. Resolve the named run, per unit: host, project, lane (`host.listAssistants`), repository, branch, base SHA, its reported commits, `dependsOn`, checks; from the plan and replies kept; none: ask the person (records hold no text). Titles, authors and times only find candidates.
2. Unless every unit is closed, stop the run first (daintree.hosts.steer-swarm); release nothing more of it. Each lane's confirmed stop (branch, HEAD, dirty files) is its repository's checkpoint; none: it waits.
3. Order: dependents before what they build on, across projects; each SHA once per checkout. A dependent kept, or a consumer outside the run (merged, deployed): ask; keep both, or stage it.
4. Per lane, this stage's units: one `host.sendMessage`, `taskId` `<run>-undo-<unit>`: "Follow daintree.git.undo-agent-changes in <repository> (check `origin`) on <branch>, start <base>: undo newest first the run's commits <SHAs> and pane <ids>' uncommitted edits, as you attribute them; keep all other staged, unstaged and untracked work; no reset, clean, stash, push or history drop; anything unattributed or mixed: one input-required list, unchanged. Checks: <commands>. Reply each original and revert SHA, kept work, checks." End the turn.

```json tool=host.sendMessage surface=hub-full
{"hostId":"<hostId>","projectId":"<projectId>","slot":0,"generation":"<generation>","taskId":"<run>-undo-<unit>","text":"<undo brief>"}
```

On notice or resume, after the hosts foundation's reconcile:
- `input-required`: this stage's items from every lane, one list to the person; each answer names its unit.
- `completed`: verified if it pairs each original with its revert, shows saved work kept and checks passing; else one narrow question. A provider goes only after all its dependents verify; a failed or open one blocks it.
- No reply (`replies` empty after a host restart, or open too long): never resend; ask that lane, changing nothing, for the local procedure's resume check on the unit; a remainder goes on that report alone, under a new `taskId`.

Stop and recovery: one undo per unit; a failed one stays as the lane left it; publishing reverts is the person's later call.

Report back: per repository: host, branch, original and revert SHAs (lane-verified), kept work, open decisions, checks; what is left outside code.
