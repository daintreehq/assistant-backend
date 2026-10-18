---
id: daintree.hosts.hand-off-swarm
title: 'Hand a run on the Remote Hosts to a replacement session'
summary: 'Move coordination of a run already out on the Remote Hosts to a replacement session on the same Shell, Claude Code to Codex and back: the outgoing session freezes and writes a hand-over, the person switches, and the successor rebuilds every exchange from the Shell''s task records with nothing resent, then supervises on.'
whenToUse: 'Use when the session coordinating a run ALREADY OUT on the Remote Hosts is being REPLACED BY ANOTHER SESSION NOW while the hosts keep working: the swarm manager moving from Claude Code to Codex or back mid-run, its outstanding tasks carried over, "your context is nearly full, hand the cross-machine swarm to a fresh hub assistant", "you are the new hub, take over the run from the hand-over the last one wrote", or whether a hub on another machine can take over a run this one''s hub sent. The deliverable is a hand-over between the two sessions: the outgoing one frozen, the hand-over kept where the person chooses (where each task stands and with which lane, open questions, what must never be redone) and the person''s switch steps, then the successor reconciling it with this Shell''s task records, nothing resent, and supervising on. Do not use for putting the run on hold to resume later (daintree.hosts.park-swarm), an account of a run after a restart or dropped link with no hand-over, or a cleanup (daintree.hosts.recover-and-clean-up), following a run its own hub sent (daintree.hosts.supervise-swarm), or a coordinator pane''s agents on this machine (daintree.orchestration.hand-off-orchestration).'
risk: external
requiredTools:
  - host.listTasks
  - host.getTask
  - host.listAssistants
  - assistant.getInbox
optionalTools:
  - host.list
  - assistant.reply
hostEligibility: configured
---
Records belong to the Shell: a successor reads and cancels every task, but no update line comes for earlier sends. A host's own message goes to the lane here that last sent to or started its lane.

Outgoing session:
1. Freeze: no new send, start or cancel; tell the person. Per host `host.listTasks` from your cursor, every page; `host.getTask` each task not final and each final one you have not read.
2. `assistant.getInbox`, every page: answer each host message in scope. A successor in this same sidebar inherits this inbox; one elsewhere does not: list what is left in the hand-over.
3. Write the hand-over where the person names, never in a repository or the sidebar scratch folder: goal; what the person allowed; per unit its host, lane address and generation, `taskId`, `hostTaskId`, `contextId`, brief in full (records keep no text), state, branch, SHA, PR checked; each host's cursor; open questions, answers sent; never redo: sends, starts, pushes.
4. Tell the person: pick the other CLI in this sidebar, or open a session elsewhere on this Shell; then say "Take over the run <run> from <path>".

Successor:
5. Read the hand-over (none: ask the person for scope). Per host `host.listTasks` from its cursor (none: 0), every page; `host.getTask` each task not final in it or changed since; `assistant.getInbox`. A task it lacks: match by lane and time, or ask. Never resend.
6. `host.listAssistants` per host before any send: a relaunched lane keeps its address, not its memory; brief by reference.
7. Continue under daintree.hosts.supervise-swarm (`get_runbook`). Inherited exchanges raise no update line: re-read them on each activation; tell the person. Your first send to a lane routes its own messages to you.

Another Shell sees none of these tasks: nothing transfers. With the person: finish or stop every unit from this Shell, confirmed by each host's assistant with its branch and SHA; the new Shell then briefs only unfinished units afresh by reference, never this Shell's ids.

Stop and recovery: `NOT_ELIGIBLE`: foundation step 1.

Report back: outgoing, where the hand-over is and the person's steps; successor, per unit its state, what moved since the hand-over, what waits on the person.
