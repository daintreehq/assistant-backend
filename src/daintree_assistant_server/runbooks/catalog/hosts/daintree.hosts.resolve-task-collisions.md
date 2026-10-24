---
id: daintree.hosts.resolve-task-collisions
title: 'Resolve task collisions across Remote Hosts'
summary: 'From this Shell, when two Remote Hosts turn out to be doing the same work by accident, hold both, verify what each pushed and kept unpushed, give the work one owner that alone publishes, and keep the other attempt intact.'
whenToUse: 'Use on the person''s own Shell when work it sent out COLLIDES BY ACCIDENT across its Remote Hosts: two machines given the same issue, or two hosts pushing one branch, as in "I sent the login bug to both the mini and the build box", "both studios picked up the same ticket under different branch names and one has uncommitted changes, work out which keeps it", "two of my machines keep pushing to the same branch". The deliverable is every host''s writer on that work stopped with an acknowledgement, each side''s pushed SHA verified and its unpushed work accounted for, one owner chosen on evidence or by the person and alone allowed to push or publish, and the other attempt kept, never deleted or merged unasked. Do not use for agents colliding on this machine (daintree.orchestration.resolve-task-collisions), attempts the person asked to race or have reviewed (daintree.hosts.race-and-review), a unit moved to another host because its first host could not finish it, including that host coming back afterwards (daintree.hosts.reassign-host-work), stopping or changing a run with no overlap to settle (daintree.hosts.steer-swarm), or combining finished branches into one (daintree.hosts.integrate-host-branches).'
risk: external
requiredTools:
  - host.list
  - host.call
hostEligibility: connected
---
Recency, confidence and who claims more are not evidence. Letting both carry on "to compare" is a race only the person starts. The other attempt is kept: its worktree, branch and uncommitted files stay.

Procedure:
1. Match: the units from your rows; per host, through `host.call`, `terminal.list` and one `terminal.getStatus` with `includeOutput:{"lines":40}` over the candidates. Same unit: one repository and an overlapping issue, PR or acceptance, or contradicting edits; never by branch name, project id or pane title. Unsure: ask.
2. Hold every side still writing, now: two hosts can both push before either turn ends. Your own agent: `terminal.interruptOwned` through `host.call`, then its screen shows it stopped (`requested` is not a stop). Not yours (an earlier session's, the person's), or its host `disconnected`: it may still write; the person stops it in that pane or accepts that risk, and no owner is chosen meanwhile. Then one hold per side you own, and end the turn:

```json tool=host.call
{"hostId":"<hostId>","projectId":"<projectId>","tool":"terminal.sendCommandOwned","args":{"terminalId":"<terminalId>","command":"Hold: another machine has been doing the same task. Change, commit and push nothing. Reply in prose: the task and acceptance as you understand them, what you did, files changed, remote, branch, full HEAD, pushed or not, what is tested.","handback":true,"notify":true}}
```

On notice or resume:
- Holds: decide nothing until every side has replied or is confirmed stopped. Never name the other side's agent or ask either to judge the other.
- Evidence, yours: pin pushed SHAs (`git ls-remote` from your shell), read pushed diffs in a scratch clone; unpushed work from the hold reply, and `project.runCommand` there (`git status --porcelain`, `git log --oneline origin/<branch>..HEAD`, `git diff --stat`), all against the acceptance.
- Decide one owner on fit to the authorised brief and progress; the person decides scope it leaves open, or when the evidence cannot separate them. No merge or race unasked.
- Execute through `host.call`, one `terminal.sendCommandOwned` per side with `handback:true, notify:true`, then end the turn:
  - Owner: "You own <unit> (pin <branch> at <SHA>); only you push or publish it, as authorised<; take <commits> from <branch>>. State in one line what you own, then continue." Its agent gone: a fresh agent on that host from the pin (daintree.hosts.delegate-to-host).
  - Other: "Stop work on <unit>: another machine owns it. Publish nothing for it; keep your worktree, branch and uncommitted files; revert, delete and commit nothing." Its unpushed edits travel only as a commit on its own branch, pushed on the person's word.
- Decisions: the owner's first line names the unit and its diff moves on that; the other stays stopped. A side still writing or publishing (a new SHA on the remote): one restated message, then the person.
- No notice: `terminal.getStatus` through `host.call`, paced to the work.

Stop and recovery: one hold and one decision per side. Never settle by force-push, reset, deletion, PR closing or merge; tidying the kept attempt is daintree.hosts.recover-and-clean-up, on request.

Report back: per side its host, terminal, branch, SHA claimed vs verified, unpushed files, stop confirmed or not; the decision and by whom; where the other attempt lives and what to salvage; what is pending.
