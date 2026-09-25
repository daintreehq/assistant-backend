---
id: daintree.agents.recover-agent
title: Recover a broken agent or spawn
summary: Triage an agent that went wrong — reconcile the launch from what Daintree lists, tell a hang from a bad id, and re-arm observation of an unwatched agent instead of restarting it.
whenToUse: Use when something has gone WRONG with an agent or its spawn — "my agent is stuck", "it has been sitting there for ages", "did that agent even start?", "the spawn failed", "nothing happened when you launched it", "that terminal id does not work", "this agent has no watcher on it", "there is an agent running from earlier that nobody is watching", "is it hung or just slow?". Covers a launch that returned no terminalId or an ambiguous result, a hung or unresponsive agent, a stale or mistyped terminal id, and adopting an orphaned or unsupervised agent. Do not use when the agent is healthy and merely ASKING something — answer it with daintree.agents.answer-waiting-agent; for routine triage of a fleet where nothing is broken use daintree.orchestration.supervise-fleet or daintree.orchestration.status-and-resume; to start fresh work use daintree.edits.spawn-visible-agent. Do NOT use when the agent and its launch are both healthy and the user is questioning something YOU did — "why did you not wait", "why did you spawn that" — a complaint about your own handling is not a broken agent, so answer it directly rather than opening a recovery.
risk: terminal
requiredTools:
  - terminal.list
  - terminal.getStatus
optionalTools:
  - agent.listAvailable
  - agent.launch
  - worktree.list
  - terminal.getOutput
  - terminal.waitUntilIdle
  - terminal.notifyWhenIdle
  - terminal.interruptOwned
---
Use when: an agent or its launch has gone wrong. The launch returned no terminal or an ambiguous result, an agent stopped responding, a terminal id no longer resolves, or a running agent has nothing arranged to bring you back when it stops.

You are recovering observation and the task, not replacing the work: one corrective action, then hand it to the user. Never resend the original prompt into a terminal you do not understand.

Availability: Core. Status reads work on any terminal; interrupting needs ownership. An API-key client has no `terminal.notifyWhenIdle`: one bounded wait instead.

Procedure:
1. Gather evidence: what you recorded of the launch (agent, `name`, worktree, `requestedId`, time), its result if you have it, `terminal.list` (`owned:true` for yours; `location:"trash"` or `"background"` for panels off the grid), and one `terminal.getStatus` over the candidates with `includeOutput`.
2. Name the shape, then act:
   - Launch reply lost or ambiguous: the panel may exist. Look it up by `requestedId`; a match on worktree, agent, title and recent `spawnedAt` is only a candidate, and with several candidates ask rather than adopt one. Nothing found after a second read: the outcome is unknown. Relaunch, under the same identity, only when a refusal establishes nothing was created or the user agrees.
   - `launched:false`: the launcher declined, usually because a same-kind launch was still starting. Retry once after it has started.
   - An error naming an unknown agent or worktree: correct exactly what it names from `agent.listAvailable` or `worktree.list`. It usually means nothing launched, but check the listing before retrying, since a panel can exist after a later failure.
   - Null destination fields are unknown, not a wrong destination; call it wrong only when the listing shows it.
   - `spawnStatus:"missing-cli"`: a setup panel. Report it.
   - A startup screen (trust, login, update): a trust dialog for the directory the user asked to launch in is answered as the foundation says; anything else goes to the user. Afterwards check whether the launch prompt ran before sending anything.
   - Exited: `agentState` exited, or a CLI error left at a shell prompt. Report the error with credentials removed. Read `exitCode` as a signal kill only where the result says so; a bare prompt with no error is ambiguous, not grounds to relaunch.
   - Slow: `working` with recent output.
   - Asking a question or waiting on an approval: not hung. Use daintree.agents.answer-waiting-agent, which also owns any send.
   - Provider error (`waitingReason:"error"`, rate limit, auth, network): report it; waiting fixes nothing.
   - Wrong locator: a per-entry `error`, or an id missing from `terminal.list`. Check for a truncated id, a filter or the trash, correct it and resume the same observation. Never relaunch a healthy agent because an id was wrong.
   - Lost ownership: RESOURCE_NOT_OWNED from an owned tool is not evidence the terminal is missing. Status reads show whether it exists. Ask for a hand-over through Daintree or leave the action to the user.
   - Unknown (`source:"pty"`, `unavailableFields`, unreadable output): say exactly what is unknown.
3. Hung or slow: one bounded observation (`terminal.waitUntilIdle`, at most 60 seconds), then report the id, the screen and how long since output changed.
4. Interrupt only when the user authorized stopping this turn: `terminal.interruptOwned`. `requested` confirms a request, not a stop; read the terminal after. Respect a refusal.
5. Healthy but unobserved and working: do not restart it; arm one notice.

```json tool=terminal.notifyWhenIdle
{"terminalId":"<terminal id>","note":"recovered: check <task>"}
```

   `armed:false` means it was not working; classify its returned `state` now. Arming again replaces your earlier notice for that terminal. Another coordinator's notices are invisible to you, so never call an agent unsupervised because you hold none for it. End your turn.

On notice or resume: classify again with the shapes above, reading the reply from the notice quote. A recovered agent's result is still a claim, checked against the original task.

Stop and recovery: one corrective action; if it does not hold, stop and report the ids and evidence. A second relaunch is a loop. Never close a terminal to fix a failed launch, and keep anything a partial launch created.

Report back: the shape and its evidence; the terminal, worktree and launch details; what you recovered and what you deliberately did not do; what is still running and what will bring you back, or what you need from the user.
