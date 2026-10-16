---
id: daintree.hosts.check-usage-limits
title: Check agent usage left on other machines
summary: From this Shell's hub, have the assistant on each Remote Host read its own machine's agent usage screen, and bring back one table of what every host printed, host by host and never added together.
whenToUse: 'Use when the user wants agent usage checked on OTHER MACHINES — how much Claude Code or Codex allowance each Remote Host this Shell reaches has left, on one host, several or all of them, or which machine has the most usage headroom — reported host by host and account by account, never as one combined total. The deliverable is one table of figures each host''s assistant read off that machine''s own usage screen. Do not use for the usage of agents on this machine (daintree.agents.check-usage-limits), or for a host''s own assistant answering such a request from the hub (daintree.hosts.handle-hub-message).'
risk: external
requiredTools:
  - host.list
  - host.listAssistants
  - host.sendMessage
  - host.getTask
optionalTools:
  - host.listTasks
  - host.startAssistant
hostEligibility: connected
---
1. Hosts: those the user named, else every host `host.list` gives. Per host, `host.listAssistants`: one `available` lane with a `generation` is enough. None: foundation step 2. This machine only when the user includes it: daintree.agents.check-usage-limits, here.
2. CLI per host: the one the user named. Unnamed or unknown: every agent CLI signed in there; the hub cannot see which, so never guess.
3. One `host.sendMessage` per host, in one round, no `waitMs`, to the lane step 1 chose (its own `slot`). Brief, adapted:

```json tool=host.sendMessage surface=hub-full
{"hostId":"<host id>","projectId":"<project id>","slot":0,"generation":"<generation>","text":"Read Codex's usage on this machine with runbook daintree.agents.check-usage-limits, in a new pane. Codex not signed in here: say so and check nothing else. Reply with, exactly as printed: the command sent; plan or account; each window, per-model ones too, as % used or % left, saying which; resets with the timezone shown; credits; the pane id. Leave the pane open."}
```

   User asked to close the panes: "Close the pane after; a close that waits on a confirmation: leave it open and say so."
4. End the turn while any exchange is open. A send with `taskId: null` is that host's row now.

On notice or resume: the foundation's (the line's ids are host-side), then `host.getTask`. `failed`, `rejected`, `canceled`: that host's row, with its state. A `completed` reply missing figures it should have: once per host, a new message naming the missing fields and the pane id: read that pane again, launch nothing. Never substitute another CLI.

Report back: one table, a row per host and CLI: plan or account, each window as printed and whether used or left, resets with timezone, credits, pane, and "read off <host>'s <command> screen by its assistant", or "not checked: <reason>". Never add hosts up: each can be a different account with different resets; one account on two hosts is one allowance shown twice. Most headroom: the largest % left on the same CLI and window, with plan and reset; different plans are not like for like, so say so. Open exchanges: pending, with state. Panes left open, per host (closing them later: daintree.hosts.recover-and-clean-up).
