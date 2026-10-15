---
id: daintree.hosts.foundation
title: Talk to the assistant on another machine
summary: The base judgment for messaging between Daintree assistants on different machines — reach a Remote Host's assistant from this Shell's hub, follow each exchange without resending, verify what comes back by reference, and leave enrollment and consent to the person.
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
---
Sidebar assistants only, never panes: this Shell's hub messages the assistant enrolled in a host's project (a lane); hosts never reach each other, so the hub relays. Steps are the hub's; a lane follows Daintree's messaging instructions, plus step 3 and the stops.

1. No `host.*` tools: not a Shell's sidebar assistant with a Remote Host, or Windows. Host absent from `host.list`: the person turns on "Allow assistant messaging with this host". `NOT_ELIGIBLE`: "Accept messages from other machines" in this assistant's menu. Send, cancel and start need Tool set = Full.
2. Address: `hostId`, `projectId`, `slot`, `generation` from `host.listAssistants`, fresh each round. No generation: `host.startAssistant` only if "Let the assistant start assistants on this host" is on; else the person opens that project in a window here and turns on "Accept messages from other machines" in its assistant. Never switch their window, take a lease or displace an assistant.
3. A lane sees your text, never `referenceTaskIds`: brief by reference (repository, branch, SHA, PR URL); hosts share remotes, not working trees. A host's paths, names, pane ids and ports stay with its `hostId`, never used here.
4. Keep each message's `hostId`, address, `taskId`, `hostTaskId`, `contextId`, and each host's `host.listTasks` cursor.

On notice or resume: the hub's `Daintree: delegated exchange update` line names `hostTaskId`s: match them to what you kept, else `host.listTasks` from that host's cursor; then `host.getTask` by `taskId`. Exchanges sent before a restart or re-enrollment wake nobody: reconcile the same way (cursor 0 if lost); report open ones as pending.
- `completed`: a reply arrived, not that it is right; check the branch, SHA or PR on the shared remote.
- `input-required`: answer within the user's request by a new `host.sendMessage` to that lane naming the task in `referenceTaskIds`, or ask the person.
- A host's message needing a decision beyond the request: the person decides; `assistant.reply` it.

Stop and recovery: an outcome is not a host failure; read `detail`. `taskId: null`: not sent; otherwise look it up before any resend. Uncertain start: `host.listAssistants`, never `startNew` again. A cancel after delivery is only a request and a pause only blocks new messages: neither stops work. Hop and rate limits are stops.

Report back: per host, outcome, `taskId`, state, `delivery` times; timings, not theories.
