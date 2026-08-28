---
id: daintree.agents.answer-waiting-agent
title: Answer a waiting agent
summary: Answer a waiting agent’s prompt — one agent, or a whole cohort asking at once — with the exact protocol reply, wait for them to continue, and report the requested results.
whenToUse: Use when one or more already-running visible agents/terminals are waiting on a QUESTION and the user wants them answered — "the agent is asking something, tell it yes", "it's blocked on a prompt", "three of them are waiting on questions, unblock them", "answer the waiting agents and report the result". Also covers an agent blocked on a permission or trust dialog rather than a typed question — waitingReason "approval" ("it wants me to approve something", "it's asking whether to trust the folder") — which needs the same move of reading what it wants and sending the answer. Includes the fan-out case where several ask at once (terminal.awaitAll returns an askingQuestion array, or the roster shows waitingReason "question" or "approval"). Do not use when the agent is asking nothing but is hung, silent, or behind a bad terminal id (daintree.agents.recover-agent), for a whole-fleet sweep where answering is only one of several actions (daintree.orchestration.supervise-fleet), to spawn a new agent to ask a question (daintree.agents.ask-and-relay or daintree.edits.spawn-visible-agent), or to poll agents for opinions, votes, or verdicts (daintree.agents.poll-pinned-agents).
opener: "Waiting agents — a question to answer exactly, not a hang"
risk: terminal
requiredTools:
  - context.snapshot
  - terminal.extract
  - terminal.extract.json
  - terminal.sendCommand
  - terminal.awaitAll
  - terminal.await.async
  - terminal.summarize
  - terminal.read
  - terminal.focus
  - queue.resolve
---
Use when: one or more already-running Daintree agents/terminals are waiting for user input and the user tells you what to answer and what to report back.

Procedure:
1. Read current state with context.snapshot. Identify the target from the user’s words: terminal id, agent title/name, task, worktree, or visible waiting/asking state. Then separate the two multi-terminal cases — confusing them sends an answer into the wrong agent’s live work:
   - AMBIGUITY → still stop and ask. Several terminals might be the ONE you mean. List the candidates and ask the user to choose. Do not guess, do not spawn a new agent, do not commandeer an unrelated terminal.
   - COHORT → answer them. Several terminals are all ASKING: terminal.awaitAll listed them in its top-level `askingQuestion` array, or the roster shows several at agentState "waiting" with waitingReason "question" or "approval". After a fan-out that is routine, not ambiguity — use the Cohort branch below instead of running steps 2-8 once per agent.
2. Verify the prompt before replying. Use terminal.extract on the target terminal to return the exact current question/prompt. If the terminal is not actually asking a question, summarize/read the status and ask whether to still send input. Do not send blind text just because the terminal title matches.
3. Use the user-provided answer exactly when it is explicit. If the answer is missing, ambiguous, or would authorize destructive/external/high-risk action beyond the user’s words, pause for confirmation. Do not ask again for a clearly provided safe answer.
4. Send one input line to that terminal with terminal.sendCommand, e.g. `terminal.sendCommand({"terminalId":"<terminalId>","command":"<exact answer>"})`. Send the answer only, not explanatory commentary, unless the agent explicitly requested more detail. Do not use terminal.run, spawnForEdits, or a raw daintree.call for this pattern.
5. Wait according to the user’s request. The wait is an ARGUMENT, not an instruction — putting “wait until the agent finishes” in the `instruction` text does nothing; without a `wait` argument the call is a single tail read of an agent that has barely started, and `maxAttempts` is inert. If they asked for a specific final field/result, use ONE targeted extract that actually polls: `terminal.extract({"terminalIds":["<terminalId>"],"wait":{},"instruction":"return only <requested field>","maxAttempts":60})` — `wait:{}` is the settled default and is single-terminal only. If they asked generally for completion, use terminal.awaitAll on the same terminal and then summarize/extract the final output.
6. Keep the wait bounded. If extract/await reports no match or still working, re-wait the same terminal at most twice, raising maxAttempts for a known-slow operation rather than hand-polling terminal.read in a loop. If it still has not finished, hand it off with `terminal.await.async({"terminalIds":["<terminalId>"],"title":"waiting agent completion"})`, tell the user it is still running, and end the turn.
7. If the agent asks a follow-up question during the wait, extract/read the follow-up. Answer only if the user already authorized that exact information; otherwise ask the user. Do not invent operational choices.
8. When output is available, use terminal.extract for the requested value, terminal.summarize for a concise deploy/task summary, and terminal.read only when exact literal wording is needed or extraction clearly missed essential content. Verify the result came from the target terminal.
9. If this began from an inbox/attention item for the waiting agent, resolve it with queue.resolve after you have answered and reported the result. Leave it open if the agent still needs the user.

Cohort branch — two or more terminals are ASKING at once (from step 1). Answer them in one pass, never six serial round-trips:
   a. Read every question in ONE `terminal.extract.json` over the cohort, with an array schema keyed by terminalId. WHY: a plain-text `terminal.extract` over several terminalIds MERGES their tails into ONE answer, so it cannot attribute questions. Shape: `terminal.extract.json({"terminalIds":["<id1>","<id2>"],"instruction":"For EACH terminal, its exact pending question verbatim, including any QUESTION_ID shown.","jsonSchema":"{\"type\":\"object\",\"properties\":{\"questions\":{\"type\":\"array\",\"items\":{\"type\":\"object\",\"properties\":{\"terminalId\":{\"type\":\"string\"},\"question\":{\"type\":\"string\"}},\"required\":[\"terminalId\",\"question\"]}}},\"required\":[\"questions\"]}"})` — jsonSchema is a real JSON Schema passed as a string. This is the ONE legitimate structured read of a cohort: it IDENTIFIES who asked what so replies route correctly; it does not judge agents’ answers. Opinions, votes, verdicts, and drafts stay free-form via terminal.read — the no-JSON rule in the poll, competition, and multi-agent runbooks stands.
   b. Classify each question: (i) answered by the user’s original request — use that; (ii) adjacent optional work nobody asked for (“shall I also update the tests?”) — default “no, stay in scope”; (iii) genuinely needs the user — a real trade-off, a destructive/external action, or scope the user never spoke to.
   c. Batch the replies in ONE turn: one `terminal.sendCommand({"terminalId":"<id>","command":"<protocol answer>"})` per (i)/(ii) terminal, each carrying that terminal’s own answer matched by terminalId from (a) — never reuse one agent’s answer for another. Send the exact protocol answer, never prose: a bare lowercase `yes`/`no` when the prompt shows no id, else the single line `QUESTION_ID=<id> ANSWER=<yes|no|short answer>` (the protocol in the spawn-visible-agent runbook).
   d. Re-await the cohort ONCE with a real budget — one `terminal.awaitAll({"terminalIds":[...],"maxAttempts":60})` over the ids you answered, never one wait per agent — then peek each tail with one bounded `terminal.read` per terminal (a no-wait plain-text extract over the cohort merges them again): idle is not done. Re-await stragglers at most twice, then hand them to `terminal.await.async` and end the turn.
   e. For (iii), ask the user ONCE for all of them together — terminal id, agent name, question — not one round-trip per agent. Do not answer them on the agents’ behalf.
   f. queue.resolve only the items you actually closed; leave open every item whose agent still needs the user.
   g. "Show me the next one" is navigation, not an answer: pick the still-waiting terminal out of the roster you already read and `terminal.focus({"terminalId":"<id>"})` it. UI only — it answers nothing and changes no state.

Confirmation: sending input mutates terminal/agent state and may trigger that agent’s next project or external action; confirm before sending — batched cohort sends included — unless the user already explicitly told you the answer and the active tier permits.

Report back: for a single agent, the terminal id/name, the answer sent, whether it finished or is still running, and the requested value/result or async checkpoint. For a cohort, one line per terminal — id/name, the question, the answer sent (or “needs you”) — plus who is still working and which inbox items you resolved.
