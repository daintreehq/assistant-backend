---
id: daintree.orchestration.basic
title: Daintree orchestration basics
summary: How to inspect Daintree state and choose safe orchestration actions.
whenToUse: Use for general requests about current Daintree state, terminals, worktrees, agents, closed/resumable sessions, queues, timers, or what to do next.
priority: 100
risk: read
tags:
  - daintree
  - state
  - orchestration
  - mcp
  - status
  - monitor
  - sessions
requiredTools:
  - context.snapshot
  - daintree.status
  - worktree.list
  - worktree.getCurrent
  - git.getProjectPulse
  - daintree.listTools
  - tool.search
  - daintree.call
  - agentTask.spawnForEdits
  - queue.publish
  - queue.digest
  - queue.resolve
  - terminal.read
  - terminal.summarize
  - terminal.extract
  - terminal.extract.async
  - terminal.awaitAll
  - terminal.sendCommand
  - timer.schedule
  - timer.list
  - timer.cancel
  - watcher.terminal.create
  - watcher.list
  - watcher.cancel
  - grant.create
  - grant.list
  - grant.revoke
---
Use when: the user asks about current Daintree state or what to do next.
Procedure:
1. Establish current state first. Prefer context.snapshot; use Daintree MCP read tools for more detail. For worktrees specifically, prefer the typed reads worktree.list (enumerate all worktrees by id/status) and worktree.getCurrent (the active one) over digging through the snapshot or a raw daintree.call.
2. Never guess the active worktree, focused terminal, agent state, git state, or recipe availability — read it.
3. For tool discovery, use tool.search before a raw daintree.call.
4. To wait on an agent or long-running process, never hand-poll terminal output in a loop. Choose ONE supervision mode: (a) IN-TURN — when you are driving an interactive relay the user is waiting on, spawn the agents with NO watcher and wait for the whole cohort with one terminal.awaitAll (it polls their agentState concurrently — fast, no model call), then read the WHOLE cohort with ONE terminal.extract.json over all the terminalIds (an array of `{terminalId, …}`) — when it returns matched:true with one entry per agent, those ARE the answers; go straight to scoring/relay and do NOT re-read the same agents with per-terminal terminal.extract calls afterward (verify each, and self-heal any that reported finished but still look busy — re-await/watch it), then relay with terminal.sendCommand. awaitAll names its stragglers at the top level (`stillWorking` / `askingQuestion` id arrays). If the ENTIRE cohort reports still-working for the whole budget (especially round 1, right after spawn) or a read returns "Terminal not found", suspect a BAD/truncated/stale terminalId before assuming slow agents — re-read live ids with terminal.list (or context.snapshot), confirm each id matches a listed terminal EXACTLY and in full, fix any mismatch, then resume. If the budget runs out, re-await the `stillWorking` ids directly (not the whole cohort) **at most twice** — three awaitAll calls total on the same terminal (raise maxAttempts, default 30 ≈ 60s / max 240 ≈ 480s, on a re-await for a known-slow agent rather than looping). After two re-awaits with no finish, treat the agent as HUNG and take the defined exit — never a fourth await: if other agents finished, proceed and note the hung one in the synthesis; if it is the only work left, publish a blocked inbox item (`queue.publish` with the required `source:"model_worker"`, `severity:"blocked"`, `title`, and `summary`, plus `target:{terminalId}` and `dedupeKey:"hung-<terminalId>"`) and attach a watcher (`watcher.terminal.create` with `stopWhen:{ all:[{stateIs:"waiting"},{modelJudge:"Has the agent finished its work and stopped, not just paused?"}] }`), then end the turn. (b) BACKGROUND — for fire-and-forget work, spawn WITH a watcher (watch:true, watchGoal) or schedule a timer, then end your turn and react on the attention-queue wake. Never attach a watcher to an agent you are also waiting on in-turn.
5. When creating a terminal watcher (BACKGROUND supervision), set stopWhen/alertWhen with the WatchCondition DSL. Members: stateIs, runtimeStatusIs, contains, regex, noOutputForMs, modelJudge, and one level of all/any/not combinators. Prefer the deterministic leaves (contains/regex/stateIs/runtimeStatusIs/noOutputForMs) — they are free. Reach for modelJudge only when a condition needs semantic inference the deterministic leaves cannot express: each distinct modelJudge question is one model call per check at the watcher's configured tier (modelTier, default small; deduped across stopWhen and alertWhen). Worked example — stop a build watcher once the build resolves, alert if it breaks:
   watcher.terminal.create({ terminalIds: ["terminal-5284bfef-3d11-424c-90cb-136f24046295"], title: "build", goal: "wait for green",
     stopWhen: { any: [{ contains: "BUILD SUCCEEDED" }, { stateIs: "exited" }] },
     alertWhen: { modelJudge: "Did the build fail or report errors?" } })
   Special case — "stop when the AGENT finishes its turn": do NOT use a bare `stateIs: "waiting"` stopWhen. A `waiting` state is an unreliable proxy — an agent reads "waiting" before it starts, when paused, or when its window is backgrounded — so a raw `stateIs: "waiting"` stop fires too early. For an explore agent you usually need NO custom stopWhen: the default supervisor already treats working→waiting as a candidate and CONFIRMS it with a small-model tail check before stopping (and publishes completed_*). If you must express "finished" yourself, pair the state with a judge so a bare idle can't false-trip: `stopWhen: { all: [{ stateIs: "waiting" }, { modelJudge: "Has the agent finished its work and stopped, not just paused?" }] }`. Trust that confirmed completion — never hand-poll the terminal in a loop to decide it yourself. (For IN-TURN coordination you do not need a watcher at all: terminal.awaitAll blocks on the whole cohort's agentState and hands the result straight back — reach for it instead of building a watcher just to block once. Note awaitAll is state-based, not a small-model check, so verify its result by reading the tail and self-heal a terminal that still looks busy.)
6. Use queue.digest to summarize sub-thread updates. When you have fully handled an inbox item — reported a finished agent, surfaced a result the user has now seen — resolve it with queue.resolve {"id": "<id>"} so the "needs attention" badge reflects only what still needs the user; a finished watch has already stopped its own watcher, so resolve the item rather than trying to watcher.cancel it. Leave OPEN anything still awaiting the user (an agent waiting on a question).
7. Closed / resumable sessions. When the user asks to see or reopen a session they closed, list them with `daintree.call agentSessionHistory.list` (optional `worktreeId` scopes it to one worktree; omit it to list every resumable session across all worktrees). It returns records — `{ sessionId, agentId, worktreeId, title, projectId, savedAt (newest-first), agentLaunchFlags?, agentModelId?, cwd?, branch? }` — so surface the relevant ones by title / branch / when. There is NO restore tool, and you cannot read a closed session's transcript. To bring one back, spawn a FRESH agent in that worktree with agentTask.spawnForEdits (reuse the record's worktreeId and agentId; write a taskPrompt for what to continue) — and tell the user plainly it is a new agent in the old worktree, not a faithful resume of the original session (its prior conversation is not restored).
Report back: a concise status, any watcher/timer ids, and the next checkpoint.
