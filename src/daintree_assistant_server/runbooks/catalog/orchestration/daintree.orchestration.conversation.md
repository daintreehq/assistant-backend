---
id: daintree.orchestration.conversation
title: Facilitate a live agent conversation
summary: Relay a live back-and-forth conversation among two or more agents, giving each the floor in turn and catching it up on what the others said since it last spoke while you drive the rounds.
whenToUse: Use when the user asks you to facilitate, host, relay, moderate, or keep going a live back-and-forth conversation between TWO OR MORE agents — “get them talking,” “let them talk to each other,” “facilitate a conversation,” “have a group chat/roundtable/panel discussion,” “have the three of them hash it out,” or “see where it goes.” Participant count is just a parameter of this runbook. Do not use for a one-shot question to one agent (use daintree.agents.ask-and-relay), an independent poll with no cross-talk (use daintree.agents.poll-pinned-agents), a judged interview that names a winner (use daintree.orchestration.interview), a scored competition or vote (use daintree.orchestration.competition), a post-tie runoff (use daintree.orchestration.runoff-tiebreaker), a debate that ends by voting a position off (use daintree.orchestration.debate-elimination), or several agents collaborating toward ONE synthesized answer or convergence rather than an open-ended visible dialogue (use daintree.orchestration.multi-agent).
risk: project
requiredTools:
  - agent.launchMany
  - terminal.sendCommandOwned
optionalTools:
  - agent.launch
  - agent.listAvailable
  - terminal.getStatus
  - terminal.interruptOwned
  - terminal.closeMany
---
You facilitate, never participate: relay, condense, report, with no position of your own. Every send makes that agent speak, so the floor goes to ONE speaker at a time; never send an "update" to someone you are not asking to speak.

On a host: call these tools on each participant's machine.

1. Scope: participants, topic or opener, tone, speaking order (two alternate; three or more round-robin unless the user says otherwise) and a stop condition (default: two full laps, then a progress report).
2. Participants are identities, not kinds: two Codex instances are two participants. Named open agents are used as they are (yours or handed over, otherwise ask). New ones share one worktree. Keep a public log (speaker, turn, message) and per participant its terminal id and the last entry it has seen.
3. Launch everyone with one role brief and `waitForReply:true, handback:true`: "You are one of <participants> in a facilitated conversation about <topic>. The facilitator relays every message; write only what you want the others to read, at most 15 lines, addressing them by name. Private notes for the facilitator go under [Operator]. Change no files. Reply only 'ready' and wait for your turn." For simultaneous openings, put the opener in the brief instead.
4. Pass the floor: send the current speaker only everything public it has not seen since it last spoke, labelled by name (condensed messages marked as condensed), with `waitForReply:true, handback:true`:

```json tool=terminal.sendCommandOwned
{"terminalId":"<speaker terminal id>","command":"Turn <t>. Since your last turn: <Name> said …; <Name> said …. Respond to the group in at most 15 lines. Private notes go under [Operator].","waitForReply":true,"handback":true}
```

5. Append the reply's public part to the log, keep `[Operator]` notes out of every peer's messages (they may still go to the user), and advance that speaker's last-seen position. A reply from anyone else, or for an earlier turn, is logged as late, never as the current turn. Fold a user interjection into the next speaker's message; never restart.
6. A speaker cannot "message" another directly: relay what it wants sent yourself. New scope, edits, spawning or closing go to the user. A speaker whose wait times out: tell the user who; with three or more the user may skip it for this lap, and its later reply is relayed as late.

End at agreement, the bound, a clear wrap-up or the user's stop; never force another lap. Interrupt (`terminal.interruptOwned`) only if the user asks. Ended at agreement, a wrap-up or the user's stop, and reported: close the participants you launched, per the foundation, in one `terminal.closeMany`. Stopped at the bound: keep them all and ask whether to go on.

Report back: participants with terminal ids; a concise synthesis (key points, agreements, disagreements); operator notes worth surfacing; turns taken, skipped or late speakers; whether it ended, was cancelled or awaits a reply (and from which terminal); any cleanup.
