---
id: daintree.hosts.recover-and-clean-up
title: Recover and clean up a run across Remote Hosts
summary: From the hub, rebuild where every exchange with the Remote Hosts stands after a restart, a new hub or a dropped link, from each host's task records rather than waiting for notices, and clean up after a named run by having each host's own assistant remove what it made.
whenToUse: 'Use on the hub when the user wants to know what happened on their other machines while they were away, where things stand with the work handed to another machine, or why a task sent to a Remote Host never came back, usually after Daintree restarted, the hub was replaced or the link to a Remote Host dropped: which delegated exchanges finished, which are still open, which will never be answered, and whether to send one again. Also when they want what a run left on the Remote Hosts, its panes (usage-check panes included), worktrees and branches, cleaned up through each host''s own assistant. The deliverable is a per-host account of every exchange from the records, and for a cleanup what each host removed and kept, and why. Do not use for agents, terminals or worktrees on this machine: catching up on local work (daintree.orchestration.status-and-resume), tearing down local panes and worktrees (daintree.orchestration.workspace-cleanup), a broken or hung local agent (daintree.agents.recover-agent) or triaging the local fleet (daintree.orchestration.supervise-fleet); nor for Daintree itself misbehaving on this machine (daintree.diagnostics).'
risk: system
requiredTools:
  - host.list
  - host.listTasks
  - host.getTask
  - host.listAssistants
  - host.sendMessage
optionalTools:
  - host.startAssistant
---
Nothing on a host is touched from here: its own assistant does the work.

1. `host.list`; per host `host.listTasks` (no cursor after a restart), paging while `hasMore`; `host.getTask` each open task and each finished one the user has not heard; `host.listAssistants` per host. A task's `laneId` is its project id and slot.
2. Each open task, by that lane:
   - Listed with a generation and `available`: may still be on it, unconfirmed unless you kept the generation it went to. Leave it; say how long since `acceptedAt`. With a generation, not `available`: not running, or paused.
   - No generation, or absent from a listing with no `projectsOmitted`: its enrollment ended (relaunch, host restart) and it will not be answered. Say so, and how the lane comes back. Resend only on the person's word: a separate new `host.sendMessage`, the brief in full (records keep no text), the old `taskId` in `referenceTaskIds`.
   - A lane reporting `SESSION_BINDING_GONE`: its window never came back; a person opens that project in this Shell's window for that host. Resend nothing.
   Pause is "Pause messages between machines" in this assistant's menu; every lane then reads not `available`.
3. Clean up only what the request names (panes only means panes), for a named run. A lane no longer enrolled: its items need the person. Never send a lane a second cleanup while one is open; without its `taskId` to check, ask first.
4. One `host.sendMessage` per enrolled lane, its run tasks in `referenceTaskIds` (at most 16), the text naming the run and every item the replies named. Brief: follow daintree.hosts.clean-up-for-hub for that run; close exactly the named panes; remove a worktree only if its panes closed, it is clean and pushed or abandoned on the person's word, and its teardown touches nothing remote, never `force` or `deleteBranch`; unpushed or dirty work, a branch, anything remote: one `input-required` list; a `CONFIRMATION_TIMEOUT` stays standing, reported; reply `completed` with each item removed or kept, and why. End the turn.

On notice or resume: an `input-required` list goes to the person with exactly what would go. A timed-out item stays; retry it only when they ask, while someone watches that host's window here or its "Daintree confirmations" is Never ask.

Report back: in progress (confirmed or not), will not be answered, needs the person, apart; for a cleanup, each host's removed and kept, as it says.
