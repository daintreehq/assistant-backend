---
id: daintree.agents.poll-pinned-agents
title: Poll pinned agents
summary: Poll every pinned agent with the same question, collect structured verdicts, and synthesize the consensus.
whenToUse: Use when the user asks to ask or poll all pinned agents for an opinion, verdict, or short answer and wants a synthesized response.
priority: 100
risk: project
tags:
  - daintree
  - agents
  - pinned-agents
  - poll
  - consensus
  - orchestration
  - structured-extract
requiredTools:
  - context.snapshot
  - daintree.call
  - agentTask.spawnForEdits
  - terminal.awaitAll
  - terminal.extract.json
  - terminal.read
  - terminal.summarize
---
Use when: the user asks to poll/ask all pinned agents — not existing open terminals — for a decision, opinion, or short answer, and wants a synthesized consensus.

Procedure:
1. Get the roster. Call `context.snapshot` to see current terminals and `daintree.call` for `agentSettings.get`; filter returned agents to `pinned:true`. Treat "pinned terminals" as shorthand for pinned agents unless the user explicitly says open terminals. Do NOT use docs search to discover pinned agents, and never relay raw settings/env/secrets — only agent ids/display names and pinned status.
2. If the user only asked "can you see them?", report the pinned-agent count and names. If they asked to poll/ask them, continue. If no pinned agents exist, report that and stop.
3. Compose one neutral, self-contained prompt with the user's question/brief. Tell each agent it is being polled independently, to not modify files, and to finish with a compact structured answer: `verdict` plus 2-5 `reasons`. Do not first do your own local investigation unless the user asked for your opinion too; put any needed context into the prompt.
4. Spawn one fresh read-only terminal per pinned agent using `agentTask.spawnForEdits`, e.g. `{"agent":"<agentId>","mode":"explore","title":"poll: <agent name>","taskPrompt":"<neutral poll prompt>"}`. Use one call per agent, in one batch or groups of about 4. Build from fresh spawns; do not commandeer an existing terminal unless the user explicitly named it. Ensure each call's JSON args are valid; if a spawn call fails, retry only the missing agent(s), not the successful ones.
5. When spawn results return, record each full `terminalId`→agent map exactly. Do not call await/extract in the same batch as the spawns that mint ids.
6. Wait for responses with one `terminal.awaitAll({"terminalIds":[...],"maxAttempts":60})`, not one wait per agent. If all finish, proceed. If some are `stillWorking`, read finished agents now and re-await only the still-working ids; cap at two re-awaits, then omit hung agents with a note.
7. Extract verdicts in one `terminal.extract.json` call over the finished terminalIds. Use a real JSON Schema, not a sample object, e.g. `{"type":"object","properties":{"answers":{"type":"array","items":{"type":"object","properties":{"terminalId":{"type":"string"},"verdict":{"type":"string"},"reasons":{"type":"array","items":{"type":"string"}}},"required":["terminalId","verdict","reasons"]}}},"required":["answers"]}`. For a single late finisher, either use the same schema or a single-answer schema. If `extract.json` fails or looks sparse, use `terminal.read` or `terminal.summarize` for that terminal.
8. Normalize verdict labels only enough to tally equivalent positions; keep meaningful nuance in the reasons. Map each answer back to the agent name via `terminalId`, not by terminal title.
9. Synthesize: state consensus ratio, majority verdict, dissenters, and 1-3 strongest reasons on each side. Do not dump raw scrollback.
10. Leave the spawned terminals open and idle unless the user explicitly asks to close them.

Confirmation: The user's explicit request to "ask/poll all pinned agents" is enough confirmation to spawn fresh read-only agent terminals. If they only ask about the roster or capabilities, ask before spawning.

Report back: the pinned agents polled, consensus tally, per-agent verdicts with concise reasoning, any agent omitted or still running, and the synthesized recommendation/answer.

Notes:
- If `agentSettings.get` returns sensitive env/config fields, ignore them and never quote them.
- If the user corrects you midstream that they wanted an agent poll, stop local analysis and switch to the poll; do not keep digging locally.
