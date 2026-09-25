---
id: daintree.orchestration.interview
title: Run a judge-led interview competition among agents
summary: One agent interviews several others on a topic while you relay questions and answers — optionally with the judge verifying claims against the codebase and narrowing to a top 2 — until it names a winner.
whenToUse: Use when the user asks to have one agent interview, question, or grill several other agents on a topic and expects a winner — "have one agent interview the others and pick the best", "which of these should be my pair programmer?". Covers both depths, a light Q&A interview and a deep one where the judge verifies candidates' claims against the codebase, issues challenging follow-ups, and narrows to a top 2 before the verdict. Do not use for a plain one-round poll with no interviewer (use daintree.agents.poll-pinned-agents), a scoring-only competition where entries are judged rather than questioned (use daintree.orchestration.competition), or an open-ended dialogue with no judge and no winner (use the daintree.orchestration.conversation runbook).
risk: project
requiredTools:
  - agentTask.spawnForEdits
  - context.snapshot
  - scratch.create
  - scratch.set
  - scratch.get
  - scratch.drop
  - terminal.awaitAll
  - terminal.read
  - terminal.summarize
  - terminal.extract
  - terminal.sendCommand
  - terminal.close
  - artifact.read
  - terminal.await.async
---
Use when: the user wants one agent to interview several others on a topic and pick a winner, and you are the relay between them.

Procedure:
1. Scope the interview. Identify the topic, which agent is the judge and which are the candidates; if the user did not say, pick a judge able to ask deep questions on the topic and the candidates to evaluate from the available-agent roster in your startup context. Then set the VERIFICATION DEPTH — the one thing that varies between runs: PLAIN (judge asks, candidates answer, judge decides) or VERIFIED (the judge checks candidates' claims against the actual codebase and narrows to a top 2 before the final verdict). Read "verify", "check their claims", "make them prove it", "top 2", or "finalists" as verified; default to plain when unstated and say which you chose. Create a scratch store: `scratch.create({"name":"interview"})`.
2. Spawn the judge and all candidates in ONE BATCH with `agentTask.spawnForEdits` — one call per agent, all in the same reply. Give each a distinct title, the agent's resolved id as `agentId` (omitting it silently launches the default agent, so a cohort spawned without it is N copies of the same agent interviewing itself), and a short self-contained `taskPrompt`. Judge prompt: prepare interview questions, evaluate the answers relayed to you, ask follow-ups, then announce a winner with reasoning. On a VERIFIED interview add to the judge's prompt: verify each candidate's concrete claims by reading and searching the project before scoring, challenge any claim the evidence does not support, narrow to a top 2 with reasoning, then pick the winner from those two — and expect at least one extra follow-up round. Candidate prompt: make your case and answer the interviewer's questions thoroughly. Use `mode:"explore"` for every agent unless the user wants file changes — explore instructs the judge to investigate without source edits; it is not sandbox enforcement. Require it to report any generated or unexpectedly changed files. Do NOT attach watchers (`watch`/`watchGoal`): you drive the rounds in-turn. Record each terminalId exactly as returned.
3. Store the terminalIds and roles under one state key with `scratch.set` — never in the same batch as the spawns; wait for the real ids to come back. Then wait for the whole cohort with ONE `terminal.awaitAll` over every id, not one wait per agent.
4. Read every output in free-form: one bounded `terminal.read` per terminal, batched in a single reply. Use `terminal.summarize` only for garbled multi-screen TUI output, and a plain `terminal.extract` only to pull one specific detail from ONE terminal at a time — its `terminalIds` is an ARRAY, so pass a single id in it (`{"terminalIds":["<one-id>"]}`), because an extract over several ids MERGES their tails into a single answer and destroys per-agent attribution. Do NOT ask agents for JSON or run a structured extraction; their own prose is the answer. Page only raw output actually archived with an artifactId, using bounded offsets for the needed question or verdict. A terminal.read tail is capped and cannot recover an unlimited transcript. Re-await a straggler individually at most twice, then proceed without it.
5. Relay. Before sending, confirm the cohort ids are still live in `context.snapshot` — a closed or crashed terminal silently swallows a sendCommand. Condense the judge's questions to their gist and send with `terminal.sendCommand`, one call per candidate, all in one batch, with `terminal.awaitAll` as the LAST call in that SAME batch. Send the FULL question set to every candidate unless the judge addressed a question to a named individual; only then craft a distinct message per candidate carrying just that candidate's questions. If the candidates opened with pitches and the judge has no questions yet, relay the condensed pitches to the judge instead.
6. Repeat the cycle: read the answers, condense each to a few sentences, relay to the judge ("here are the candidates' answers — follow-ups, or is a decision ready?"), await. Every round is sendCommand(s) plus one awaitAll in a single batch, and every round overwrites the scratch state key so compaction cannot lose the ids. On a VERIFIED interview expect the judge to come back with evidence-based challenges and a top-2 shortlist before its verdict; relay those challenges to the named candidates only, then run one more round. Read any blocking prompt literally. Skip only an optional survey when its controls permit; permission/trust questions require the specific authorized response, never blind “Skip” or “Continue”. Set a finite round budget with the judge; if no verdict emerges, report the unresolved criteria rather than looping indefinitely.
7. Close out. Read the final verdict completely (`terminal.read`, `artifact.read` if archived) and present the judge's decision and reasoning to the user. Close terminals ONLY if the user explicitly asked to close the losers — `terminal.close` with the exact recorded ids, judge left open unless asked. Then `scratch.drop`.

Guardrails:
- You are the conductor, not a participant. Never answer the judge's questions yourself, never add your own assessment of a candidate, and never override the verdict — the judge decides. If its verdict looks wrong, report that as an observation rather than substituting your own.
- A summary is a lossy paraphrase: when a summarized verdict conflicts with the visible terminal text, trust the terminal text and re-read it.
- Condense when relaying; never paste raw scrollback into another agent's input.
- "Verification" means the judge agent reading and searching the project itself — that alone is not CI, runner or forge evidence. Nothing in this runbook fetches forge state: if the brief needs it, read it BEFORE the round (daintree.workflow.follow-up-on-issue) and relay it to the judge as a given fact; never ask the judge to fetch it. The Assistant’s forge tools do not expose job-log bodies or formal/inline review text. Fetch such evidence with authorized provider tooling through an agent when needed, then relay it; do not present the CI roll-up as the missing detail.

Confirmation: needed only to close terminals, or to run an interview that requires file changes.

Record the same judging rubric and evidence requirements for every candidate. Preserve the judge’s exact questions and constraints when condensing; do not accidentally give one candidate an easier question. For an attributed interview say that the judge knows the candidates; fresh neutral labels cannot erase prior interaction. Obey the shared wait budget, carry state through async pauses, and report a provisional/no-verdict result if an absent candidate or missing verification changes the outcome. A top-two shortlist requires at least two eligible candidates; do not invent a second finalist.

Report back: the winner, the judge's reasoning (including the top-2 shortlist on a verified run), how many rounds it took, and which losing terminals were closed, if any.
