# Provider policy

**Authoritative** for: which model answers, what the upstream promises about
request data, how failures are retried, and what error the caller gets back. Every other document (README, `CLAUDE.md`, `.env.example`, CLI copy)
summarises this one and links here.

`tests/unit/test_docs_drift.py` fails CI when *selected* facts below stop matching
the code — privacy wording, thinking posture, model ids, credential ownership,
retry layers, the wire shape, the environment set. It is a targeted gate over
this repository's own prose, not a proof that every sentence here is accurate, and
it does not see the vendored CLI at all.

Claims marked ⏳ are **not yet implemented**. Do not repeat them elsewhere in the
present tense.

---

## 1. Provider and models

The **only** upstream is **Cerebras Inference** (`https://api.cerebras.ai/v1`,
OpenAI-compatible wire format). It is a model **host**, not a router: one endpoint
serves each model, and there is nothing to select between.

That single fact is why this document is much shorter than it used to be. Under
OpenRouter, ~24 interchangeable endpoints served the same weights and a `provider`
block on every request decided which one answered — which meant a privacy filter,
a ranking axis, a latency floor, a fallback policy, an output-quality denylist, a
sticky-affinity key and a caller-facing `routing` preference to expose all of it.
None of that has a subject any more, so all of it is **deleted rather than
neutered** (git history has the previous revision). What remains is the part that
was never about routing: which model answers, what the upstream promises about
data, how failures are retried, and what error the caller gets back.

| Call | Model | Source of truth |
|---|---|---|
| Main completion | `gemma-4-31b` | `config.CEREBRAS_MODEL` |
| Utility tasks | `gemma-4-31b` | `config.CEREBRAS_MODEL` |
| Skill selection | — local two-stage ONNX selector, no upstream call | `skills/local/setwise.py` |

**One model, singular.** The second one existed for skill learning — a background
pass that read a completed workflow and wrote or rewrote a skill file from model
output. That feature has been removed: it spent a large thinking budget per turn
on a catalog a human curates anyway.

The id is **hardcoded** — a product decision, not deployment config. There is no
environment override, and there must not be one: every prompt and benchmark in
this repository is calibrated against this exact model.

**`gemma-4-31b` is a preview model and Cerebras has said it will be replaced.**
When that happens the constant is the whole code change, but not the whole job:
see §9. Two properties of this specific model are load-bearing beyond quality —
it does not reason (§2) and it is what the prompt budgets are sized against.

> **Skill selection is not an upstream call.** It runs as a LOCAL in-process
> two-stage selector (`skills/local/`, no credential, no endpoint, ~10ms). The
> prompt-based selector survives only as the scripted test seam; where selector
> latency or selector cost is discussed below, that is what is meant.

**The credential is the server's.** `CEREBRAS_API_KEY` funds every turn, and
callers supply none of their own — see [`KEY_HANDLING.md`](KEY_HANDLING.md). This
inverted with the move to Cerebras and it is the fact most likely to be
misremembered from older copy.

`/v1/daintree/respond` and `/v1/daintree/capabilities` report
`config.PUBLIC_MODEL_ID` (`daintree-assistant`), never a real model id.
`/v1/daintree/tasks` currently does **not**: its response carries the pinned
upstream id verbatim. That is an inconsistency, not a design — do not write "the
client never sees a real model id" without qualifying it.

## 2. Thinking posture

**Nothing in this server thinks — and that is now a property of the model, not a
flag held down.**

Under the previous stack it took an explicit `reasoning: {"enabled": false}` on
every call, because a reasoning model reasons by default when the field is
absent. Cerebras has no such switch: it accepts a bare `reasoning_effort` string
with `low`/`medium`/`high` and **no off value**, and rejects OpenRouter's
`reasoning` object outright with a 400. So the posture is not sent — it is
inherited from `gemma-4-31b`, which does not reason at all (measured
`reasoning_tokens: 0`).

| Surface | Thinking | How |
|---|---|---|
| Main completion | off | model has no reasoning mode; `INTERACTIVE_THINKING_ENABLED = False` |
| Utility tasks | off | same |
| Skill selector | n/a | local classifier, no model call |

Skill learning used to be the one exception — it ran fire-and-forget after the
response, so its latency never reached a user, and "read a whole transcript and
write a skill" was genuinely worth reasoning about. It has been removed, so no
call in this system sets `reasoning_effort` at all.

`INTERACTIVE_THINKING_ENABLED` stays in the code as the decision record and the
switch a future reasoning-capable serving model would need;
`INTERACTIVE_REASONING_EFFORT` sits beside it so flipping the flag does not also
require choosing a tier. `tests/live/test_live_cerebras.py` asserts the serving
model reports zero reasoning tokens, so a replacement model that *does* reason
announces itself there rather than in an unexplained latency regression.

## 3. Wire shape — what a request carries, and what it must never carry

**Cerebras is not a router: one endpoint serves each model.** So there is no
endpoint-selection policy and no caller-facing `routing` block — a request
carries the model, the messages, and the parameters the call actually needs, and
nothing about where it should go. Four details are Cerebras-specific and each one is a hard failure if got
wrong — they are pinned by `tests/unit/test_docs_drift.py` against the adapter's
source and by `tests/live/test_live_cerebras.py` against the real API.

| Rule | Consequence of breaking it |
|---|---|
| `max_completion_tokens`, never `max_tokens` | Both spellings together is a 400. Either alone is fine, so this is a live trap: the normalized field is called `max_tokens` and passing it straight through *works* until something else also sets the other name. |
| No `reasoning` object | `400 wrong_api_format`. Use the bare `reasoning_effort` string (§2). |
| No `provider` block | `400 wrong_api_format`. |
| No `session_id` | `400 wrong_api_format`. The affinity key is `prompt_cache_key` (§3a). |

Error bodies are **flat** — `{"message", "type", "param", "code"}` — not wrapped
in OpenAI's `{"error": {...}}` envelope. The adapter reads both shapes, flat
first; reading only the nested one would silently discard every diagnostic this
provider produces, which is the sort of failure that surfaces as "HTTP 400" and
nothing else.

### 3a. Cache affinity — `prompt_cache_key`

Cerebras caches prompt prefixes **automatically**: matched in 128-token blocks,
scoped to the organization, held in memory with a guaranteed 5-minute TTL (longer
under light load). Nothing has to be configured, and there is no endpoint to
steer traffic toward — the mechanism the previous adapter's sticky-key machinery
existed to drive is simply built in here.

Main conversation calls and utility tasks still name a key:

```json
{"prompt_cache_key": "daintree-main-<sha256(session id + salt)>"}
```

**It is a hint, not the mechanism.** The upstream is free to ignore it, and the
honest expectation is that caching works identically without it. It is sent
because it is the OpenAI-standard spelling for exactly this intent, it costs one
short field, and it states which calls belong to one growing prefix — which is
information a future provider may use and which no one can recover later.

It is a salted digest, never the client's own session id: the field crosses to a
third party, and a client session id is opaque local text we make no promises
about. The salt is shared by every instance, so the key is stable across a
stateless fleet without any instance storing it.

| Call | Key | What it names |
|---|---|---|
| Main conversation | `daintree-main-<sha256(session id + salt)>` | that session's growing prefix |
| Main, JSON mode | `daintree-main-json-<sha256(session id + salt)>` | ditto, minus the tool schemas |
| Utility task | `daintree-task-<task id>` | that task's server-owned system prompt |

Task keys are per task rather than one key for all of them: `terminal_summarize`
and `checkpoint` have different prompts, and one shared key would describe two
unrelated prefixes to the upstream as a single conversation.

**What prompt layout still buys.** Everything in
[`prompt_assembler`](../src/daintree_assistant_server/services/prompt_assembler.py)
about ordering rows widest-shared-first is *unchanged in value* by the provider
move — arguably more valuable, since org-wide automatic prefix matching rewards a
byte-stable head and nothing else. What is gone is only the part about keeping a
conversation pinned to one node.

## 4. Privacy — what is and is not promised

> **Daintree's promise is a no-training guarantee.**
> **It is deliberately not a no-retention guarantee.**

**The basis of this changed and the wording deliberately did not.** Under
OpenRouter, the no-training floor was something this server *enforced* per
request, by sending `data_collection: "deny"` and filtering the eligible endpoint
pool. Cerebras is a single host: there is no pool, no filter, and no per-request
data-handling parameter to send. What Daintree can say now rests entirely on
Cerebras's own published policy.

**Cerebras publishes a stronger position than the copy below claims** — no
training on inference inputs or outputs, and zero retention of them by default across all tiers <!-- privacy-copy-ok -->,
with prompt caching held in ephemeral memory. Authoritative sources, which a
reader should check rather than take from here:

- Trust centre — <https://trust.cerebras.ai>
- Privacy policy — <https://cerebras.ai/privacy-policy>
- Inference terms — <https://cerebras.ai/inference-terms>

The copy is **not** upgraded to match, and that is a decision rather than an
oversight. Understating a privacy guarantee cannot mislead a user; overstating
one is the failure this section exists to prevent, and it becomes part of a
tester's trust decision about source code, terminal output, issue text and
project instructions. Raising the claim to "does not retain" is a deliberate act
that needs a human to verify Cerebras's terms against Daintree's actual account
tier, then relax the scan below — not a default that drifts in.

| Phrase | Allowed? |
|---|---|
| "does not collect or train on request data" | ✅ accurate |
| "the provider does not train on your prompts" | ✅ accurate |
| "does not store your prompts" | ❌ overstated <!-- privacy-copy-ok --> |
| "zero retention" / "not retained" | ❌ overstated <!-- privacy-copy-ok --> |
| "ZDR" / "zero data retention" | ❌ overstated <!-- privacy-copy-ok --> |

`tests/unit/test_docs_drift.py` scans this repository's prose files (README,
`CLAUDE.md`, `.env.example` and `docs/*.md` — not the vendored CLI) for the banned
rows above and fails CI on a hit. A line that needs to *discuss* the banned
phrasing — like the three rows in this table — opts out by carrying the literal
marker `privacy-copy-ok`. That is deliberately an explicit marker rather than a
"looks negated" heuristic: an earlier version of the scan skipped any line
containing the word "not", which exempted "does **not** store your prompts", the
one phrasing it was written to catch.

There is no per-request privacy preference and no caller-facing knob, because the
upstream exposes none. `tests/unit/test_docs_drift.py` asserts the adapter sends
nothing in this space, so an edit that invented one would fail rather than quietly
make this section wrong.

If the posture ever changes, every one of these must change with it, in the same
commit: this file, `README.md`, `CLAUDE.md`, `.env.example`, and the CLI's
onboarding copy.

## 5. Retries — two hops, two layers, both legitimate

There are **two independent retry layers on two different network hops.** They are
not a stacking bug, and neither may be "fixed" by assuming the other does not
exist.

```
  CLI  ──(hop 1: CLI → backend)──►  backend  ──(hop 2: backend → Cerebras)──►  model
        cli/internal/backend/retry.go          providers/retry.py
        ≤10 attempts / 2-minute window         MAX_UPSTREAM_RETRIES = 1
```

### Hop 2 — backend → Cerebras (`MAX_UPSTREAM_RETRIES`, default 1)

Applies to every **model** call: main completion, stream open, and utility
tasks. It does **not** apply to the
key-verification probe (`verify_key` calls the upstream directly, with no retry
wrapper — a sign-in probe should answer or fail, not stall behind a backoff).

Retries **transient** failures only — 5xx, timeouts, rate limits, transport
errors — and only while no model output or other replay-unsafe side effect has
been exposed.

- Non-streaming: the retry happens before any public bytes.
- Streaming: an open/prefetch retry may happen *after* the `meta` event has been
  emitted, but **never** once a model delta has been sent.
- A valid upstream `Retry-After` is authoritative and replaces the computed
  backoff entirely, including `0` (retry at once). One larger than 10s abandons
  the retry rather than waiting — a live turn cannot absorb that pause.
- Backoff is `base * 2**attempt`, capped at 5s, plus up to one extra `base` of
  jitter.

### Hop 1 — CLI → backend (`cli/internal/backend/retry.go`)

Up to 10 attempts, exponential from 500ms and settling into a 10–15s poll, inside
a 2-minute elapsed window — sized to ride out a **backend restart**, which is a
failure mode hop 2 cannot see at all.

That window bounds *scheduling*, not wall clock: it decides whether another
attempt may start. An attempt already in flight when the budget expires is allowed
to finish, so a single long turn can exceed two minutes.

The CLI never replays:

- a deterministic failure (401/403 auth, 400 contract, 426 protocol);
- an application **verdict** arriving as a pre-stream HTTP error, keyed on
  `error.code` (`task_output_invalid`, `upstream_error`, `internal_error`) — the
  backend reuses 502 for both a verdict and a real gateway failure, so the code,
  not the status, decides;
- a respond turn that has already streamed visible tokens.

⚠️ **The verdict exclusion is narrower than it looks.** It applies only when the
error arrives as an HTTP response. The same `upstream_error` delivered as a
terminal **SSE** event *is* retried, provided no visible content has committed.
Because `meta` is emitted before the canonical upstream stream is opened, a
deterministic upstream rejection — a revoked key, a malformed parameter mix —
can reach the CLI as an SSE `upstream_error` and be replayed to no purpose. This
is the strongest argument for the error-taxonomy work in §6: with distinct codes,
the SSE path could exclude the deterministic ones too.

A retry after `meta` adopts that meta's signed `state` token in the next POST, so
the backend reuses the same selection rather than re-running the selector.

## 5a. Deadlines — bounding dead air, not turn length

An `httpx` read timeout is an **inactivity** bound, not a wall-clock one: it
measures the gap between chunks, so a response that keeps trickling a byte resets
it forever. Combined with a retry, a stuck turn could approach the Cloud Run
request ceiling with nothing in the application able to stop it — producing no
useful error, no phase attribution, and no released capacity.

`services/deadlines.py` closes that, and the posture matters as much as the
numbers: **these limits detect a turn that has stopped; they do not cap one that
is merely long.** A long agentic round is legitimate work someone is waiting on.
Dead air is the failure.

| Phase | Bounds | Scope | Default |
|---|---|---|---:|
| `preparation` | validation, state decode, skill selection — before the model is opened | absolute | 25 s |
| `open_stall` | stream open → first upstream event | per attempt, **retryable** | 45 s |
| `first_output` | successful open → first visible content or tool call | cumulative from the open | 120 s |
| `stream_idle` | longest gap between chunks once the stream is live | per gap | 60 s |
| `total` | wall-clock backstop for the whole turn | absolute | 900 s |

Properties worth knowing:

- **A pre-output stall is retryable, not fatal.** `open_stall` raises
  `upstream_timeout` (retryable) rather than a deadline error, so `retry_upstream`
  actually sees it. Nothing of the turn is public before the first delta — `meta`
  carries no model output — so replaying is invisible. This was a live incident:
  an endpoint accepted the connection, sent no first event, and the turn died at
  30 s with the configured retry unused and 90% of the total budget unspent, one
  round after the same request opened in 1.2 s.
- **Only `preparation` and `total` are absolute.** `RequestBudget.remaining()`
  raises for the others rather than serving them. `open_stall` is per *attempt* (or
  the retry starts with nothing left and is decorative), `stream_idle` per *gap* (a
  chunk arriving is the evidence the stream is alive), and `first_output` is
  cumulative *from the open* — anchored there so a retried open does not hand the
  stream that replaced it an already-spent allowance, and cumulative so an endpoint
  emitting a reasoning fragment just inside every gap cannot satisfy it forever.
  Progress the user cannot see is not progress.
- **A stalled open is logged where it happens.** `respond_open_stall` names the
  attempt, its allowance and whether the budget was exhausted, so a recovered stall
  is still visible to an operator — a turn that merely looks slow is how the
  original incident hid.
- **Every phase is clamped by the total.** A 60 s idle allowance means nothing
  when 5 s of turn budget remains, and a per-attempt open allowance cannot outlive
  the turn either.
- **Retry attempts share the total.** `retry_upstream` takes the remaining time
  and refuses to start an attempt that cannot finish — without that, N attempts
  at T seconds each cost N×T, which is exactly how a per-attempt timeout composes
  into an unbounded turn. It applies to the stream *open* too, not just chunks.
- **`total` must stay under the platform request timeout** (960 s in
  `cloudbuild.yaml`). Move one and move the other: if Cloud Run wins the race the
  client gets an untyped 504 with no phase and the slot stays held until it fires.
- **None of this has to fit inside the CLIENT's idle window, because the heartbeat
  keeps the stream alive.** The CLI aborts a respond stream after 90 s with no read
  progress (`sseIdleTimeout`), and `_stream_with_heartbeat` emits a `: hb` comment
  every 8 s through selection, the open, its retries and any generation gap —
  comments are invisible to SSE event parsing but do count as read progress. The
  cadence, not any deadline, is the cross-repo invariant, and
  `tests/wire/test_stream_silence_budget.py` pins it against the CLI's own source
  *and* proves a heartbeat actually goes out during a stalled open. Without it the
  client would kill and replay turns the server was still recovering, charging a
  second model call for each.
- **Timing is monotonic.** A clock adjustment mid-turn cannot extend or collapse
  a deadline.

`preparation` means "before the turn had anything to show", which is what a
user experiences.

The failure is `504 turn_deadline_exceeded`, naming the phase. Before headers it
is an ordinary HTTP error; after the stream has committed a 200 it is a terminal
SSE `error` event — never a `done`, which would present a truncated answer as a
complete one. Stream teardown on that path is itself bounded (5 s), because an
`aclose()` hanging on the same dead upstream would otherwise swallow the terminal
error and let the request outlive the deadline that just fired.

Utility tasks get the same treatment with a budget derived from their profile
timeout, because a task that also repairs its JSON makes two upstream calls.

**Known gap:** an abandoned *non-streaming* turn is not cancelled on client
disconnect. ASGI offers no disconnect callback for a buffered request, and
polling `request.is_disconnected()` deadlocks against this app's body-buffering
middleware — the poll delegates to the raw `receive`, which does not report a
disconnect until the response completes, so the handler waits for the client
while the client waits for the handler. The total deadline bounds the waste.
The streaming path has no such gap: `GeneratorExit` closes the upstream stream.

## 5b. Skill selection — the no-task fast path, and why shortlisting waits

Selection itself is local now (~10ms), so the fast path below no longer buys
first-byte latency — it survives because "a greeting clears the active set"
is a semantic contract, not a latency hack.

**Shipped: the no-task fast path.** A cold opening turn whose message is a
recognised greeting or acknowledgement skips the call entirely and clears the
active set. It matches a **closed list of exact phrases**, not a vocabulary — a
token-bag version accepted `"ok ｃｌｏｓｅ ａｌｌ ｔｅｒｍｉｎａｌｓ"` (an ASCII matcher saw
only `ok`) and `"right all this"` (every word allowed, the combination an
imperative). It also refuses whenever anything is in flight: an existing active
set, a background wake, a goal, a workflow digest or run, async operations, or a
resumed watcher. `tests/unit/test_no_task_fast_path.py` replays the whole
selector benchmark corpus and fails on any case that declares a required skill.

**Not shipped: candidate shortlisting.** Sending 6–10 lexically-retrieved
candidates instead of all 31 skills would cut the ~11k-token selector prompt
substantially. It is not implemented because the saving is not obviously a win:

> The catalog block is deliberately request-INVARIANT (`Catalog.selector_metadata`)
> so the upstream can match a shared content prefix across calls. The selector
> cannot use `prompt_cache_key` at all. Shortlisting makes that block vary per
> request, trading a smaller prompt for the loss of prefix locality — and TTFT,
> not token count, is the actual goal.

That is a measurement, not an argument. The experiment that settles it: a
sequential, endpoint-attributed A/B over the benchmark corpus comparing the full
catalog against shortlist sizes 6/8/10, at concurrency 1, recording endpoint,
selector latency, prompt tokens, **cached** tokens, repair calls and cost —
scoring retrieval recall@K separately from end-to-end selection quality, and
including both repeated-shortlist and changing-shortlist pairs so locality is
actually exercised. The existing selector benchmark captures neither cached
tokens nor endpoint attribution and runs at concurrency six, so it cannot answer
this as written.

## 5c. Cost reporting — built for a number the current provider does not send

> **Cerebras reports no cost.** Every field described in this section is
> therefore absent today. This is not a regression to route around: the rules
> below were written so that "we do not know what this cost" is a first-class
> answer, and a client that implemented them correctly already behaves correctly.

Two failure modes are unacceptable and stay unacceptable: reporting a cost we did
not observe, and reporting **zero** for one we could not observe. The second is
the one that bites — a zero reads as "this turn was free", which is a claim about
someone's money with nothing behind it.

**Where it would come from.** `ProviderUsage.cost` is whatever the provider says
it billed, never a token-price estimate computed here. Anything we calculated
ourselves would be a guess presented as a bill, and would be wrong in the
direction that looks authoritative. Cerebras returns token counts and a
`cached_tokens` figure but no billed amount, so `cost` parses to `None` on every
call. Asking for one is not merely useless here — a `usage` key on the request is
a hard 400 (§3).

The plumbing is kept rather than deleted, and the reason is the same one that
justifies the honesty rules: `None` already means exactly "not reported", so
there is nothing to strip out, and a provider that does report cost needs no
change above the adapter.

**What is reported.**

| surface | field | covers |
|---|---|---|
| `POST /respond` body | `cost.total` | every upstream call the request made |
| `POST /respond` body | `cost.main`, `cost.selector` | the breakdown |
| `POST /respond` body | `cost.complete` | whether `total` is exact or a floor |
| streaming | `done` event, same shape | as above |
| `POST /tasks` body | `usage.cost` | that task, including any repair call — omitted unless every call in it reported |

A `/respond` request can bill **more calls than are obvious**:

- the main completion;
- a bare-intent semantic re-roll, whose output the user never saw.

(Skill selection is local and bills nothing; `cost.selector` is null on
production turns and populated only by the scripted test seam.)

`cost.total` sums every one it can observe, because every one of them lands on
the caller's card. The breakdown is kept because it answers the follow-up
question ("why was that turn expensive?").

**Absent means unknown.** When the provider reports no cost, the whole `cost`
block is omitted rather than filled with zeros. A zero would read as "this turn
was free" — a claim about someone's money that we would have no basis for. A
client must treat a missing block as unknown, and
`/v1/daintree/capabilities` says so explicitly under
`respond.cost_reporting.absent_when_unknown`.

**`complete: false` means the total is a floor.** A turn can spend money we
cannot measure, and the field exists so that case never masquerades as an exact
figure. Three things clear it:

- **A component that ran but reported no cost.** A selector call whose cost is
  missing would otherwise contribute `0.0` to a field named `total`, turning a
  partial sum into a claimed complete one — the same "zero means free" error the
  absent-block rule prevents, one level up. Note the distinction: a turn that
  *skipped* selection (continuation round, no-task fast path) has no selector
  usage at all, and its missing cost is a truthful zero rather than a gap.
- **A stream cancelled before it reported.** A stream cut off mid-flight never
  sends the final chunk that carries usage, so its spend is real but
  unobservable — recovering it would mean a second `/generation?id=` call.
  (This was common when speculative streaming existed; with one upstream call
  per round it is now limited to a discarded bare-intent re-roll.)
- **A component that is itself a partial sum.** `main` and `selector` are each
  one *number* but not always one *call*: a bare-intent re-roll bills the main
  slot twice, and a malformed selection bills the selector slot twice. If only
  one of those two attempts reported, the surviving figure is real, non-null,
  and short — and the float alone cannot say so. The summing helpers therefore
  carry a `cost_complete` bit alongside the number
  (`ProviderUsage.cost_complete`, propagated through
  `_RespondMeta.selector_cost_complete`) and `turn_cost` folds it into
  `complete`. This was the last hole: every other case surfaced as a `None` that
  the rules above already caught.

The `/tasks` surface has no `complete` field of its own, so it takes the other
branch of the same rule: a repaired task whose two calls did not both report has
`usage.cost` **omitted**, since absent already means unknown. A partial there is
not a rounding error — a busy orchestration session runs dozens of
`terminal_summarize` / `terminal_extract` calls, and each silently-low figure
would compound into the session total.

The authority on a bill is always the provider's own dashboard; these figures,
when a provider supplies them, are for proportion and trend.

---

## 6. Failure mapping

The caller must be able to tell *whose* problem a failure is: their key, their
account balance, their key's permissions, Daintree's request, or the provider
being down. `providers/cerebras.py` maps upstream status codes onto the stable
codes in [`DAINTREE_API.md`](DAINTREE_API.md#errors); the CLI branches on
`error.code`, never on prose.

### Current mapping

| Upstream | Daintree | Code | Retried? |
|---|---:|---|---|
| 401 | 401 | `provider_invalid_api_key` | no |
| 402 | 402 | `provider_insufficient_credits` | no |
| 403 | 403 | `provider_key_forbidden` | no |
| 404 | 502 | `upstream_request_rejected` | no |
| 429 | 429 | `upstream_rate_limited` (bounded `Retry-After`) | yes |
| 408, 504 | 504 | `upstream_timeout` | yes |
| other ≥ 500 | 503 | `upstream_unavailable` | yes |
| other 4xx | 502 | `upstream_request_rejected` | no |
| unparseable response | 502 | `upstream_protocol_error` | no |

Two subtleties worth knowing before editing this:

- **Retryability is load-bearing, not decoration.** An account failure or a
  malformed request is deterministic; replaying it reaches the same answer more
  slowly, and on the streaming path it burns the CLI's retry budget *after* `meta`
  has already committed a 200.
- **404 is our bug, not a routing dead-end.** Against a single-host provider the
  chat path is fixed and correct, so a 404 can only mean the *model id* is wrong.
  It therefore shares `upstream_request_rejected` with 400 — one condition, one
  answer ("fix what the server sent"), with the upstream's own reason carried
  through. The previous stack needed a whole marker-matching layer to separate a
  routing dead-end from an outage; that ambiguity no longer exists, and neither
  does the code, nor the `upstream_no_compliant_provider` code it produced.
- **Every 5xx is simply an outage** and stays retryable. Prose that used to be
  read as a permanent policy failure ("No endpoints found matching your data
  policy") must not be, and `tests/unit/test_error_taxonomy.py` pins that —
  misclassifying an outage as permanent kills a retry that would have worked.
- **An in-band stream error may carry a symbolic code, not a numeric one.**
  Cerebras sends `"wrong_api_key"` rather than `401`, so the streaming path
  matches those markers explicitly; without that a rejected credential would fall
  through to a generic retryable error and the CLI would replay a request that
  can never succeed.

### Diagnostics and content

A **bounded excerpt** of the upstream error body is logged, and for a non-auth 4xx
it is appended to the client-facing message — it is usually the only thing that
says *why* a request was rejected. Two consequences to be honest about:

- the caller's credential is scrubbed from that excerpt, and if it is still
  recoverable afterwards the excerpt is dropped wholesale;
- request content is **not** scrubbed. A provider that quotes the offending part
  of a request back at us will have that excerpt logged.

Whole raw bodies are never returned, and auth-class failures carry no upstream
prose at all. But note that a body which is not JSON is passed through as text, so
"a bounded, credential-scrubbed excerpt" is the honest description — not "raw
upstream bodies are never echoed".

## 7. Readiness is *configuration* readiness, not provider readiness

`/readyz` validates the server's own secrets, the prompt files, the skill catalog
and provider-object construction. `CEREBRAS_API_KEY` being **present** is part of
that (`missing_secrets`) — without it nothing can be answered at all, and
discovering that on the first turn surfaces as an upstream 401 that reads like the
caller's fault. But presence is not validity: `/readyz` **cannot** prove Cerebras
is reachable or that the key works, and it deliberately does not try. A readiness
probe that made a network call to a third party would fail this service for their
outage.

Base-URL validation is narrower still: the origin is only checked in **hardened**
environments (`staging`, `production`), where the server credential is forwarded
to it. A development deployment constructs the provider from whatever string it is
given.

Live provider readiness belongs to the `/v1/daintree/auth/verify` probe (a
tokenless model listing) and to release smoke — never to `/readyz`.

## 8. What this document can and cannot prove

The code proves exactly one class of fact: **which parameters we send**. That is
what the drift tests pin.

Everything else here is either a Cerebras behaviour or a measurement, and is
recorded as a **dated observation** rather than a timeless truth. Specifically:

- what Cerebras retains and for how long (§4) — their published policy, not
  something this repository can assert or enforce;
- that `prompt_cache_key` is accepted but not required;
- that `gemma-4-31b` does not reason;
- the prefix-cache block size and TTL;
- every latency and token figure quoted above.

The live suite (`tests/live/test_live_cerebras.py`) does pin the *wire* half
against the real API — which parameters are accepted, which are rejected, and
that the serving model reports zero reasoning tokens — so the facts most likely
to break silently are checked rather than remembered. The rest comes from this
repository's benchmark suites, which write their results to a gitignored
directory, so those numbers cannot be traced back to a committed run. Treat a
quoted figure as "what we saw on the date given", re-measure before relying on
it, and prefer citing Cerebras's own documentation (with the date read) for
provider semantics.

## 9. Changing the pinned model

A model change requires, before merge: the full deterministic suite, the full live
suite, a skill-selection comparison, an injection comparison, a latency
comparison, a tool-call correctness comparison, a refreshed
`benchmarks/baseline.json`, and an explicit release note. Do not switch to a
floating alias during internal testing.

**`gemma-4-31b` is a preview model with a stated replacement coming**, so this is
scheduled work rather than a hypothetical. Two things beyond quality have to be
re-decided when it moves:

- **Thinking posture.** Nothing thinks because this model cannot reason, not
  because a flag is set — and Cerebras offers no way to switch reasoning off. A
  reasoning-capable replacement is therefore a *deliberate* posture decision, not
  a drop-in (§2). The live suite asserts zero reasoning tokens so this cannot be
  missed.
- **Prompt budgets.** The per-file budget tests and `benchmarks/baseline.json`
  are sized against this model's context window and this model's behaviour under
  the current prompts.
