---
id: daintree.hosts.reassign-host-work
title: Reassign a remote host's unfinished work to another host
summary: From this Shell's hub, reassign one delegated unit off a Remote Host that dropped, stalled or ran out of agent allowance onto another host as a new task, after settling what the first host pushed and who owns the result, so nothing is lost and only one worker publishes.
whenToUse: 'Use on the hub when a unit of work ALREADY SENT to one Remote Host must be finished by a DIFFERENT HOST because the first cannot carry on: the machine dropped off or went unreachable mid-task, its assistant or agent is stuck, it ran out of allowance on the agent CLI the work needs or lacks that CLI, as in "the mini went offline halfway through the parser fix, have the tower finish it" or "move that blocked task to a host with Codex without losing what it did". The deliverable is the unit continued on the new host as a new task that references the original, briefed from what the first host verifiably pushed and reported, with the old worker stopped and acknowledged or its risk named, exactly one owner allowed to push or publish, and, if the first host comes back online, both attempts reconciled with neither lost. Do not use for where delegated exchanges stand with nothing moved, a resend to the same host, or cleanup (daintree.hosts.recover-and-clean-up); first placement of new pieces (daintree.hosts.place-work); new work for one named host (daintree.hosts.delegate-to-host); a different agent taking over in the same checkout (daintree.agents.hand-off-task); or a host''s own assistant stopping its agents for the hub (daintree.hosts.steer-for-hub).'
risk: external
requiredTools:
  - host.list
  - host.getTask
  - host.listTasks
  - host.listAssistants
  - host.sendMessage
optionalTools:
  - host.cancelTask
  - host.startAssistant
---
A new task on another host, never a resend under the old id; one owner.

1. The original: your brief (lost: ask the person), its `taskId` (else `host.listTasks`), `host.getTask` (`disconnected`: stale or null, no replies), `host.list` `connection`, `lastSeenAt`. On the remote, `git ls-remote` and `gh pr list --head` its branch: that SHA, not its claims, is the checkpoint.
2. Stop it:
   - Maybe unread: `host.cancelTask`; only `canceled` rules out delivery.
   - Lane reachable: one `host.sendMessage`: "Follow daintree.hosts.steer-for-hub for unit <u>: stop its agents, keep their work; reply per agent as it says, plus done, half-done and failed approaches." With the person's push authority add "then commit what each left, push to <branch>, leave them stopped". End the turn; on the reply, re-read the remote.
   - Host `disconnected` (a send records nothing) or silent: not stopped, may still push. Tell the person what is verified, that unpushed edits stay there, and the two-publisher risk.
   Go on only when every agent is stopped, gone or done, or on the person's word.
3. Target: daintree.hosts.place-work steps 1-5 for this piece (a named host its only candidate), exempt from its in-flight screen; never the original.
4. One `host.sendMessage`, no `waitMs`: daintree.hosts.delegate-to-host's brief at the verified SHA (none: the original base), on a new branch not on the remote, plus the handoff and "continue from this commit, never restart; you are the sole owner; push, PR or merge only as authorised; leave <old branch>". End the turn.

```json tool=host.sendMessage surface=hub-full
{"hostId":"<hostId>","projectId":"<projectId>","slot":0,"generation":"<generation>","text":"<brief>","referenceTaskIds":["<old taskId>"]}
```

On notice or resume: the successor's reply gets delegate-to-host's checks. The old host back (`connected`, or a late reply): not the owner; its reply advances nothing. Step 2's message plus "push nothing"; compare its reported diffs and the remote with the successor's. Porting any is the person's call, via the owner.

Stop and recovery: one successor per unit; one stop message, one more if the old host returns. Never settle ownership by force-push, deletion, PR closing or merge.

Report back: original `taskId`, state, verified SHA, what stayed unpushed; stop confirmed, unconfirmed or unreachable; owner; successor host, `taskId`, branch, base.
