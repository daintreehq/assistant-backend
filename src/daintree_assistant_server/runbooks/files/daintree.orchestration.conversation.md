---
id: daintree.orchestration.conversation
title: Facilitate a live agent conversation
summary: Relay a live back-and-forth conversation among two or more agents, giving each the floor in turn and catching it up on what the others said since it last spoke while you drive the rounds.
whenToUse: Use when the user asks you to facilitate, host, relay, moderate, or keep going a live back-and-forth conversation between TWO OR MORE agents — “get them talking,” “let them talk to each other,” “facilitate a conversation,” “have a group chat/roundtable/panel discussion,” “have the three of them hash it out,” or “see where it goes.” Participant count is just a parameter of this runbook. Do not use for a one-shot question to one agent (use daintree.agents.ask-and-relay), an independent poll with no cross-talk (use daintree.agents.poll-pinned-agents), a judged interview that names a winner (use daintree.orchestration.interview), a scored competition or vote (use daintree.orchestration.competition), a post-tie runoff (use daintree.orchestration.runoff-tiebreaker), a debate that ends by voting a position off (use daintree.orchestration.debate-elimination), or several agents collaborating toward ONE synthesized answer or convergence rather than an open-ended visible dialogue (use daintree.orchestration.multi-agent).
risk: project
requiredTools:
  - agent.listAvailable
  - agent.launch
  - terminal.sendCommandOwned
  - terminal.getStatus
  - terminal.getOutput
optionalTools:
  - agent.listPresets
  - worktree.list
  - terminal.list
  - terminal.readLastMessageOwned
  - terminal.waitUntilIdle
  - terminal.interruptOwned
  - terminal.sendCommand
  - terminal.closeOwned
  - terminal.close
---
Use when: the user wants two or more agents to hold a live, open-ended conversation (a back-and-forth, roundtable, panel or group chat) and wants you to facilitate the turns by relaying between their terminals.

Availability: Core. In an agent pane, `notify` on each turn's send brings the speaker's reply back to you. An API-key client has no `notify`: give each turn one bounded `terminal.waitUntilIdle`, and tell the user a turn still running needs their continuation. The sidebar assistant uses `terminal.sendCommand` for the send.

You are the only conduit and a facilitator, not a participant: relay, condense, report. Never add a position of your own or do a participant's work. Every send makes that agent take a turn, so the floor goes to ONE speaker at a time. Never send an "update" to an agent you are not asking to speak.

Procedure:
1. Scope: participants, topic or opener, tone, the speaking order and a stop condition. Two participants simply alternate. Three or more go round-robin by default; the user may want a fixed sequence, an order you moderate, or "whoever was addressed answers next". With no stop condition given, set one (two full laps, then a progress report); "keep going until I stop you" still gets periodic reports.
2. Participants. Named open agents are used as they are, with no replacements; the owned send reaches only panels you launched or the user handed you, so ask for a hand-over otherwise. New participants are resolved to real, distinct `agentId`s in one worktree.
3. Checkpoint (the foundation's), adding: the speaking order and current speaker, the turn number, the stop condition, a compact public log (speaker, turn, message or condensed message), and per participant its terminal id, the last log entry it has seen, and the turn and `submissionToken` of any pending reply. Keep each speaker's `[Operator]` notes apart from the public log.
4. Launch without a duplicate opener. Only the first speaker gets a `prompt`: its role brief plus the opener. Every other participant launches with no `prompt`, so it waits silently, and its brief rides on its first turn. (If the user wants simultaneous opening statements, every participant gets brief and opener at launch instead.) Start same-kind agents one at a time: the next launch of the same `agentId` only after the previous call returned.

```json tool=agent.launch
{"agentId":"<first speaker's id>","name":"Conversation: <name>","worktreeId":"<resolved worktree id>","prompt":"You are <name>, one of <N> participants in a facilitated conversation with <the others, by name> about <topic>. You cannot contact them; the facilitator relays every message. Write only the message you want the others to read, addressing them by name. Put private notes for the facilitator under [Operator]. Change no files. <opener>","notify":true,"handback":true}
```

```json tool=agent.launch
{"agentId":"<next participant's id>","name":"Conversation: <name>","worktreeId":"<resolved worktree id>"}
```

   Record every terminal id, then end your turn.
5. Pass the floor. Before a participant's first turn, confirm it has started and sits at its own input: `terminal.getStatus` with output shows `idle` (no turn yet) or `waiting` at its prompt, and the screen is its input box, not a trust or login screen (those are the user's to clear). Send the current speaker, and only them, everything public it has not seen since its last turn, labelled by name, plus the ask:

```json tool=terminal.sendCommandOwned
{"terminalId":"<speaker terminal id>","command":"Turn <t>. Since your last turn: <Name> said …; <Name> said …. Respond to the group, addressing people by name if you like. Private notes for the facilitator go under [Operator].","notify":true,"handback":true}
```

   Quote short messages; label a condensed one as condensed. With two participants this is simply "Message from <name>: …". Record the token and pending turn, then end your turn.

On notice or resume:
1. Follow the foundation's resume procedure, and apply any user interjection before the next relay: fold it into the next speaker's message, never restart the conversation.
2. Correlate: accept the reply only from the current speaker, for the pending turn, settled after that turn's send (`terminal.getStatus` with its `submissionToken`). A reply that does not match is late or stale: log it as such, never as the current turn.
3. Read the reply (`terminal.readLastMessageOwned` for Claude Code, otherwise `terminal.getOutput`), append its public message to the log, keep `[Operator]` notes private, and advance that speaker's last-seen position.
4. If it says it will "call" or "message" another participant directly, correct it: it cannot. Take the message it wants sent and relay it yourself.
5. A question within the user's intent (for instance "should I send this?") gets answered and the turn goes on. New scope, file edits, spawning or closing terminals, or an approval screen go to the user.
6. Only then advance to the next speaker and send its catch-up (step 5).
7. A speaker still working after other activity: at most one bounded wait, then tell the user who holds the floor. With three or more, the user may have you skip it for this lap; log the skip, and when it does answer, relay that reply to later speakers as a late message. With two, offer to wrap up from the completed turns.

Stop and recovery: end at a natural stop: agreement, the bound reached, a clear wrap-up, or the user stopping it. Never force another lap, or a final turn after a reply that already closed the conversation. On cancellation while a speaker is responding, record a tombstone, relay nothing more, and when its reply lands report it rather than passing the floor; interrupt it (`terminal.interruptOwned`) only if the user asks. Never resend a turn to a speaker already pending. Leave terminals open; close only the recorded participant ids on request.

Report back: participants with terminal ids, a concise synthesis (key points, agreements, disagreements), operator notes worth surfacing, turns taken and any skipped or late speakers, whether it ended naturally, was cancelled or is awaiting a reply (and which terminal's notice is pending), and any cleanup performed.
