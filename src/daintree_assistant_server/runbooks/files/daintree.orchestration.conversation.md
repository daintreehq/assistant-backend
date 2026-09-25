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
optionalTools:
  - worktree.list
  - terminal.list
  - terminal.getStatus
  - terminal.waitUntilIdleBatch
  - terminal.interruptOwned
  - terminal.closeOwned
---
Use when: two or more agents hold a live, open-ended conversation (back-and-forth, roundtable, panel) and you facilitate the turns by relaying between their terminals.

Availability: Core. An API-key client has no `notify`: give each turn one bounded `terminal.waitUntilIdleBatch`.

You facilitate, never participate: relay, condense, report, with no position of your own. Every send makes that agent speak, so the floor goes to ONE speaker at a time; never send an "update" to someone you are not asking to speak.

Procedure:
1. Scope: participants, topic or opener, tone, speaking order (two alternate; three or more round-robin unless the user wants another order) and a stop condition (default: two full laps, then a progress report).
2. Participants are identities, not kinds: two Codex instances are two participants. Named open agents are used as they are (yours or handed over, otherwise ask). Resolve new ones from `agent.listAvailable`, one worktree for all. Keep a public log (speaker, turn, message) and per participant its terminal id and the last entry it has seen. Keep each speaker's `[Operator]` notes out of every peer's messages; they may still go to the user.
3. Launch everyone in this turn with its role brief. Only the first speaker gets the opener; the others are asked to confirm, so each notice shows it at its prompt (or on a dialog) before its first real turn. For simultaneous openings, give everyone the opener.

```json tool=agent.launch
{"agentId":"<id from agent.listAvailable>","name":"Conversation: <name>","worktreeId":"<worktree id>","prompt":"You are <name>, one of <N> participants (<the others, by name>) in a facilitated conversation about <topic>. The facilitator relays every message; write only what you want the others to read, at most 15 lines, addressing them by name. Private notes for the facilitator go under [Operator]. Change no files. <first speaker: the opener. Others: reply only 'ready' and wait for your turn.>","focusPolicy":"preserve","notify":true,"handback":true}
```

4. Pass the floor: send the current speaker only, everything public it has not seen since it last spoke, labelled by name (condensed messages marked as condensed); a participant's first turn repeats its role brief. Then end your turn.

```json tool=terminal.sendCommandOwned
{"terminalId":"<speaker terminal id>","command":"Turn <t>. Since your last turn: <Name> said …; <Name> said …. Respond to the group in at most 15 lines. Private notes go under [Operator].","notify":true,"handback":true}
```

On notice or resume:
1. Apply any user interjection first by folding it into the next speaker's message; never restart.
2. The quote is the current speaker's message. A reply from anyone else, or for an earlier turn, is logged as late, never as the current turn. Append the public part to the log, keep `[Operator]` notes private, and advance that speaker's last-seen position.
3. A speaker cannot "message" another directly: relay what it wants sent yourself. New scope, edits, spawning or closing go to the user.
4. Pass the floor to the next speaker (step 4). A speaker holding the floor while nothing else moves: tell the user who. With three or more the user may skip it for this lap; relay its later reply as a late message.

Stop and recovery: end at agreement, the bound, a clear wrap-up or the user's stop; never force another lap. On cancellation relay nothing more; interrupt (`terminal.interruptOwned`) only if the user asks. Never resend a pending turn. Close only the recorded participant ids, on request.

Report back: participants with terminal ids; a concise synthesis (key points, agreements, disagreements); operator notes worth surfacing; turns taken, skipped or late speakers; whether it ended, was cancelled or awaits a reply (and from which terminal); any cleanup.
