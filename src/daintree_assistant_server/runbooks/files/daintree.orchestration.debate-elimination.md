---
id: daintree.orchestration.debate-elimination
title: Run agent debate with elimination vote
summary: Facilitate a multi-agent debate, collect final positions, run a peer vote to eliminate the weakest position, and report the tally.
whenToUse: Use when the user asks several agents to debate a topic and then eliminate, vote off, remove, or identify the weakest agent/position after the debate, including when the elimination request arrives mid-debate. Do not use for a collaborative debate that ends in one synthesized answer with no vote-off (use daintree.orchestration.multi-agent), an open-ended visible conversation among agents with no elimination vote (use daintree.orchestration.conversation), a one-shot independent scored contest or best-entry vote (use daintree.orchestration.competition), a simple fleet poll with no debate (use daintree.agents.poll-pinned-agents), or a runoff after a tied vote (use daintree.orchestration.runoff-tiebreaker).
risk: project
requiredTools:
  - context.snapshot
  - agentTask.spawnForEdits
  - terminal.awaitAll
  - terminal.sendCommand
  - terminal.read
  - terminal.extract
  - terminal.summarize
  - terminal.close
  - scratch.create
  - scratch.set
  - scratch.get
  - scratch.drop
  - user.askMultipleChoice
---
Use when: the user wants multiple agents to debate a topic and then eliminate, vote off, or identify the intellectually weakest agent or position.

Procedure:
1. Scope the debate. Identify the topic, participant agents, expected debate rounds, elimination criterion, and whether the user wants the loser merely removed from the active debate, notified, or terminal-closed. If the user says only “eliminate,” treat it as remove from the active cohort and notify the loser; close terminals only on an explicit close/kill/shut-down request.

2. Create compact state with `scratch.create({"name":"agent-debate-elimination"})`. Track full terminalIds, agent names, activeTerminalIds, round number, one-line position summaries, votes, eliminated ids, and any close instructions in one scratch key. Do not call `scratch.set` with terminalIds in the same batch that creates/spawns them; wait until the spawn results return and copy the real full ids exactly.

3. Build the cohort. If the user named existing terminals, confirm their full terminalIds from context.snapshot or prior results and use only those named terminals. Otherwise spawn the requested configured agents with `agentTask.spawnForEdits`, passing each agent's resolved id as `agentId`, plus `mode:"explore"`, clear titles, and short prompts asking for an opening position on the topic. Omitting `agentId` silently launches the default agent, so a debate cohort spawned without it is N copies of the same agent arguing with itself. Tell each agent to write in its own terminal; agents cannot contact each other directly, so you will relay every hand-off.

4. Collect opening positions. Wait for all active terminals with ONE `terminal.awaitAll({terminalIds:[...]})`, not one wait per agent. Then read each answer with one batched `terminal.read` per terminal, or a targeted `terminal.extract` for the latest position if the terminal is small and clear. Use `terminal.summarize` only as a fallback for garbled multi-screen TUI output; do not rely on summaries as the sole source for exact votes or nuanced positions.

5. Run debate rounds. For each active agent, send a condensed, labeled summary of the other agents’ positions plus one direct prompt for the next round. Use `terminal.sendCommand({terminalId, command})` once per active agent; when possible, batch all sends and put `terminal.awaitAll` as the last call in the same batch. After the wait, read each agent once, update scratch with compact position summaries, and do not re-read the same round unless output is missing or ambiguous.

6. Handle mid-debate user changes. If `terminal.awaitAll` returns `interruptedByUser:true`, read the user’s new instruction and adapt instead of restarting. If the user asks for elimination while one agent is still finishing a current round and that round is still relevant, await only the straggler once or twice; otherwise use the latest available positions and state any missing input.

7. Start the elimination vote — even-handed framing. List every active agent’s final/current position with EQUAL detail and neutral wording: no loaded adjectives, no hint of which position you or anyone judged stronger or weaker, and state ONLY the user’s criterion exactly, e.g. “vote for the intellectually weakest position.” Because the debaters already learned each other’s positions by name over the rounds, this elimination vote is necessarily attributed — you cannot blind it; keep it fair by treating every position identically, not by hiding authorship. (If instead the user wants the merit of the IDEAS judged by fresh/neutral agents rather than a known peer voted off, that is a blind vote: anonymize and shuffle the options per the neutral-voting rule in the daintree.foundation runbook and use the competition runbook.) If the user did not define self-voting, make the rule explicit in the prompt; for intellectual-correctness eliminations, allow voting for any active position, including self only if the agent sincerely judges its own position weakest. Ask for one named target and a short reason. Do not ask for JSON.

8. Collect votes. Send the elimination prompt to all active agents and await the active cohort. For terminals reported as asking a question, read them first: sometimes the vote is already present and the “question” is just the shell/agent prompt. Extract or read the latest vote text, map each voter terminalId to exactly one voted target, and ask a concise clarification only for ambiguous votes. Do not score a removed, closed, failed, or non-active terminal.

9. Tally mechanically. Count votes by eliminated target, not by voter. If one target has the most votes, that agent is eliminated. If the top vote is tied, report the tie and ask the user whether to keep co-losers, choose manually, or run a runoff; do not invent a sole loser. Update scratch by removing eliminated terminalIds from activeTerminalIds and recording the vote reasons.

10. Notify and continue or close. Send the eliminated agent(s) a short message with the verdict, vote tally, and key reasons. If the user explicitly requested terminal closure, notify first, then close only the exact eliminated terminalIds you recorded for this run. If the debate continues, relay the elimination result only to surviving active agents and continue with the remaining cohort.

11. Clean up state. Keep scratch while a debate, runoff, or survivor follow-up is expected. Drop it with `scratch.drop` only when the tournament/debate is done and no follow-up state is needed.

Confirmation: Closing eliminated terminals requires an explicit user request. After any close, confirm the exact terminalIds closed and which survivor terminals remain open.

Distinguish a ballot winner from an eliminated participant: tally votes received for elimination and apply only the announced rule. Do not interpret terminal closure, silence or a missing vote as a vote to eliminate. Preserve the requested electorate, show valid/missing counts, and label a result provisional when an unresolved vote could change it. An elimination notification sent through terminal.sendCommand starts another agent turn; do not wait for an unsolicited farewell or call it delivered to a terminal already closed. Keep rounds bounded, obey budgetExhausted, and preserve the remaining cohort’s supervision and state across any handoff.

Report back: the final vote tally, who was eliminated, each agent’s vote and one-line reason, which agents remain active, whether the loser was notified, and any terminal cleanup performed.
