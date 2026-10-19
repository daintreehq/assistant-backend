---
id: daintree.hosts.foundation
title: Talk to the assistant on another machine
summary: The base judgment for messaging between Daintree assistants on different machines — reach a Remote Host's assistant from this Shell's hub, follow each exchange without resending, verify what comes back by reference, and leave consent to the person.
whenToUse: 'Use when the user wants the Daintree assistant on ANOTHER MACHINE — a Remote Host this Shell reaches — asked something, handed a task or answered, its reply brought back, or wants to know how messaging between the assistants on two machines works, whether it is set up, how to switch it on, or why one is not reachable. It is the DOMAIN FOUNDATION for every daintree.hosts.* runbook: co-load it with whichever one fits, and load it ALONE only when none does. Do not use for a worktree''s cloud devbox or remote resource (daintree.worktree.manage-resource), or Daintree itself misbehaving on this machine (daintree.diagnostics).'
foundation: true
risk: external
optionalTools:
  - host.list
  - host.listAssistants
  - host.sendMessage
  - host.getTask
  - host.listTasks
  - host.cancelTask
  - host.startAssistant
  - assistant.reply
requires:
  - daintree.foundation
hostEligibility: any
---
Sidebar assistants only: any assistant in this Shell's windows messages one running in a host's project (a lane), at any Tool set, nothing to enroll; hosts never reach each other, so this Shell relays. Steps are the sender's; a lane follows Daintree's messaging instructions, plus step 3 and the stops.

1. No `host.*` tools, or `NOT_ELIGIBLE`: not a sidebar assistant on a Shell with a Remote Host, or Windows. Host absent from `host.list`: "Allow assistant messaging with this host" is off. Other refusals: a switch the person turned off, the host down or too old; never bypass them.
2. Address: `hostId`, `projectId`, `slot`, `generation` from `host.listAssistants`, each round. None running: one `host.startAssistant`, within the request. Running, `generation: null`: closed to this Shell (toggle, host setting, old host); never start beside it. Never switch their window, take a lease or displace an assistant.
3. A lane sees your text, never `referenceTaskIds`: brief by reference (repository, branch, SHA, PR URL); hosts share remotes, not working trees. A host's paths, names, pane ids and ports stay with its `hostId`, unused here.
4. Keep each message's `hostId`, address, `taskId`, `hostTaskId`, `contextId`, and each host's `host.listTasks` cursor.

On notice or resume: the `Daintree: delegated exchange update` line wakes only the sender and names `hostTaskId`s: match them to what you kept, else `host.listTasks` from that host's cursor; then `host.getTask` by `taskId`. Tasks are the Shell's, but one sent before this Shell restarted or by a closed run wakes nobody: reconcile the same way (cursor 0 if lost); report open ones as pending.
- `completed`: a reply arrived, not that it is right; check the branch, SHA or PR on the shared remote.
- `input-required`: answer within the user's request by a new `host.sendMessage` to that lane, the task in `referenceTaskIds`, or ask the person.
- A host's message needing a decision beyond the request: the person decides; `assistant.reply` it.

Stop and recovery: an outcome is not a host failure; read `detail`. `taskId: null`: not sent; otherwise look it up before any resend. Uncertain start: list again; never start blindly. A cancel after delivery is a request and a pause blocks new messages: neither stops work. Rate limit: wait `retryAfterMs`.

Report back: per host, outcome, `taskId`, state, `delivery` times; timings, not theories.
