---
id: daintree.hosts.park-swarm
title: Park a run on the Remote Hosts for later and resume it
summary: 'From this Shell, park a run on the Remote Hosts for later: each agent stopped with its work left in place on its host, a resume record of what every host holds and which never acknowledged, and a later session resumes the run from that record and each host''s live terminals.'
whenToUse: 'Use on the person''s own Shell when a run ALREADY SENT to their Remote Hosts must be PUT ON HOLD TO COME BACK TO, over a break or across a restart: "park my swarm until tomorrow with a resume record", "pause the cross-machine run for the weekend and keep every worktree", "my Shell restarted, pick the parked remote run back up from its record". The deliverable is a resume record kept where the person chooses (per host: project, worktree, branch, SHA, pushed or not, uncommitted files, panes, open questions, what must not be redone, and which hosts never acknowledged), and later the run resumed from that record and each host''s live terminals with no second agent on any unit. Do not use for readying a host for its own planned restart (daintree.hosts.prepare-host-maintenance), a stop or change with no comeback planned (daintree.hosts.steer-swarm), agents on this machine (daintree.orchestration.park-work), an account of what happened while away, or cleanup (daintree.hosts.recover-and-clean-up), or coordination passing to another Shell session while the run goes on (daintree.hosts.hand-off-swarm).'
risk: external
requiredTools:
  - host.list
  - host.call
hostEligibility: configured
---
Parking schedules and closes nothing. A host's Daintree restart ends its panes, never its worktrees. After your Shell session restarts, this run's panes stay readable but take no send or close from you (`RESOURCE_NOT_OWNED`). The record is what lasts.

Procedure:
1. Ask where the record goes if the person has not said (the stop does not wait): a place they name outside any repository, never `$DAINTREE_ASSISTANT_SCRATCH_DIR` (new per sidebar restart) or a host's files. Unsaved there, it will not survive a restart: say so. It holds the goal; each unit's brief in full; what the person allowed (push, PR); per unit its host, `projectId`, worktree and terminal ids, agent, branch and state; open questions; what must never be redone (launches, pushes, PRs).
2. Per host one `terminal.getStatus` over the run's terminals through `host.call`. Stop every working unit with one `terminal.sendCommandMany` per host project, `notify:true, handback:true`; it is read when the current turn ends. "Stop now": `terminal.interruptOwned` first, the stop confirmed from its screen, as daintree.hosts.steer-swarm does.

```json tool=host.call
{"hostId":"<hostId>","projectId":"<projectId>","tool":"terminal.sendCommandMany","args":{"sends":[{"terminalId":"<terminalId>","command":"Parking for later: stop at a safe point and start nothing new. No commit or push; close no pane, remove no worktree. Reply: done, half-done, uncommitted files, branch, HEAD and whether it is pushed, open questions, the exact next step."}],"notify":true,"handback":true}}
```

   A unit a host's assistant runs: brief that assistant the same (hosts foundation, Delegating; `ASSISTANT_BUSY`: on "stop now" `terminal.interruptOwned` it first, else send on its `terminal.notifyWhenIdle` notice). One not this session's: listed for the person, with the words to type in its pane. A `disconnected` host, or a call not `ok`: its units unknown, recorded as such, never parked.
3. Save the record with every send and every unit unacknowledged, give the person the prompt "Resume the parked run <run> from <where>", and end the turn.

On notice: copy each reply's facts into the record and save it. Parked: at its prompt with a reply giving its state. Anything else is unacknowledged and may still be working, never parked. A late notice for a unit already resumed changes nothing.

On resume (new session, record read): `host.list`, then per host `worktree.list` and one `terminal.getStatus` over the recorded terminals through `host.call`; check pushed SHAs on the remote in your shell. A unit's agent still working, or its HEAD or files not as recorded: a question to the person, never a second agent. A host gone for good: daintree.hosts.reassign-host-work, on the person's word. Per unit not done and not already resumed: its idle agent, still this session's, gets one `terminal.sendCommandOwned`; otherwise one `agent.launch` in its recorded worktree there, both `notify:true, handback:true`: "Resume <run> <unit>: <brief in full; branch, recorded HEAD, uncommitted files, next step; never redo: <list>>. First run git status and git log -1; anything not as recorded: stop and say so." Save each resumed unit's terminal id before ending the turn.

Stop and recovery: one park message and one resume per unit per request; a unit with a recorded resume terminal is looked up, never relaunched. A launch in doubt: `terminal.list` on that host first.

Report back: per host and unit, parked, unacknowledged, unknown or resumed, with its state as its agent said, apart from SHAs you checked; the record's place and resume prompt.
