---
id: daintree.hosts.steer-swarm
title: Stop or change a run already sent to the Remote Hosts
summary: 'From this Shell, stop or correct a named run already sent to one or several Remote Hosts: withdraw the messages not yet delivered, send each host that has read its task a scoped stop or correction for its own assistant to carry out, and report every host as acknowledged, finished, unresolved or disconnected from what its assistant answers.'
whenToUse: 'Use when the person wants work they ALREADY SENT to their other machines, one run on one or several Remote Hosts, STOPPED, CALLED OFF or CHANGED while it is under way: "call off what I sent to every machine but keep what they have done", "cancel the remote run and tell me which machines acknowledged", "the swarm needs new acceptance criteria, the rest of its brief stands". The deliverable is, per host, the stop or correction delivered to that host''s own assistant and each worker''s state as its answer gives it (acknowledged, finished, unresolved, disconnected), with their work left in place. Do not use on the receiving machine itself (daintree.hosts.steer-for-hub), for agents on this machine (daintree.agents.steer-running-agent; parking them, daintree.orchestration.park-work), for sending new work or redoing finished work (daintree.hosts.delegate-to-host, daintree.hosts.swarm-fan-out), for catching up on or cleaning up after a run (daintree.hosts.recover-and-clean-up), for keeping it moving to its end (daintree.hosts.supervise-swarm), or for reverting what it changed (daintree.hosts.undo-swarm-changes).'
risk: external
requiredTools:
  - host.list
  - host.listTasks
  - host.getTask
  - host.listAssistants
  - host.sendMessage
  - host.cancelTask
---
1. Resolve the run the person names: its tasks, `hostTaskId`s and lanes from what you kept, else `host.listTasks` per host (records hold no text: unsure which are this run's, ask). Only the hosts and units named. `host.getTask` each; `host.listAssistants` for lane addresses.
2. One row per task:
   - `completed`: finished if its reply says the work ended; else step 3.
   - `canceled`, `rejected`, or `failed` with no `inboxFetchedAt`: never read.
   - Host `disconnected`: nothing reaches it or queues; unresolved till it is back.
   - A stop, `submitted`: `host.cancelTask`. `state:"canceled"`: withdrawn. Only `cancelRequestedAt`: delivery may have begun; step 3.
   - Otherwise, or a change: step 3.
3. One `host.sendMessage` per lane, up to 16 originals' `taskId`s in `referenceTaskIds`. Text: the run id; each original by `hostTaskId` with the units, agents and pane ids its replies named; "Follow daintree.hosts.steer-for-hub: <stop | the change; the rest of the brief stands>. An original not yet started: <never start it | start it changed>. Keep all work. Report any push, PR or other external step done or under way. Close each stopped original still open with a completed reply." End the turn.

```json tool=host.sendMessage surface=hub-full
{"hostId":"<hostId>","projectId":"<projectId>","slot":0,"generation":"<generation>","text":"<run id, originals, stop or change>","referenceTaskIds":["<taskId>"]}
```

   `paused`: nothing went; the pause holds the stop, never the work: the person turns messaging back on, then send. `lane_unavailable`: list again once; still unavailable: unresolved, the person stops it at that machine.

On notice or resume: `host.getTask` the steer and original tasks the notice names, and read the reply per agent: stopped or on the new course, acknowledged; already done, or gone on a stop, finished; anything else, unresolved. A change's result comes later, on its original or as a host message.

Stop and recovery: one stop or change per lane per request; a resend only on the person's word. Never clean up, force, revert, merge or reassign here; external steps already taken stand, reported. `cancelRequestedAt` proves nothing about a worker.

Report back: per host and task, withdrawn, acknowledged, finished, unresolved or disconnected, each agent's state, branch, HEAD and uncommitted files as its assistant said; what is pending and what brings it back.
