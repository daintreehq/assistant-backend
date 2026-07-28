---
id: daintree.agents.answer-waiting-agent
title: Answer a waiting agent
summary: Answer an existing waiting agent’s prompt, wait for it to continue, and extract the requested final result.
whenToUse: Use when the user says an already-running visible agent or terminal is asking/waiting for input, tells you what answer to send, and asks you to wait for completion or report a specific result. Do not use to spawn a new agent to ask a question (use the ask-and-relay or spawn-visible-agent skill), to poll several agents (use poll-pinned-agents), or for general Daintree status only (use orchestration basic).
priority: 180
risk: terminal
tags:
  - daintree
  - agent
  - terminal
  - waiting
  - question
  - answer
  - send-command
  - extract
  - result
requiredTools:
  - context.snapshot
  - terminal.extract
  - terminal.sendCommand
  - terminal.awaitAll
  - terminal.await.async
  - terminal.summarize
  - terminal.read
  - queue.resolve
---
Use when: an already-running Daintree agent/terminal is waiting for user input and the user tells you what to answer and what final result to report.

Procedure:
1. Read current state with context.snapshot. Identify the target terminal from the user’s words: terminal id, agent title/name, task, worktree, or visible waiting/asking state. If more than one terminal plausibly matches, stop and ask the user to choose. Do not spawn a new agent or commandeer an unrelated existing terminal.
2. Verify the prompt before replying. Use terminal.extract on the target terminal to return the exact current question/prompt. If the terminal is not actually asking a question, summarize/read the status and ask whether to still send input. Do not send blind text just because the terminal title matches.
3. Use the user-provided answer exactly when it is explicit. If the answer is missing, ambiguous, or would authorize destructive/external/high-risk action beyond the user’s words, pause for confirmation. Do not ask again for a clearly provided safe answer.
4. Send one input line to that terminal with terminal.sendCommand, e.g. `terminal.sendCommand({"terminalId":"<terminalId>","command":"<exact answer>"})`. Send the answer only, not explanatory commentary, unless the agent explicitly requested more detail. Do not use terminal.run, spawnForEdits, or a raw daintree.call for this pattern.
5. Wait according to the user’s request. The wait is an ARGUMENT, not an instruction — putting “wait until the agent finishes” in the `instruction` text does nothing; without a `wait` argument the call is a single tail read of an agent that has barely started, and `maxAttempts` is inert. If they asked for a specific final field/result, use ONE targeted extract that actually polls: `terminal.extract({"terminalIds":["<terminalId>"],"wait":{},"instruction":"return only <requested field>","maxAttempts":60})` — `wait:{}` is the settled default and is single-terminal only. If they asked generally for completion, use terminal.awaitAll on the same terminal and then summarize/extract the final output.
6. Keep the wait bounded. If extract/await reports no match or still working, re-wait the same terminal at most twice, raising maxAttempts for a known-slow operation rather than hand-polling terminal.read in a loop. If it still has not finished, hand it off with `terminal.await.async({"terminalIds":["<terminalId>"],"title":"waiting agent completion"})`, tell the user it is still running, and end the turn.
7. If the agent asks a follow-up question during the wait, extract/read the follow-up. Answer only if the user already authorized that exact information; otherwise ask the user. Do not invent operational choices.
8. When output is available, use terminal.extract for the requested value, terminal.summarize for a concise deploy/task summary, and terminal.read only when exact literal wording is needed or extraction clearly missed essential content. Verify the result came from the target terminal.
9. If this began from an inbox/attention item for the waiting agent, resolve it with queue.resolve after you have answered and reported the result. Leave it open if the agent still needs the user.

Confirmation: sending input to a waiting agent mutates terminal/agent state and may trigger the agent’s next project or external action; confirm before sending unless the user already explicitly told you the answer and the active tier permits.

Report back: the target terminal id/name, the answer you sent, whether the agent finished or is still running, and the requested final value/result or async checkpoint.
