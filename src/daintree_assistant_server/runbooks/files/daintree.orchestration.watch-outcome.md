---
id: daintree.orchestration.watch-outcome
title: Watch an outcome unattended
summary: Set up supervision or a scheduled action that outlives the turn — a terminal watcher, an async verdict, a PR watch, or a timer that ACTS at fire time — and pre-authorize with a narrow grant so it can run with nobody present.
whenToUse: Use when the user wants something to happen LATER rather than now — either to be woken ("run this overnight and wake me", "let me know when the build finishes", "ping me when the PR merges", "remind me in an hour", "keep an eye on it while I'm out") or to have the assistant ACT unattended at a chosen time ("in ten minutes spawn a terminal and run the tests", "in 10 minutes' time spawn a terminal and run the test suite", "in half an hour send npm test to the build terminal", "wait an hour then start the migration", "at 6pm kick off the deploy check"). Any deferred action belongs here, INCLUDING one that spawns an agent — a delayed spawn is a scheduled action, not an immediate one, so do not fall through to daintree.edits.spawn-visible-agent just because the verb is "spawn" — that runbook is for spawning NOW. Also to pre-authorize an unattended action with a grant, and to list, cancel or revoke watchers, timers, async waits and grants you left. Do not use to cancel ONE named watcher (daintree.foundation), a one-off check of an agent that already finished (daintree.orchestration.verify-agent-work), triage of what is running now (daintree.orchestration.supervise-fleet), what happened while you were away (daintree.orchestration.status-and-resume), or wholesale teardown (daintree.orchestration.workspace-cleanup). `watcher.watchPR` sees only a PR's open/merged/draft/closed state, so a one-off read of CI, checks, reviews or an issue thread is not a watch at all (use daintree.workflow.follow-up-on-issue).
confirmation: "Setting up unattended supervision. The aim is to leave the right watcher or timer running after this turn, and to be plain about whether it wakes you, acts on its own, or only leaves a note."
risk: system
requiredTools:
  - context.snapshot
  - agentTask.spawnForEdits
  - terminal.sendCommand
  - watcher.terminal.create
  - watcher.watchPR
  - watcher.list
  - watcher.cancel
  - terminal.extract
  - terminal.await.async
  - timer.schedule
  - timer.list
  - timer.cancel
  - grant.create
  - grant.list
  - grant.revoke
  - async.list
  - async.cancel
  - queue.digest
  - queue.resolve
  - agentTask.superviseTerminal
  - daintree.invoke
  - terminal.run.async
  - tool.schema
---
Use when: the user wants observation, a reminder, or work at a stated later time to outlive this turn.

Set up the mechanism and report it. A scheduled action is not performed now as well. Preserve the requested delay and existing authority; resolve new scope before scheduling. Distinguish a stored record, an active scheduler, an assistant wake, and a human-facing inbox note.

Procedure:

1. Select ONE mechanism for each target after reading the live ids and existing `watcher.list`, `async.list` or `timer.list` as needed:
   - Plain completion of running agents: `terminal.await.async`. It returns an accepted handle, then wakes with per-terminal state outcomes. Inspect the task output on the wake; state settlement is not proof of success.
   - A command to start NOW in a terminal authorized for this task: `terminal.run.async`.
   - Acceptance-checked agent completion: spawn with `watch:true`, `watchGoal` and edit `acceptanceCriteria`, or adopt with `agentTask.superviseTerminal`. Reuse existing supervision; do not stack an async wait or a foreground wait on it.
   - A specific condition in terminal output: `watcher.terminal.create`, step 2. If the terminal already settled and only a verdict is wanted, read it once with `terminal.extract`; no watcher is needed.
   - A PR merging/closing: `watcher.watchPR`. It records a note; it does not wake the assistant or monitor CI/reviews.
   - A reminder for the human: a timer `enqueue` payload.
   - Work that must START later: a timer `call_safe_tool` payload targeting one resolved invocation, such as an agent spawn with a self-contained task brief (steps 4–5). Do not spawn an idle agent now merely to obtain an id for later.

2. Create an output watcher with required `terminalIds`, `title`, `goal`. Optional `stopWhen` ends it; `alertWhen` alerts while it keeps running. Set `cadenceMs` to the resolution needed (default 120000), `stopAfterMs` to a finite horizon (default 24 hours), and optionally `startAfterMs` and `modelTier:"small"|"medium"`. These generic watchers do not acquire the acceptance-checking behavior of a spawn supervisor just because you wrote a goal. For ordinary agent completion, prefer spawn/adopt supervision.

   Each WatchCondition has EXACTLY ONE key:
   - Free leaves: `stateIs` (`idle|working|waiting|directing|completed|exited`), `runtimeStatusIs` (`running|exited`), `contains` (literal substring), `regex` (Go/RE2), `noOutputForMs` (no new output for that interval).
   - Costed leaf: `modelJudge`, a yes/no question checked against the tail at each cadence. Only a confident yes matches; an unknown is not false and does not become true under `not`. Give pass and fail separate affirmative questions. Identical judge wording can be shared across stop/alert checks.
   - Combinators: `all:[...]`, `any:[...]`, `not:{...}`, with each child the same one-key shape. Keep the tree shallow.

   Example arguments to `watcher.terminal.create` for a build whose actual output uses these markers:

```json tool=watcher.terminal.create
{"terminalIds":["<exact terminal id>"],"title":"nightly build","goal":"Report whether this build passes or fails","stopWhen":{"any":[{"contains":"BUILD SUCCESSFUL"},{"contains":"BUILD FAILED"},{"runtimeStatusIs":"exited"}]},"alertWhen":{"contains":"BUILD FAILED"},"cadenceMs":30000,"stopAfterMs":43200000}
```

   Check that markers belong to this run, not old scrollback or echoed commands. A marker match is text evidence; exit alone is not a passing result. Never stop an agent watch on bare `stateIs:"waiting"`: it can fire on startup, a permission prompt or a pause. If a custom condition is essential, pair state with a final-answer judge; the standard spawn/adopt supervisor already performs a finish check.

3. Arrange authority BEFORE leaving. Unattended confirm-required mutations need a matching automation grant; interactive auto-approve does not authorize timer/wake actors. Only the interactive main actor can mint one.
   - For an assistant turn triggered by eligible watcher/async completion while away: `grant.create({"actorType":"wake","actorId":"wake","allowedToolNames":["terminal.sendCommand"],"ttlMs":28800000,"maxUses":3})`. Tailor the tools, lifetime and uses to the requested task.
   - For a `call_safe_tool` timer: create the timer, read its real `tmr_…` id, then grant against `actorType:"timer",actorId:"<that id>"` in the next tool round of THIS user turn. A grant on the wrong actor is never consumed.
   - Watcher checks observe and publish; any assistant turn they trigger runs as `wake`. Do not mint an unused watcher-scoped grant for that turn.
   - Require `ttlMs`, `maxUses` and a narrow non-empty `allowedToolNames` list. Use dotted CLI names, not wire names. TTL starts NOW and must cover delay, expected lateness and execution. No blanket risk-class grants.
   - `grant.create`, `grant.revoke` and `daintree.call` cannot be granted. For a discovered, reviewed dynamic action, scope to the exact composite `daintree.invoke:<action>`; never to the bare invoker. Check `invocable:true` and `tool.schema` first. Unclassified or withheld actions remain unavailable for unattended execution.
   - A grant cannot exceed the tier: supervisor permits read/local/ui; operator adds terminal/project/external; system adds git/system. The tier gate runs before the grant lookup. Daintree's own host confirmation can still require the human.
   If a timer was created but its required grant cannot be obtained, cancel the timer and report that the deferred action was NOT armed; do not leave a predictably failing one-shot scheduled.

4. For a DELAYED AGENT TASK, schedule the spawn itself:

```json tool=timer.schedule
{"title":"run the build later","delayMs":600000,"payload":{"type":"call_safe_tool","toolCall":{"toolName":"agentTask.spawnForEdits","args":{"agentId":"<configured agent id>","mode":"explore","title":"verify build","worktreeId":"<resolved worktree id>","taskPrompt":"Run the requested test suite in this checkout and report the command, result and any failures. Do not change source files.","watch":true,"watchGoal":"Report the completed test result or blocker"}}}}
```

   These are `timer.schedule` arguments; the nested args must also match the target tool's schema. Grant the returned timer id permission to call `agentTask.spawnForEdits` before leaving. Resolve the agent and destination NOW. Include the task, limits and success condition in the brief; do not refer only to "that one" or rely on scratch surviving a restart. Do not embed credentials. A later spawn's watcher attachment is best-effort; report the registered timer now and inspect the actual launch/supervision result when available.

   The pinned CLI accepts ONLY `enqueue` and `call_safe_tool`, not a `message` or `run_check` payload. It cannot schedule an arbitrary future assistant conversation or autonomously resume a workflow graph. If the task cannot be expressed as a supported fixed call/agent brief, state that limit and offer a reminder instead of pretending the work is armed.

   Use relative `delayMs` when no trusted clock is available; do not invent `fireAt`. For a wall-clock request, resolve timezone and delay from a trustworthy time source or ask for the missing time information. Supply only one time form. For repeats choose a sensible `everyMs` plus finite `maxRuns` or `until`, and enough grant uses for the intended runs; the current schema does not enforce a one-minute minimum or finite recurrence. Missed repeats can collapse into one catch-up fire, so this is not a guarantee of one execution per missed interval.

5. Use a FROZEN CALL only when one exact invocation is the deliverable:

```json tool=timer.schedule
{"title":"run the known build command","delayMs":600000,"payload":{"type":"call_safe_tool","toolCall":{"toolName":"terminal.sendCommand","args":{"terminalId":"<exact authorized terminal id>","command":"<verified bounded build command>"}}}}
```

   Schedule first, then grant the returned timer id as step 3 requires. Payload arguments are frozen NOW: supply an explicit worktreeId for a delayed spawn, since an omitted target resolves at FIRE time. `target` only labels events; it supplies no payload argument. Do not use a delay so short that the fire can race the grant.

   Despite its name, `call_safe_tool` can target any registered tool, subject to ordinary tier/confirmation policy. Keep this task to the requested work; do not build self-replicating timer chains. It performs ONE AT-MOST-ONCE attempt per firing: the timer is marked fired (or a repeat rescheduled) before dispatch, and a grant is consumed before the handler runs. An unreachable MCP or handler failure burns that attempt; it is not retried. Terminal-fire grant revocation happens AFTER dispatch, so a one-shot can spend its grant. A read-only payload needs no grant.

   A fixed spawn can carry a multi-step agent brief. It cannot give that agent the Assistant's control-plane tools, and a timer's own result does not wake the Assistant. Do not promise automatic follow-up from an unconfirmed watcher.

6. For a REMINDER only:

```json tool=timer.schedule
{"title":"review the result","delayMs":3600000,"payload":{"type":"enqueue","message":"Review the build result in Daintree."}}
```

   `enqueue` posts an inbox note, runs no tool and triggers no autonomous assistant turn. It is not a substitute for requested deferred work unless the user agrees to a reminder. Bound a repeating reminder to the requested cadence and horizon.

7. Read setup results and END the user turn. Report every actual timer/async/watcher/grant id and which of these applies:
   - WAKES THE ASSISTANT: eligible terminal watcher events and async completion; inspect per-terminal evidence on the wake.
   - ACTS LATER: a timer `call_safe_tool` performs one fixed call and records its result. A fixed call itself does not wake the assistant.
   - LEAVES A NOTE: `watcher.watchPR` or timer `enqueue`.
   Confirm that the scheduler/background supervisor is available before promising after-close work. If Daintree or its scheduler is stopped, records can remain stored without checking/firing. Say "scheduled" or "watch registered", not "will have run". A missing watcher id on an issue-start result requires a watcher-ledger lookup, not a duplicate launch.

8. On a wake, revalidate targets and authority. Read the event and current state; do not replay an already completed action from a stale timer or compacted checkpoint. Report blocked, failed, timed-out and unverified outcomes accurately. A refused one-shot has already fired; a pending approval does not replay it. Take a newly authorized action or reschedule interactively only when the user wants that. Resolve the handled queue item once reported.

9. Retire only the requested tracking. Use `watcher.cancel({"id":"..."})`, `timer.cancel({"id":"..."})`, `async.cancel({"asyncId":"..."})`, and `grant.revoke({"id":"..."})`. Check scope and shared cohorts before cancelling; cancelling tracking never kills or closes the underlying terminal. Finished watchers are already stopped. Revoke task-specific wake authority when its window ends, preserving grants for unrelated live work.

PR watcher boundary: `watcher.watchPR({"prNumber":123,"cwd":"<verified worktree path>","title":"PR outcome"})` sees `open|merged|draft|closed` and STOPS ON ITS OWN on merged/closed. A coarse activity ping is not a CI or review verdict. For CI now, use the follow-up-on-issue runbook; for future log diagnosis, supervise an agent terminal that can actually observe the result. Never promise "merge when green" from this watcher or a grant for `daintree.call`.

Report back: the selected mechanism, target and exact handles, trigger/delay/expiry, whether it wakes, acts or only leaves a note, whether scheduling and authority succeeded, and any verification or availability limitation.
