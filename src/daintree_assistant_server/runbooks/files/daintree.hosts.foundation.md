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
Sidebar assistants only, never agent panes: this Shell's hub messages the assistant enrolled in a host's project (a lane); hosts never reach each other, so the hub relays. The steps are the hub's. A lane follows Daintree's own messaging instructions; of these, step 3 and the stops hold for it too.

1. Setup. No `host.*` tools: this is not the sidebar assistant of a Shell with a Remote Host, or the platform has no messaging (Windows). A host missing from `host.list`: the person turns on "Allow assistant messaging with this host" in its host settings. `NOT_ELIGIBLE`: they turn on "Accept messages from other machines" in this assistant's menu. Sending, cancelling and starting need Tool set = Full.
2. Address: `hostId`, `projectId`, `slot`, `generation` from `host.listAssistants`, read before each round. None with a generation: `host.startAssistant` only with "Let the assistant start assistants on this host" on; else the person opens that host's project in a window here and turns on "Accept messages from other machines" in its assistant. No other way in: never switch their window, take a lease or displace an assistant.
3. Brief by reference (repository, branch, SHA, PR URL): hosts share git remotes, not working trees. A host's paths, names, pane ids and ports are its own: keep them with its `hostId`; never use them here.
4. Keep per message its `hostId`, address, `taskId`, `hostTaskId` and `contextId`; per host, the `host.listTasks` cursor.

On notice or resume: on the hub, a `Daintree: delegated exchange update` line names hosts' ids (`hostTaskId`), not your `taskId`: `host.listTasks` from that host's cursor shows what moved, then `host.getTask`. Exchanges sent before a restart or re-enrollment wake nobody: reconcile them the same way (cursor 0 if lost); report open ones as pending.
- `completed` means a reply arrived, not that it is right: check the branch, SHA or PR on the shared remote.
- `input-required`: answer within the user's request by a new `host.sendMessage` with that task in `referenceTaskIds`, or ask the person.
- A host's message needing a decision beyond the user's request: put it to the person, then `assistant.reply`.

Stop and recovery: no outcome means the host failed; read its `detail`. `taskId: null`: not sent. Otherwise look the `taskId` up before any resend. Uncertain start: `host.listAssistants`, never `startNew` again. A cancel only records a request once the message may have arrived, and a pause blocks new messages; neither stops work under way. Hop and rate limits are stops.

Report back: per host, outcome, `taskId`, state and `delivery` times; timings, not theories, for anything slow.
