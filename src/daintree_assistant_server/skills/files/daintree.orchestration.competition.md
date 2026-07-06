---
id: daintree.orchestration.competition
title: Orchestrate a competition or peer-judging among agents
summary: Spawn a cohort of agents to produce independent responses, then judge and tally the best one — competitive evaluation pattern.
whenToUse: Use when you need to spawn several agents to produce independent outputs (facts, ideas, solutions) and then aggregate or judge the best through scoring, voting, or peer review. Do not use for a judge-led interview (use the interview skill), a simple independent fleet poll with no scoring (use the poll-pinned-agents skill), or a runoff after a tie (use the runoff tie-breaker skill).
priority: 90
risk: project
tags:
  - daintree
  - orchestration
  - competition
  - judging
  - voting
  - multi-agent
  - peer-review
requiredTools:
  - agentTask.spawnForEdits
  - terminal.awaitAll
  - terminal.extract.json
  - terminal.sendCommand
  - scratch.create
  - scratch.set
  - scratch.get
  - scratch.drop
---
Use when: the user asks you to conduct a competition, poll, or peer-judging among agents — e.g., each agent gives an answer, fact, or solution, and you need to determine the best one by tallying votes or scores.

Procedure:
1. Define the independent output each agent must produce (e.g., one interesting fact, a design idea) and the judging method (e.g., you pick initial winners, then all agents vote). Create a scratch store with scratch.create to hold mapping and scores.
2. Spawn the cohort in ONE BATCH with agentTask.spawnForEdits, assigning each a distinct `title` and a `taskPrompt` that is short and self-contained, asking only for that independent output. No watchers. Build the cohort from your OWN fresh spawns — do not fold an already-open workspace terminal into it (it may be the user's own work or another agent mid-task); include an existing terminal only if the user explicitly says to use that one. Record each terminalId exactly as returned. The agents cannot see or message each other — they have no Daintree MCP; YOU are the only relay, so write each taskPrompt to produce its output in its OWN terminal and never tell an agent to send its answer to, or coordinate with, another agent.
3. Wait for the whole cohort with ONE terminal.awaitAll. Then extract each agent's output in ONE structured pass with terminal.extract.json, using a JSON schema that returns an array of objects with `terminalId` and the output field (e.g., `fact`, `answer`). If any agent is not finished, re-await only stragglers (max 2 re-awaits) and handle hung agents accordingly.
4. Optionally, perform an initial judging step yourself: review the outputs and award points to the best (e.g., 1 point each to the top N). Update the scratch store with scores.
5. Relay the compiled outputs (condensed per agent) to each agent for peer judging, using terminal.sendCommand. Construct a short prompt that lists each agent's output and asks: "Which ONE is the best? Reply with the agent's name and a one-line reason." For the agent itself, mark its own output clearly (e.g., **Claude (YOU)**). Emit all sendCommands in one batch, then awaitAll again in the SAME batch (as the last call). Do NOT include a watcher.
6. Extract the votes in one terminal.extract.json call, structured with `terminalId`, `votedFor`, and `reason`. Combine with your own scores (if any) to compute a final tally. Do NOT re-extract per agent after the cohort extract.
7. Report the final tally and winner with a synthesis, showing each agent's output, votes received, and total. Clear scratch store when done (scratch.drop), and close terminals only if the user asks.

Report back: the final tally and winner, with a table or summary of scores.
