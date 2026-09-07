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
---
Use when: the user wants something to OUTLIVE this turn — wake me when it finishes, alert me if it breaks, remind me later, or DO it at a stated later time — rather than a wait you sit through now.

You are setting up the mechanism, not doing the work in this turn: on a wake you spawn, verify and relay — never fix the build, edit the branch, or land a merge yourself. A scheduled action is the same discipline moved in time: you arrange the ONE call that will run later, you do not run it now. Reads need no authority; only a MUTATION performed unattended needs the grant in step 3.

Procedure:

1. Resolve full terminal ids or the PR number live this turn, then choose ONE mechanism:
   - Terminal outcome needing classification ("wake me when it's green") → `watcher.terminal.create`, step 2.
   - A pass/fail verdict → if already settled, `terminal.extract` judges it now. Otherwise use a terminal watcher with `modelJudge`. It matches only a CONFIDENT yes; uncertainty remains UNKNOWN and satisfies nothing, including `not`. Give each outcome its own affirmative question: `stopWhen` an `any` of "did it pass?" and "did it fail?", `alertWhen` the failure question alone.
   - PR terminal state → `watcher.watchPR {"prNumber":123,"title":"…"}` (optional `cwd`, `stopAfterMs`). Read "What the PR watcher cannot watch" before promising a CI alert.
   - Reminder without work → `timer.schedule` with `enqueue`, step 4.
   - Work that starts later → a timer with `message`; use `call_safe_tool` only for one already-decided call. See "Work that starts later" for delivery and grant rules. Do NOT do the work now too, or spawn an idle terminal merely to obtain an id.
   Spawning NOW? `agentTask.spawnForEdits` with flat `watch:true`, `watchGoal:"…"`, and `acceptanceCriteria` already supervises completion. Add no second watcher.

2. `watcher.terminal.create` requires `terminalIds` (full ids), `title`, and a plain-language `goal`. Optional: `stopWhen` (ends with `condition_met`), `alertWhen` (alerts and KEEPS running), `cadenceMs` (default 120000), `modelTier` (`small` | `medium`), `startAfterMs`, `stopAfterMs` (default 24 h). Set the horizon you mean; prefer free deterministic conditions in the DSL below and only the cadence you need.

3. Pre-authorize unattended MUTATIONS now. Only the interactive main actor can mint a grant. Without one, a confirm-required action becomes a pending-approval item; a one-shot `call_safe_tool` timer has already fired and cannot retry that action.
   `grant.create {"actorType":"wake","actorId":"wake","allowedToolNames":["terminal.sendCommand"],"ttlMs":28800000,"maxUses":3}`
   - A watcher or timed-message autonomous turn dispatches as `wake`, with literal `actorId:"wake"`. NEVER mint a watcher grant: watcher checks only read and publish.
   - A `call_safe_tool` timer dispatches as `timer`, with its own `tmr_…` id. Schedule it first, then grant against the returned id in the SAME turn. A timed MESSAGE needs a wake grant instead.
   - Interactive auto-approve covers neither actor. `ttlMs`, `maxUses`, and at least one scope (`allowedToolNames` / `allowedRiskClasses`) are required. Use explicit tool names, the expected number of calls, and only the needed time window; never a blanket risk-class grant.
   - `grant.create`, `grant.revoke`, `daintree.call`, and the bare dynamic-invoke entry point cannot be granted. A wrapper-less MCP mutation can use its exact resolved identity daintree.invoke:ACTION; read-risk targets need no grant. Otherwise do it interactively or explain that it waits for the user.
   - A grant cannot widen the tier: `supervisor` allows read/local/ui, `operator` excludes git/system. The tier gate runs first.

4. Schedule with relative `delayMs`, never an invented `fireAt` — you have no reliable wall clock. Choose the payload:
   - Reminder: `timer.schedule {"title":"…","delayMs":3600000,"payload":{"type":"enqueue","message":"…"}}`. It posts a note for the human; it runs nothing and does not wake the closed assistant.
   - Deferred work: `timer.schedule {"title":"…","delayMs":1500000,"payload":{"type":"message","message":"Send npm test to the build terminal and report the result"}}`. At fire time you choose tools against live state. See the timed-message section below.
   - Frozen call: `payload:{"type":"call_safe_tool","toolCall":{"toolName":"…","args":{…}}}`. Almost any registered tool is eligible except `timer.schedule`; ordinary tier/confirmation rules still apply. See the fixed-call section below.
   A `repeat` requires `everyMs`. Repeating `message` or `call_safe_tool` must be at least 60s apart and bounded by a realistic `maxRuns` or `until`; `enqueue` is exempt because it only writes a row. Optional `target` labels events with `terminalId` / `worktreeId` / `projectId` / `workflowRunId`; it supplies no payload arguments.

5. Report the ids and END the turn. Do not also wait in-turn. Distinguish the delivery:
   - WAKES YOU: a terminal watcher meeting `stopWhen`/`alertWhen`, or a finished `terminal.await.async`. A `watch:true` spawn earns this promise only if it returned a `watcherId`.
   - ACTS: a timer `message` runs an ordinary turn (normal confirmations when open, wake grants when closed). A `call_safe_tool` runs one fixed call under timer authority and queues its result; it does not itself wake you.
   - NOTE ONLY: `watcher.watchPR` and timer `enqueue` leave an inbox item until the user looks.

6. When the window closes, inspect `watcher.list`, `timer.list`, `async.list`, `grant.list` (optional `actorId`). Retire with `watcher.cancel` / `timer.cancel` / `grant.revoke` (`{"id":"…"}`) or `async.cancel {"asyncId":"asy_…"}`. Async cancellation stops tracking, not the terminal. Revoke wake grants as soon as the window ends.

## The WatchCondition DSL

`stopWhen` and `alertWhen` each take a WatchCondition: an object with EXACTLY ONE key.

Deterministic leaves — free, prefer these:
- `stateIs` — agent state: `idle` `working` `waiting` `directing` `completed` `exited`.
- `runtimeStatusIs` — `running` or `exited`.
- `contains` — literal substring in the terminal tail.
- `regex` — Go/RE2, must compile.
- `noOutputForMs` — no NEW output for that many ms.

Costed leaf: `modelJudge` — a yes/no question answered against the tail on EVERY check, one model call each at `modelTier` (deduped across `stopWhen`/`alertWhen`, so reuse identical wording if both need it). A tight cadence plus a judge over a long night is thousands of calls.

Combinators, one level, each member the same one-key shape: `all: [...]` (AND), `any: [...]` (OR), `not: {...}` (one nested condition).

Worked example — stop when the build resolves, alert if it breaks:

```
watcher.terminal.create {
  "terminalIds": ["terminal-<uuid>"],
  "title": "nightly build",
  "goal": "watch the build to a verdict and alert me if it fails",
  "stopWhen": {"any": [{"contains": "BUILD SUCCESSFUL"},
                       {"contains": "BUILD FAILED"},
                       {"runtimeStatusIs": "exited"}]},
  "alertWhen": {"contains": "BUILD FAILED"},
  "cadenceMs": 30000,
  "stopAfterMs": 43200000
}
```

THE KEY TRAP: to stop when an AGENT finishes its turn, do NOT use a bare `stopWhen: {"stateIs":"waiting"}`. An agent reads `waiting` before it starts, while paused, and when its window is backgrounded, so a raw state stop fires early and reports a finish that never happened. For an explore or edit agent you usually need NO custom `stopWhen` at all — the default supervisor treats working→waiting as a candidate and CONFIRMS it with a small-model tail check before stopping. If you must express it yourself, pair the state with a judge so a bare idle cannot false-trip:
`{"all": [{"stateIs": "waiting"}, {"modelJudge": "has the agent posted its final answer and stopped working?"}]}`

`terminal.await.async` needs none of this DSL — it watches plain agent state and wakes you when every terminal has finished, failed, or asked a question. Reach for the DSL only when the stop or alert depends on what the output SAYS.

## Work that starts later

"Wait ten minutes, THEN run it" HAS an unattended path, and there are two shapes of it. Pick by whether the ONE call is already decided.

### The normal shape: a timed message

`timer.schedule {"title":"…","delayMs":600000,"payload":{"type":"message","message":"Run the test suite in the build terminal and report the result"}}` — ONE call, and usually the whole job. At fire time the message is delivered to you as the user's request and you run an ordinary turn: you pick the tools then, against the world as it actually is, so nothing has to be resolved in advance and a stale id cannot poison it. Write the message the way the user would say it to you.

Do NOT also do the work in this turn. "Start a timer, THEN spawn an agent" means the spawn happens at fire time, not twice.

What it delivers depends on where the assistant is when it fires. If the panel is OPEN you get a normal turn with normal confirmations. If it is CLOSED the supervisor runs it as the `wake` actor, so a confirm-required mutation needs a WAKE grant — `grant.create {"actorType":"wake","actorId":"wake","allowedToolNames":["terminal.sendCommand"],"ttlMs":<the wait plus a margin>,"maxUses":1}`. NOT a timer-scoped grant: the fired message dispatches as `wake`, so a `tmr_…` grant is stored and consumed by nothing. A repeating message must be at least 60s apart and bounded by `maxRuns` or `until`.

A timed message is delivered AT LEAST ONCE within an hour of its due time: one interrupted by a restart is delivered again, so when one arrives check before repeating anything destructive. Past that hour it is reported as MISSED and not carried out — acting on a stale instruction is worse than not acting — so say "I have scheduled it", never "it will have run". No autonomous turn can schedule a timer — not a timed message, and not a watcher or async wake that one of them started. Only you, interactively, can. If the instruction cannot be carried out when it fires, it says so and leaves it for the user.

### The frozen shape: one fixed tool call

Use this only when the exact call and its arguments are already known and must not be re-decided at fire time. It is two calls, both in THIS turn, in this order:

1. `timer.schedule` with a `call_safe_tool` payload naming the ONE tool that does the work — `agentTask.spawnForEdits` to start an agent, `terminal.sendCommand` to drive a terminal that already exists. Use `delayMs`. Read the `timerId` off the result.
2. `grant.create {"actorType":"timer","actorId":"<that tmr_… id>","allowedToolNames":["agentTask.spawnForEdits"],"ttlMs":<the wait plus a margin>,"maxUses":1}` — scoped to exactly the tool the payload names, with `maxUses` matching the fires you actually expect (1 for a one-shot).

   Three details in that pair. `allowedToolNames` matches the DOTTED tool name (`agentTask.spawnForEdits`) — the wire spelling matches nothing and the fire is refused. Pass `worktreeId` explicitly inside the payload args: omitted, a spawn resolves it to whatever is ACTIVE at FIRE time, not the worktree you reasoned about. And `target` only LABELS the events the fire publishes — it never fills a payload argument.

   The TTL runs from NOW, not from the fire, so cover the delay plus lateness (a project with no scheduler running catches up only when one next starts). Do not use this shape for a few-seconds delay: the timer can fire before the grant lands, and `grant.create` does not check its actor still exists, leaving a live grant for a dead timer.

At fire time the scheduler dispatches that tool as the timer actor, consumes the grant, and publishes the result to the attention queue. Nobody has to be present, and no model turn runs — which is the difference from a timed message, and the reason this shape cannot adapt to anything it finds.

A one-shot `call_safe_tool` is ONE AT-MOST-ONCE ATTEMPT (a bounded repeat is rescheduled before each dispatch, so it gets one attempt per firing). The timer is marked fired before the payload runs and is never rescheduled on failure, and the grant is spent before the handler discovers it cannot work, so a fire landing while Daintree is unreachable burns the grant, files an error and does not retry. Say "I have scheduled it", never "it will have run".

Miss step 2 and the dispatch returns `CONFIRMATION_REQUIRED` and the fire is wasted (step 3). A read-only payload needs no grant.

Three things this does NOT do. Do not promise them:

- **A `call_safe_tool` or `enqueue` timer does not WAKE you.** Their events never trigger an autonomous turn, so the result waits in the inbox — UNLESS the payload leaves a watcher (spawn with flat `watch:true` + `watchGoal`), whose completion IS a wake. Attachment is best-effort: a spawn can succeed with a `watcherWarning` and no `watcherId`, so promise the wake only against an id you can see.
- **It runs ONE tool.** A multi-step sequence is not a timer payload. Make the one tool a spawn whose `taskPrompt` carries the whole job.
- **It cannot exceed the tier, or reach the ungrantable names in step 3.** The tier gate is checked before any grant is looked up, so a grant never widens what the tier allows. Those still wait for the user, and saying so plainly is the honest answer.

Still prefer starting NOW when the delay was only about not blocking the user — the spawn shape in step 1. Reach for the timer when the delay is about the CLOCK, which is what "in ten minutes" and "at 6pm" mean.

## What the PR watcher cannot watch

`watcher.watchPR` sees the pull request's state and nothing else: `open` / `merged` / `draft` / `closed`. It polls, and it STOPS ON ITS OWN the moment the PR merges or closes — that is its terminal condition, so there is no watcher left to cancel afterwards and nothing keeps observing a merged PR. It does NOT wake on CI runs, check results, job logs, or review decisions, so never promise an alert "when CI goes green" off a PR watch: a push or a review surfaces only as a coarse "PR has new activity" ping, and a red build on its own surfaces nothing at all.

That is a limit of this WATCHER, not of Daintree. CI roll-ups, review decisions and issue threads are all readable on demand — one-off, in a turn — with the follow-up-on-issue runbook (daintree.workflow.follow-up-on-issue). So when the user wants a CI verdict NOW, read it there rather than leaving a watcher that cannot see it. What still needs an agent in a terminal is the DETAIL no forge read carries: job logs and failure output. To be woken on that, spawn the agent with the spawn-visible-agent runbook and watch that TERMINAL with (a) or (b) — the terminal is the observable thing, not the PR.

Report back: the mechanism you chose and why, every id you left running (watcher, timer, async handle, grant, plus the terminals or PR under watch), what will trigger it and whether that wakes you, acts, or only leaves a note, when the watch and grant expire, and anything that must wait for the user — naming any mutation you could not pre-authorize.
