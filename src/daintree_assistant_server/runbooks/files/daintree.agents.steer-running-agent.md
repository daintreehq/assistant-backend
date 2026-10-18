---
id: daintree.agents.steer-running-agent
title: Steer or stop a running agent
summary: Change the course of an agent that is already working — stop its turn when the change cannot wait, confirm the stop from its screen, send one correction that keeps the rest of its task, and check that it took the new course.
whenToUse: 'Use when an agent that is ALREADY WORKING on a task, or paused mid-task at its prompt, must change course or stop: corrected, redirected, given an extra requirement, or told to stop what it is doing — "tell Claude to stop touching the migrations", "it''s going the wrong way, make it use the existing helper", "interrupt Codex and tell it to use pnpm", "also make it keep the old API working", "stop that agent''s turn but keep it open", or one correction sent to several running agents. The deliverable is each agent stopped, or working on the corrected course, confirmed from its own screen and reply. Do not use when the agent is ASKING a question or waiting on an approval (daintree.agents.answer-waiting-agent), is hung, silent or its launch is unclear (daintree.agents.recover-agent), to forward a reviewer''s findings (daintree.agents.relay-review-feedback), to ask it something without changing its work (daintree.agents.ask-running-agent), to give the task to a different agent (daintree.agents.hand-off-task), to run one of its CLI commands such as /compact (daintree.agents.run-agent-command), to bring agents to a resumable stop for the day (daintree.orchestration.park-work), to close panels (daintree.orchestration.workspace-cleanup), to move it to another worktree (daintree.worktree.relocate-agents), to re-plan several agents around newly urgent work (daintree.orchestration.reprioritize-work), to settle which of two agents duplicating one task keeps it (daintree.orchestration.resolve-task-collisions), or to take back changes it already made (daintree.git.undo-agent-changes).'
risk: terminal
requiredTools:
  - terminal.getStatus
  - terminal.interruptOwned
  - terminal.sendCommandOwned
optionalTools:
  - terminal.list
  - terminal.waitUntilIdle
  - terminal.notifyWhenIdle
  - terminal.sendCommandMany
---
A send is typed into the agent's input at once but never stops its turn; whether the CLI reads it mid-turn is up to the CLI. A stop, or a change that cannot wait, needs `terminal.interruptOwned` first (Escape; the conversation is kept).

Procedure:
1. Targets: the terminal ids you hold, else `terminal.list` with `owned:true`; several plausible matches for one agent: ask. One `terminal.getStatus` over them with `includeOutput:{"lines":40}`. Classify each:
   - `working`: step 2.
   - Idle at its input line: already stopped. Stop-only request: report that and finish; otherwise step 4.
   - `waiting` on a question or approval: daintree.agents.answer-waiting-agent.
   - Exited, an error screen, or unresponsive: daintree.agents.recover-agent.
   - Not yours: touch nothing; give the user the correction to type after pressing Escape in that pane, or ask for a hand-over.
2. Now or later, from the user's words; say which you chose. "Stop", "not that", "it's rewriting the whole file", "change its task": interrupt now (step 3). "Also", "afterwards", "when you get to it": no interrupt; arm `terminal.notifyWhenIdle` with note "steer: <gist>" and end your turn, sending step 4 on that notice (`armed:false`: it already stopped, so step 4 now). A bare "stop" with nothing to say next: step 3, then finish without sending.
3. Interrupt each working agent once, then confirm, in this order; no correction goes out before (c):
   a. Interrupt:

```json tool=terminal.interruptOwned
{"terminalId":"<agent terminal id>"}
```

   b. `terminal.waitUntilIdle` with `timeoutMs:15000`: `requested` or `requested-unverified` is a request, not a stop.
   c. `terminal.getStatus` with `includeOutput:{"lines":30}`. Stopped means the screen shows the turn interrupted and an empty input line, not a spinner; Claude Code's "Interrupted · What should Claude do instead?" is that stop, even when it reads as `waitingReason:"question"`. `working` or a spinner: the stop is unconfirmed; report it, send no correction, never interrupt again.
   d. `git -C "<its worktree path>" status --short`. Revert nothing; name the changed files in the correction for it to re-read.
   - Refused as not mid-turn: that state reading lags, so read the screen again. Idle at an input line: step 4 (stop-only: finish). Still working: report it and send nothing. Refused because its CLI stops on Ctrl+C (Goose) or its input is locked: nothing was sent; the user stops it from its pane. Never substitute `terminal.sendKeysOwned` Escape or Ctrl+C: a stray Escape at an idle Claude prompt opens its rewind menu, and Ctrl+C can quit the CLI.
4. One correction per agent, short and complete: what changes (the user's words); what stays (the rest of its task still applies); what to undo, only if the user said so; files to re-read; "First state in one line what you will now do differently, then continue with the rest of the task" (drop "continue" when the user wants it to stop after). Without the "rest of the task" line an agent treats the correction as its whole new task and drops its plan. Send with `notify:true, handback:true`: `terminal.sendCommandOwned` for one, `terminal.sendCommandMany` for several (one call).

```json tool=terminal.sendCommandOwned
{"terminalId":"<agent terminal id>","command":"Correction from the user: use the existing retryWithBackoff helper instead of writing a new retry loop. Everything else in your task still applies. src/net/client.ts was left mid-edit when you were stopped: re-read it before continuing. First state in one line what you will now do differently, then continue with the rest of the task.","notify":true,"handback":true}
```

5. Keep each `submissionToken` (delivery in doubt: as the foundation says). End the turn.

On notice or resume: the notice armed in step 2 carries no reply: send step 4. After the correction, judge only output that follows it: its one-line statement plus work on the new course means the steer landed; the task's result is still a claim ("it reports 15 passing"), verified as the runbook that started the work says, never relayed as fact. No statement yet is not a miss. Work that continues the old course after the correction: one restated correction naming exactly what it did against it, with `notify`; a second miss goes to the user. A question: answer it as daintree.agents.answer-waiting-agent.

Stop and recovery: one interrupt, one correction and at most one restatement per agent per request. Never close, restart or relaunch an agent to change its course; a different agent taking over is daintree.agents.hand-off-task.

Report back: per agent, the terminal id; interrupted or not, and how the stop was confirmed or that it is unconfirmed; changed files found; the correction's gist and delivery; its acknowledgment or the notice outcome; what will bring you back.
