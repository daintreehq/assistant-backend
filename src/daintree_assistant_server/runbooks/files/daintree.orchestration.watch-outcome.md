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

1. Fix the target, then pick ONE mechanism. Resolve the full terminal ids or PR number live this turn: a watcher built on a stale id waits forever on nothing and never wakes you.
   - (a) A terminal outcome needing goal-based classification ("wake me when it's green", "alert me if it breaks") → `watcher.terminal.create`, step 2.
   - (b) A pass/fail verdict on a terminal → there is no fire-and-forget verdict call. If the work has ALREADY settled, `terminal.extract` reads and judges it once, this turn. If it has not, that is case (a): a `watcher.terminal.create` whose `alertWhen` is a `modelJudge` question ("did the suite pass with zero failures?") publishes the judged answer to the attention queue. A `modelJudge` matches only on a CONFIDENT yes, so a hedged yes reads the same as a flat no, and a judge that could not answer stays UNKNOWN, which satisfies nothing — `not:` included. Give each outcome its OWN affirmative question — `stopWhen` an `any` of "did it pass?" and "did it fail?", `alertWhen` the failure one alone — rather than trusting one question to report both.
   - (c) A PR reaching a terminal state → `watcher.watchPR {"prNumber":123,"title":"…"}` (optional `cwd`, `stopAfterMs`) — read "What the PR watcher cannot watch" before promising anything about it.
   - (d) A nudge with no terminal to observe → `timer.schedule`, step 4.
   - (e) The WORK itself starts later, so its terminal does not exist yet ("wait an hour, THEN run it") — unlike (d), where nothing is ever run and the user is only reminded → `timer.schedule` a `call_safe_tool` payload plus a matching grant, per "Work that starts later" below. Do NOT spawn now just to have an id: that parks an agent idle on a worktree for the whole wait.
   Spawning the agent right now? The cheapest supervisor is the spawn itself: `agentTask.spawnForEdits` with flat `watch:true` + `watchGoal:"…"` and an `acceptanceCriteria` contract. Its supervisor already confirms completion — add no second watcher.

2. Create the watcher. `watcher.terminal.create` requires `terminalIds` (array of full ids), `title`, and `goal` — plain language for what you are observing, which drives the per-check classification. Optional: `stopWhen` (ends the watcher, status `condition_met`), `alertWhen` (alerts and KEEPS running), `cadenceMs` (default 120000), `modelTier` (`small` | `medium`), `startAfterMs`, `stopAfterMs` (defaults to 24 h — a watcher never runs forever, so set the horizon you mean). Write the conditions with the DSL below: prefer the free deterministic leaves, and set `cadenceMs` to the resolution you genuinely need.

3. Grant BEFORE you leave, if the deferred action must mutate. An unattended action has nobody to confirm: a confirm-required tool called by a timer/wake actor with no matching grant is blocked and lands as a pending-approval item. For a wake this stalls the overnight run; for a ONE-SHOT TIMER the fire is wasted, because the timer is already marked fired and the authorization that inbox item offers can no longer be spent. Either way it is the exact failure the user asked you to prevent. Only the interactive main actor can mint a grant, so it happens now or never.
   `grant.create {"actorType":"wake","actorId":"wake","allowedToolNames":["terminal.sendCommand"],"ttlMs":28800000,"maxUses":3}`
   - `actorType` here is `timer` | `wake`. Do NOT mint a WATCHER grant: a watcher check only reads and publishes, and the autonomous turn its event may trigger dispatches as `wake`, so a `wch_…` grant is stored and then consumed by nothing. For `timer`, `actorId` is the timer's own `tmr_…` id — create it first (step 4), read the id off the result, and mint the grant in the SAME turn, since that id does not exist until the create call returns. For `wake` (the supervisor daemon's autonomous turns while the user is away) `actorId` must be the literal `"wake"`.
   - An auto-approve session covers NEITHER actor. It silences the prompt for YOU, the interactive one, and nothing else, so a mutation you could fire yourself without a prompt is still blocked unattended. Never read "it did not ask me" as "it will not ask later".
   - `ttlMs` and `maxUses` are required, and so is at least one of `allowedToolNames` / `allowedRiskClasses` — a grant with no scope authorizes nothing and is rejected.
   - Scope NARROW: an explicit `allowedToolNames` list, a `maxUses` equal to the actions you actually expect, a `ttlMs` covering only the window (8 h for overnight, not the 30-day ceiling). Never hand out a blanket `allowedRiskClasses` entry.
   - `grant.create`, `grant.revoke` and `daintree.call` can NEVER be granted, and neither can the raw dynamic-invoke entry point under its own bare name. A wrapper-less MCP action is still not automatically out of reach: dispatch resolves a per-call identity of the form daintree.invoke:ACTION before it looks for a grant, and a grant naming that exact composite IS honoured (a read-risk target needs no grant at all). So either name the resolved action explicitly, do it yourself now, or say plainly that it waits for the user.

4. For a time-based nudge: `timer.schedule {"title":"…","payload":{"type":"enqueue","message":"…"},"delayMs":3600000}`. Use `delayMs` (relative), never `fireAt` — you have no reliable clock, so an invented wall-clock time fires at the wrong hour or never. `payload.type` is `enqueue` (a reminder in the attention queue) or `call_safe_tool` with `payload.toolCall {"toolName":"…","args":{…}}`. A `repeat` requires `everyMs` and must be bounded by `maxRuns` or `until`, or it pings forever. Optional `target` only labels the fire's events with a `terminalId` / `worktreeId` / `projectId` / `workflowRunId`.
   - `enqueue` posts that queue item and does NOTHING else — it runs no tool, and once the assistant is closed nothing reads it until the user next looks. It is a note to a human, never unattended work, so never schedule one to "handle" something.
   - To ACT at fire time the payload must be `call_safe_tool`, and it fires with nobody there to confirm. `toolName` may name any registered tool — the timer adds no allowlist of its own — but the usual tier and confirmation policy still decides what runs.
   - A confirm-required tool needs a live grant, and a timer CAN hold one against its own `tmr_…` id (step 3). The revoke on a TERMINAL fire (every one-shot, and the last fire of a bounded repeat) is DEFERRED until after the payload has dispatched, precisely so the single fire the grant was minted for can still spend it. So a one-shot timer DOES reach a confirm-required tool unattended: schedule it, then grant against the id it returned. Two limits, both spelled out under "Work that starts later": it is ONE at-most-once attempt, and the TIER gate runs BEFORE the grant is even looked up, so a grant can never authorize what the tier forbids: `operator` excludes git and system, `supervisor` allows only read/local/ui. Beyond that, the ungrantable names in step 3.

5. Report the ids and END the turn — the setup IS the deliverable, so do not then wait on the same terminal in-turn. Every mechanism here outlives the turn, but they do not deliver the same thing once the assistant is CLOSED, so promise only what yours does:
   - WAKES YOU: a terminal watcher reaching its `stopWhen`/`alertWhen`, and a finished `terminal.await.async`. Only here is "I'll wake you when it finishes" a promise you can actually keep — and a `watch:true` spawn earns it only if it came back with a watcher id.
   - ACTS, QUIETLY: a timer's `call_safe_tool` runs its ONE tool — no-confirmation outright, or confirm-required under the grant from step 3 — and leaves the result in the queue. It never wakes you itself; what it STARTS can, but only best-effort (see "Work that starts later"), so promise a wake only against a `watcherId` you can see.
   - NEITHER: `watcher.watchPR` and a timer `enqueue` only leave an item that sits until the user looks. Promise a note waiting, never a wake.

6. Clean up when the window closes. `watcher.list`, `timer.list`, `async.list` and `grant.list` (optional `actorId`) answer "what is still watching". Retire with `watcher.cancel` / `timer.cancel` / `grant.revoke` (each `{"id":"…"}`) or `async.cancel {"asyncId":"asy_…"}` — cancelling an async only stops tracking, nothing is killed or closed. Revoke a wake grant the moment the window is over; never leave standing authority behind.

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

"Wait ten minutes, THEN run it" HAS an unattended path. It is two calls, both in THIS turn, in this order:

1. `timer.schedule` with a `call_safe_tool` payload naming the ONE tool that does the work — `agentTask.spawnForEdits` to start an agent, `terminal.sendCommand` to drive a terminal that already exists. Use `delayMs`. Read the `timerId` off the result.
2. `grant.create {"actorType":"timer","actorId":"<that tmr_… id>","allowedToolNames":["agentTask.spawnForEdits"],"ttlMs":<the wait plus a margin>,"maxUses":1}` — scoped to exactly the tool the payload names, with `maxUses` matching the fires you actually expect (1 for a one-shot).

   Three details in that pair. `allowedToolNames` matches the DOTTED tool name (`agentTask.spawnForEdits`) — the wire spelling matches nothing and the fire is refused. Pass `worktreeId` explicitly inside the payload args: omitted, a spawn resolves it to whatever is ACTIVE at FIRE time, not the worktree you reasoned about. And `target` only LABELS the events the fire publishes — it never fills a payload argument.

   The TTL runs from NOW, not from the fire, so cover the delay plus lateness (a project with no scheduler running catches up only when one next starts). Do not use this shape for a few-seconds delay: the timer can fire before the grant lands, and `grant.create` does not check its actor still exists, leaving a live grant for a dead timer.

At fire time the scheduler dispatches that tool as the timer actor, consumes the grant, and publishes the result to the attention queue. Nobody has to be present, and no model turn runs.

It is ONE AT-MOST-ONCE ATTEMPT. The timer is marked fired before the payload runs and is never rescheduled on failure, and the grant is spent before the handler discovers it cannot work, so a fire landing while Daintree is unreachable burns the grant, files an error and does not retry. Say "I have scheduled it", never "it will have run".

Miss step 2 and the dispatch returns `CONFIRMATION_REQUIRED` and the fire is wasted (step 3). A read-only payload needs no grant.

Three things this does NOT do. Do not promise them:

- **It does not WAKE you.** A timer's own event never triggers an autonomous turn, so the result waits in the inbox — UNLESS the payload leaves a watcher (spawn with flat `watch:true` + `watchGoal`), whose completion IS a wake. Attachment is best-effort: a spawn can succeed with a `watcherWarning` and no `watcherId`, so promise the wake only against an id you can see.
- **It runs ONE tool.** A multi-step sequence is not a timer payload. Make the one tool a spawn whose `taskPrompt` carries the whole job.
- **It cannot exceed the tier, or reach the ungrantable names in step 3.** The tier gate is checked before any grant is looked up, so a grant never widens what the tier allows. Those still wait for the user, and saying so plainly is the honest answer.

Still prefer starting NOW when the delay was only about not blocking the user — the spawn shape in step 1. Reach for the timer when the delay is about the CLOCK, which is what "in ten minutes" and "at 6pm" mean.

## What the PR watcher cannot watch

`watcher.watchPR` sees the pull request's state and nothing else: `open` / `merged` / `draft` / `closed`. It polls, and it STOPS ON ITS OWN the moment the PR merges or closes — that is its terminal condition, so there is no watcher left to cancel afterwards and nothing keeps observing a merged PR. It does NOT wake on CI runs, check results, job logs, or review decisions, so never promise an alert "when CI goes green" off a PR watch: a push or a review surfaces only as a coarse "PR has new activity" ping, and a red build on its own surfaces nothing at all.

That is a limit of this WATCHER, not of Daintree. CI roll-ups, review decisions and issue threads are all readable on demand — one-off, in a turn — with the follow-up-on-issue runbook (daintree.workflow.follow-up-on-issue). So when the user wants a CI verdict NOW, read it there rather than leaving a watcher that cannot see it. What still needs an agent in a terminal is the DETAIL no forge read carries: job logs and failure output. To be woken on that, spawn the agent with the spawn-visible-agent runbook and watch that TERMINAL with (a) or (b) — the terminal is the observable thing, not the PR.

Report back: the mechanism you chose and why, every id you left running (watcher, timer, async handle, grant, plus the terminals or PR under watch), what will trigger it and whether that wakes you, acts, or only leaves a note, when the watch and grant expire, and anything that must wait for the user — naming any mutation you could not pre-authorize.
