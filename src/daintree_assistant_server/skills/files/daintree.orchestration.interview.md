---
id: daintree.orchestration.interview
title: Run a judged interview competition among agents
summary: Have one agent interview others on a topic and select a winner, with the
  assistant relaying questions and answers.
whenToUse: Use when the user asks to have an agent interview other agents on a topic
  (e.g., choose a pair programmer, evaluate candidates) and expects a winner.
priority: 85
risk: project
tags:
  - daintree
  - orchestration
  - interview
  - multi-agent
  - competition
  - judge
  - relay
  - sendcommand
requiredTools:
  - agentTask.spawnForEdits
  - scratch.create
  - scratch.set
  - scratch.get
  - scratch.drop
  - terminal.sendCommand
  - terminal.awaitAll
  - terminal.extract
  - terminal.extract.json
  - terminal.read
  - terminal.summarize
  - artifact.read
  - terminal.close
---
Use when: the user wants one agent to interview several others on a topic and pick a winner — e.g. "which agent should be my pair programmer?" — and you are the relay between them.

Procedure:
1. Understand the topic, who is the judge and who are the candidates. If the user did not specify, pick a judge agent capable of asking deep, relevant questions (often the one most familiar with the topic) and candidate agents to evaluate. Create a scratch store for state: scratch.create({"name":"interview-competition"}).
2. Spawn the judge and all candidates in ONE BATCH with agentTask.spawnForEdits. Give each a distinct title and a short, self-contained taskPrompt. For the judge: instruct it to prepare interview questions, then evaluate answers, ask follow-ups, and finally announce the winner with reasoning. For candidates: instruct them to make their case and thoroughly answer the interviewer's questions. Use mode:"explore" unless file changes are required; do not attach watchers (you drive in-turn). Record each terminalId exactly as returned.
3. Store the terminalIds and roles in the scratch store (scratch.set). Then wait for the judge to produce initial questions (or for candidates to produce opening statements, depending on the task) with ONE terminal.awaitAll over the cohort.
4. Read every output. First try terminal.extract.json with a schema that fits the expected answer shape; if it fails (502 or parse error), fall back to terminal.extract (plain), terminal.summarize, or terminal.read for the full text. For any unfinished agent, re-await individually (max 2 re-awaits) and drop if hung. For large outputs, use artifact.read if the answer is archived.
5. Relay to the appropriate party: if the judge gave questions, condense them into a single message per candidate and send via terminal.sendCommand (one per candidate, all in one batch). If candidates gave pitches and the judge hasn't yet produced questions, relay the condensed pitches to the judge so it can formulate questions. Then awaitAll again as the LAST call in the SAME batch.
6. Repeat the relay cycle: wait for answers, read them, condense the gist, relay to the judge (e.g., "here are the candidates' answers; any follow-ups or is a decision ready?"). The judge may ask follow-ups or give a verdict. Continue until the judge announces a winner. Each relay: sendCommand(s) + awaitAll in one batch.
   - If a candidate asks an off‑topic question (survey, permission prompt), send a short command ("Skip." or "Continue.") to get it back on track. Avoid stalling.
7. When the judge delivers the final verdict, read it completely (terminal.read, artifact.read if needed) and present the decision to the user: the winner and the judge's reasoning. If the user asked to close losing candidates, close ONLY those you spawned with terminal.close (leave the judge open unless asked). Clean up: scratch.drop.

Confirmation: Only if the interview involves file changes or closing terminals without explicit user request.

Report back: the winner, the judge's reasoning, and the list of losers closed (if any).

Notes:
- The judge may need to explore the project (read files, search) to craft good questions or verify answers — spawn it in explore mode and allow time.
- When relaying answers, condense to the gist (a few sentences per candidate); never relay raw scrollback.
- If an agent's output is truncated, page through artifact.read.
- The judge is the decider; do not override its verdict.
- Extract.json is best‑effort — fall back to plain extract or summarise when it fails.
