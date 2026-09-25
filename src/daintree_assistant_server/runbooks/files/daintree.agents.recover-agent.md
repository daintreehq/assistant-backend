---
id: daintree.agents.recover-agent
title: Recover a broken agent or spawn
summary: Triage an agent that went wrong — reconcile the launch from what Daintree lists, tell a hang from a bad id, and re-arm observation of an unwatched agent instead of restarting it.
whenToUse: Use when something has gone WRONG with an agent or its spawn — "my agent is stuck", "it has been sitting there for ages", "did that agent even start?", "the spawn failed", "nothing happened when you launched it", "that terminal id does not work", "this agent has no watcher on it", "there is an agent running from earlier that nobody is watching", "is it hung or just slow?". Covers a launch that returned no terminalId or an ambiguous result, a hung or unresponsive agent, a stale or mistyped terminal id, and adopting an orphaned or unsupervised agent. Do not use when the agent is healthy and merely ASKING something — answer it with daintree.agents.answer-waiting-agent; for routine triage of a fleet where nothing is broken use daintree.orchestration.supervise-fleet or daintree.orchestration.status-and-resume; to start fresh work use daintree.edits.spawn-visible-agent. Do NOT use when the agent and its launch are both healthy and the user is questioning something YOU did — "why did you not wait", "why did you spawn that" — a complaint about your own handling is not a broken agent, so answer it directly rather than opening a recovery.
risk: terminal
requiredTools:
  - terminal.list
  - terminal.getStatus
  - terminal.getOutput
optionalTools:
  - agent.listAvailable
  - agent.launch
  - worktree.list
  - terminal.readLastMessageOwned
  - terminal.waitUntilIdle
  - terminal.notifyWhenIdle
  - terminal.interruptOwned
---
Use when: an agent or its launch has gone wrong. The launch returned no terminal or an ambiguous result, an agent stopped responding, a terminal id no longer resolves, or a running agent has nothing arranged to bring you back when it stops.

You are recovering observation and the task, not replacing the work. Never replace a terminal unless the user asked; never resend the original prompt into a terminal you do not understand; make one corrective action, then hand it to the user.

Availability: Core. Diagnosis uses status reads, which work on any terminal. Interrupting and sending need ownership (`terminal.interruptOwned`, `terminal.sendCommandOwned`). `terminal.notifyWhenIdle` is offered to panes and the sidebar assistant, not API-key clients, which get one bounded wait instead.

Procedure:
1. Gather evidence before naming the shape: your checkpoint (the recorded launch intent: agent, `name`, worktree, `requestedId`, time), the launch result if you have it, `terminal.list` (`owned:true` for yours; `location:"trash"` or `"background"` for panels that left the grid), and one `terminal.getStatus` over the candidates with `includeOutput`.
2. Name the shape, then act on it:
   - Launch reply lost, timed out or ambiguous. The panel may well exist: a timeout after the panel was created is common. Look for it by `requestedId`, or by worktree, agent, title and a recent `spawnedAt`. Found: that is your agent; observe it (step 5). Not found: read once more, since a panel can take a moment to appear. Still nothing means the outcome is unknown; relaunch only when a refusal or error establishes nothing was created, or the user agrees. Two agents in one worktree is worse than none.
   - `launched:false` with the other fields null: the launcher declined and nothing exists, usually because a same-kind launch was still starting. An identical launch within two minutes returns the same refusal, so retry once, after that launch has started, with a changed `name`.
   - A tool error naming an unknown agent or worktree: nothing launched. Correct exactly what it names from `agent.listAvailable` or `worktree.list`, and retry once.
   - `spawnStatus:"missing-cli"`: the CLI is not installed and the terminal is a setup panel. Report it and leave it with the user.
   - A startup screen: a trust, login, update or permission prompt before the first turn. The prompt has not been consumed; the user must clear the screen. Afterwards, check the screen for whether the prompt ran before sending anything.
   - Exited: `agentState` exited with an `exitCode` (null means killed by a signal), or a CLI that failed at startup and left a shell prompt with an error. That is a failed launch: report the error with any credentials removed. A bare prompt with no error is ambiguous, not grounds to relaunch.
   - Slow: `working` with recent output. `lastOutputChangeAt` is not a hang verdict either way.
   - Asking a question or waiting on an approval: not hung. Use daintree.agents.answer-waiting-agent.
   - Provider error: `waitingReason:"error"`, or output showing a rate limit, auth or network failure. Report the concrete error; waiting longer fixes nothing.
   - Wrong locator: a per-entry `error` from `terminal.getStatus`, or an id missing from `terminal.list`. Check it against the listing (truncated or mistyped id, a worktree filter, the trash) before concluding anything, then correct it and resume the same observation. Never relaunch a healthy agent because an id was wrong.
   - Lost ownership: `RESOURCE_NOT_OWNED` from an owned tool. That is not evidence the terminal is missing; the code covers missing, foreign and user-owned alike. Status reads are not ownership-scoped, so use them to see whether it exists. A pane's ownership survives an MCP reconnect, but not the pane relaunching or an app restart. Ask the user to hand the terminal over through Daintree, or leave the action to them.
   - Unknown: `source:"pty"` or `unavailableFields` in the status, or unreadable output. Say exactly what is unknown.
3. Hung or just slow: take one bounded observation (`terminal.waitUntilIdle`, at most 60 seconds, or the status tail) and report the terminal id, what the screen shows and how long since its output changed. Do not hold finished agents hostage: report their results now and the stuck one separately. For a disputed send, check its `submissionToken` phase first; never resend from `unknown`, `failed` or `cancelled` alone.
4. Interrupt only when the user authorized stopping this agent's turn: `terminal.interruptOwned` with its `terminalId`. `requested` or `requested-unverified` confirms a request, not a stop; read the terminal afterwards. Respect a refusal (idle agent, a different cancel key, not owned): no raw keystrokes, kill or close instead.
5. Unobserved but healthy: do not restart it. If it is working, arm one notice and put your run and step in the note:

```json tool=terminal.notifyWhenIdle
{"terminalId":"<terminal id>","note":"<run id>: recovered, verify step <n>"}
```

   `armed:false` means it was not working; classify its returned `state` now instead. Arming again from this pane replaces your earlier notice for that terminal. Another coordinator's notices are invisible to you, so never call an agent unsupervised just because you hold none for it. Record the notice in the checkpoint and end your turn.

On notice or resume: follow the foundation's resume procedure, then classify again with the shapes above. A recovered agent's result is still a claim, checked against the original task's acceptance.

Stop and recovery: one corrective action (a retry, an id fix, a re-armed notice); if it does not hold, stop and report the ids and evidence. A second relaunch is a loop. Never close a terminal to fix a failed launch: the failed launch did not create it. Keep any terminal or worktree a partial launch created, and reconcile before retrying.

Report back: the shape and the evidence for it; the terminal, worktree and launch details involved; what you recovered and what you deliberately did not do (nothing closed, no blind resend, at most one relaunch); what is still running and what will bring you back, or what you need from the user.
