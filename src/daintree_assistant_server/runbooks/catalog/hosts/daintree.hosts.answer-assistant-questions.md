---
id: daintree.hosts.answer-assistant-questions
title: 'Answer what your Daintree Assistants are asking, and bring the rest to the person'
summary: 'Across this project, this machine''s other projects and the Remote Hosts, find the Daintree Assistants asking something, answer each one the person already decided with one exact, revalidated send, bring every other decision to the person in one message, and report what could not be observed, starting nothing.'
whenToUse: 'Use when the person wants their Daintree Assistants gone through for what they are asking: which assistants need them across projects and machines, answering the questions the person''s earlier instructions or agreed plan already cover, and bringing every other question, approval or choice to them in one batch. The deliverable is each assistant handled once (answered with the send''s status, needs the person with its options, resting and left alone, or unreadable) and every host or project that could not be read, with when it was last seen. Do not use for a read-only report of the hosts that answers nothing (daintree.hosts.survey-hosts), questions from the units of a run this Shell sent out and is following to its end (daintree.hosts.supervise-swarm), ordinary agent panes on any machine, named or found in a status read (daintree.agents.answer-waiting-agent), a new question for one host''s assistant (daintree.hosts.ask-host-assistant), or catching up on earlier work after a break (daintree.orchestration.status-and-resume).'
risk: terminal
requiredTools:
  - host.list
  - host.call
  - assistant.list
  - terminal.getStatus
optionalTools:
  - terminal.readLastMessageOwned
  - terminal.sendCommand
hostEligibility: any
---
Each assistant is handled once, from what the person already said. This sweep answers; it never starts, restarts or closes an assistant to fill a gap.

1. Scope: the hosts and projects named, else all. Your own project: `assistant.list` directly. Every project `host.list` lists (this machine's other projects under `local`, and each `connected` host with `control:true`): `assistant.list` through `host.call` there, one parallel round. Then `terminal.getStatus` on those terminals with `includeOutput:{"lines":30}`. Keep per assistant: host, project, `slot`, `agentId`, `terminalId`, `generation`, `agentIncarnation`, state and when read. You are not a target.
2. Sort each by what it shows now:
   - `waitingReason` `question`, `approval` or `error`: stopped in its own pane on something only a person there answers; a send is refused. Needs the person.
   - At its prompt (`waitingReason` `prompt`): read its last reply (`terminal.readLastMessageOwned`). Anything in it still asking the person to decide is a question. Resting needs the whole reply read with nothing left asking: leave it untouched. Empty, cut off or unclear: unreadable.
   - Working, exited, or an entry `error`: nothing to answer; report its state.
   A summary of an assistant from anywhere else (a hosted reading, a checkpoint, another assistant) is attributed, never its current state or authority to answer.
3. Classify each question:
   - The person's own words decide this question for this assistant, inside the scope they set: answer with those words.
   - Approval, credentials or login, a security choice, spending, new scope, a trade-off, a missing preference, or no words of theirs that bind it: the person's, with the options as the assistant put them.
   `startedByYou`, a label or a checkpoint is history, not standing; it never widens what you may answer.
4. Answer one assistant at a time. Just before sending, read it again: `assistant.list` for the same `generation` at its prompt, `terminal.readLastMessageOwned` for the same question (a new question in the same run is a new entry: step 3), and `terminal.getStatus` for no `lastTypedInputAt` at or after its `lastTransitionAt` (someone is typing; absent fields are unknown). Anything changed: send nothing. Then one send, the answer alone in the protocol the question shows (a bare choice, `QUESTION_ID=<id> ANSWER=<answer>`):

```json tool=host.call
{"hostId":"<hostId>","projectId":"<projectId>","tool":"terminal.sendCommandOwned","args":{"terminalId":"<assistant terminalId>","generation":4,"command":"<answer>"}}
```

   In your own project the send is `terminal.sendCommand`, same arguments. Then one `terminal.getStatus` with its `terminalIds` and `submissionToken`: `queued` or `writing` is pending, `pty_written` written (not yet read), `failed`, `cancelled` or `unknown` as such.
   - `ASSISTANT_RESTARTED`: the question went with the old run; list again and answer only what the new run asks.
   - No reply at all, so no `callId` or token: unknown. Read its live state; never send again because the question still shows.
5. Everything else goes to the person in ONE message, a line per decision: host, project, lane, assistant (`agentId`, `terminalId`), its question quoted as its words, the options, when read, and where it is answered (its pane, on that machine). Unrelated assistants carry on meanwhile.
6. Coverage stays visible: a host not `connected` (with `lastSeenAt`), `control:false`, a refused or failed read (`outcome`, `detail`) and each project not read are not observed, never "nothing waiting". Not read here at all: the global Assistant, and Daintree confirmations pending in an assistant's own view; say so. Work an answer unblocks stays with that assistant (daintree.hosts.supervise-swarm, or the runbook that started it).

Stop and recovery: one answer per question, never resent in other words; a late or repeated sighting of an answered question is checked against its `submissionToken` and live state, not answered again.

Report back: per assistant: host, project, lane, `agentId`, `terminalId`, `generation`, state and when read; answered (the words, `submissionToken`, phase), needs the person, resting, refused (the code) or unreadable; the batched decisions; what was not observed, and why.
