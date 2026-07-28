---
id: daintree.orchestration.group-conversation
title: Facilitate a group agent conversation
summary: Relay a live multi-party conversation among three or more agents, giving each the floor in turn and catching it up on what the group said since it last spoke while you drive the rounds.
whenToUse: Use when the user asks you to facilitate, host, relay, moderate, or keep going a live back-and-forth GROUP conversation among THREE OR MORE agents — “get them all talking,” “have a group chat/roundtable/panel discussion,” “let them all discuss,” “have the three of them hash it out,” or “see where it goes” with 3+ participants. Do not use for exactly two agents (use the one-on-one-conversation skill), a one-shot question to one agent (use ask-and-relay), an independent fleet poll with no cross-talk (use poll-pinned-agents), a judged interview (use interview or verified-interview), a scored competition/vote (use competition), a post-tie runoff (use runoff-tiebreaker), or several agents collaborating/debating toward one synthesized answer or convergence rather than an open-ended visible group dialogue (use multi-agent).
priority: 105
risk: project
tags:
  - daintree
  - orchestration
  - agents
  - conversation
  - group
  - roundtable
  - panel
  - discussion
  - multi-party
  - relay
  - sendcommand
  - terminal
requiredTools:
  - context.snapshot
  - agentTask.spawnForEdits
  - agentTask.list
  - terminal.sendCommand
  - terminal.awaitAll
  - terminal.extract
  - terminal.read
  - terminal.summarize
  - terminal.await.async
  - artifact.read
  - scratch.create
  - scratch.set
  - scratch.get
  - scratch.drop
  - terminal.close
  - queue.resolve
---
Use when: the user wants three or more agents to have a live, open-ended group conversation — a roundtable, panel, or group chat — and wants you to facilitate the turns by relaying messages between their terminals so everyone stays in the discussion.

You are the ONLY conduit. The agents have no way to see, hear, or message each other — every hand-off flows through you. And each terminal.sendCommand makes that agent take a turn, so you cannot silently "update" the bystanders in the background: an agent only hears what the group said when you give it the floor and catch it up. So the loop is one speaker at a time — catch the current speaker up on everything said since its last turn, let it respond, then pass the floor to the next agent. Never fan an "here's an update" sendCommand out to agents you are not asking to respond; you would force them all to speak out of order.

Procedure:
1. Scope the conversation. Identify the participants (three or more), the topic/opening prompt, desired tone, the speaking dynamic (round-robin by default; or the user may want a fixed sequence, a moderated order you choose, or "whoever was addressed answers next"), and any stop condition such as "a couple of rounds," "until they agree," "until it wraps up," or "keep going until I stop you." If the user names existing terminals, use those exact terminals after confirming their full terminalIds from context.snapshot or prior tool results. Do not spawn replacements for agents that are already open unless the user asks.

2. Create compact state for the run: scratch.create({"name":"group-agent-conversation"}). In one key such as conversation_state, store the full terminalIds, each agent's name/role, the speaking order, the current-speaker index, the round/turn count, any max-turn limit, and a short running history (or the last message from each agent so you can catch stragglers up). Keep this in scratch because a group is easier to lose than a pair — compaction must not drop the terminalIds, the speaking order, or the thread.

3. Prepare participants. If any participant is not already running, spawn whichever configured agents best fit with agentTask.spawnForEdits in mode:"explore" (mode:"edit" only if they must change files), passing each participant's resolved id as `agentId` — omitting it silently launches the default agent, so a "group" spawned without it is N copies of the same agent talking to itself. Spawn all missing agents in ONE batch, each with a distinct title and a short prompt like: "You are one of <N> participants in a facilitated group conversation with <the others, by name>. You cannot contact the other participants directly; the facilitator relays every message. When it is your turn, write the exact message you want sent to the group, addressing people by name where useful. Put private notes for the facilitator under a clearly labeled [Operator] section. Do not call, bridge, message, or coordinate with any other agent yourself." Do not attach watchers — you are driving in-turn. Record every full terminalId exactly as returned; an id you spawn only exists on the turn AFTER the spawn batch, so never await, relay, or scratch.set against ids in the same batch that mints them.
   - If using existing terminals, send a setup message only if needed. Do not tell agents they can literally see each other's terminal; they see only what you relay.

4. Open the conversation. If the user supplied an opener, send it to the chosen first speaker; otherwise ask the first speaker for an opening message to the group. A natural start is to go once around the circle — give each agent the floor in order for an opening statement, catching each new speaker up on what the earlier ones just said. Use terminal.sendCommand({terminalId, command}) for the current speaker, and put terminal.awaitAll({terminalIds:[speakerId]}) as the last call in the same reply. Give one agent the floor at a time; do not prompt several agents to speak at once unless the user explicitly wants simultaneous opening statements.

5. Collect the current speaker's message. After awaitAll, read that speaker's output. Prefer terminal.extract with an instruction such as "Extract the latest message intended for the group; include any [Operator] note separately." If extract returns empty, partial, or only old scrollback, fall back to terminal.read; if read is archived, page with artifact.read. Use terminal.summarize only for garbled TUI output, never as the source for exact relay text. Append the message to the running history in scratch.
   - Guardrail: if an agent says it will "call," "message," or "reach" another participant directly, do not accept that as facilitation. Extract the actual message it wants sent, or ask it to write that message, then relay it yourself.
   - Keep [Operator] notes private unless the agent clearly marked them as part of the message to the group.

6. Pass the floor to the next speaker and catch them up. Pick the next speaker (round-robin by default, or per the user's dynamic). Send them a clearly framed command carrying only what they missed since their last turn — the other agents' messages, labeled by name — plus this round's ask: "Since your last turn: <Name> said …; <Name> said …. Respond to the group; address people by name if you like, and put any private facilitator notes under [Operator]." Preserve each speaker's voice and meaning: quote when short, condense long messages and label them as a condensed relay. Do not add your own opinions unless the user gave a moderation instruction. Send with terminal.sendCommand and put the next speaker's terminal.awaitAll as the last call in the same batch — this saves a round trip and keeps the dialogue moving. Address only the ONE agent whose turn it is.

7. Keep the rounds going. Repeat collect → advance the speaker → catch up → await. At the top of each turn, scratch.get the conversation_state if you need it, then overwrite it after the turn with the new current-speaker index, round/turn count, compact history, and any unresolved operator notes. If the user interjects mid-conversation (a message between turns or an awaitAll that returned interruptedByUser:true), fold the instruction into the next relay — do not restart the conversation.
   - If an agent asks the facilitator an operational question and the user's intent already answers it, answer and continue. Example: if it asks whether to send its reply and the user asked you to facilitate, relay the reply without asking the user again. Ask the user only for new scope, unsafe actions, file edits, spawning/closing terminals, or genuinely ambiguous permission.

8. End at a natural stopping point. Stop when the group reaches agreement, the requested number of rounds is complete, the conversation clearly wraps, or the user stops it. Do not force another lap after the conversation has clearly concluded, and do not force a final turn after an empty extract if the previous message already concluded. If one agent hangs on its turn, re-await that terminal at most twice; if it still does not finish, skip it for this round and pass the floor on with a noted skip, or hand it off with terminal.await.async and tell the user you will report later. A single slow participant must not stall the whole table.

9. Clean up only what the user requested. Leave all terminals open unless the user explicitly asks to close some or all of them. If the user asked for cleanup, close only the exact terminalIds you recorded for this conversation. Drop the scratch store with scratch.drop once the conversation is done and no follow-up turn is expected.

Confirmation: Ask before spawning new visible agents or closing terminals unless the user already explicitly requested that action.

Report back: the participants and their terminalIds, a concise synthesis of the discussion — key points, agreements, and notable disagreements — any private operator notes worth surfacing, whether the conversation ended naturally or is awaiting a follow-up, and any terminal cleanup performed.
