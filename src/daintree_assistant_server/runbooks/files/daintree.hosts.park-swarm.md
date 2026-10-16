---
id: daintree.hosts.park-swarm
title: Park a run on the Remote Hosts for later and resume it
summary: 'From this Shell''s hub, park a run on the Remote Hosts for later: each host''s assistant stops its agents with their work left in place, the hub keeps a resume record of what every host holds and which never acknowledged, and a later hub session resumes the run from fresh host records.'
whenToUse: 'Use on the hub, the person''s own Shell, when a run ALREADY SENT to their Remote Hosts must be PUT ON HOLD TO COME BACK TO, over a break or across a restart: "park my swarm until tomorrow with a resume record", "pause the cross-machine run for the weekend and keep every worktree", "the hub restarted, pick the parked remote run back up from its record". The deliverable is a resume record kept where the person chooses (per host: project, slot, branch, SHA, pushed or not, uncommitted files, panes, open questions, what must not be redone, and which hosts never acknowledged), and later the run resumed from fresh host task records with no second agent on any unit. Do not use for a stop or change with no comeback planned (daintree.hosts.steer-swarm), agents on this machine (daintree.orchestration.park-work), an account of what happened while away, or cleanup (daintree.hosts.recover-and-clean-up), the receiving machine stopping its own agents (daintree.hosts.steer-for-hub), or coordination passing to another hub session while the run goes on (daintree.hosts.hand-off-swarm).'
risk: external
requiredTools:
  - host.list
  - host.listAssistants
  - host.listTasks
  - host.getTask
  - host.sendMessage
optionalTools:
  - host.cancelTask
---
Parking schedules nothing: a host's restart ends its panes and empties `replies`, never its worktrees. Exchanges expire in 24 hours and their records in about 8 days: the record is what lasts.

1. Ask where the record goes if the person has not said (the stop does not wait): a place they name outside any repository, never the sidebar scratch folder (wiped at app start) or a host's files. Unsaved there, it will not survive a restart: say so. It holds the goal; each unit's brief in full (records keep no text); what the person allowed (push, PR); per lane the address, ids and cursor you keep (generation as seen); each unit's state; open questions; what must never be redone (launches, pushes, PRs).
2. Stop through daintree.hosts.steer-swarm (`get_runbook` it if absent), its lane text adding: "Parking for later: no commit or push; no pane closed or worktree removed. Per agent also: worktree id, and whether HEAD is pushed." Save the record with every send's `taskId` and every lane unacknowledged, give the person the prompt "Resume the parked run <run> from <where>", and end the turn.

On notice: copy each reply's facts into the record and save it. Parked: its reply gives every agent stopped or done. Withdrawn before delivery: not started. Anything else is unacknowledged and may still be working, never parked.

On resume (new hub session, record read): reconcile from each recorded cursor; `host.getTask` every park, resume and open task it names. A changed generation: a re-enrolled lane that may remember nothing. Check pushed SHAs on the remote. A host gone for good: daintree.hosts.reassign-host-work. One `host.sendMessage` per unit not done and not already resumed: "Resume <run> <unit>: <brief in full; worktree and pane ids, branch, recorded HEAD, uncommitted files, next step; never redo: <list>>. Check first: its agent still working, or HEAD or files not as recorded, is a question back. Its idle agent gets the next step (daintree.hosts.steer-for-hub); none live, daintree.hosts.do-delegated-work in that worktree." Save each `taskId` before ending the turn.

Stop and recovery: one park message per lane and one resume message per unit per request; a unit with a recorded resume `taskId` is looked up, never resent.

Report back: per host and unit, parked, not started, unacknowledged or resumed, with its state as its lane said, apart from SHAs you checked; the record's place and resume prompt.
