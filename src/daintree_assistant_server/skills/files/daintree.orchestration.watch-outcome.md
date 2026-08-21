---
id: daintree.orchestration.watch-outcome
title: Watch an outcome unattended
summary: Set up supervision that outlives the turn — a terminal watcher, an async verdict, a PR watch, or a timer — and pre-authorize with a narrow grant when the wake must act.
whenToUse: Use when the user wants to be woken later, not waited on now — "run this overnight and wake me", "let me know when the build finishes", "ping me when the PR merges", "remind me in an hour", "keep an eye on it while I'm out" — or any request to leave something watching after this turn. Also when the ACTION itself is deferred rather than the report — "spawn it in five seconds", "start the build in ten minutes", "kick it off at midnight" — where the wait is the request and acting now would not satisfy it. A deferred spawn belongs HERE, payload and all, not in daintree.edits.spawn-visible-agent. Also to pre-authorize an unattended action with a grant, and to list, cancel or revoke watchers, timers, async waits and grants you left. Do not use to cancel ONE named watcher (daintree.foundation), a one-off check of an agent that already finished (daintree.orchestration.verify-agent-work), triage of what is running now (daintree.orchestration.supervise-fleet), what happened while you were away (daintree.orchestration.status-and-resume), or wholesale teardown (daintree.orchestration.workspace-cleanup). `watcher.watchPR` sees only a PR's open/merged/draft/closed state, so a one-off read of CI, checks, reviews or an issue thread is not a watch at all (use daintree.workflow.follow-up-on-issue).
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
   - (b) A pass/fail verdict on a terminal → there is no fire-and-forget verdict call. If the work has ALREADY settled, `terminal.extract` reads and judges it once, this turn. If it has not, that is case (a): a `watcher.terminal.create` whose conditions publish the verdict. A `modelJudge` only matches on a CONFIDENT YES, so one question cannot report both outcomes — "did the suite pass?" stays silent when it fails. Ask each outcome affirmatively: `stopWhen` an `any` of "did it pass?" and "did it fail?", with `alertWhen` on the failure question alone.
   - (c) A PR reaching a terminal state → `watcher.watchPR {"prNumber":123,"title":"…"}` (optional `cwd`, `stopAfterMs`) — read "What the PR watcher cannot watch" before promising anything about it.
   - (d) A nudge with no terminal to observe → `timer.schedule`, step 4.
   - (e) The WORK itself starts later, so its terminal does not exist yet ("wait an hour, THEN run it") — unlike (d), where nothing is ever run and the user is only reminded → a one-shot `timer.schedule` whose payload IS the spawn, plus a grant scoped to it; read "Work that starts later" below for the exact pair. Do NOT spawn now just to have an id: that parks an agent idle on a worktree for the whole wait.
   Spawning the agent right now? The cheapest supervisor is the spawn itself: `agentTask.spawnForEdits` with flat `watch:true` + `watchGoal:"…"` and an `acceptanceCriteria` contract. Its supervisor already confirms completion — add no second watcher.

2. Create the watcher. `watcher.terminal.create` requires `terminalIds` (array of full ids), `title`, and `goal` — plain language for what you are observing, which drives the per-check classification. Optional: `stopWhen` (ends the watcher, status `condition_met`), `alertWhen` (alerts and KEEPS running), `cadenceMs` (default 120000), `modelTier` (`small` | `medium`), `startAfterMs`, `stopAfterMs` (defaults to 24 h — a watcher never runs forever, so set the horizon you mean). Write the conditions with the DSL below: prefer the free deterministic leaves, and set `cadenceMs` to the resolution you genuinely need.

3. Grant BEFORE you leave, if the wake must mutate. An unattended turn has nobody to confirm: a confirm-required tool called by a timer or wake actor with no matching grant is REFUSED — it does not queue and wait for morning, it fails on the spot and leaves a blocked notification recommending the grant you should have minted. Nothing replays it: a one-shot timer has already spent its only fire, so granting afterwards cannot make it run. Only the interactive main actor can mint a grant, so it happens now or never.
   `grant.create {"actorType":"wake","actorId":"wake","allowedToolNames":["terminal.sendCommand"],"ttlMs":28800000,"maxUses":3}`
   - `actorType` is `watcher` | `timer` | `wake`, and only TWO of them are worth minting. A TIMER payload consumes `actorType:"timer"` with `actorId` the `tmr_…` id `timer.schedule` returned — so schedule first, then grant against what came back. A mutation during a WAKE turn (what a terminal watcher or a finished async actually triggers) consumes `actorType:"wake"` with `actorId` the literal `"wake"`. A watcher's own checks never dispatch registered tools, so a `wch_…`-scoped grant has nothing to consume — do not mint one and do not count on it.
   - An auto-approve session does NOT cover any of them. Auto-approve silences the prompt for YOU, the interactive actor, and nothing else: a watcher, timer or wake turn has only the grant path, so a mutation you could fire yourself without a prompt is still blocked unattended. Never read "it did not ask me" as "it will not ask later".
   - `ttlMs` and `maxUses` are required, and so is at least one of `allowedToolNames` / `allowedRiskClasses` — a grant with no scope authorizes nothing and is rejected.
   - Scope NARROW: an explicit `allowedToolNames` list, a `maxUses` equal to the actions you actually expect, a `ttlMs` covering only the window (8 h for overnight, not the 30-day ceiling). Never hand out a blanket `allowedRiskClasses` entry, and never mint a grant for read-only watching — reads need none.
   - `grant.create`, `grant.revoke` and `daintree.call` can NEVER be granted, so a wrapper-less capability cannot run unattended: do it yourself now, or say plainly that it waits for the user.

4. For a time-based nudge: `timer.schedule {"title":"…","payload":{"type":"enqueue","message":"…"},"delayMs":3600000}`. Use `delayMs` (relative), never `fireAt` — you have no reliable clock, so an invented wall-clock time fires at the wrong hour or never. `payload.type` is `enqueue` (a reminder in the attention queue) or `call_safe_tool` with `payload.toolCall {"toolName":"…","args":{…}}`. A `repeat` requires `everyMs` and must be bounded by `maxRuns` or `until`, or it pings forever. Optional `target` (`terminalId` / `worktreeId` / `projectId` / `workflowRunId`) only LABELS the events the fire publishes — it never fills payload arguments or chooses the worktree the payload runs in.
   - `enqueue` posts that queue item and does NOTHING else — it runs no tool, and once the assistant is closed nothing reads it until the user next looks. It is a note to a human, never unattended work, so never schedule one to "handle" something.
   - To ACT at fire time the payload must be `call_safe_tool`, and it fires with nobody there to confirm. `toolName` may name any registered tool — the timer adds no allowlist of its own — but the usual tier and confirmation policy still decides what runs, so a tool that needs no confirmation simply runs and a read-only payload is unaffected.
   - A confirm-required payload needs a live grant, and a timer CAN hold one — including a one-shot. Mint it AFTER `timer.schedule` returns, scoped to the `tmr_…` id it gave you (step 3). The fire spends one use; a TERMINAL fire (every one-shot, and the last fire of a bounded repeat) then revokes whatever is left, so the authority never outlives the timer that carried it. `ttlMs` is counted from when the GRANT is minted and must still be live at the moment of dispatch, so budget it past the delay with room to spare — a missed occurrence catching up on a later tick fires later than you scheduled it, and an expired grant is a hard refusal, not a wait.

5. Report the ids and END the turn — the setup IS the deliverable, so do not then wait on the same terminal in-turn. Every mechanism here outlives the turn, but they do not deliver the same thing once the assistant is CLOSED, so promise only what yours does:
   - WAKES YOU: a terminal watcher reaching its `stopWhen`/`alertWhen`, and a finished `terminal.await.async`. Only here is "I'll wake you when it finishes" a promise you can actually keep — and a `watch:true` spawn earns it only if it came back with a watcher id.
   - ACTS, QUIETLY: a timer's `call_safe_tool` runs its one tool — no-confirmation, or confirm-required against a live grant — and leaves the result in the queue. It never starts a turn, so say "it will happen and a note will be waiting", never "I'll tell you when it does".
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

Combinators nest recursively — a member may itself be a combinator — and every object at every depth still carries EXACTLY ONE key: `all: [...]` (AND), `any: [...]` (OR), `not: {...}` (one nested condition).

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

"Wait an hour, THEN spawn it" HAS an unattended path: a one-shot timer whose `call_safe_tool` payload is the action, plus a grant scoped to that timer. A spawn is the worked example below; any registered tool can be the payload.

It takes TWO ROUNDS, and they cannot be collapsed into one reply. `grant.create` needs the real `tmr_…` that `timer.schedule` mints, and arguments are fixed when a reply is emitted — so round one schedules, and round two grants against the id that came back. Never write a placeholder id into a call you actually send.

Round 1:

```
timer.schedule {
  "title": "spawn the nightly build agent",
  "delayMs": 3600000,
  "payload": {"type": "call_safe_tool",
              "toolCall": {"toolName": "agentTask.spawnForEdits",
                           "args": {"title": "nightly build", "taskPrompt": "…",
                                    "acceptanceCriteria": "…",
                                    "worktreeId": "/Users/…/app",
                                    "watch": true, "watchGoal": "surface what the agent reported"}}}}
```

Round 2, with the returned id:

```
grant.create {"actorId": "tmr_a1b2c3", "actorType": "timer",
              "allowedToolNames": ["agentTask.spawnForEdits"], "ttlMs": 4200000, "maxUses": 1}
```

Use the tool's DOTTED name in `allowedToolNames` — `agentTask.spawnForEdits`, never the wire spelling — or the grant matches nothing and the fire is refused.

Three hazards a spawn you make YOURSELF does not have:

- **The worktree resolves late.** Omitted, `worktreeId` means "whatever is active at FIRE time", which is not the worktree you reasoned about an hour earlier. Pass it explicitly.
- **Nothing is watching unless the payload says so.** The timer fire itself is silent and never starts a turn, so put `watch`/`watchGoal` INSIDE the payload args. Then the agent's own watcher reports completion later — the FIRE is quiet, the finish is not.
- **A short delay can outrun the grant.** The countdown starts the moment the timer is stored, and round two costs a round-trip. Under roughly a minute, the timer can fire before its grant exists — and a refused fire is spent, not retried. For a very short confirm-required delay, say so and do it yourself when the moment comes, rather than promising a pre-authorized fire you cannot guarantee. It also needs the scheduler actually running: if `timer.schedule` came back reporting no active scheduler, the fire waits for the next launch.

The delay itself is part of what the user asked for. Do not collapse it to "spawn now" because the spawn is the interesting half, and never PRESENT an `enqueue` reminder as the work having been done — it runs nothing. Pick deliberately and SAY which you did:

- The delay was really about not blocking the user → start the work NOW and watch it: spawn with flat `watch:true` + `watchGoal` and an `acceptanceCriteria` contract, per step 1. The user is free the moment you hand back, and no grant is needed.
- The delay is genuinely about the clock, and there is room to arm it → schedule the spawn as above, then report the `tmr_…` and grant ids, when it fires, and that the fire itself will not wake anyone.
- The wait needs a HUMAN at the other end (a judgement call, a confirm-required step you cannot pre-authorize, a delay too short to arm) → `timer.schedule` an `enqueue` reminder carrying the whole instruction, and say plainly that the work itself waits for them. That is the same honest answer step 3 requires for any capability you cannot pre-authorize.

Never pre-spawn an agent now to reserve a terminal id for later. It holds a worktree and burns its context idling, and it is the exact failure branch (e) exists to prevent.

## What the PR watcher cannot watch

`watcher.watchPR` sees the pull request's state and nothing else: `open` / `merged` / `draft` / `closed`. It polls, and it STOPS ON ITS OWN the moment the PR merges or closes — that is its terminal condition, so there is no watcher left to cancel afterwards and nothing keeps observing a merged PR. It does NOT wake on CI runs, check results, job logs, or review decisions, so never promise an alert "when CI goes green" off a PR watch — it never tells you WHAT changed: a push or a review surfaces only as a coarse "PR has new activity" ping, never as CI or review detail, and a red build on its own surfaces nothing at all.

That is a limit of this WATCHER, not of Daintree. CI roll-ups, review decisions and issue threads are all readable on demand — one-off, in a turn — with the follow-up-on-issue skill (daintree.workflow.follow-up-on-issue). So when the user wants a CI verdict NOW, read it there rather than leaving a watcher that cannot see it. What still needs an agent in a terminal is the DETAIL no forge read carries: job logs and failure output. To be woken on that, spawn the agent with the spawn-visible-agent skill and watch that TERMINAL with (a) or (b) — the terminal is the observable thing, not the PR.

Report back: the mechanism you chose and why, every id you left running (watcher, timer, async handle, grant, plus the terminals or PR under watch), what will trigger it and whether that wakes you, acts, or only leaves a note, when the watch and grant expire, and anything that must wait for the user — naming any mutation you could not pre-authorize.
