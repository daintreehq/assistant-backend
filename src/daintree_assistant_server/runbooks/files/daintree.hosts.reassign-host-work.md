---
id: daintree.hosts.reassign-host-work
title: 'Reassign a remote host''s unfinished work to another host'
summary: 'Reassign a unit of work one Remote Host started but cannot finish (it went offline, stalled or ran out of usage) to a different Remote Host as a new task, after checking what the first one pushed and settling who owns the result, so nothing is lost and only one machine publishes.'
whenToUse: 'Use when a piece of work ALREADY SENT to one Remote Host has to MOVE TO A DIFFERENT MACHINE because the first cannot finish it: that machine went offline or silent mid-task, is stuck, ran out of usage, or cannot run the CLI the work needs, as in "the mini went offline halfway through the parser fix, have the tower finish it" or "move that blocked task to a host with Codex without losing what it did". The deliverable is the work continued on the new machine as a new task, briefed from what the first one verifiably pushed, the first one stopped or its risk put to the person, exactly one machine allowed to push or publish, and, if the first machine comes back online, both attempts reconciled with neither lost. Do not use for where delegated pieces stand with nothing moved, a resend to the same host, or cleanup (daintree.hosts.recover-and-clean-up); first placement of new pieces (daintree.hosts.place-work); new work for one named host (daintree.hosts.delegate-to-host); another agent taking over in the same checkout (daintree.agents.hand-off-task); or a run still moving that only needs following (daintree.hosts.supervise-swarm).'
risk: external
requiredTools:
  - host.list
  - host.call
hostEligibility: connected
---
The shared remote is the only thing that moves between machines: the old host's worktree, its unpushed edits and its agent stay where they are.

Procedure:
1. The original: its brief (lost: ask the person), host, project, terminal, worktree and branch, from your rows. `host.list`: its host's `connection`, `lastSeenAt`. Connected: one `terminal.getStatus` through `host.call` with `includeOutput:{"lines":40}`. From your shell: `git ls-remote origin <branch>`, `gh pr list --head <branch> --state all`: that SHA, not any claim, is the checkpoint.
2. Stop it:
   - Connected and yours: still working, `terminal.interruptOwned` through `host.call`, then its screen shows it stopped. Then one `terminal.sendCommandOwned` with `handback:true, notify:true`: "Stop: this unit moves to another machine. Change nothing more. Reply: done, half-done, failed approaches, files changed, branch, HEAD, pushed or not." With the person's push authority add "first commit what you have and push it to <branch>". End the turn; on the reply, re-read the remote.
   - Connected but not yours (`RESOURCE_NOT_OWNED`): the person stops it in that pane, or accepts the risk.
   - `disconnected` or silent: not stopped; it may push when it returns. Tell the person what is verified, that unpushed edits stay there, and the two-publisher risk.
   Go on only when it is stopped, gone or done, or the person accepts that risk; else ask and end the turn.
3. Target: weigh candidates as daintree.hosts.place-work does for this one piece (a named host is the only candidate), never the original's host; the CLI the work needs must be launchable there (`agent.listAvailable`).
4. On the target, through `host.call`: a worktree on a new branch from the verified SHA (none pushed: the original base), then `worktree.waitUntilReady`:

```json tool=host.call
{"hostId":"<hostId>","projectId":"<projectId>","tool":"worktree.createWithRecipe","args":{"source":{"kind":"newBranch","branchName":"<new branch>","baseBranch":"origin/<old branch>","fromRemote":true,"collisionPolicy":"error"},"focusPolicy":"preserve"}}
```

   Then `agent.launch` there with `handback:true, notify:true`: daintree.hosts.delegate-to-host's brief, plus the hand-off (what was done, half-done, failed) and "first confirm HEAD is <SHA>, else stop and report; continue from this commit, never restart; you alone own this unit; push, PR or merge only as authorised; leave <old branch>". End the turn.

On notice or resume:
- The successor's report gets daintree.hosts.delegate-to-host's checks: verified by branch, SHA or PR, never by its claim.
- The old host back (`connected` again, or a late notice from its agent): not the owner; its report advances nothing. If yours, one `terminal.sendCommandOwned` there: "Stopped: another machine owns this unit. Push nothing. Reply with `git diff --stat <SHA>` and the commits past it." Not yours: the person reads it there. Compare with the successor's branch; porting any of it is the person's call, through the owner.
- No notice: `terminal.getStatus` through `host.call`, paced to the work.

Stop and recovery: one successor per unit; one stop message, one more if the old host returns. A lost launch reply is reconciled with `terminal.list` there before any second. Never settle ownership by force-push, deletion, PR closing or merge.

Report back: the old host, terminal, state, verified SHA, what stayed unpushed; stop confirmed, unconfirmed or unreachable; the owner; the successor's host, terminal id, worktree, branch and base SHA.
