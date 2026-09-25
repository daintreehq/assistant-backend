---
id: daintree.agents.answer-waiting-agent
title: Answer a waiting agent
summary: Answer a waiting agent’s prompt — one agent, or a whole cohort asking at once — with the exact protocol reply, wait for them to continue, and report the requested results.
whenToUse: Use when one or more already-running visible agents/terminals are waiting on a QUESTION and the user wants them answered — "the agent is asking something, tell it yes", "it's blocked on a prompt", "three of them are waiting on questions, unblock them", "answer the waiting agents and report the result". Also covers an agent blocked on a permission or trust dialog rather than a typed question — waitingReason "approval" ("it wants me to approve something", "it's asking whether to trust the folder") — which needs the same move of reading what it wants and sending the answer. Includes the fan-out case where several ask at once (a wait or status read shows several at waitingReason "question" or "approval"). Do not use when the agent is asking nothing but is hung, silent, or behind a bad terminal id (daintree.agents.recover-agent), for a whole-fleet sweep where answering is only one of several actions (daintree.orchestration.supervise-fleet), to spawn a new agent to ask a question (daintree.agents.ask-and-relay or daintree.edits.spawn-visible-agent), or to poll agents for opinions, votes, or verdicts (daintree.agents.poll-pinned-agents).
risk: terminal
requiredTools:
  - terminal.list
  - terminal.getStatus
  - terminal.sendCommandOwned
optionalTools:
  - terminal.getOutput
  - terminal.readLastMessageOwned
  - terminal.revealOwned
  - terminal.waitUntilIdleBatch
---
Use when: one or more running agents are waiting on a question or prompt, and the user says what to answer (or to answer them) and what, if anything, to report afterwards.

Availability: Core. Reads work on any terminal; the send needs a terminal you launched or were handed. An API-key client has no `notify`: collecting a result there is one `terminal.waitUntilIdleBatch`, or the user's continuation.

Procedure:
1. Find the targets with `terminal.list`, then one `terminal.getStatus` over the candidates with `includeOutput` (up to 50 lines each).
   - Ambiguity (several could be the ONE meant): list them and ask. Never pick by focus or title.
   - Cohort (several asking at once): handle them in one pass, each with its own answer.
2. Read each terminal's question from its own entry, never a merged summary; `terminal.getOutput` with a bounded `maxLines` when the tail is not enough. For a Claude Code pane you own, `terminal.readLastMessageOwned` shows a question's options, but only the live screen shows it is waiting now. A terminal that is not asking gets no input.
3. Build a local map, one entry per target id: the exact question, any `QUESTION_ID` and the choices shown. Mark one you cannot read "unreadable, not answered". Route by id only.
4. Classify each entry:
   - Answered by the user's words: use them exactly.
   - A follow-up inside the task: answer within scope and decline expansion.
   - Needs the user: a trade-off, a missing preference, new scope, a destructive or external step.
   - Approval, trust, login and security prompts stay with the user: answer one only on the user's explicit decision about that prompt, with the exact key the screen shows. (The trust exception in the foundation is for the agent you just launched, handled by the launching runbook.)
   - Not owned: do not send; ask for a hand-over or give the user the answer to type.
5. Choose the mode before sending: answer only, or answer and collect the result. Then make ONE send per terminal, just after confirming it is still at that question and holds no user draft. Send exactly the protocol answer (the bare token, the key a menu shows, or `QUESTION_ID=<id> ANSWER=<answer>` when that protocol is displayed), never `handback`, which appends text to the answer. The send cannot press arrows, Tab or Escape; hand those to the user.
   - Answer only: no notice. Report the answers queued and stop.

```json tool=terminal.sendCommandOwned
{"terminalId":"<id from the map>","command":"<that terminal's protocol answer>"}
```

   - Collect the result: the same send with `notify` (the notice that brought the question has fired), then end your turn.

```json tool=terminal.sendCommandOwned
{"terminalId":"<id from the map>","command":"<that terminal's protocol answer>","notify":true}
```

6. Everything that needs the user goes to them in one message: terminal id, agent and the exact question. "Show me the next one" is navigation: `terminal.revealOwned`.

On notice or resume:
1. The notice quotes each terminal's screen after your answer. The notice only wakes you: match it to the question you answered in that terminal, and if the quote still shows the same question, read it once more. Resend only if your answer was malformed or missed the `QUESTION_ID`; otherwise report that terminal blocked. Never send variants.
2. A new question is a new entry: classify it, answer once with a fresh `notify` or take it to the user. The original task stays open.
3. Finished: relay the requested result from the quote (read again only if it is missing or cut) as the agent's account unless you checked it.

Stop and recovery: one protocol-correct answer per question; one terminal's answer never goes to another, and no answer is reused across terminals. A disputed send is read by its `submissionToken` and the screen, never resent blind. An unreadable or unowned target is reported, not worked around.

Report back: one line per terminal: id and agent, the question, the answer sent (or "needs you" and why), the send's status, and when requested, the result or that it is still running with a notice pending.
