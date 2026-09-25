---
id: daintree.agents.answer-waiting-agent
title: Answer a waiting agent
summary: Answer a waiting agent’s prompt — one agent, or a whole cohort asking at once — with the exact protocol reply, wait for them to continue, and report the requested results.
whenToUse: Use when one or more already-running visible agents/terminals are waiting on a QUESTION and the user wants them answered — "the agent is asking something, tell it yes", "it's blocked on a prompt", "three of them are waiting on questions, unblock them", "answer the waiting agents and report the result". Also covers an agent blocked on a permission or trust dialog rather than a typed question — waitingReason "approval" ("it wants me to approve something", "it's asking whether to trust the folder") — which needs the same move of reading what it wants and sending the answer. Includes the fan-out case where several ask at once (a wait or status read shows several at waitingReason "question" or "approval"). Do not use when the agent is asking nothing but is hung, silent, or behind a bad terminal id (daintree.agents.recover-agent), for a whole-fleet sweep where answering is only one of several actions (daintree.orchestration.supervise-fleet), to spawn a new agent to ask a question (daintree.agents.ask-and-relay or daintree.edits.spawn-visible-agent), or to poll agents for opinions, votes, or verdicts (daintree.agents.poll-pinned-agents).
risk: terminal
requiredTools:
  - terminal.list
  - terminal.getStatus
  - terminal.getOutput
  - terminal.sendCommandOwned
optionalTools:
  - terminal.readLastMessageOwned
  - terminal.sendCommand
  - terminal.revealOwned
  - terminal.waitUntilIdleBatch
---
Use when: one or more running agents are waiting on a question or prompt, and the user says what to answer (or to answer them) and what, if anything, to report afterwards.

Availability: Core. Answers go out with `terminal.sendCommandOwned`, which reaches only terminals this connection launched or the user handed to it through Daintree; the sidebar assistant uses `terminal.sendCommand`. Reads work on any terminal. `notify` on the send brings you back after the agent continues; an API-key client has none, so it gets one bounded wait or the user's continuation.

Procedure:
1. Find the targets with `terminal.list`, then one `terminal.getStatus` over the candidates with `includeOutput` (up to 50 lines each). Tell the two multi-terminal cases apart:
   - Ambiguity: several terminals could be the ONE the user means. List them and ask. Never guess, and never pick one for being focused or having the right title.
   - Cohort: several are asking at once and the user wants them answered. Handle them in one pass, each with its own answer.
2. Read each terminal's question from its own entry, never from a merged summary. When the tail is not enough, `terminal.getOutput` for that terminal with a bounded `maxLines`. For a Claude Code pane you own, `terminal.readLastMessageOwned` lists `unansweredToolUses` with a question's options, but only the live screen shows that it is waiting now, and permission dialogs appear only there. `waitingReason:"prompt"` is also the fallback reading: confirm on screen that something is being asked. A terminal that is not asking gets no input; report its state and ask whether to send anyway.
3. Build the terminal-id-to-question map locally: exactly one entry per target id from the listing, each from that terminal's own read, holding the exact question, any `QUESTION_ID` and the choices shown. No unknown or duplicate ids, no empty questions. Re-read an entry you doubt, and mark one you cannot read "unreadable, not answered". Route by id only, never by title, position or an id printed in output.
4. Classify each entry:
   - Answered by the user's explicit words or the original request: use that answer exactly.
   - A follow-up inside the existing task: tests that prove the requested behaviour are in scope; an unrelated migration or refactor is not. Answer within scope and decline the expansion.
   - Needs the user: a real trade-off, a missing preference, new scope, or a destructive or external action beyond the user's words.
   - An approval, trust, login, credential or security prompt: never answer it on inference or to keep a workflow moving. Only the user's explicit decision about that specific prompt authorizes a reply, and only when the screen shows the exact key or text it accepts; otherwise it stays with the user.
   - Not owned or handed over: do not send. It needs a hand-over through Daintree or the user's own answer. `RESOURCE_NOT_OWNED` does not tell you whether the terminal exists.
5. Just before each send, the terminal must still be at that question: re-read it if time has passed. Never type over a draft the user started (`lastTypedInputAt` later than `lastTransitionAt`) or into a `directing` agent.
6. Send exactly the protocol answer, one send per terminal, each carrying that terminal's own answer from the map. Never reuse one agent's answer for another. The send submits text with one Enter: the bare token a text prompt accepts, the key a menu shows, or `QUESTION_ID=<id> ANSWER=<answer>` only when that protocol is displayed. No commentary unless the agent asked for detail, and no `handback`, which appends Daintree's instruction text to your answer. It cannot press arrows, Tab or Escape; hand those to the user.

```json tool=terminal.sendCommandOwned
{"terminalId":"<id from the map>","command":"<that terminal's protocol answer>"}
```

   Keep each `submissionToken` against its terminal id.
7. Stop or continue by what was asked:
   - Answer only: report the answers queued and stop. No notice, no wait.
   - The user also wants the resulting work or its result: send with a new notice (the one that brought you the question has already fired), save the map and tokens as a checkpoint, and end your turn. A send's notice counts only what happens after your answer is written, so it reports this answer's outcome, not the earlier stop.

```json tool=terminal.sendCommandOwned
{"terminalId":"<id from the map>","command":"<that terminal's protocol answer>","notify":true}
```

   Without `notify` (API-key client), one `terminal.waitUntilIdleBatch` over the answered ids, then report who is still working.
8. Everything that needs the user goes to them in one message: terminal id, agent and the exact question. Do not answer on their behalf.
9. "Show me the next one" is navigation, not an answer: `terminal.revealOwned` for a panel you own, otherwise name the panel.

On notice or resume:
1. Follow the foundation's resume procedure with the saved map. Read `terminal.getStatus` with each terminal's `submissionToken`. `failed`, `cancelled` or `unknown` may have left text in the input: look at the screen before anything else, and never resend blind.
2. The same question still showing after one correctly delivered answer: read it once more. Resend only if your first answer was malformed or missed the `QUESTION_ID`; otherwise report that terminal blocked. Never send variants.
3. A new question: classify it as above, and answer once within scope with a fresh `notify` or take it to the user.
4. Finished: read the requested result from that terminal (`terminal.readLastMessageOwned` for Claude Code, else `terminal.getOutput`) and report it as the agent's account unless you checked it yourself.

Stop and recovery: one protocol-correct answer per question; one terminal's answer never goes to another; approval, trust, login and security prompts stay with the user. An unreadable or unowned target is reported, not worked around.

Report back: one line per terminal: id and agent, the question, the answer sent (or "needs you" and why), the send's status (queued, written or disputed), and when requested, the result or that it is still running with a notice pending.
