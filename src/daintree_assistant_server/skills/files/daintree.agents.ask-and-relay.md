---
id: daintree.agents.ask-and-relay
title: Ask an agent and relay its answer
summary: Spawn a configured agent in read-only mode, ask it a question, wait for completion, and report its answer cleanly.
whenToUse: Use when the user asks you to spawn a single agent, ask it one question, and relay its answer. Do not use to poll the whole pinned/configured fleet (use the poll-pinned-agents skill) or for multi-round debate, a scoring competition, or interviews (use the relevant orchestration skill).
risk: project
tags:
  - daintree
  - agent
  - terminal
  - ask
  - relay
  - explore
  - read-only
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
---
Use when: the user asks you to create/spawn/open a configured agent terminal, ask it a question, and relay the answer.

Procedure:
1. If the user provided the question, use it exactly. If they say “you decide,” choose a small, safe, self-contained question. Do not crawl the project just to invent a question; if you need context, use context.snapshot or the minimum read-only context available, not a broad file listing.
2. Choose the agent from the configured-agents roster available this turn. Treat a named agent from the user as a hint that must resolve to a real configured agent id; if it does not resolve, ask which available agent to use. If no agent is named, pick whichever configured agent is best suited to answer the question.
3. Decide the launch context. If the question is about a project/worktree, resolve the target worktree from context.snapshot; if ambiguous, ask. If the question is general and not project-specific, use the current workspace only as the launch context and make the prompt explicitly read-only.
4. Spawn with agentTask.spawnForEdits in `mode: "explore"`. Use a distinct short title, the resolved real `agentId`, the target worktree/context if required by the tool, and a taskPrompt that includes: the exact question, “answer concisely,” and “read-only: do not create, modify, delete, or mutate files/state.” Use ONE spawn call; do not use terminal.run.async, raw daintree.call, or an edit-mode spawn for this pattern.
5. For this ask-and-relay pattern, supervise in-turn: do NOT attach a watcher, do NOT call agentTask.superviseTerminal, and do NOT set watch/watchGoal. You are about to wait with terminal.awaitAll; adding a watcher double-supervises the same terminal and creates a later stale inbox event.
6. Wait for the returned terminalId with terminal.awaitAll. If it reports `askingQuestion`, answer with terminal.sendCommand only when you can answer safely; otherwise ask the user. If it reports `stillWorking`, re-await that terminal id directly at most twice, raising maxAttempts if needed. After that, stop looping and hand off with terminal.await.async, telling the user you will report when it finishes.
7. Read the result cleanly. Default to terminal.summarize for the gist. Use terminal.extract when you need a specific final answer field. Use terminal.read only when the user explicitly needs exact literal wording or summarize/extract clearly missed essential content; do not paste raw TUI scrollback.
8. Report the agent’s answer with attribution and the terminalId. Keep your own commentary separate from the agent’s answer.
9. If a completed watcher/inbox event appears later for the same terminal despite this runbook, and you already relayed the result, just clear it with queue.resolve and say it was already handled. Do not re-read or re-report the same answer.

Confirmation: spawning a visible agent terminal mutates Daintree state — confirm before launch per the active tier.

Report back: the agent used, terminal id, the relayed answer or gist, and whether anything is still pending.
