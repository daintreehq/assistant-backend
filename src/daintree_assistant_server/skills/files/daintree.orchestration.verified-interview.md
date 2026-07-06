---
id: daintree.orchestration.verified-interview
title: Run a multi-round interview with codebase verification
summary: Orchestrate a deeper interview where the judge creates questions, contestants answer, the judge verifies answers by exploring the codebase and issues challenging follow-ups, then narrows to top 2 and selects a winner.
whenToUse: Use when the user wants one agent to deeply interview several others, with the judge actively verifying claims and asking follow-up challenges, and expects a narrow-to-top-2-then-winner verdict. Do not use for a light interview with no evidence-checking (use the interview skill) or a one-round poll (use the poll-pinned-agents skill).
priority: 82
risk: project
tags:
  - daintree
  - orchestration
  - interview
  - multi-round
  - verification
  - follow-up
  - narrow
requiredTools:
  - scratch.create
  - scratch.set
  - scratch.get
  - scratch.drop
  - agentTask.spawnForEdits
  - terminal.awaitAll
  - terminal.extract.json
  - terminal.extract
  - terminal.summarize
  - terminal.read
  - terminal.sendCommand
  - terminal.close
  - artifact.read
---
Use when: the user wants one agent to deeply interview several others, with the judge verifying claims and asking challenging follow-ups, and expects a narrow-to-top-2-then-winner verdict.

Procedure:
1. Understand the topic, who is the judge and who are the contestants. Create a scratch store for state (scratch.create).
2. Spawn the judge and all contestants in ONE BATCH with agentTask.spawnForEdits. Give each a distinct title. For the judge, write a taskPrompt that instructs it to: prepare initial interview questions, collect answers, optionally verify by searching/reading project files (if the topic is technical), issue challenging follow-up questions, narrow to top 2 with reasoning, then pick the winner. For contestants, the taskPrompt should ask them to make their case and answer all questions thoroughly. Use mode:"explore" (read-only) unless file changes are required; do NOT attach watchers (you are the conductor driving rounds in-turn). Record each terminalId exactly as returned.
3. Store the terminalIds and roles in the scratch store (scratch.set). Then wait for the whole cohort with ONE terminal.awaitAll. The judge should produce initial questions. Read the judge's output: first try terminal.extract.json; if it fails (e.g., 502), fall back to terminal.extract (plain) or terminal.summarize. If the text is long, use terminal.read or artifact.read for the full content.
4. Relay the judge's questions to each contestant via terminal.sendCommand — ONE call per contestant, all in a single batch. Send the full set of questions to each contestant. Include a trailing terminal.awaitAll in the SAME batch. Do NOT expect each contestant to receive different questions unless the judge specifically addresses them individually.
5. Collect the contestants' answers. Wait for the cohort to finish (awaitAll). Read each answer using terminal.extract.json over all contestants (ONE call), or fall back to terminal.summarize/terminal.read. Condense each answer to a short gist. Relay the condensed gist of all contestants' answers back to the judge in a single sendCommand, requesting follow-up challenges or narrowing.
6. Wait for the judge's follow-up. The judge may have read files or searched the project to verify claims; read its full output. If the judge sends follow-up questions for individual contestants, relay each follow-up to ONLY that contestant via sendCommand (one per targeted contestant). If the follow-ups are the same for all, send to all in one batch. Again, batch sendCommand(s) with an awaitAll in the same turn.
7. Repeat the read/relay cycle (steps 5–6) until the judge announces a winner. The judge may narrow to top 2 before the final round. Keep tracking state in the scratch store (overwrite `orchestration_state` each round) so compaction does not lose ids.
8. When the judge delivers the final verdict and reasoning, present it to the user. If the user explicitly asked to close losing contestants, close ONLY those terminals with terminal.close. Clean up the scratch store with scratch.drop.

Guardrails:
- If terminal.extract.json fails, fall back to plain terminal.extract or terminal.summarize — do NOT retry the structured extract.
- If a contestant asks an off‑topic question (e.g., a survey), send a short command ("Skip." or "Continue.") to keep it on track.
- If an agent's output is truncated, read the full content via terminal.read or artifact.read; never rely on a partial extract.
- If an agent is slow, re-await it individually (max 2 re-awaits), then proceed without it if still hung.
- Before sending commands, check the terminal snapshot (context.snapshot) to ensure the terminal IDs are still live.
- Condense answers to a short gist when relaying to the judge; never relay raw scrollback.

Confirmation: Only if it involves closing losing terminals at the user's explicit request.

Report back: the winner, the judge's reasoning, and the list of losing terminals closed (if any).
