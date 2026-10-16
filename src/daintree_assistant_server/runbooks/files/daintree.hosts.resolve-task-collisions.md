---
id: daintree.hosts.resolve-task-collisions
title: 'Resolve task collisions across Remote Hosts'
summary: 'From this Shell''s hub, when two Remote Hosts turn out to be doing the same work by accident, hold both, verify what each pushed and kept unpushed, give the work one owner that alone publishes, and keep the other attempt intact.'
whenToUse: 'Use on the hub, the person''s own Shell, when work it sent out COLLIDES BY ACCIDENT across its Remote Hosts: two machines given the same issue, or two hosts pushing one branch, as in "I sent the login bug to both the mini and the build box", "both studios picked up the same ticket under different branch names and one has uncommitted changes, work out which keeps it", "two of my machines keep pushing to the same branch". The deliverable is every host''s writer on that work stopped with an acknowledgement, each side''s pushed SHA verified and its unpushed work accounted for, one owner chosen on evidence or by the person and alone allowed to push or publish, and the other attempt kept, never deleted or merged unasked. Do not use for agents colliding on this machine (daintree.orchestration.resolve-task-collisions), attempts the person asked to race or have reviewed (daintree.hosts.race-and-review), a unit moved to another host because its first host could not finish it, including that host coming back afterwards (daintree.hosts.reassign-host-work), stopping or changing a run with no overlap to settle (daintree.hosts.steer-swarm), or combining finished branches into one (daintree.hosts.integrate-host-branches).'
risk: external
requiredTools:
  - host.listTasks
  - host.getTask
  - host.listAssistants
  - host.sendMessage
optionalTools:
  - host.list
  - host.cancelTask
---
1. Match: the tasks from your record, else `host.listTasks` per host (no text: unsure, ask); `host.getTask` each. Same unit: one repository and an overlapping issue, PR or acceptance, or contradicting edits; never by branch name, project id or pane title.
2. Hold every side still writing. `submitted`: `host.cancelTask`; only `canceled` needs no hold. Else one `host.sendMessage` per lane, the original in `referenceTaskIds`, no `waitMs`: "Follow daintree.hosts.steer-for-hub: stop the agents on <hostTaskId>, never start it if unstarted; commit, push, revert nothing. Reply per agent as it says, plus the unit as briefed, remote, branch, full HEAD, pushed or not." End the turn.

```json tool=host.sendMessage surface=hub-full
{"hostId":"<hostId>","projectId":"<projectId>","slot":0,"generation":"<generation>","text":"<message>","referenceTaskIds":["<taskId>"]}
```

On notice or resume:
- Holds: a side stopped, done or gone is settled. Refused (`taskId` null), disconnected or stop unconfirmed may still write: the person stops it there or accepts that risk; else end the turn. Pin pushed SHAs (`git ls-remote`), read pushed diffs in a scratch clone, unpushed work through its lane, against the acceptance. One owner on fit to the authorised brief and progress; the person decides scope it leaves open. No merge or race unasked. One `host.sendMessage` per lane, earlier tasks referenced, each opening "Follow daintree.hosts.steer-for-hub:"; end the turn.
  - Owner: "resume <unit> (pin <branch> at <SHA>); only you publish it, as authorised<; take <commits> from <branch>>." Agent gone: daintree.hosts.delegate-to-host's brief from the pin.
  - Other: "leave <unit>'s agents stopped, another machine owns it; publish nothing for it; keep worktree, branch, uncommitted files; close <hostTaskId> if open." Its unpushed edits travel only as a commit on its own branch, pushed on the person's word.
- Decisions: the owner's line and the other's acknowledgement settle it. A side still writing or publishing: one restated message, then the person.

Stop and recovery: one hold and one decision per lane. Never settle by force-push, reset, deletion, PR closing or merge; tidying the kept attempt is daintree.hosts.recover-and-clean-up, on request.

Report back: per side branch, SHA claimed vs verified, unpushed files, stop acknowledged or not; decision and by whom; where the other attempt lives and what to salvage; pending.
