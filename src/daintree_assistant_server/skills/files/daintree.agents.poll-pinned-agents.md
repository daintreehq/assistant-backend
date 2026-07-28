---
id: daintree.agents.poll-pinned-agents
title: Poll pinned agents
summary: Poll the pinned or configured agent fleet with one neutral question or option vote, read their free-form verdicts, and synthesize a consensus.
whenToUse: Use when the user asks to poll the pinned or configured agent fleet (or several named agents) for independent opinions, votes among options, verdicts, recommendations, or short answers and wants a synthesized consensus. The deliverable is opinions, not work. Do not use when each agent is instead to be given its own unit of real work (use daintree.orchestration.fan-out-work), for a single agent (use the ask-and-relay skill), for multi-round debate or collaboration (use the multi-agent skill), for an open-ended dialogue where the agents talk to each other (use the conversation skill), for a judged scoring competition or interview (use the competition or interview skill), or for breaking a tie (use the runoff tie-breaker skill).
risk: project
tags:
  - daintree
  - agents
  - pinned-agents
  - poll
  - vote
  - consensus
  - orchestration
requiredTools:
  - context.snapshot
  - daintree.call
  - agentTask.spawnForEdits
  - terminal.awaitAll
  - terminal.read
  - terminal.summarize
  - terminal.close
---
Use when: the user asks to poll/ask all pinned agents — not existing open terminals — for a decision, vote, opinion, or short answer, and wants a synthesized consensus.

Procedure:
1. Get the roster. Start from the agent registry already in your startup context — it lists configured agents and marks pinned ones — and call `context.snapshot` to see current terminals. Prefer it over `daintree.call agentSettings.get`, which is a system-risk escape hatch requiring typed confirmation. But CHECK the registry before trusting it as the full fleet: it says "complete" or "partial", and it reports any rows omitted by the size limit. Fall back to `agentSettings.get` whenever the registry is absent, marked partial, reports omitted rows, or does not contain an agent the user named — and never report "no pinned agents" off an incomplete roster. If the user says the configured fleet, all configured agents, or several named agents, use the whole configured roster (or just the named ones) instead of filtering to the pinned subset. Treat "pinned terminals" as shorthand for pinned agents unless the user explicitly says open terminals; a pinned/configured agent may have zero open terminals. Do NOT use docs search to discover agents, and never relay raw settings/env/secrets — only agent ids/display names and pinned status.
2. If the user only asked "can you see them?", report the pinned-agent count and names. If they asked to poll/ask them, continue. If no pinned agents exist, report that and stop.
3. Compose a neutral, self-contained prompt template with the user's question/brief. Tell each agent it is being polled independently, to not modify files, and to answer in plain prose, ending with a clear bottom-line verdict/vote and its 2-5 strongest reasons. Do NOT ask for JSON or any rigid output format — the agent's own words are the answer. Do not first do your own local investigation unless the user asked for your opinion too; put any needed context into the prompt.
4. For a vote among multiple options, minimize presentation bias. Present only the option text needed to decide, with equal detail/length as much as practical. Do NOT label options by origin, status, or praise: avoid labels like "<agent name>'s original", "combined consensus", "synthesis", "winner", "best so far", or "draft" unless the user explicitly makes authorship/status part of the judgment. Use neutral temporary labels such as A/B/C only. Randomize the option order separately for each agent; if you lack a randomizer, use different balanced rotations/shuffles and do not put the presumed favorite in the same slot for every agent. Record each terminal's label→actual-option map privately and use that map when tallying. If an option is a combined proposal, describe it only by its content, not as "combined" or "synthesis".
5. Spawn one fresh read-only terminal per selected agent using `agentTask.spawnForEdits`, e.g. `{"agentId":"<agentId>","mode":"explore","title":"poll: <agent name>","taskPrompt":"<neutral poll prompt>"}`. The parameter is `agentId` — omitting it silently launches the default agent, so a poll spawned without it is N copies of the same agent rather than the fleet. Use one call per agent, in one batch or groups of about 4. Build from fresh spawns; do not commandeer an existing terminal unless the user explicitly named it. For impartial votes, freshness is mandatory: if an agent participated earlier, authored an option, or has context from a prior round, still spawn a fresh terminal for that agent rather than reusing the biased terminal. Ensure each call's JSON args are valid; if a spawn call fails, retry only the missing agent(s), not the successful ones.
6. When spawn results return, record each full `terminalId`→agent map exactly, plus any per-terminal option-label map from step 4. Do not call await/extract in the same batch as the spawns that mint ids.
7. Wait for responses with one `terminal.awaitAll({"terminalIds":[...],"maxAttempts":60})`, not one wait per agent. If all finish, proceed. If some are `stillWorking`, read finished agents now and re-await only the still-working ids; cap at two re-awaits, then omit hung agents with a note.
8. Read the verdicts in free-form: one bounded `terminal.read` per finished terminal, all batched in one turn. Take each agent's verdict and reasoning directly from what it actually wrote — do NOT run a structured/JSON extraction over the cohort, including for A/B/C vote polls; you are the reader and synthesizer, and nothing downstream parses these answers. Use `terminal.summarize` only for garbled multi-screen TUI output, and treat a summary as a lossy paraphrase: if its verdict label contradicts the reasoning in the visible text, trust the terminal text and re-read it.
9. Normalize verdict labels only enough to tally equivalent positions; keep meaningful nuance in the reasons. For randomized option votes, translate each terminal's chosen label through that terminal's own label→actual-option map before tallying; never assume A/B/C means the same thing across agents. Map each answer back to the agent name via `terminalId`, not by terminal title.
10. Synthesize: state consensus ratio, majority verdict, dissenters, and 1-3 strongest reasons on each side. Do not dump raw scrollback.
11. Leave the spawned terminals open and idle unless the user explicitly asks to close them. If the user asks to close them, close only the exact terminal ids spawned or used for this poll and confirm which were closed.

Confirmation: The user's explicit request to "ask/poll/vote all pinned agents" is enough confirmation to spawn fresh read-only agent terminals. If they only ask about the roster or capabilities, ask before spawning. Closing terminals requires an explicit user request.

Report back: the pinned agents polled, consensus tally, per-agent verdicts with concise reasoning, any agent omitted or still running, the synthesized recommendation/answer, and for randomized option votes the final mapped option identities.

Notes:
- Never relay raw settings/env/secrets — only agent ids/display names and pinned status. If a fallback `agentSettings.get` returns sensitive env/config fields, ignore them and never quote them.
- If the user corrects you midstream that they wanted an agent poll, stop local analysis and switch to the poll; do not keep digging locally.
- If the user flags poll-prompt bias, discard the biased poll as biased, rewrite the prompt neutrally, use fresh terminals, randomize option order if choices are involved, and rerun; do not mix biased and corrected votes.
- This is a one-shot independent poll. If the user then wants the agents to debate or vote on each other, a judged/scoring competition, an interview, or a tie-breaker, switch to the multi-agent, competition, interview, or runoff tie-breaker skill instead of extending this one.
