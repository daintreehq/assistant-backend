---
id: daintree.hosts.steer-swarm
title: Stop or change a run already sent to the Remote Hosts
summary: 'From this Shell, stop or correct a named run already sent to one or several Remote Hosts: interrupt or correct each named agent on its host, or brief the host''s assistant that runs it, and report every agent as acknowledged, finished, unresolved or disconnected from what its screen and answer show.'
whenToUse: 'Use when the person wants work they ALREADY SENT to their other machines, one run on one or several Remote Hosts, STOPPED, CALLED OFF or CHANGED while it is under way: "call off what I sent to every machine but keep what they have done", "cancel the remote run and tell me which machines acknowledged", "the swarm needs new acceptance criteria, the rest of its brief stands". The deliverable is, per host, the stop or correction delivered to each named agent and its state as its screen and answer give it (acknowledged, finished, unresolved, disconnected), with their work left in place. Do not use for agents on this machine (daintree.agents.steer-running-agent; parking them, daintree.orchestration.park-work), for sending new work or redoing finished work (daintree.hosts.delegate-to-host, daintree.hosts.swarm-fan-out), for a change to the contract the hosts build against (daintree.hosts.coordinate-contract-change), for catching up on or cleaning up after a run (daintree.hosts.recover-and-clean-up), for keeping it moving to its end (daintree.hosts.supervise-swarm), for reverting what it changed (daintree.hosts.undo-swarm-changes), or for a stop at a time set when the work is sent (daintree.hosts.timebox-swarm).'
risk: external
requiredTools:
  - host.list
  - host.call
hostEligibility: configured
---
A send never stops a turn: it is read when the agent's current turn ends. A stop, or a change that cannot wait, interrupts first. Work stays in place: nothing reverted, closed or cleaned up here.

Procedure:
1. Resolve the run the person names: host, `projectId` and terminal ids from your rows, else per host `terminal.list` through `host.call`, matched by name and worktree (unsure which are this run's: ask). Only the hosts and units named. One `terminal.getStatus` per host project over them, `includeOutput:{"lines":40}`. Per unit:
   - At its prompt with its result, or exited: finished; nothing sent.
   - `working`: a stop or an urgent change, step 2. A change that can wait ("also", "afterwards"): `terminal.notifyWhenIdle` through `host.call`, note "steer: <gist>", and step 3 when it stops (`armed:false`: it already has; step 3 now).
   - `waiting` on a question: the stop or change is its answer; step 3.
   - Host `disconnected`: nothing was sent; unresolved till it is back. `ambiguous` or `expired`: as the hosts foundation says; never resend a stop or correction blind.
   - Run by the host's assistant: step 4.
2. Interrupt each working agent once, then confirm before anything is sent:

```json tool=host.call
{"hostId":"<hostId>","projectId":"<projectId>","tool":"terminal.interruptOwned","args":{"terminalId":"<terminalId>"}}
```

   `requested` is a request, not a stop: `terminal.waitUntilIdle` (`timeoutMs:15000`), then `terminal.getStatus` with output. Stopped means the turn shows interrupted at an empty input line. Still working: unconfirmed; report it, send nothing, never interrupt again. Refused as idle: re-read the screen. Refused for its cancel key: nothing went; the person stops it in that pane.
3. One message per agent, in one `terminal.sendCommandMany` per host project, `notify:true, handback:true`, each `submissionToken` kept. A stop: "Stop here and start nothing new. Keep all work; revert, commit and push nothing. Reply: done, half-done, uncommitted files, branch, HEAD, and any push, PR or external step done or under way." A change: what changes, in the person's words; "everything else in your task still applies"; files to re-read if it was stopped mid-edit; "First state in one line what you will now do differently, then continue." A unit not yet started gets "never start it" or the changed brief. End the turn.
4. `RESOURCE_NOT_OWNED` (your Shell session restarted, or another session launched it), or a unit its host's assistant runs: nothing went. Brief that host's assistant (hosts foundation, Delegating; `ASSISTANT_BUSY` mid-turn: `terminal.interruptOwned` it, confirm it at its prompt, then send) with the terminal ids and the stop or change: "Follow daintree.agents.steer-running-agent for these; keep all work; reply per agent: stopped or on the new course, branch, HEAD, uncommitted files, any external step." No assistant there, or it is refused too: unresolved; give the person the words to type in that pane on that machine.

On notice or resume: match the notice to its agent, then one `terminal.getStatus` per host over the open units. A stop: at its prompt with its reply, acknowledged; still working, unresolved. A change: its one-line statement and work on the new course, acknowledged; the old course after the correction, one restated correction with `notify`, a second miss to the person. An assistant's answer (`terminal.readLastMessageOwned`) is a claim: read those agents' screens yourself. A change's result comes later, verified as daintree.hosts.supervise-swarm does.

Stop and recovery: one interrupt, one message and at most one restatement per agent per request; a resend only on the person's word. Never close, force, revert, merge or reassign here; external steps already taken stand, reported.

Report back: per host and agent, acknowledged, finished, unresolved or disconnected and how confirmed; branch, HEAD and uncommitted files as it said; what is pending and what brings it back.
