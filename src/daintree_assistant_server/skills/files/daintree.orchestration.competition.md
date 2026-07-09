---
id: daintree.orchestration.competition
title: Orchestrate a competition or peer-judging among agents
summary: 'Run a scored named-agent contest: ask agents such as Claude, Anti-Gravity, OpenCode, Grok, Codex, etc. each for one interesting fact or other quick independent item, pick top entries, relay all submissions for peer votes, tally points, and declare the winner or tie.'
whenToUse: Use when the user asks you to ask Claude, Anti-Gravity, OpenCode, Grok, Codex, or several named agents to each give one interesting fact, give you one interesting fact off the top of its head, not related to the codebase, something completely unique, or another quick independent item, then choose the best/top two, give or award points, send each agent the facts/submissions from the other agents, ask each open agent to choose which is best, tally everything up, tell which agent gave the most interesting fact, declare a winner, or handle winner-only follow-up after a tied tally. Do not use for a judge-led interview (use the interview skill), a simple independent fleet poll with no scoring/voting/tally (use the poll-pinned-agents skill), collaborative debate toward one answer (use the multi-agent skill), or a runoff/new voting round after a tie (use the runoff tie-breaker skill).
priority: 100
risk: project
tags:
  - daintree
  - orchestration
  - competition
  - judging
  - voting
  - multi-agent
  - peer-review
  - facts
  - interesting-fact
  - off-the-top-of-head
  - named-agents
  - points
  - top-two
  - tally
  - winner
  - tie
requiredTools:
  - agentTask.spawnForEdits
  - terminal.awaitAll
  - terminal.await.async
  - async.cancel
  - terminal.read
  - terminal.summarize
  - terminal.sendCommand
  - terminal.close
  - scratch.create
  - scratch.set
  - scratch.get
  - scratch.drop
  - user.askMultipleChoice
---
Use when: the user asks you to conduct a competition, poll, or peer-judging among agents — e.g., each agent gives an answer, fact, or solution, and you need to determine the best one by tallying votes or scores.

Procedure:
1. Define the independent output each agent must produce (e.g., one interesting fact, a design idea) and the judging method (e.g., you pick initial winners, then all agents vote). Create a scratch store with scratch.create to hold terminalId→agent mapping, activeTerminalIds, omitted/disqualified agents, scores, and any asyncId if you intentionally use an async wait. If the user supplied exact rules (e.g., “choose the two best and give them each one point; each peer vote gives one more point”), follow those rules mechanically. If no tie rule is specified, default to reporting a tie rather than inventing a tie-breaker.
2. Spawn the cohort in ONE BATCH with agentTask.spawnForEdits, assigning each a distinct `title` and a `taskPrompt` that is short and self-contained, asking only for that independent output. No watchers. No background async wait for normal quick contests: use terminal.awaitAll in-turn, not terminal.await.async. If you intentionally choose terminal.await.async because you are ending the turn, record the asyncId and exact terminalIds it covers in scratch when it returns; any later cohort membership change invalidates it and must be canceled/reissued (step 3). Build the cohort from your OWN fresh spawns — do not fold an already-open workspace terminal into it (it may be the user's own work or another agent mid-task); include an existing terminal only if the user explicitly says to use that one. Record each terminalId exactly as returned, and after spawn results return write the terminalId→agent map plus activeTerminalIds to scratch before the first wait. A successful spawn result IS the launch confirmation — do not spend a round listing spawns to re-verify; go straight to the await. The agents cannot see or message each other — they have no Daintree MCP; YOU are the only relay, so write each taskPrompt to produce its output in its OWN terminal and never tell an agent to send its answer to, or coordinate with, another agent.
3. Wait for the whole active cohort with ONE terminal.awaitAll. Then read each agent's output in free-form — one bounded terminal.read per terminal, batched in one turn; the agents answer in plain prose and their own words are the entries. Do NOT substitute terminal.summarize for these reads — a summary can drop the one sentence you need (the fact, the vote) and force a re-read round; reserve it for garbled multi-screen TUI output only. Do NOT ask agents for JSON or run a structured extraction over the cohort. If any agent is not finished, re-await only stragglers (max 2 re-awaits) and handle hung agents accordingly. If the user says an agent is broken, asks to “take it out,” disqualifies it, or its terminal is closed/failed, immediately update scratch: remove that terminalId from activeTerminalIds and record it as omitted/disqualified. If any outstanding terminal.await.async includes that terminalId, call async.cancel on that asyncId BEFORE claiming the wait will continue; async waits are over the exact original id list, not a live set, and may never wake if a removed member cannot settle. Then start the next wait (terminal.awaitAll in-turn, or a fresh terminal.await.async only if you are ending the turn) with ONLY the remaining active terminalIds. Never wait again on, relay to, score peer votes from, or close a second time a removed terminal.
4. Optionally, perform an initial judging step yourself: review the outputs and award points to the best (e.g., 1 point each to the top N). Update the scratch store with scores.
5. Relay the entries for peer judging — BLIND, so every vote is on merit alone and never tainted by authorship or framing. Compile the active agents' outputs, strip each of its author AND any characterization (no "the synthesis", "the front-runner", etc.), give each equal detail, and label them with neutral tags (Entry 1/2/3 or A/B/C). VARY the tag order across voters (rotate or shuffle) so no slot is systematically favored, and record each voter's tag→agent map in scratch. To each active agent send, via terminal.sendCommand, ONLY the neutral list plus: "Judge these on merit. Which ONE is best? Reply with its label (e.g. 'Entry 2') and a one-line reason." Do NOT reveal who wrote any entry, do NOT mark the voter's own entry as (YOU), and do NOT ask for JSON. A voter may unknowingly pick its own entry — that is a clean merit vote; if the user forbade self-votes, YOU detect it via the map and quietly ask that voter for its next choice, never disclosing authorship. Emit all sendCommands in one batch, then awaitAll again in the SAME batch (as the last call). Do NOT include a watcher. If you intentionally switch to async because you are ending the turn, apply the same active-id rule from step 3: no removed terminals in the wait set, and store the new asyncId.
6. Read the votes the same way — one terminal.read per voter terminal, batched. Each vote is a neutral tag plus a one-line reason in prose; map each voter's chosen tag back to the authoring agent via that voter's scratch map, then attribute the vote to the voter by terminalId. Combine with your own scores (if any) to compute a final tally. Do NOT re-read a voter you have already read. If a vote is ambiguous, read that terminal once more; if still unclear, send one concise clarification prompt and await only that voter.
7. Resolve the result. If one entrant has the top score, declare that entrant the winner. If the top score is tied, report the tie and do NOT invent a sole winner. If the user asks for winner-only follow-up while the tie is unresolved (e.g., “close all terminals besides the winner” or “tell the winner to...”), ask the user to choose between the tied entrants, co-winners, or a runoff. Use user.askMultipleChoice when available. If the user chooses a runoff/new vote, switch to the runoff tie-breaker skill before cleanup. Until the user resolves the tie, keep all tied terminals open and do not send sole-winner prompts.
8. Report the final tally and winner/co-winners with a synthesis, showing each agent's output, votes received, and total. Reveal which agent authored each entry HERE, in the report to the user — the blinding applied only to the voters, never to your own tally or the user. Also note any removed/failed agent and whether its entry or vote was excluded. Clear scratch store when done (scratch.drop) — but if the user chose a runoff, hand off with the scratch store INTACT (the runoff tie-breaker restores the terminalId → agent map, submissions, and scores from it and handles its own cleanup).
9. Close terminals only if the user asks. For “besides the winner” cleanup, close ONLY the exact recorded cohort IDs that are not the resolved winner(s); keep all winner/co-winner terminals open. If the user asks the winner(s) for a short follow-up such as an acceptance speech, send the prompt only to the resolved winner/co-winner terminals, await/read their replies, and report them.

Confirmation: Closing terminals requires an explicit user request; after closing, confirm which agents were closed and which winner/co-winner terminals remain open.

Report back: the final tally and winner/co-winners, with a table or summary of scores, plus any requested winner follow-up or terminal cleanup.
