---
id: daintree.agents.answer-waiting-agent
title: Answer a waiting agent
summary: Answer a waiting agent’s prompt — one agent, or a whole cohort asking at once — with the exact protocol reply, wait for them to continue, and report the requested results.
whenToUse: Use when one or more already-running visible agents/terminals are waiting on a QUESTION and the user wants them answered — "the agent is asking something, tell it yes", "it's blocked on a prompt", "three of them are waiting on questions, unblock them", "answer the waiting agents and report the result". Also covers an agent blocked on a permission or trust dialog rather than a typed question — waitingReason "approval" ("it wants me to approve something", "it's asking whether to trust the folder") — which needs the same move of reading what it wants and sending the answer. Includes the fan-out case where several ask at once (a wait or status read shows several at waitingReason "question" or "approval"). Do not use when the agent is asking nothing but is hung, silent, or behind a bad terminal id (daintree.agents.recover-agent), to change the course of a working agent that asked nothing (daintree.agents.steer-running-agent), to put the user's own question to an agent that already exists (daintree.agents.ask-running-agent), for a whole-fleet sweep where answering is only one of several actions (daintree.orchestration.supervise-fleet), to spawn a new agent to ask a question (daintree.agents.ask-and-relay or daintree.edits.spawn-visible-agent), or to poll agents for opinions, votes, or verdicts (daintree.agents.poll-pinned-agents).
risk: terminal
requiredTools:
  - terminal.list
  - terminal.getStatus
  - terminal.sendCommandOwned
optionalTools:
  - terminal.sendCommandMany
  - terminal.sendKeysOwned
  - terminal.getOutput
  - terminal.readLastMessageOwned
  - terminal.revealOwned
---
1. Find the targets with `terminal.list`, then one `terminal.getStatus` over the candidates with `includeOutput`. Several could be the ONE meant: list them and ask; never pick by focus or title. Several asking at once: handle them in one pass, each with its own answer.
2. Read each question from its own entry, never a merged summary (`terminal.getOutput` or, for a Claude Code pane you own, `terminal.readLastMessageOwned` for the options; only the live screen shows it is waiting now). A terminal that is not asking gets no input. Map target id to the exact question, any `QUESTION_ID` and the choices; mark one you cannot read "unreadable, not answered".
3. Classify each:
   - Answered by the user's words: use them exactly.
   - A follow-up inside the task: answer within scope, decline expansion.
   - Needs the user: a trade-off, a missing preference, new scope, a destructive or external step.
   - Approval, trust, login and security prompts stay with the user: answer one only on the user's explicit decision about that prompt. (The foundation's trust exception is for an agent you just launched.)
   - Not owned: do not send; ask for a hand-over or give the user the answer to type.
4. Confirm each is still at that question with no user draft, then send exactly the protocol answer: the bare token, `QUESTION_ID=<id> ANSWER=<answer>` when that protocol is shown, or for a dialog or menu `terminal.sendKeysOwned` with `choose` set to the option label. No `handback` on an answer; it appends text. Several terminals: one `terminal.sendCommandMany`, each its own answer.
5. Answer only: send without waiting and report the answers queued. Result wanted: add `waitForReply:true` and relay each reply as the agent's account.
6. Everything that needs the user goes to them in one message: terminal id, agent, exact question. "Show me the next one" is `terminal.revealOwned`.
7. A reply still showing the same question: resend only if your answer was malformed or missed the `QUESTION_ID`, otherwise report it blocked; never send variants. A new question is a new entry: classify it again.

One protocol-correct answer per question; one terminal's answer never goes to another, and no answer is reused across terminals. A disputed send is read by its `submissionToken`, never resent blind.

Report back: one line per terminal: id and agent, the question, the answer sent (or "needs you" and why), the send's status, and when requested the result or that it is still running.
