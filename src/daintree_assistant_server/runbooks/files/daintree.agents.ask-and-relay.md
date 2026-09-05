---
id: daintree.agents.ask-and-relay
title: Ask an agent and relay its answer
summary: Spawn a configured agent in read-only mode, ask it a question, wait for completion, and report its answer cleanly.
whenToUse: 'Use when the user wants ONE agent asked ONE question and its answer brought back — "ask claude what it makes of the retry logic", "get an agent to look at the auth module and tell me what it finds", "spawn something to check X and report back". The signal is that the DELIVERABLE IS THE ANSWER, relayed this turn — read-only, one spawn, one in-turn wait, no watcher. Do not use when the deliverable is WORK rather than an answer — implementing, refactoring, fixing, or an investigation the user wants to watch and steer (use daintree.edits.spawn-visible-agent); to poll the pinned or configured fleet (daintree.agents.poll-pinned-agents); for multi-round debate, a scored competition or a judged interview (the relevant daintree.orchestration runbook); or when the agent already exists — answer it with daintree.agents.answer-waiting-agent, or use daintree.agents.recover-agent if it has gone wrong.'
confirmation: "Putting a question to a fresh agent. The aim is to bring its answer back intact and clearly attributed, since what is wanted here is the answer rather than changed work."
risk: project
requiredTools:
  - context.snapshot
  - agentTask.spawnForEdits
  - terminal.awaitAll
  - terminal.sendCommand
  - terminal.await.async
  - terminal.summarize
  - terminal.extract
  - terminal.read
  - queue.resolve
  - workflow.update
---
Use when: the user asks you to create/spawn/open a configured agent terminal, ask it a question, and relay the answer.

Procedure:
1. If the user provided the question, use it exactly. If they say “you decide,” choose a small, safe, self-contained question. Do not crawl the project just to invent a question; if you need context, use context.snapshot or the minimum read-only context available, not a broad file listing.
2. Choose the agent from the configured-agents roster available this turn. Treat a named agent from the user as a hint that must resolve to a real configured agent id; if it does not resolve, ask which available agent to use. If no agent is named, pick whichever configured agent is best suited to answer the question.
3. Decide the launch context. If the user named a different project/worktree, resolve it from context.snapshot and pass its exact `worktreeId`; ask only for genuine target ambiguity. Otherwise use the turn-bound default, including for a question about this project. If the question is general and not project-specific, OMIT `worktreeId` — the agent then launches in the worktree this turn started in, which stays fixed even if the user switches worktrees mid-turn — and make the prompt explicitly read-only. Do not describe the target as "wherever is active": what is active can change under you, and the omitted default deliberately does not.
4. Spawn with agentTask.spawnForEdits in `mode: "explore"`. Use a distinct short title, the resolved real `agentId`, the target worktree/context if required by the tool, and a taskPrompt that includes: the exact question, “answer concisely,” and “read-only: do not create, modify, delete, or mutate files/state.” Use ONE spawn call; do not use terminal.run.async, raw daintree.call, or an edit-mode spawn for this pattern.
5. For this ask-and-relay pattern, supervise in-turn: do NOT attach a watcher, do NOT call agentTask.superviseTerminal, and do NOT set watch/watchGoal. You are about to wait with terminal.awaitAll; adding a watcher double-supervises the same terminal and creates a later stale inbox event.
6. Wait for the returned terminalId with terminal.awaitAll. If it reports `askingQuestion`, answer with terminal.sendCommand only when you can answer safely; otherwise ask the user. If it reports `stillWorking`, re-await that terminal id directly at most twice, raising maxAttempts if needed. After that, stop looping and hand off with terminal.await.async, telling the user you will report when it finishes.
7. Read the result cleanly. Default to terminal.summarize for the gist. Use terminal.extract when you need a specific final answer field. Use terminal.read only when the user explicitly needs exact literal wording or summarize/extract clearly missed essential content; do not paste raw TUI scrollback.
8. Report the agent’s answer with attribution and the terminalId. Keep your own commentary separate from the agent’s answer.
9. If a completed watcher/inbox event appears later for the same terminal despite this runbook, and you already relayed the result, just clear it with queue.resolve and say it was already handled. Do not re-read or re-report the same answer.

Confirmation: spawning a visible agent terminal mutates Daintree state — confirm before launch per the active tier.

Keep the question verbatim inside a self-contained brief, including context the new agent cannot see. After an async handoff, retain the original question and terminal/launch/workflow ids; inspect the answer before reporting success. A timeout means no answer yet, not an incorrect answer. Preserve qualifications, citations and dissent from the agent; do not turn a summary into an exact quotation. Close its flat workflow row on the evidenced outcome even though the terminal stays open.

Report back: the agent used, terminal id, the relayed answer or gist, and whether anything is still pending.
