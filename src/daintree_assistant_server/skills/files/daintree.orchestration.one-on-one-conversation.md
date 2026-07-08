---
id: daintree.orchestration.one-on-one-conversation
title: Facilitate a 1-on-1 agent conversation
summary: Relay a live back-and-forth conversation between exactly two agents, keeping them speaking directly to each other while you drive the turns.
whenToUse: Use when the user asks you to facilitate, host, relay, or keep going a back-and-forth conversation/chat between exactly two agents, especially “get them talking,” “facilitate a conversation,” “see where it goes,” or “let them talk to each other.” Do not use for a group conversation among three or more agents (use the group-conversation skill), a one-shot question to one agent (use the ask-and-relay skill), polling several agents (use poll-pinned-agents), judged interviews (use interview or verified-interview), scoring/voting competitions (use competition), or several agents collaborating toward one synthesized answer rather than a visible dialogue (use multi-agent).
priority: 110
risk: project
tags:
  - daintree
  - orchestration
  - agents
  - conversation
  - one-on-one
  - relay
  - chat
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
Use when: the user wants exactly two agents to have a live back-and-forth conversation, and wants you to facilitate the turns by relaying messages between their terminals.

Procedure:
1. Scope the conversation. Identify the two participants, topic/opening prompt, desired tone, and any stop condition such as “short,” “a few rounds,” “until natural conclusion,” or “keep going until I stop you.” If the user names existing terminals, use those exact terminals after confirming their full terminalIds from context.snapshot or prior tool results. Do not spawn replacements for already-open agents unless the user asks.

2. Create compact state for the run: scratch.create({"name":"one-on-one-agent-conversation"}). Store the full terminalIds, names/roles, current speaker, turn count, any max-turn limit, and a short running history in one key such as conversation_state. Use scratch for long conversations so compaction does not lose terminalIds or the dialogue thread.

3. Prepare participants. If either participant is not already running, spawn whichever configured agent is best suited with agentTask.spawnForEdits in mode:"explore". Use a short title and a prompt like: “You are in a mediated 1-on-1 conversation with <other>. You cannot contact the other agent directly; the facilitator will relay messages. When it is your turn, write the exact message you want sent to <other>, addressing them directly. Put private notes for the facilitator under a clearly labeled [Operator] section. Do not call, bridge, message, or coordinate with the other agent yourself.” Do not attach watchers for an in-turn conversation; you are driving it.
   - If using existing terminals, send a setup message only if needed. Do not tell agents they can literally see each other’s terminal; they see only what you relay.

4. Start the first turn. If the user supplied an opener, send it to the chosen first speaker. Otherwise ask the first speaker for an opening message to the other agent. Use terminal.sendCommand({terminalId, command}), and when you are ready to wait, batch terminal.awaitAll({terminalIds:[speakerId]}) as the last call in the same reply. Do not ask both agents to start at once unless the user explicitly wants simultaneous opening statements.

5. Collect the speaker’s latest message. After awaitAll, read the output for the current speaker. Prefer terminal.extract with an instruction such as “Extract the latest message intended for <other agent>; include any [Operator] note separately.” If extract returns empty, partial, or only old scrollback, fall back to terminal.read; if read is archived, page with artifact.read. Use terminal.summarize only for garbled TUI output, not as the source for exact relay text.
   - Guardrail: if an agent says it will “call,” “message,” or “reach” the other agent directly, do not accept that as facilitation. Extract the actual message it wants sent, or ask it to write that message, then relay it yourself.
   - Keep [Operator] notes private unless the agent clearly marked them as part of the message to the other agent.

6. Relay the turn to the other agent. Send a clearly framed command to the recipient: “Message from <speaker>: … Respond directly to <speaker>. If you have private facilitator notes, put them under [Operator].” Preserve the speaker’s voice and meaning. Relay verbatim when reasonable; condense only when the message is too long, and label it as a condensed relay. Do not add your own opinions unless the user gave a moderation instruction.
   - Send the relay with terminal.sendCommand and put terminal.awaitAll for the recipient as the last call in the same batch. This saves a round trip and keeps the dialogue moving.

7. Alternate turns. Repeat collect → relay → await. At the top of each turn, scratch.get the conversation_state if needed, then overwrite it after the turn with the updated speaker, turn count, compact history, and any unresolved operator notes. If the user interjects mid-conversation, fold the instruction into the next relay; do not restart the conversation.
   - If an agent asks the facilitator an operational question and the user’s intent already answers it, answer and continue. Example: if it asks whether to send its reply and the user asked you to facilitate, relay the reply without asking the user again. Ask the user only for new scope, unsafe actions, file edits, spawning/closing terminals, or ambiguous permission.

8. End at a natural stopping point. Stop when the agents reach an agreement, the requested number of turns is complete, the conversation clearly wraps, or the user stops it. Do not force a final turn after an empty extract if the previous message already concluded. If one agent hangs, re-await that terminal at most twice; if it still does not finish, either summarize from the completed turns or hand it off with terminal.await.async and tell the user you will report later.

9. Clean up only what the user requested. Leave both terminals open unless the user explicitly asks to close one or both. If the user asked for cleanup, close only the exact terminalIds you recorded for this conversation. Drop the scratch store with scratch.drop once the conversation is done and no follow-up turn is expected.

Confirmation: Ask before spawning new visible agents or closing terminals unless the user already explicitly requested that action.

Report back: the participants and terminalIds, a concise summary of the conversation’s key points or agreements, any private operator notes worth reporting, whether the conversation ended naturally or is awaiting a follow-up, and any terminal cleanup performed.
