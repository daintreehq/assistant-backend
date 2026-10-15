---
id: daintree.hosts.steer-for-hub
title: 'Stop or correct the hub''s work on this host'
summary: 'On a Remote Host''s lane, stop, pause or correct only the agents this lane launched for the run the hub names, confirm each from its own screen with its work left in place, and answer the hub with each agent''s actual state rather than the exchange''s.'
whenToUse: 'Use on a Remote Host''s lane when the hub asks it to STOP, PAUSE or CHANGE work it delegated here that an agent launched by this lane is running or ran: "the hub wants the agent on its parser unit stopped and its saved work reported", "hub update: new acceptance criteria for the run, every other constraint stands", "pause the hub''s child panes and send back a checkpoint", or the hub cancelling an exchange whose agent is still working. The deliverable is each of that run''s agents interrupted, redirected or found already done, confirmed from its own screen with nothing it made thrown away, and the hub told each agent''s actual state and next step. Do not use for reading or answering the hub''s message as such (daintree.hosts.handle-hub-message), starting the hub''s unit of work (daintree.hosts.do-delegated-work), the person at this machine changing an agent''s course (daintree.agents.steer-running-agent), wrapping up their agents for the day (daintree.orchestration.park-work), or the hub''s own side (daintree.hosts.delegate-to-host).'
risk: terminal
requiredTools:
  - terminal.list
  - terminal.getStatus
  - terminal.interruptOwned
  - terminal.waitUntilIdle
  - terminal.sendCommand
  - assistant.reply
optionalTools:
  - assistant.sendMessage
---
After daintree.hosts.handle-hub-message has read it: steer only the agents this lane launched for the run the hub names. A hub cancel never shows here; act on its message.

1. Targets: that run's pane ids from your launches, in `terminal.list` `owned:true`. Missing, ambiguous or not owned: ask back with what you found; never by a similar name, never another pane.
2. One `terminal.getStatus` over them, `includeOutput:{"lines":40}`:
   - At its input line (`idle`, `completed`, `waiting` `prompt`): nothing to stop; a change is its next prompt.
   - `exited` or missing: gone.
   - Mid-turn (`working`, or `waiting` on a question, approval or error): step 3 first, unless the hub lets a change wait for a `working` one (queued; it may miss this turn).
3. `terminal.interruptOwned` once, then `terminal.waitUntilIdle` `timeoutMs:15000` and `terminal.getStatus` with `includeOutput`. Stopped: the turn shown interrupted at an empty input line, no spinner (may read `waiting`). Still working: unconfirmed; send nothing, never interrupt again. Refused as not mid-turn: read the screen again. Other refusals (a Ctrl+C CLI, locked input): nothing was sent; a blocker, never keys or a prompt instead.
4. A change: one send per agent; keep its `submissionToken`.

```json tool=terminal.sendCommand surface=lane-full
{"terminalId":"<pane id>","command":"Correction from the hub: <change>. The rest of your task still applies; re-read <files>. First state in one line what you will now do differently, then continue.","notify":true,"handback":true}
```

5. In each worktree `git status --short`, `git log -1 --oneline`. Reply per agent: pane id; before; what you did; after, as read (stopped, stop unconfirmed, change sent, already done, waiting, gone, blocked); branch, HEAD, uncommitted files; next step from its brief and screen. A change sent: end the turn.

On notice or resume: a stopped or paused unit's notice restarts nothing until the hub asks. A changed agent's notice: its one-line statement and work on the new course acknowledge it; verify its result as its launching runbook says, then answer that unit's exchange if open, else one `assistant.sendMessage` hand-off.

Stop and recovery: one interrupt and one change per agent per request. Never commit, push, revert, reset, stash, close, kill, restart or delete a worktree to stop or tidy, unless the brief says.

Report back: the reply as sent.
