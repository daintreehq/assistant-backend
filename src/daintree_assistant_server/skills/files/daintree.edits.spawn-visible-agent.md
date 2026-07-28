---
id: daintree.edits.spawn-visible-agent
title: Spawn a visible agent for edits or exploration
summary: How to handle requests that need a visible agent — to change files or to explore read-only.
whenToUse: Use when the user asks for a direct visible agent to change project files (implement, refactor, fix, add tests, update docs) or to explore/investigate a project read-only, and no more specific workflow applies. Prefer a typed workflow first — daintree.workflow.start-work-on-issue for a forge issue, daintree.workflow.prep-branch-for-review for a PR, daintree.recipe.run-or-create for a workspace recipe, daintree.orchestration.fan-out-work when several independent units each need their own agent, or a poll/competition/interview/multi-agent skill when several agents work together. This is the fallback SINGLE visible-agent path; if the agent is already running and has gone wrong use daintree.agents.recover-agent.
risk: project
tags:
  - edits
  - agent
  - worktree
  - supervision
  - implement
  - fix
  - refactor
  - test
  - explore
  - investigate
  - spawn
  - launch
requiredTools:
  - context.snapshot
  - agentTask.spawnForEdits
  - terminal.awaitAll
  - terminal.await.async
  - terminal.summarize
  - terminal.extract
  - terminal.read
  - terminal.sendCommand
  - watcher.cancel
  - queue.resolve
---
Use when: the task needs a single visible agent — either to change files (implement, refactor, fix, tests, docs) or to explore/investigate a project read-only — and no more specific workflow fits. First check for a typed workflow: a forge issue → start-work-on-issue; preparing or opening a PR → prep-branch-for-review; a workspace recipe → recipe run-or-create; several agents polling, competing, or interviewing → the matching orchestration skill. If one of those fits, use it instead; this is the fallback visible-agent path.
Procedure:
1. Say plainly that the work needs a visible agent: for edits, "This needs file changes, so I'll spawn a visible agent to do them"; for exploration, "I'll spawn a visible agent to investigate this."
   Decide the supervision mode up front (it shapes the spawn in step 4):
   - EDITS, or any job whose OUTCOME needs verification/classification (did the tests pass? is the worktree clean?) → BACKGROUND: spawn WITH a watcher and end your turn; react when its completed_* event lands on the attention queue. Don't block the turn on a long edit.
   - A LONG explore/investigation whose result you will simply relay when it lands (no outcome verification needed) → ASYNC: spawn with NO watcher, then ONE `terminal.await.async({"terminalIds":["<the terminalId>"]})` on the NEXT turn (after the real id comes back) and END the turn — the runtime polls the agent's state for you (no model cost) and wakes you with the outcome; read and relay it then.
   - A QUICK read-only EXPLORE the user is waiting on right now → IN-TURN: spawn with NO watcher, then wait with terminal.awaitAll and relay the answer in the same turn. (For several explore agents at once, use the multi-agent orchestration runbook.)
   Pick ONE — never stack two supervisors on the same terminal (no watcher on an agent you await in-turn or async; no awaitAll on a terminal an async operation already watches).
2. Read only the minimum context needed to scope the task.
3. Resolve the target worktree before spawning. Check context.snapshot for the worktrees list and the active/current worktree, then apply this rule exactly: if the user named a worktree, branch, path, or id — resolve it to its worktreeId from the snapshot and proceed; if exactly one worktree plausibly matches the request — proceed with that worktreeId; if multiple plausible candidates exist and none was named (or none can be confidently chosen) — STOP, list the candidates, and ask the user which one to use; if no worktree plausibly matches or the worktrees list is unavailable — STOP and ask the user to name the target worktree. Do not silently fall back to the active worktree when the target is ambiguous. Only call agentTask.spawnForEdits once the worktree is resolved.
4. Use agentTask.spawnForEdits — NEVER a raw agent.launch via daintree.call — with: agentId (a REAL configured Daintree agent id, resolved against the available-agent registry in your startup context; a name the user spoke or typed is only a HINT, since voice transcription drops letters and a spoken id can arrive subtly misspelled, so do not pass an unverified name through — the wrapper rejects an unknown id with the available roster and a "did you mean" suggestion, and you then re-spawn with the right id, never the typo), mode ("edit" to change files, "explore" for a read-only investigation), title (short task name — becomes the spawned agent's visible name/tab label in Daintree the instant it launches, so keep it distinct when running several in parallel; you know what you're delegating, so name it correctly HERE and never follow the spawn with a terminal.rename — naming at spawn is the whole job), taskPrompt (exact instructions), and context.filePaths when known. Then supervise per the mode you chose in step 1:
   - BACKGROUND (edits / fire-and-forget) — attach a watcher with the FLAT top-level fields `watch: true` and `watchGoal: "..."` (plain top-level scalars; providing `watchGoal` alone also attaches the watcher). Emit them as their own top-level keys — never a dotted/flattened `watcher.create` key (an unknown field the strict decoder rejects). A complete legacy nested `watcher: {"create": true, "goal": "..."}` object is still accepted, but prefer the flat fields.
   - IN-TURN (quick explore you're waiting on) — OMIT `watch`/`watchGoal` entirely; you will block on terminal.awaitAll in step 8 instead. Attaching a watcher here would double-supervise the agent you are already waiting on.
5. For edit mode the taskPrompt must tell the agent to: make changes only in the selected worktree; run relevant tests if practical; report changed files, tests run, and remaining risks. If the user gave a narrow scope, include it explicitly: do only that scope; do not migrate/refactor/fix adjacent out-of-scope areas just because they are nearby; skip and report optional/out-of-scope work instead of asking unless it blocks the requested task. For explore mode: investigate without touching files and report findings. (The wrapper appends the matching constraints automatically.)
6. Never edit files yourself.
7. After the call returns, report what actually happened: quote the real terminalId/watcherId from the result. If the launch errored or returned no terminalId, or the watcher reports the terminal exited, say so — do not claim a clean launch.
8. Close the loop, per mode:
   - ASYNC — on the turn after the spawn returns its real terminalId, call `terminal.await.async({"terminalIds":["<the terminalId>"]})`, report what is running, and END the turn. The completion wakes you with the per-terminal outcome; read the result then (terminal.summarize / terminal.extract), relay it, and resolve the completion's inbox item with `queue.resolve`. Do NOT also awaitAll/extract-wait or poll async.list while it runs.
   - BACKGROUND — end your turn after the spawn; the watcher confirms completion and wakes you with a completed_* event. Once you've relayed the agent's result, clear that inbox item with `queue.resolve {"id": "<the inbox id>"}` so it stops counting as needing attention — a finished agent's watcher has already stopped ITSELF, so there is nothing to cancel, only the lingering inbox item to resolve. Never call `watcher.cancel` on an already-finished watch (it is refused as "already ended"). If instead you abandon a STILL-ACTIVE watch — you got what you needed early, or the task was dropped — cancel it with `watcher.cancel {"id": "<watcherId>"}` and say so plainly, noting you can re-attach later. Leave OPEN any item that still needs the user (an agent waiting on a question).
   - IN-TURN — block on `terminal.awaitAll({ terminalIds: ["<the terminalId>"] })` (bounded; polls agentState only, no model call), then read the result with terminal.summarize (or terminal.extract for a specific field) and relay it in the same turn — and since awaitAll is state-based, if the tail shows the agent is still working, re-await/watch it rather than relaying a half-done screen. The result names the stragglers at the top level (`stillWorking` / `askingQuestion` id arrays): if the budget ran out, re-await the `stillWorking` id directly **at most twice** (three awaitAll calls total on the same terminal; raise `maxAttempts` — default 30 ≈ 60s, max 240 ≈ 480s — on a re-await for a known-slow agent rather than looping). After two re-awaits with no finish, the agent is HUNG — stop re-awaiting (never a fourth await), hand it off, and END the turn: ONE `terminal.await.async({"terminalIds":["<terminalId>"],"title":"hung agent recovery"})` keeps watching it out-of-turn and wakes you when it eventually settles; tell the user plainly that the agent is slow and you'll report when it finishes.
   Question handling (all modes): if awaitAll, an async completion, or a watcher/queue item reports the agent in `askingQuestion`, read/extract the exact question before answering. If the original user request or safe scope guard answers it, answer directly; for adjacent optional work not requested, default to "no / stay in scope". If it needs user judgment, ask the user; if no interactive question surface is available and you cannot infer safely, stop and report the terminalId and question. When sending an answer, send exactly the protocol answer, not prose: for a plain yes/no prompt with no id, send one lowercase token (`yes` or `no`) and nothing else; if the question shows `QUESTION_ID=<id>`, include that id in a single machine-readable line `QUESTION_ID=<id> ANSWER=<yes|no>` (or `ANSWER=<short answer>` for non-yes/no), with no extra words or punctuation. Then await again with a real budget. If the same question/id remains after one protocol-correct answer, do not spam variants; read the tail once to confirm the id/text, retry only if your first answer was malformed or omitted the id, otherwise report the agent as blocked and leave any still-needing-user inbox item open.
   If awaitAll, async, or watcher status reports failed, say so. On the happy path there is no inbox item to resolve — the hung-agent handoff is the one exception (its async completion arrives as an inbox item you resolve after relaying).
Confirmation: spawning the agent mutates real state — confirm before launch per the active tier.
Report back: the terminal id, the watcher id if you attached one (BACKGROUND), and either the relayed result (IN-TURN) or the expected next update (BACKGROUND).
