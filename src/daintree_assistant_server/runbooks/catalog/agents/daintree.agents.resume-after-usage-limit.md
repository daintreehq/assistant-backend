---
id: daintree.agents.resume-after-usage-limit
title: Continue agents a usage limit stopped, once it resets
summary: When agents stop on their plan's usage limit, record each account's reset time, start nothing more on that account, and after the reset continue each stopped agent in its own conversation with one send, confirming it works again, without buying or switching on extra usage.
whenToUse: 'Use when agents STOPPED because their plan''s usage limit was reached and the work should carry on in the SAME agents once the limit resets: "my Claude agents all hit the usage limit, continue them when it resets", "the host ran out of its five-hour allowance, pick the agents back up at 3:10", panes showing "Usage limit reached · resets <time>" or Daintree notices that they stopped at an error, here or on a Remote Host. The deliverable is, per stopped agent, its account and reset time, nothing new launched on that account until then, and after the reset one continue sent into its own conversation and its pane seen working again, beside any that resumed by themselves. Do not use to have a DIFFERENT agent or CLI finish the task now (daintree.agents.hand-off-task), to read how much allowance is left (daintree.agents.check-usage-limits), for a hung or failed spawn or a turn cut off by sleep or a dropped connection (daintree.agents.recover-agent), or to move unstarted work off a host that is out of allowance (daintree.hosts.rebalance-load).'
risk: terminal
requiredTools:
  - terminal.getStatus
  - terminal.sendCommandOwned
optionalTools:
  - host.list
  - host.call
---
On a host: call these tools on the agents' machine through `host.call` there, per the hosts foundation.

A limit is the account's, not the pane's: every agent on that account stops, on any machine that shares it. Waiting is the fix. The screen's offer to keep going on paid usage (Claude Code's `/usage-credits`) changes billing: never select or send it.

Procedure:
1. Find them: one `terminal.getStatus` with `includeOutput:{"lines":20}` over the agents you supervise, per machine. A limit stop is `waiting` with `waitingReason:"error"` and names itself on screen ("Usage limit reached · limit resets 3:10am"); a question, another error or an end marker is not this runbook's. Group by machine and account; record each reset as printed, with its timezone (none printed: the machine's local time).
2. Hold: launch nothing more on that account until its reset; other accounts go on. Tell the user which agents stopped, the reset time, and what waits.
3. Leave each stopped agent as it is: no interrupt, close or relaunch; its conversation is intact. If you can schedule, one check just after the reset; else say the work waits for the user's continuation.
4. After the reset, one fresh `terminal.getStatus` with `includeOutput`. One working again resumed by itself (a background task woke it): leave it. Each still stopped gets one send in its own pane:

```json tool=terminal.sendCommandOwned
{"terminalId":"<terminal id>","command":"The usage limit has reset. Continue from where you stopped, through to the end of your task.","notify":true,"handback":true}
```

5. Read again shortly: `working` with moving output has resumed. Still on the limit screen: that account's reset is later (a weekly limit, another window): back to step 2 with the new time. Any other stop: classify it as the workflow that owns the agent does.

On notice or resume: "stopped at an error" is a reason to read the screen, not proof of a limit. A notice after your continue is that agent's next stop: classify it, never answer it with another continue.

Stop and recovery: one continue per agent per reset; stopped again on the same limit, it waits for the next reset. Never resend its original task prompt.

Report back: per machine and account the reset time read; agents stopped, resumed by themselves, continued by you and now working, or still stopped and why; what waited, and when it started again.
