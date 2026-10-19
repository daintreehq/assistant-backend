---
id: daintree.agents.ask-running-agent
title: Ask an existing agent a question
summary: Get an answer from an agent that is already open because it holds the context — read it from what the agent already said when it is there, otherwise ask once at its prompt without derailing its task — and relay it with attribution.
whenToUse: 'Use when the user wants an answer FROM an agent that already exists, because that agent holds the context: "ask the Claude doing the auth refactor why it chose JWT", "what did the codex agent decide about the schema?", "what did it say last?", "ask terminal 4 whether it ran the tests", "ask the implementer what is left to do". The deliverable is that agent''s own answer, quoted with attribution, with its task left running. Do not use to launch a NEW agent to answer a question (daintree.agents.ask-and-relay), to answer a question the agent is asking you (daintree.agents.answer-waiting-agent), to change what it is doing (daintree.agents.steer-running-agent), to poll freshly launched agents on one question (daintree.agents.poll-pinned-agents), for a state sweep of the whole fleet (daintree.orchestration.supervise-fleet), or to check whether its claimed work is actually true (daintree.orchestration.verify-agent-work).'
risk: terminal
requiredTools:
  - terminal.getStatus
  - terminal.sendCommandOwned
optionalTools:
  - terminal.list
  - terminal.readLastMessageOwned
  - terminal.getOutput
  - terminal.notifyWhenIdle
---
The point is this agent's context: never launch a fresh agent to answer instead. Asking costs it a turn, so read what it already said first.

On a host: call these tools on the agent's machine.

Procedure:
1. Resolve the terminal: an id you hold, or `terminal.list`; several plausible matches: list them and ask, never pick by focus or title. Several open agents named: run this procedure for each. One `terminal.getStatus` with `includeOutput:{"lines":40}`.
2. From the record, sending nothing, whenever the question is about something it already said or did ("what did it say last", "what did it decide", "did it run the tests"):
   - `terminal.readLastMessageOwned` on a Claude Code pane you own or were handed (`messageIndex` 1, 2… for earlier replies; `message.nextCursor` as `cursor` for the start of a long one). `status:"unavailable"`, another agent, or not yours: `terminal.getOutput` with `maxLines` about 200 (screen reads work on any agent terminal).
   - Found: relay it, citing the source, and stop here. `lastCheckResult` is a best-effort parse of its output: cite it as that, never as a pass.
   - Not found, and the user wanted only what it said earlier: report the history as unavailable or partial and stop. A fresh answer is different evidence; ask only if the user wants the question put to it.
3. Asking needs a terminal you own or were handed; otherwise give the user the question to type, or ask for a hand-over. Then by state:
   - `working`: do not interrupt for a question unless the user said it cannot wait (then interrupt and confirm the stop as daintree.agents.steer-running-agent step 3 does, and go to step 4; step 5 resumes its task). Otherwise arm a notice and end your turn; step 4 runs on it.

```json tool=terminal.notifyWhenIdle
{"terminalId":"<agent terminal id>","note":"ask it: <the question, short>"}
```

   `armed:false`: it has already stopped; read its returned `state` and continue.
   - `waiting` on a question or approval: it is asking you. Deal with that first (daintree.agents.answer-waiting-agent); never stack a question on an open one.
   - Idle at its input line: step 4.
4. Ask once with exactly this command, only the question filled in (its no-changes line keeps the agent's task and files safe); first read its state as step 1 did, never from `terminal.list` alone:

```json tool=terminal.sendCommandOwned
{"terminalId":"<agent terminal id>","command":"Question from the user: <question verbatim>. Answer from what you already know or can read. Do not change files, run anything that changes state, or start other work in this reply. At most 20 lines; say what you are unsure of.","waitForReply":true,"handback":true,"waitSeconds":180}
```

5. Read the result's `reply`: `outcome` `handback` or `settled` gives the candidate answer at `reply.reply.text` (`reply.reply.truncated`: read the rest with `terminal.readLastMessageOwned`, or relay it as partial). Relay it only if it answers the question. `timeout`: still answering; recheck, never send the question again. `exited` or `closed`, or a `state`/`waitingReason` showing it asking something: report that instead of an answer. Files changed in answer to it are a boundary failure: report them. If you interrupted its task in step 3, now send one separate "Resume the task you were doing before my question, where you stopped." with `notify:true, handback:true`.
6. Relay with attribution, keeping its qualifications. What it says it did ("tests pass") is its claim: offer daintree.orchestration.verify-agent-work.

On notice or resume: the notice means it stopped; read its state from the quote. At its prompt: step 4. It stopped on a question of its own: step 3's `waiting` case. Your question was already sent (you hold its `submissionToken`): read the answer, never send again.

Stop and recovery: at most one question per agent per request; one armed notice; one resume after an interrupt. Nothing here closes, restarts or re-briefs the agent.

Report back: agent and terminal id; where the answer came from (its transcript, its screen, or a new reply) and when; the answer quoted or condensed faithfully, marked partial if cut; whether you waited for its turn to end, or interrupted it and whether its resume was sent; what it claims that nobody verified.
