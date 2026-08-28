---
id: daintree.orchestration.watch-outcome
title: Watch an outcome unattended
summary: Set up supervision that outlives the turn — a terminal watcher, an async verdict, a PR watch, or a timer — and pre-authorize with a narrow grant when the wake must act.
whenToUse: Use when the user wants to be woken later, not waited on now — "run this overnight and wake me", "let me know when the build finishes", "ping me when the PR merges", "remind me in an hour", "keep an eye on it while I'm out" — or any request to leave something watching after this turn. Also to pre-authorize an unattended action with a grant, and to list, cancel or revoke watchers, timers, async waits and grants you left. Do not use to cancel ONE named watcher (daintree.foundation), a one-off check of an agent that already finished (daintree.orchestration.verify-agent-work), triage of what is running now (daintree.orchestration.supervise-fleet), what happened while you were away (daintree.orchestration.status-and-resume), or wholesale teardown (daintree.orchestration.workspace-cleanup). `watcher.watchPR` sees only a PR's open/merged/draft/closed state, so a one-off read of CI, checks, reviews or an issue thread is not a watch at all (use daintree.workflow.follow-up-on-issue).
opener: "Unattended supervision — a watcher wakes you, a reminder only waits"
risk: system
requiredTools:
  - context.snapshot
  - agentTask.spawnForEdits
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
Use when: the user wants supervision that OUTLIVES this turn — wake me when it finishes, alert me if it breaks, remind me later — rather than a wait you sit through now.

You are setting up the supervisor, not doing the work: on the wake you spawn, verify and relay — never fix the build, edit the branch, or land a merge yourself. Reads need no authority; only a MUTATION performed unattended needs the grant in step 3.

Procedure:

1. Fix the target, then pick ONE mechanism. Resolve the full terminal ids or PR number live this turn: a watcher built on a stale id waits forever on nothing and never wakes you.
   - (a) A terminal outcome needing goal-based classification ("wake me when it's green", "alert me if it breaks") → `watcher.terminal.create`, step 2.
   - (b) A pass/fail verdict on a terminal → there is no fire-and-forget verdict call. If the work has ALREADY settled, `terminal.extract` reads and judges it once, this turn. If it has not, that is case (a): a `watcher.terminal.create` whose `alertWhen` is a `modelJudge` question ("did the suite pass with zero failures?") publishes the judged answer to the attention queue.
   - (c) A PR reaching a terminal state → `watcher.watchPR {"prNumber":123,"title":"…"}` (optional `cwd`, `stopAfterMs`) — read "What the PR watcher cannot watch" before promising anything about it.
   - (d) A nudge with no terminal to observe → `timer.schedule`, step 4.
   - (e) The WORK itself starts later, so its terminal does not exist yet ("wait an hour, THEN run it") — unlike (d), where nothing is ever run and the user is only reminded → read "Work that starts later" below before promising anything. Do NOT spawn now just to have an id: that parks an agent idle on a worktree for the whole wait.
   Spawning the agent right now? The cheapest supervisor is the spawn itself: `agentTask.spawnForEdits` with flat `watch:true` + `watchGoal:"…"` and an `acceptanceCriteria` contract. Its supervisor already confirms completion — add no second watcher.

2. Create the watcher. `watcher.terminal.create` requires `terminalIds` (array of full ids), `title`, and `goal` — plain language for what you are observing, which drives the per-check classification. Optional: `stopWhen` (ends the watcher, status `condition_met`), `alertWhen` (alerts and KEEPS running), `cadenceMs` (default 120000), `modelTier` (`small` | `medium`), `startAfterMs`, `stopAfterMs` (defaults to 24 h — a watcher never runs forever, so set the horizon you mean). Write the conditions with the DSL below: prefer the free deterministic leaves, and set `cadenceMs` to the resolution you genuinely need.

3. Grant BEFORE you leave, if the wake must mutate. An unattended turn has nobody to confirm: a confirm-required tool called by a watcher/timer/wake actor with no matching grant is blocked and lands as a pending-approval item — the overnight run stalls until morning, the exact failure the user asked you to prevent. Only the interactive main actor can mint a grant, so it happens now or never.
   `grant.create {"actorType":"wake","actorId":"wake","allowedToolNames":["terminal.sendCommand"],"ttlMs":28800000,"maxUses":3}`
   - `actorType` is `watcher` | `timer` | `wake`. For `watcher`/`timer`, `actorId` is that supervisor's own `wch_…`/`tmr_…` id — create it first (a watcher in step 2, a timer in step 4), then grant against the id it returned. A one-shot timer is the exception — step 4 says why it can never consume one. For `wake` (the supervisor daemon's autonomous turns while the user is away) `actorId` must be the literal `"wake"`.
   - `ttlMs` and `maxUses` are required, and so is at least one of `allowedToolNames` / `allowedRiskClasses` — a grant with no scope authorizes nothing and is rejected.
   - Scope NARROW: an explicit `allowedToolNames` list, a `maxUses` equal to the actions you actually expect, a `ttlMs` covering only the window (8 h for overnight, not the 30-day ceiling). Never hand out a blanket `allowedRiskClasses` entry, and never mint a grant for read-only watching — reads need none.
   - `grant.create`, `grant.revoke` and `daintree.call` can NEVER be granted, so a wrapper-less capability cannot run unattended: do it yourself now, or say plainly that it waits for the user.

4. For a time-based nudge: `timer.schedule {"title":"…","payload":{"type":"enqueue","message":"…"},"delayMs":3600000}`. Use `delayMs` (relative), never `fireAt` — you have no reliable clock, so an invented wall-clock time fires at the wrong hour or never. `payload.type` is `enqueue` (a reminder in the attention queue) or `call_safe_tool` with `payload.toolCall {"toolName":"…","args":{…}}`. A `repeat` requires `everyMs` and must be bounded by `maxRuns` or `until`, or it pings forever. Optional `target` ties it to a `terminalId` / `worktreeId` / `projectId` / `workflowRunId`.
   - `enqueue` posts that queue item and does NOTHING else — it runs no tool, and once the assistant is closed nothing reads it until the user next looks. It is a note to a human, never unattended work, so never schedule one to "handle" something.
   - To ACT at fire time the payload must be `call_safe_tool`, and it fires with nobody there to confirm. `toolName` may name any registered tool — the timer adds no allowlist of its own — but the usual tier and confirmation policy still decides what runs, so a tool that needs no confirmation simply runs and a read-only payload is unaffected.
   - A confirm-required tool needs a live grant and cannot hold one: a timer REVOKES its own grants on its terminal fire (every one-shot, and the last fire of a bounded repeat) BEFORE the payload dispatches. So a one-shot timer cannot carry out a confirm-required mutation, however you grant it — do it yourself now, or say plainly that it waits for the user.

5. Report the ids and END the turn — the setup IS the deliverable, so do not then wait on the same terminal in-turn. Every mechanism here outlives the turn, but they do not deliver the same thing once the assistant is CLOSED, so promise only what yours does:
   - WAKES YOU: a terminal watcher reaching its `stopWhen`/`alertWhen`, and a finished `terminal.await.async`. Only here is "I'll wake you when it finishes" a promise you can actually keep — and a `watch:true` spawn earns it only if it came back with a watcher id.
   - ACTS, QUIETLY: a timer's `call_safe_tool` runs its one no-confirmation tool and leaves the result in the queue.
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

"Wait an hour, THEN run it" has no unattended path today, so do not promise one. A `call_safe_tool` payload carrying a spawn is blocked by the revocation in step 4, and an `enqueue` payload only lands an inbox reminder — once the assistant is closed a timer never triggers an autonomous wake turn, since only a terminal watcher or an async completion does. Pick one of these and SAY which you did:

- The delay was really about not blocking the user → start the work NOW and watch it: spawn with flat `watch:true` + `watchGoal` and an `acceptanceCriteria` contract, per step 1. The user is still free the moment you hand back.
- The delay is genuinely about the clock → `timer.schedule` an `enqueue` reminder carrying the whole instruction, and tell the user plainly that the spawn itself waits for them. That is the same honest answer step 3 requires for any capability you cannot pre-authorize.

Never pre-spawn an agent now to reserve a terminal id for later. It holds a worktree and burns its context idling, and it is the exact failure branch (e) exists to prevent.

## What the PR watcher cannot watch

`watcher.watchPR` sees the pull request's state and nothing else: `open` / `merged` / `draft` / `closed`. It polls, and it STOPS ON ITS OWN the moment the PR merges or closes — that is its terminal condition, so there is no watcher left to cancel afterwards and nothing keeps observing a merged PR. It does NOT wake on CI runs, check results, job logs, or review decisions, so never promise an alert "when CI goes green" off a PR watch — it never tells you WHAT changed: a push or a review surfaces only as a coarse "PR has new activity" ping, never as CI or review detail, and a red build on its own surfaces nothing at all.

That is a limit of this WATCHER, not of Daintree. CI roll-ups, review decisions and issue threads are all readable on demand — one-off, in a turn — with the follow-up-on-issue runbook (daintree.workflow.follow-up-on-issue). So when the user wants a CI verdict NOW, read it there rather than leaving a watcher that cannot see it. What still needs an agent in a terminal is the DETAIL no forge read carries: job logs and failure output. To be woken on that, spawn the agent with the spawn-visible-agent runbook and watch that TERMINAL with (a) or (b) — the terminal is the observable thing, not the PR.

Report back: the mechanism you chose and why, every id you left running (watcher, timer, async handle, grant, plus the terminals or PR under watch), what will trigger it and whether that wakes you, acts, or only leaves a note, when the watch and grant expire, and anything that must wait for the user — naming any mutation you could not pre-authorize.
