# Run Supervisor — the durable orchestration runtime (`/v2/runs`)

The Run Supervisor moves the assistant's turn loop off the CLI and onto the server. The
backend owns the durable event log, the model/tool loop, tool-call state (leases),
approvals for background mutation, timers/wakeups, and replay. The CLI (or any other
frontend) becomes a **view plus local tool executor**: it submits user turns, subscribes
to the run's event stream, claims tool leases, executes them through its existing local
registry/permission system, and posts results back.

`/v1/daintree/respond` is unchanged and remains fully supported — v2 is an additive,
parallel protocol. The signed skill-state token is a v1 concern only; v2 runs carry
their skill state in the run record.

## Vocabulary

- **Run** — one durable conversation/workflow. Owns the transcript, the active skill
  set, tool-call state, and status. Survives disconnects, restarts, and process death.
- **Turn** — one user (or wake) instruction submitted to a run. A turn spans one or
  more **rounds**.
- **Round** — one upstream model call. A round either ends the turn (final answer) or
  emits tool calls, which become **leases**.
- **Lease** — a durable, claimable unit of local tool work: `lease_id`, `tool_call_id`,
  tool name/arguments, deadline, idempotency key, approval requirement. An executor
  claims a lease, runs the tool locally, and posts exactly one result.
- **Executor** — a connected CLI (or other frontend) advertising its tool inventory and
  capabilities via heartbeat. Presence distinguishes *actively supervising* from
  *durably waiting*.
- **Wakeup** — a durable timer owned by the backend. When it fires, the supervisor
  submits a wake turn to the run — whether or not any CLI is connected.

## Event log

Everything durable is an append-only event on the run, with a global monotonically
increasing `seq` (the SSE resume cursor). Persisted event types:

| type | payload highlights |
|---|---|
| `run.created` | session_id, project_id, title |
| `turn.submitted` | turn_id, source (`user`/`wake`), messages, effective context (runtime, turn, tools digest, generation) |
| `selection.completed` | round, active/newly loaded skill ids, degraded flag, usage |
| `round.started` | round, prompt_fingerprint, catalog_revision, prompt_version, skill body hashes, model |
| `assistant.message` | round, content, reasoning_content, tool_calls, finish_reason, usage |
| `lease.created` | lease_id, tool_call_id, tool_name, arguments, risk, requires_approval, deadline, idempotency_key |
| `lease.claimed` | lease_id, executor_id, attempt |
| `lease.released` | lease_id, reason (claim expired / executor gave up) |
| `tool.result` | lease_id, tool_call_id, status (`ok`/`error`/`declined`/`cancelled`/`expired`), content, executor_id, synthesized flag |
| `approval.requested` | approval_id, lease_id, reason |
| `approval.resolved` | approval_id, approved, note |
| `turn.completed` | turn_id, finish_reason |
| `turn.cancelled` | turn_id, reason |
| `round.failed` | round, error code/message (after retries exhausted) |
| `run.status` | status transition, reason |
| `wake.scheduled` / `wake.cancelled` / `wake.fired` | wakeup_id, fire_at, note |

**Ephemeral** events (SSE only, no `seq`, never persisted): `model.delta` (streamed
content / reasoning_content / tool_call fragments) and `model.status` (`thinking`).
Reconnecting clients lose only these; the durable `assistant.message` carries the full
text, so a dropped delta is cosmetic, never state.

The transcript is a pure projection of the log: `turn.submitted` messages, then per
round the `assistant.message`, then one tool message per tool call **in the assistant's
tool_calls order** (not result-arrival order) — deterministic replay regardless of the
order executors finish in. Every tool call always gets a tool message: declined,
cancelled, and expired leases synthesize one (the upstream provider rejects a transcript
with an unanswered tool call).

## Run status machine

```
idle ──turn──▶ running ──tool_calls──▶ awaiting_tools ──all results──▶ running
                  │                        │(approval needed)
                  │                    awaiting_approval ──resolved──▶ awaiting_tools
                  │
                  ├── final answer ──▶ idle   (turn.completed)
                  ├── retries exhausted ─▶ failed  (new turn resets to running)
                  └── cancel ──▶ idle          (turn.cancelled; open leases cancelled)
```

A new turn is rejected with `409 run_busy` while a turn is in flight (`running` /
`awaiting_tools` / `awaiting_approval`); cancel first. `failed` accepts a new turn.

## Selector cadence

The selector runs once per submitted turn (before its first round) and is skipped on
tool-continuation rounds — the same cadence as v1, but tracked in the run record
(`needs_selection`), not a client-carried token. A successful run is authoritative
(wholesale replacement, capped at `MAX_ACTIVE_SKILLS`); a degraded run fails open to
the prior active set. `selection: {policy, force}` on turn submission behaves as in v1.

## Prompt assembly and caching

Rounds are assembled by the **same `PromptAssembler`** and layer order as v1 — base
prompt → integrations → append-only conversation → skill bodies → runtime → turn footer
→ response contract — from the server-side transcript instead of a client-resent one.
The cache-optimized layout is preserved byte-for-byte; the CLI simply stops resending
the conversation every round.

## Idempotency fences

- **Turn submission** is idempotent on `(run_id, turn_id)`: an identical resubmission
  returns the original acceptance (`duplicate: true`); a different payload under the
  same turn_id is `409 turn_conflict`.
- **Lease claim**: first claim wins; the same executor re-claiming extends its claim;
  a different executor gets `409 lease_claimed` until the claim TTL lapses, after which
  the lease is claimable again (`attempt` increments, `lease.released` logged).
- **Lease result**: exactly-once. The first result transitions the lease to a terminal
  state in the same transaction that appends `tool.result`. An identical duplicate
  (same status + content hash) returns `200 {duplicate: true}`; a conflicting duplicate
  is `409 result_conflict`. The model continuation fires at most once per round via a
  compare-and-swap on run status (`awaiting_tools → running`).
- **Lease creation** is keyed unique on `(run_id, tool_call_id)` with insert-or-ignore,
  so crash-recovery re-driving a round can never mint duplicate leases.

Executors should treat `claim → execute → result` as their fence: never execute before
a successful claim, and include the lease's `idempotency_key` in any side-effecting
operation that supports one.

## Approvals — background mutation is never auto-authorized

Interactive turns keep today's model: the CLI confirms locally as part of executing a
claimed lease, and posts `declined` when the user refuses. Server-side approvals guard
the case with **no user present**: on a wake-sourced turn, any lease whose tool risk is
not clearly read-only (`read`/`local`/`ui`, learned from executor heartbeats; unknown
risk counts as mutating) is created `awaiting_approval` and the run parks in
`awaiting_approval` until `POST /v2/approvals/{id}` resolves it. Declining synthesizes a
`declined` tool result so the turn can continue honestly.

## Failure, restart, reconnect

- **Mid-round upstream failure**: deltas are advisory, so the whole round retries
  (bounded by `MAX_UPSTREAM_RETRIES`); exhaustion appends `round.failed` and parks the
  run `failed`. No fabricated completions, ever.
- **Process restart**: `RunSupervisor.recover()` re-drives every run left in `running`
  (a model call has no side effects; the resumable state machine checks persisted
  events before re-calling: an already-persisted `assistant.message` for the current
  round resumes at lease creation, not at the model call) and re-checks
  `awaiting_tools` runs whose last result arrived while the process was down.
- **CLI reconnect**: `GET /v2/runs/{id}/events?after=<seq>` (or `Last-Event-ID`)
  replays everything missed, then tails live. The heartbeat response lists pending
  leases so a reconnecting executor can sweep for work it should claim.

## Replay

`round.started` records a `prompt_fingerprint` — SHA-256 over the canonical JSON of the
assembled upstream request (model, messages, tools, tool_choice, generation and
thinking parameters; volatile transport fields excluded). `GET
/v2/runs/{id}/rounds/{round}/replay` re-derives the request from the event log and the
current catalog and reports whether the fingerprint matches (with the reconstructed
request available for inspection). A catalog revision drift is reported as an explicit
mismatch reason, not a silent diff.

## Endpoints

All under the same bearer auth as v1; the `/v2/` path is the protocol version signal
(no `X-Daintree-Protocol` header needed).

| method + path | purpose |
|---|---|
| `POST /v2/runs` | create a run and submit its first turn |
| `GET /v2/runs` | list runs |
| `GET /v2/runs/{run_id}` | run detail (status, active skills, cursors, executor presence) |
| `POST /v2/runs/{run_id}/turns` | submit a turn (idempotent on turn_id) |
| `POST /v2/runs/{run_id}/cancel` | cancel the in-flight turn |
| `GET /v2/runs/{run_id}/events` | SSE stream: replay after cursor, then live tail |
| `GET /v2/runs/{run_id}/leases` | list leases for a run |
| `POST /v2/tool-leases/{lease_id}/claim` | executor claims work |
| `POST /v2/tool-leases/{lease_id}/result` | executor posts the (exactly-once) result |
| `GET /v2/tool-leases` | pending-lease sweep for reconnecting executors |
| `POST /v2/approvals/{approval_id}` | approve/decline a parked background mutation |
| `POST /v2/executors/heartbeat` | advertise tool inventory + capabilities; returns pending leases |
| `POST /v2/runs/{run_id}/wakeups` | schedule a durable wake; `DELETE .../wakeups/{id}` cancels |
| `GET /v2/runs/{run_id}/rounds/{round}/replay` | reproduce and verify a round's exact prompt |

## Storage

SQLite (WAL) at `RUNS_DB_PATH` (default `.daintree/runs.sqlite3`). The `events` table
is the source of truth; `runs`, `turns`, `leases`, `approvals`, `executors`, and
`wakeups` are projections maintained transactionally with each append. Single-process,
single-writer by design — this is the local-first durable brain, not a distributed
store; the seams (store interface, CAS transitions, event cursors) are what a
Postgres-backed multi-instance deployment would keep.

## Limits of this v1 (deliberate, documented)

- Local tools cannot run with no executor connected: leases wait durably (`pending`,
  visible in the event log) and the run parks in `awaiting_tools`. That is the honest
  degradation — *durably waiting*, not silently cancelled.
- Watchers that need local terminals remain CLI-owned; the backend primitive shipped
  here is the durable wakeup + wake turn. Remote-check watchers can be built on it.
- Mid-turn user interjection (v1's `instruction_revision` bump) is not yet supported on
  v2 — cancel and resubmit.
- Auth is the existing single-key model; executor identity is declarative
  (`executor_id` in heartbeat/claims) and recorded for audit, not yet authenticated
  per-device.
- Skill learning fires from the supervisor on turn completion with the same transcript
  contract as v1; moving it to structured event-log evidence is the natural next step.
