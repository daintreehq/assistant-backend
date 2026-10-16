---
id: daintree.hosts.reprioritize-swarm
title: Make room on the Remote Hosts for urgent work
summary: 'From this Shell''s hub, re-plan a run busy on the Remote Hosts when something urgent jumps the queue: each task kept, paused with a resume note or redirected through its host''s assistant, and the urgent work started first.'
whenToUse: 'Use on the hub, the person''s own Shell, when something URGENT arrives while their Remote Hosts are busy with a run and it must JUMP THE QUEUE across those machines: "a blocker bug just came in, free up two of the other machines for it", "the hotfix takes priority on my hosts, hold the low-priority remote tasks but keep the migration going", "reshuffle the swarm around this security fix and tell me what gave way". The deliverable is a priority reallocation: each running task kept, paused with its resume note, or turned to the urgent work, as its host''s assistant acknowledged, with the urgent work under way first. Do not use for agents on this machine (daintree.orchestration.reprioritize-work), or when nothing urgent needs the room (daintree.hosts.steer-swarm, daintree.hosts.park-swarm).'
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
1. The ask: the urgent task, how many workers, what gives way. Resolve the run as daintree.hosts.steer-swarm's step 1 does (`get_runbook` it if absent; page `host.listTasks`), plus `host.list` for load (`sampledAt`, `agentsObserved`).
2. Per unit: keep (not named; nothing sent, state from `host.getTask`), pause, or redirect (paused, its lane then taking an urgent worker). Pause the fewest that make room; ask when picks differ. A `disconnected` host may still be working: its units stand, its room is never free.
3. Pause through steer-swarm's steps 2-3, its lane text adding: "Priority pause, resumed later: no commit, stash, reset or push<; wip commit allowed>; leave <kept units> running. Per agent also: done, open questions, the exact next step."
4. Per urgent worker one `host.sendMessage`: daintree.hosts.delegate-to-host's brief naming the CLI, to a lane with the repository and a free slot (count your sends): now if that lane pauses nothing, else once its pause is acknowledged or its message withdrawn. Fresh worktree, new branch from the base, never a paused unit's. Two on one task: one writer; the other reads only or has its own branch, briefed so. Urgency grants no push, deploy, merge, spending or skipped confirmation not given. End the turn.

On notice or resume: read as steer-swarm says. A pause counts only once its lane's reply gives each agent stopped: record its note (branch, HEAD, pushed or not, uncommitted files, question, next step), checking a pushed SHA on the remote, then send that lane's urgent worker. Silence or a late cancel proves no stop.

Resume only units the person names, on their word; the urgent task finishing is not it. Per unit one `host.sendMessage`, `referenceTaskIds` its original and pause; text: its original objective and acceptance, its note, the verified branch and SHA of any urgent result it builds on, and "Resume <run> <unit>: HEAD or files not as noted is a question back; its idle agent gets the next step (daintree.hosts.steer-for-hub); none live, daintree.hosts.do-delegated-work in that worktree."

Stop and recovery: one pause per lane, one send per urgent worker, one resume per unit per request. After a restart rebuild from `host.listTasks`; ask about what does not match.

Report back: per unit kept, paused (its note, or unacknowledged), redirected or unresolved; each urgent worker's host, branch, SHA claimed vs checked; what waits on the person.
