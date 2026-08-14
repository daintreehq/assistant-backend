# Provider policy

**Authoritative** for: which model answers, which endpoint serves it, what privacy
the request is routed under, how failures are retried, and what error the caller
gets back. Every other document (README, `CLAUDE.md`, `.env.example`, CLI copy)
summarises this one and links here.

`tests/unit/test_docs_drift.py` fails CI when *selected* facts below stop matching
the code — privacy wording, thinking posture, model ids, credential ownership,
retry layers, the routing policy, the environment set. It is a targeted gate over
this repository's own prose, not a proof that every sentence here is accurate, and
it does not see the vendored CLI at all.

Claims marked ⏳ are **not yet implemented**. Do not repeat them elsewhere in the
present tense.

---

## 1. Provider and models

The **only** upstream is OpenRouter. It is a router, not a model host: it fronts
many independent endpoints serving the same weights (roughly two dozen for this
model when last checked, 2026-07 — a provider-side number that moves, not
something this repository can assert), and the `provider` block on every request
decides which one answers.

| Call | Model | Source of truth |
|---|---|---|
| Main completion | `deepseek/deepseek-v4-flash-0731` | `config.OPENROUTER_MODEL` |
| Skill selector | `deepseek/deepseek-v4-flash-0731` | `config.OPENROUTER_MODEL` |
| Utility tasks | `deepseek/deepseek-v4-flash-0731` | `config.OPENROUTER_MODEL` |
| Skill-learning gate | `deepseek/deepseek-v4-flash-0731` | `config.OPENROUTER_MODEL` |
| Skill builder / improver | `openai/gpt-5.6-sol` | `config.SKILL_BUILDER_MODEL`, `config.SKILL_IMPROVER_MODEL` |

Model ids are **hardcoded** — a product decision, not deployment config. There is
no environment override, and there must not be one: every prompt and benchmark in
this repository is calibrated against these exact revisions. The date-pinned
DeepSeek slug is deliberate; the floating alias would silently re-point at a
differently-tuned revision.

`/v1/daintree/respond` and `/v1/daintree/capabilities` report
`config.PUBLIC_MODEL_ID` (`daintree-assistant`), never a real model id.
`/v1/daintree/tasks` currently does **not**: its response carries the pinned
upstream id verbatim. That is an inconsistency, not a design — do not write "the
client never sees a real model id" without qualifying it.

## 2. Thinking posture

**The whole interactive surface is non-thinking.** The main completion, every
utility task and the skill selector send `reasoning: {"enabled": false}`
explicitly on every call, because a reasoning model reasons by default when the
field is absent.

| Surface | Thinking | Flag |
|---|---|---|
| Main completion | off | `INTERACTIVE_THINKING_ENABLED = False` |
| Utility tasks | off | `INTERACTIVE_THINKING_ENABLED = False` |
| Skill selector | off | independent hardcode in `skills/selector.py` |
| Skill-learning gate | **high** | `OPENROUTER_REASONING_EFFORT = "high"` |
| Skill builder / improver | **high** | `SKILL_LEARNING_REASONING_EFFORT = "high"` |

Skill learning is the one surface that still thinks. It is local-development-only
and runs fire-and-forget *after* the response is sent, so its latency never
reaches a user.

There is no middle tier to retreat to: DeepSeek V4 Flash collapses `low` and
`medium` into `high` (measured, 5 samples each: 662 vs 656 reasoning tokens, fully
overlapping ranges), so the only real step below `high` is off.

The selector keeps a **second, independent** hardcode rather than reading the
shared flag. Re-enabling interactive thinking must not silently drag the selector
— which gates the turn's first byte — along with it.

## 3. Endpoint-selection policy

Sent as the top-level `provider` block on every request. The **defaults** live
beside the model ids (`providers/policy.py`, `DEFAULT_POLICY`); there are
deliberately no environment toggles.

A caller may narrow or re-rank them per request through the validated `routing`
block ([`DAINTREE_API.md`](DAINTREE_API.md#endpoint-routing-optional)) — they own
the key and pay the bill, so privacy strictness and the speed/price trade are
legitimately theirs. What they cannot do is weaken the floor: `data_collection:
"deny"` is emitted unconditionally and is derived from no caller-controlled
field. The shipped default block is:

```json
{
  "sort": "throughput",
  "data_collection": "deny",
  "require_parameters": true,
  "allow_fallbacks": true
}
```

- **`sort: "throughput"`** — rank eligible endpoints by measured tokens/second and
  take the fastest, explicitly at the cost of price. Correct for an interactive
  assistant: wall-clock is dominated by generation, and this model is cheap enough
  that a 40% price delta is noise next to a 2x speed one.
- **`data_collection: "deny"`** — the privacy floor. See §4.
- **`require_parameters: true`** — drop endpoints that would silently ignore
  something we sent. It matters most for `json_object` calls (selector, utility
  tasks): several otherwise-fast endpoints for this model do not implement
  `response_format`, and without this they would answer with unconstrained prose
  that fails our parse.
- **`allow_fallbacks: true`** — keep the next-fastest compliant endpoint available
  when the winner is down. With filters this strict, turning it off makes any
  single outage a hard 404.

**Structured calls** route under the same policy plus a tail bound,
`preferred_max_latency: {"p90": 2.0, "p99": 5.0}`. The trigger is purely
mechanical — *any* request carrying `response_format` — which today means the
selector, every utility task, the skill-learning gate, the skill
builder/improver, and a main completion whose client asked for JSON mode. `require_parameters`
already excludes the fastest endpoint for this model from that pool, and sorting
the remainder by tokens/second is the wrong axis for a call that emits a
~40-token JSON object: it elected endpoints with acceptable medians and
catastrophic tails (p99 12.6s, 13.9s). `preferred_max_latency` **deprioritises,
never excludes**, so it cannot empty the pool. Measured over 10 selector-shaped
calls: TTFT median 1222ms → 635ms, worst 1811ms → 920ms.

No `prompt_cache_key` is sent: no endpoint for this model advertises the
parameter, and with `require_parameters` on, sending an unadvertised parameter can
empty the eligible pool. Prefix caching is implicit at whichever endpoint serves
the request.

## 4. Privacy — what is and is not promised

> **`data_collection: "deny"` is a no-training guarantee.**
> **It is not a no-retention guarantee.**

This distinction is the whole of the privacy contract, and getting the wording
wrong is worse than a conservative promise, because it becomes part of a tester's
trust decision about source code, terminal output, issue text and project
instructions.

| Phrase | Allowed? |
|---|---|
| "does not collect or train on request data" | ✅ accurate |
| "no endpoint trains on your prompts" | ✅ accurate |
| "does not store your prompts" | ❌ overstated <!-- privacy-copy-ok --> |
| "zero retention" / "not retained" | ❌ overstated <!-- privacy-copy-ok --> |
| "ZDR" / "zero data retention" | ❌ not enforced <!-- privacy-copy-ok --> |

`tests/unit/test_docs_drift.py` scans this repository's prose files (README,
`CLAUDE.md`, `.env.example` and `docs/*.md` — not the vendored CLI) for the banned
rows above and fails CI on a hit. A line that needs to *discuss* the banned
phrasing — like the three rows in this table — opts out by carrying the literal
marker `privacy-copy-ok`. That is deliberately an explicit marker rather than a
"looks negated" heuristic: an earlier version of the scan skipped any line
containing the word "not", which exempted "does **not** store your prompts", the
one phrasing it was written to catch.

OpenRouter models the two separately. `data_collection: "deny"` filters to
endpoints whose policy is not to collect prompt data or train on it; an endpoint
in that pool may still hold a request transiently, e.g. for abuse monitoring.
The stricter mode is OpenRouter's separate `zdr` flag, which this server does not
send. <!-- privacy-copy-ok -->

**Current default: `NO_TRAINING`.** The stricter mode is available per request
as `routing.privacy: "zdr"`, and is `PrivacyMode.ZDR` in code. <!-- privacy-copy-ok -->
It stays off *by
default* because it narrows the endpoint pool substantially and has not been
live-tested against every request shape this server emits (streaming with tools,
non-streaming, `json_object`, forced tool choice, tool-free) — defaulting to it
would risk `upstream_no_compliant_provider` on ordinary turns.

A caller who opts in gets it on **every** upstream call the turn makes: the main
completion, the skill selector, utility tasks, `/skills/resolve`'s `find`, and
(in local development) skill learning. A privacy choice honoured only on the
visible call would be worse than none, because it would look like it worked.

If the posture ever changes, every one of these must change with it, in the same
commit: this file, `README.md`, `CLAUDE.md`, `.env.example`,
`providers/openrouter.py`, the `/v1/daintree/capabilities` response, and the CLI's
onboarding copy.

## 5. Retries — two hops, two layers, both legitimate

There are **two independent retry layers on two different network hops.** They are
not a stacking bug, and neither may be "fixed" by assuming the other does not
exist.

```
  CLI  ──(hop 1: CLI → backend)──►  backend  ──(hop 2: backend → OpenRouter)──►  endpoint
        cli/internal/backend/retry.go          providers/retry.py
        ≤10 attempts / 2-minute window         MAX_UPSTREAM_RETRIES = 1
```

### Hop 2 — backend → OpenRouter (`MAX_UPSTREAM_RETRIES`, default 1)

Applies to every **model** call: main completion, stream open, selector, utility
tasks, and the skill-learning gate/builder/improver. It does **not** apply to the
key-verification probe (`verify_key` calls OpenRouter directly, with no retry
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
deterministic OpenRouter rejection — a revoked key, an unroutable parameter mix —
can reach the CLI as an SSE `upstream_error` and be replayed to no purpose. This
is the strongest argument for the error-taxonomy work in §6: with distinct codes,
the SSE path could exclude the deterministic ones too.

A retry after `meta` adopts that meta's signed `state` token in the next POST, so
the backend reuses the same selection rather than re-running the selector.

## 5a. Deadlines — a finite end-to-end budget

An `httpx` read timeout is an **inactivity** bound, not a wall-clock one: it
measures the gap between chunks, so a response that keeps trickling a byte resets
it forever. Combined with a retry, a stuck turn could approach the Cloud Run
request ceiling with nothing in the application able to stop it — producing no
useful error, no phase attribution, and no released capacity.

`services/deadlines.py` adds four limits, because "it took too long" is four
different bugs:

| Phase | Bounds | Default |
|---|---|---:|
| `preparation` | validation, state decode, skill selection — before the model is opened | 25 s |
| `first_output` | arrival → first visible content or tool call | 30 s |
| `stream_idle` | longest gap between chunks once generation started | 60 s |
| `total` | hard wall clock for the whole turn | 300 s |

Five properties worth knowing:

- **`preparation` and `first_output` are absolute**, measured from the start of
  the turn; only `stream_idle` resets per gap, because a chunk arriving *is* the
  evidence the stream is alive. An earlier revision recomputed the first-output
  allowance per chunk, so a reasoning fragment every 29 s renewed a 30 s deadline
  indefinitely — which is why `RequestBudget.remaining()` now raises rather than
  serving `stream_idle`.
- **Every phase is clamped by the total.** A 60 s idle allowance means nothing
  when 5 s of turn budget remains.
- **Retry attempts share the budget.** `retry_upstream` takes the remaining time
  and refuses to start an attempt that cannot finish — without that, N attempts
  at T seconds each cost N×T, which is exactly how a per-attempt timeout composes
  into an unbounded turn. It applies to the stream *open* too, not just chunks.
- **Reasoning fragments do not satisfy `first_output`.** Only visible content or a
  tool call does; otherwise a model that thinks for two minutes escapes the
  deadline that exists to measure user-perceived latency.
- **Timing is monotonic.** A clock adjustment mid-turn cannot extend or collapse
  a deadline.

The phases do not cleanly mean "before the model was reached": speculation may
open an upstream stream *during* preparation. `preparation` means "before the
canonical turn had anything to show", which is what a user experiences.

The failure is `504 turn_deadline_exceeded`, naming the phase. Before headers it
is an ordinary HTTP error; after the stream has committed a 200 it is a terminal
SSE `error` event — never a `done`, which would present a truncated answer as a
complete one. Stream teardown on that path is itself bounded (5 s), because an
`aclose()` hanging on the same dead upstream would otherwise swallow the terminal
error and let the request outlive the deadline that just fired.

**Not covered:** detached post-turn skill learning. It runs after the response is
sent, so it cannot delay a turn, but it has its own timeout rather than a share
of this budget — and it is local-development-only, failing readiness in staging
and production.

Utility tasks get the same treatment with a budget derived from their profile
timeout, because a task that also repairs its JSON makes two upstream calls.

**Known gap:** an abandoned *non-streaming* turn is not cancelled on client
disconnect. ASGI offers no disconnect callback for a buffered request, and
polling `request.is_disconnected()` deadlocks against this app's body-buffering
middleware — the poll delegates to the raw `receive`, which does not report a
disconnect until the response completes, so the handler waits for the client
while the client waits for the handler. The total deadline bounds the waste.
The streaming path has no such gap: `GeneratorExit` closes the upstream stream.

## 6. Failure mapping

The caller must be able to tell *whose* problem a failure is: their key, their
account balance, their key's permissions, Daintree's request, or the provider
being down. `providers/openrouter.py` maps upstream status codes onto the stable
codes in [`DAINTREE_API.md`](DAINTREE_API.md#errors); the CLI branches on
`error.code`, never on prose.

### Current mapping

| Upstream | Daintree | Code | Retried? |
|---|---:|---|---|
| 401 | 401 | `provider_invalid_api_key` | no |
| 402 | 402 | `provider_insufficient_credits` | no |
| 403 | 403 | `provider_key_forbidden` | no |
| 404 | 503 | `upstream_no_compliant_provider` | no |
| 503 *with a no-route message* | 503 | `upstream_no_compliant_provider` | no |
| 429 | 429 | `upstream_rate_limited` (bounded `Retry-After`) | yes |
| 408, 504 | 504 | `upstream_timeout` | yes |
| other ≥ 500 | 503 | `upstream_unavailable` | yes |
| other 4xx | 502 | `upstream_request_rejected` | no |
| unparseable response | 502 | `upstream_protocol_error` | no |

Two subtleties worth knowing before editing this:

- **Retryability is load-bearing, not decoration.** An account failure or an empty
  routing pool is deterministic; replaying it reaches the same answer more slowly,
  and on the streaming path it burns the CLI's retry budget *after* `meta` has
  already committed a 200.
- **A no-route answer can arrive as 404 or 503**, depending on which filter
  emptied the pool, so the 503 branch is disambiguated by the message
  (`_looks_unroutable`). That match is biased toward reading an ambiguous 503 as
  an outage: mistaking an outage for a policy dead-end would mark a transient
  failure permanent and kill a retry that would have worked.

`upstream_no_compliant_provider` **fails closed**. The alternative — quietly
relaxing the privacy filter until something routes — would make §4 a lie.

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
and provider-object construction. It **cannot** prove OpenRouter is reachable or
that a caller's key works, because the service intentionally holds no key of its
own.

Base-URL validation is narrower still: the origin is only checked in **hardened**
environments (`staging`, `production`), where caller keys are forwarded to it. A
development deployment constructs the provider from whatever string it is given.

Live provider readiness belongs to the authenticated `/v1/daintree/auth/verify`
probe and to release smoke — never to `/readyz`.

## 8. What this document can and cannot prove

The code proves exactly one class of fact: **which parameters we send**. That is
what the drift tests pin.

Everything else here is either an OpenRouter behaviour or a measurement, and is
recorded as a **dated observation** rather than a timeless truth. Specifically:

- the size of the eligible endpoint pool;
- that `preferred_max_latency` deprioritises rather than excludes;
- the precise semantics OpenRouter attaches to `data_collection: "deny"`;
- that no endpoint for this model advertises `prompt_cache_key`;
- that this model collapses `low`/`medium` into `high`;
- every TTFT, p90/p99, price and reasoning-token figure quoted above.

The measurements come from this repository's benchmark suites, but those suites
write their results to a gitignored directory, so the numbers in this document
cannot currently be traced back to a committed run. Treat a quoted figure as
"what we saw on the date given", re-measure before relying on it, and prefer
citing OpenRouter's documentation (with the date read) for provider semantics.

## 9. Changing the pinned model

A model change requires, before merge: the full deterministic suite, the full live
suite, a skill-selection comparison, an injection comparison, a latency
comparison, a tool-call correctness comparison, and an explicit release note. Do
not switch to a floating alias during internal testing.
