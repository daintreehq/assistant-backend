---
id: daintree.hosts.reprioritize-swarm
title: Make room on the Remote Hosts for urgent work
summary: 'From this Shell, re-plan a run busy on the Remote Hosts when something urgent jumps the queue: each task kept, paused with a resume note or redirected on its host, and the urgent work started first.'
whenToUse: 'Use on the person''s own Shell when something URGENT arrives while their Remote Hosts are busy with a run and it must JUMP THE QUEUE across those machines: "a blocker bug just came in, free up two of the other machines for it", "the hotfix takes priority on my hosts, hold the low-priority remote tasks but keep the migration going", "reshuffle the swarm around this security fix and tell me what gave way". The deliverable is a priority reallocation: each running task kept, paused with its resume note, or turned to the urgent work, as its agent acknowledged, with the urgent work under way first. Do not use for agents on this machine (daintree.orchestration.reprioritize-work), or when nothing urgent needs the room (daintree.hosts.steer-swarm, daintree.hosts.park-swarm).'
risk: external
requiredTools:
  - host.list
  - host.call
hostEligibility: connected
---
Reallocation, not teardown: a paused unit's worktree stays on its host exactly as it left it; the urgent work gets its own.

Procedure:
1. The ask: the urgent task, how many workers, what gives way. Inventory: `host.list` (connection, `load` with `sampledAt`, `agentsObserved`), your rows, and per host one `terminal.getStatus` over the run's terminals through `host.call`, `includeOutput:{"lines":30}`. Rows lost: per host `terminal.list`, matched by name and worktree; unsure, ask.
2. Per unit: keep (the default for anything not named), pause, or redirect (pause, then its agent takes urgent work for what it knows). Pause the fewest that make room; ask when picks differ materially. Ownership holds: only agents this session launched are paused or redirected directly; a unit a host's assistant runs is paused by briefing that assistant (hosts foundation, Delegating); any other (`RESOURCE_NOT_OWNED`) is listed for the person with the words to type in its pane. A `disconnected` host may still be working: its units stand, its room is never free. An agent waiting on a question keeps it: the question goes in its resume note.
3. Pause in one `terminal.sendCommandMany` per host project, `notify:true, handback:true`: "Priority pause, resumed later: stop at a safe point and start nothing new; leave partial edits; no commit, stash, reset or push<; a wip commit allowed>. Reply: done, half-done, uncommitted files, branch, HEAD, open questions, the exact next step." It is read when the current turn ends: the unit counts as running until its reply. "Drop it now": `terminal.interruptOwned` each first and confirm the stop from its screen, as daintree.hosts.steer-swarm does.
4. The urgent work, on a host with room: now if that host pauses nothing, else once a pause there is confirmed. There, `worktree.createWithRecipe` with a new branch from the base (never a paused unit's), `worktree.waitUntilReady` until `ready`, then one `agent.launch` per worker, each with a distinct `name`:

```json tool=host.call
{"hostId":"<hostId>","projectId":"<projectId>","tool":"agent.launch","args":{"agentId":"claude","name":"Urgent: <task>","worktreeId":"<urgent worktree id>","prompt":"<task, acceptance, scope, write or read-only, what to report>","handback":true,"notify":true}}
```

   Two on one task: one writer; the other reads only or has its own branch, briefed so. A redirected agent's brief: "Work only in <absolute path on that host>; leave your own worktree as it is." Urgency grants no push, deploy, merge, spending or skipped confirmation not given. Tell the person the allocation; end the turn.

On notice or resume: per host one `terminal.getStatus` over the open units (a notice can be missed). A pause counts once its agent is at its prompt with its reply: record the note (branch, HEAD, pushed or not, uncommitted files, question, next step), checking a pushed SHA on the remote in your shell. Still working after it: one reminder, then unconfirmed. Silence proves no stop. An urgent worker's result is a claim: its full SHA on the remote and the forge's checks, verified in your shell.

Resume only units the person names, on their word; the urgent task finishing is not it. Its idle agent, still this session's: one `terminal.sendCommandOwned`, `notify:true, handback:true`: "Resume. First run git status and confirm it matches <note>; then continue from <next step>" (plus the verified branch and SHA of any urgent fix it builds on). Its agent gone or not yours: one `agent.launch` in its recorded worktree with its original brief and the note, once `terminal.getStatus` shows nothing working there.

Stop and recovery: one pause and one reminder per unit, one launch per urgent worker (reconcile with `terminal.list` on that host before any second), one resume per unit per request. Never reset, stash, clean, close or delete to free capacity.

Report back: per unit kept, paused (its note, or unconfirmed), redirected, not yours or unresolved; each urgent worker's host, worktree, branch and terminal id, SHA claimed versus checked; what waits on the person.
