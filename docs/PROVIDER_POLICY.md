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

## 1. Provider and model

There are **two internal provider ROLES**, and exactly one public product.

| Role | Host | Model | When it is called |
|---|---|---|---|
| **executor** | Baseten Model APIs (`https://inference.baseten.co/v1`) | `zai-org/GLM-5.3-Flash` | every turn, and every utility task |
| **executor fallback** | OpenRouter (`https://openrouter.ai/api/v1`) | `z-ai/glm-5.3-flash` | only when Baseten cannot answer (§1a) |
| **fast response** | Cerebras (`https://api.cerebras.ai/v1`) | `gpt-oss-120b` | **NEVER — the stage is OFF** (`config.FAST_RESPONSE_ENABLED`) |

The fallback is a second HOST for the same weights, not a second role in the
sense the other two are: it answers the identical request, at the identical
reasoning floor, and its existence changes nothing about an ordinary turn.

A caller cannot choose between them and is never told which answered. The public
model id stays `daintree-assistant`; the backend composes the two calls behind it.

**This is not the return of the `pro` tier.** That was a user-facing product mode
with a registry, per-mode credentials and a per-turn route on the wire, and it was
deleted when its pricing could not be made to work. What is here is a stage: an
internal role with no caller-visible surface, whose entire failure mode is that a
turn has no preamble.

**Both hosts receive request-derived content on an eligible turn** — the user's
last three messages (cropped to a shared budget) and up to three selected runbook
bodies. (Narrower than it was:
the stage no longer sends the conversation history or the tool inventory.)
Cerebras is not a cache or an infrastructure service in this design; it reads
prompt content, and section 4 applies to it exactly as it applies to Baseten —
**whenever the stage is on. It is currently OFF**, so today Cerebras receives
nothing at all and Baseten is the only host that sees prompt content. Do not
read that as the stage being removed: it is one constant away from running
again, and section 4a stays live for that reason.

**The executor host is a model host, not a router**: one
endpoint serves the model, and there is nothing to select between.

That single fact is why this document is much shorter than it used to be. Under
OpenRouter, ~24 interchangeable endpoints served the same weights and a `provider`
block on every request decided which one answered — which meant a privacy filter,
a ranking axis, a latency floor, a fallback policy, an output-quality denylist, a
sticky-affinity key and a caller-facing `routing` preference to expose all of it.
None of that has a subject any more, so all of it is **deleted rather than
neutered** (git history has the previous revision).

None of it comes back with the fallback in §1a either, and the distinction is
worth being precise about, because "we send a `provider` block again" is the
easiest thing to misread here. The deleted apparatus was a POLICY evaluated on
every ordinary turn against a host with one endpoint. What §1a sends is an
ORDERING over a pool that genuinely exists, on a path that runs only when the
preferred host has already failed. A caller still cannot express a preference,
still targets `daintree-assistant`, and is still never told which host answered.

| Call | Model | Source of truth |
|---|---|---|
| Main completion | `zai-org/GLM-5.3-Flash` | `config.EXECUTOR_MODEL` |
| Utility tasks | `zai-org/GLM-5.3-Flash` | `config.EXECUTOR_MODEL` |
| Runbook selection | — local two-stage ONNX selector, no upstream call | `runbooks/local/setwise.py` |

**A second product mode briefly lived here** — a `pro` tier on Cerebras serving
`gemma-4-31b`, faster on the same benchmark payloads (p50 486ms against 1100ms).
It is gone: Cerebras's pricing structure could not be made to work for this
product, so there was no second route left to choose between. The registry, the
per-mode credentials, the per-turn route threading and the wire rules specific to
that host all went with it rather than being kept warm. Git history has the
whole change. A genuine second tier lands there again as a deliberate piece of
work, not as a flag.

**The host and the model moved together, and no earlier number survives that.**
The previous identity was `deepseek-ai/DeepSeek-V4-Flash-0731` on Together, which
was itself a pure HOST decision — the same weights `context_compact` reached
through OpenRouter for months, replayed over 62 real utility-task payloads at
Together p50 1100ms / p90 1702ms against OpenRouter's p50 3414ms / p90 6013ms.
This change is not like that one: different weights on a different host, so every
latency, cost and quality figure recorded anywhere in this repo predating it
describes a system that no longer exists. Treat them as history, not as a
baseline, and re-measure before citing one.

`config.EXECUTOR_MODEL` names the model and `config.EXECUTOR_ORIGIN` the host the
credential is sent to; `main._build_provider` builds the adapter, readiness gates
`BASETEN_API_KEY`, and `weak_secrets` pins the origin in production.

The id is **hardcoded** — a product decision, not deployment config. There is no
environment override, and there must not be one: every prompt and benchmark in
this repository is calibrated against this exact model. Changing it is §9.

> **Runbook selection is not an upstream call.** It runs as a LOCAL in-process
> two-stage selector (`runbooks/local/`, no credential, no endpoint, ~10ms). The
> There is no other selector: the prompt-based one has been deleted, so
> "selector latency" and "selector cost" below describe a path that no longer
> exists on any route.

**Every credential is the server's.** `BASETEN_API_KEY` funds every turn,
`OPENROUTER_API_KEY` funds the ones Baseten could not, and `CEREBRAS_API_KEY`
funds the preamble stage; callers supply none of their own —
see [`KEY_HANDLING.md`](KEY_HANDLING.md). This inverted with the move off
caller-supplied keys and it is the fact most likely to be misremembered from
older copy. Variable names follow the ROLE (`EXECUTOR_*`, `FAST_RESPONSE_*`);
the ownership does not change with them.

`/v1/daintree/respond` and `/v1/daintree/capabilities` report
`config.PUBLIC_MODEL_ID` (`daintree-assistant`), never a real model id.
`/v1/daintree/tasks` currently does **not**: its response carries the pinned
upstream id verbatim. That is an inconsistency, not a design — do not write "the
client never sees a real model id" without qualifying it.

### 1a. The executor fallback — a second host, not a second product

**Before this existed, a Baseten outage was a Daintree outage.** One host held
the only copy of the executor, so a 5xx, a refused connection or a socket that
went quiet became a failed turn as soon as `MAX_UPSTREAM_RETRIES` was spent —
and retrying a dead host harder is not robustness, it is the same failure taken
more slowly. Nothing about these weights requires Baseten specifically: twenty
other endpoints serve `z-ai/glm-5.3-flash` behind OpenRouter, so the single point
of failure was an accident of how the host was picked.

`providers/failover.py` fronts the two. It is transparent above the provider
boundary — the orchestrator, the task runner, the retry layer and both response
paths see one provider and one model.

**Failover is only ever attempted before any model output has been forwarded**,
and that is structural rather than a rule anyone has to remember. There are four
sites, all pre-output: `complete`, `structured_tool_call`, `open_stream`, and the
first-event prefetch on the stream it returns. The first event closes the window,
so a mid-generation failure stays terminal exactly as it always was — replaying
content a user has already seen is the one thing this must not do.

**A first event is not a health verdict, though, and that distinction is
separate.** "The host responded" is not "the host completed a generation": one
that emits a role-only frame and then goes quiet, or a token and then drops,
would otherwise settle successfully on every turn and the breaker could never
open against it. So a stream's permit is held for its whole life — clean
termination counts as success, a late break counts as a failure, and an abandoned
stream counts as neither.

**What counts as "the host could not answer" is defined by EXCLUSION.** The
eligible set is everything except the three errors that are about our REQUEST:

| Error | Failover? | Why |
|---|---|---|
| `upstream_request_rejected` | no | A malformed body is malformed on both hosts. |
| `context_window_exceeded` | no | Same weights, same window. |
| `upstream_runaway_output` | no | We stopped the generation; the host is fine. |
| `provider_key_forbidden` | no | The one entry that is not about the request, and it is here because the code is AMBIGUOUS: it covers model permissions and spend limits, and it also covers a host's own moderation refusal, with nothing separating them. Failing over would sometimes take a prompt one host declined and send it whole to another. Split the code before revisiting. |
| everything else | yes | 5xx, timeout, rate limit, a missing route (404), an unparseable response, a dead credential, an exhausted balance — each says something about THIS host and nothing about the request. |

`upstream_unavailable` now covers **404** as well as 5xx. A chat-completions 404
is never a malformed body — the body was not parsed to produce it — and both
readings (a model deployment that is down, or an eligible pool the fallback's own
`ignore`/`require_parameters` filters emptied) are facts about a host. Under the
previous mapping the error most likely to mean "this host is down" was classified
as a request fault, which is to say it was the one error guaranteed NOT to reach
the second host.

A request-fault also does **not** count against the primary's health: the host
answered, which is exactly what health measures. Defining the small set rather
than the large one is what stops a new error code defaulting to "give up".

#### The breaker

An outage is not one failed request, and engaging purely per-call gets the worst
of both readings. Left alone, every turn during a ten-minute incident pays the
primary's failure — usually a full TIMEOUT — before reaching the host that was
always going to answer. Tripping on the first failure does the opposite: moves
all traffic to a second host over a blip that has already passed.

| State | Behaviour | Leaves when |
|---|---|---|
| closed | every call tries the primary; a failure falls back for that call and increments a counter, any success resets it to zero | `EXECUTOR_FALLBACK_FAILURE_THRESHOLD` (3) CONSECUTIVE failures |
| open | the primary is skipped outright; calls go straight to the fallback and wait for nothing | `EXECUTOR_FALLBACK_PROBE_INTERVAL_SECONDS` (180s) elapses |
| half-open | ONE call becomes a probe against the primary; every other concurrent caller stays on the fallback | the probe succeeds (→ closed) or fails (→ open, a full interval re-armed) |

There is a **fourth timer**, for the outage shape that produces no status code at
all. A host can accept the connection, answer with headers, and then never
complete an SSE event — which the orchestrator bounds with a wall clock, so it
arrives as a CANCELLATION rather than as an error the breaker can classify. The
failure count alone does not help there: the retry re-enters immediately, the
breaker is still two failures from opening, and the turn burns its whole budget
stalling twice against the same host while the fallback is never called. So a
stall also holds the primary off for `EXECUTOR_FALLBACK_STALL_HOLDOFF_SECONDS`
(30s), which is what actually puts the retry on the fallback. A client
disconnecting mid-open is cancelled identically and is misread as a stall —
deliberately, because the asymmetry is stark: that misreading costs half a minute
of traffic on a healthy second host, while missing a stall costs every turn of
the outage.

So a 10-to-15-minute incident costs: two or three failed-then-recovered turns to
open the breaker, three to five probe turns spread across the outage, and nothing
else. Recovery is noticed within one probe interval of it happening. (Two or
three rather than exactly three because the threshold counts primary ATTEMPTS,
and `MAX_UPSTREAM_RETRIES` means one turn can contribute more than one.)

State is **per-instance and per-process**. Cloud Run holds no shared state, so a
second instance opens its breaker on its own evidence — which is sound rather
than merely tolerable, since each instance's evidence is about the connections IT
is making, and a shared store would buy fewer probes without shortening the
window it takes to notice recovery.

Three log lines carry the incident, and their SEVERITIES are deliberate.
`failover_engaged` (WARNING) is a surprise — the primary could not answer.
`failover_primary_skipped` (INFO) is the mechanism working, and it fires on every
call for as long as the breaker is open, so at warning it would bury the line
that says why under minutes of lines saying nothing new. `failover_recovered`
(WARNING) is the transition with no natural line of its own: without it, recovery
reads as the logging simply going quiet, which is indistinguishable from traffic
stopping.

`/v1/daintree/doctor` reports the live posture under `provider.fallback`:
`armed`, `breaker_open`, `consecutive_failures`, `breaker_opened_count` and
`seconds_until_probe`. Neither `armed: false` nor an open breaker raises a
warning — the first is a correct deployment without a spare tyre, and the second
is the mechanism working.

#### Endpoint selection

The fallback adapter (`providers/openrouter_fallback.py`) sends a `provider`
block, and it is the **only** place in this server that does:

| Field | Value | Why |
|---|---|---|
| `sort` | `"latency"` | The visible cost of a failover is time-to-first-byte, and the turn has already spent its primary attempt. OpenRouter's default balance is price-and-throughput weighted, which is the wrong optimum here. |
| `ignore` | `["baseten"]` | OpenRouter serves this model from Baseten among others, and that is the one endpoint guaranteed not to help — this path runs *because* Baseten just failed. |
| `allow_fallbacks` | `true` | A fallback that could not fall back would be a new single point of failure. |
| `require_parameters` | `true` | OpenRouter's default is to DROP an unsupported parameter, not to avoid the endpoint. A JSON task whose `response_format` went missing answers prose, burns its repair retry and 502s, and nothing in that chain names the cause. |

The thinking posture is §2's, restated in OpenRouter's canonical spelling
(`reasoning: {"effort": "low"}`, normalized by OpenRouter to each endpoint's own
`reasoning_effort`). A fallback that reasoned differently would answer
differently, and the point of this host is to be a substitute.

One capability difference is worth knowing before reading a dashboard:
**OpenRouter reports `usage.cost` and Baseten does not**, so a failed-over turn
is priced while an ordinary one is not (§5c).


## 2. Thinking posture

**THE SERVING MODEL CANNOT BE RUN NON-THINKING, and this section used to say
the opposite.** It is the single biggest behavioural change in the move to
GLM-5.3-Flash, and the sentence that stood here for the previous model — "nothing
in this server thinks" — is now false.

The previous model, DeepSeek V4 Flash, reasoned by default and could be held
down: every call sent `reasoning: {"enabled": false}`, and Together honoured it,
measured, with `reasoning_tokens` dropping out of the usage block entirely.
GLM-5.3-Flash offers no equivalent. Baseten lists its reasoning as **always
enabled**, `none` is not an accepted effort, and an **absent `reasoning_effort`
means `high`** — so omitting the field is the most expensive posture available
rather than the cheapest.

What the server does instead is pin the floor. `config.EXECUTOR_REASONING_EFFORT`
is `low`, injected into the adapter at construction, and `providers/baseten.py`
sends it on **every** call — including the ones the code marks
`thinking_disabled`, because for this host that flag can only mean "as little as
possible", never "none".

| Surface | Thinking | How |
|---|---|---|
| Main completion | floor | `reasoning_effort: "low"` — no off switch exists |
| Utility tasks | floor | same |
| Forced tool calls | floor | `thinking_disabled=True` clamps to the floor |
| Runbook selector | n/a | local classifier, no model call |

Measured on the live API over 6 trials of one proof-and-verify prompt: with the
field **absent**, the model reasoned on 6 of 6 (median 479.5 reasoning tokens,
spread 161–2519); at `low`, on 1 of 6 (median 0, the single non-zero trial
costing 16 tokens). So the floor is close to the old posture in practice, but it
is a floor and not a zero, and it is not guaranteed to stay near zero on harder
prompts.

**The unified `reasoning` object is never sent.** Baseten accepts unknown
top-level fields silently — a made-up field answers 200, and so does an `enabled`
value that is not a boolean — so the parent adapter's `{"enabled": false}` cannot
be distinguished from something the host is discarding, however it happens to
measure. `reasoning_effort` is the documented control and the only one used.

`INTERACTIVE_THINKING_ENABLED` stays `False` in the code, but **its meaning
changed with the host**: it now records that the server asks for the least
reasoning available, not that no reasoning occurs. `/doctor` and
`/v1/daintree/capabilities` still publish it as `thinking_enabled: false`, which
should be read as a statement about what is REQUESTED. A caller that gates on it
expecting a hard zero would be wrong.

**None of this is pinned live.** There is no live contract suite (see §8), so the
numbers above rest on measurements taken by hand rather than on a test that would
notice the floor moving. `TODO.md` carries the suite.

## 3. Wire shape — what a request carries, and what it must never carry

**Baseten is not a router: one endpoint serves the model.** So there is no
endpoint-selection policy and no caller-facing `routing` block — a request
carries the model, the messages, and the parameters the call actually needs, and
nothing about where it should go.

The adapter is `providers/baseten.py`: the OpenRouter one with three class
attributes changed (base URL, key-probe path, no attribution headers), plus a
key-record decoder and the thinking posture above. The decoder exists because
Baseten's `/models` answers OpenAI's `{"object": "list", "data": [...]}` — a LIST
under `data` — where OpenRouter's `/key` answers a MAPPING under the same name,
so the parent's decoder would reject every valid key. Same body otherwise, same
SSE parsing, same error mapping.

**`reasoning_content` is Baseten's own spelling**, in the non-streaming `message`
and in the streaming `delta` alike, and it is already the name everything above
the provider boundary uses. So the parent's inbound rename is a harmless no-op
here, and its OUTBOUND rename is undone: a replayed assistant chain-of-thought is
sent as `reasoning_content`, the name the host reads.

**A table of four host-specific wire rules used to sit here.** They were
Cerebras's — `max_completion_tokens` never `max_tokens`, no `reasoning` object,
no `provider` block, no `session_id`, and flat error bodies — and every one of
them is the OPPOSITE of, or absent from, what Baseten wants. They are deleted
rather than kept as a switch-back note, because a reader who half-remembered one
would send a request this host rejects. Git history has them.

### 3a. Cache affinity — computed, and NOT sent

Baseten caches prompt prefixes **automatically**, matched on the literal prompt
prefix and billed at a separate `input_cache_read` rate. Nothing has to be
configured, and there is no endpoint to steer traffic toward — the mechanism an
earlier adapter's sticky-key machinery existed to drive is simply built in here.
(Reuse is observable: a repeated session measured `cached_tokens` rising from 0
to 9,216 across a tool-continuation round. The cache's SCOPE — per key, per
organization — is not documented here and has not been verified.)

**The server computes an affinity key and the adapter does not send it.** That
is the honest state, and this section previously claimed the opposite. Every
main conversation call and utility task carries a `cache_session_id` on the
normalized `ProviderChatRequest`:

| Call | Key | What it names |
|---|---|---|
| Main conversation | `daintree-main-<sha256(session id + salt)>` | that session's growing prefix |
| Main, JSON mode | `daintree-main-json-<sha256(session id + salt)>` | ditto, minus the tool schemas |
| Utility task | `daintree-task-<task id>` | that task's server-owned system prompt |

`_build_body` then drops it. OpenRouter's spelling (`session_id`) only ever meant
anything as a way to pin a sticky record across the endpoint pool the routing
block selected from, and that block is gone; `prompt_cache_key` was the retired
Cerebras adapter's spelling and is not a parameter this host advertises. An
unadvertised parameter is a needless 400 risk for a field that would buy nothing
against automatic prefix caching, so it is computed and withheld rather than
guessed at. `tests/unit/test_provider_openrouter.py` asserts neither name
appears on the wire.

The key is kept in the normalized request anyway because it costs nothing there,
it states which calls belong to one growing prefix, and that is information no
one can recover after the fact — so the day a host advertises a spelling, the
answer is already computed. It is a salted digest, never the client's own session
id: the field is built to cross to a third party, and a client session id is
opaque local text we make no promises about. The salt is shared by every
instance, so the key is stable across a stateless fleet without any instance
storing it.

### 3b. Cache reporting — two shapes, both read

Nothing is sent to *ask* for prefix caching (§3a), so what a request carries says
nothing about what it got. The only signal is the count the host reports back,
and hosts document it in **two places**, saying which one appears depends on
the model:

| Shape | Where | Example |
|---|---|---|
| Nested | `usage.prompt_tokens_details.cached_tokens` | the OpenAI-standard spelling |
| Flat | `usage.cached_tokens` | top level, beside `prompt_tokens` |

`ProviderUsage.from_wire` reads the nested one first and falls back to the flat
one, and records which arrived as `cached_tokens_source` (`"nested"`, `"flat"`,
or `None`). Two properties are deliberate:

- **The flat field is a fallback, never a supplement.** A host emitting both
  would be reporting one number twice; summing them would inflate reuse on
  precisely the requests where the cache is working hardest.
- **`None` means "not reported", never zero.** Reading only the nested shape —
  which is what this adapter did until the fallback landed — turns a host that
  uses the flat spelling into one that appears to have no prefix cache at all.
  That is the failure worth naming: it does not look like a parser bug, it looks
  like a caching problem, and it invites work against the measurement instead of
  the wire.

Every token field goes through the same non-negative-integer coercion, for the
reason `cost` already did: `usage` is untyped JSON, and a count arriving as
`"1024"` or `true` used to land in an `int | None` field and raise in
`turn_accounting.cache_ratio`, mid-turn, on the paid path.

**Which shape this host actually uses is UNVERIFIED** — see §8. The parser
handles both so that the answer is a log query rather than an outage.

## 4. Privacy — UNVERIFIED against the current host

> ⚠️ **READ THIS BEFORE ANY LINE OF THIS SECTION.** Nothing here has been
> verified against either host's terms for the account tiers actually in use.
> **Do not repeat any of it, anywhere, as a statement about the current
> deployment.** That includes the summary in `README.md`.
>
> **There are THREE hosts to verify, and two of them can receive traffic
> today.** Baseten receives every turn. **OpenRouter receives every turn Baseten
> could not** (§1a) — the FULL request, not a summary: the assembled prompt, the
> runbook bodies, the tool schemas and the conversation. That is not a
> hypothetical path waiting on a flag the way Cerebras's is; it runs the moment
> the primary has an incident, which is precisely when nobody is thinking about
> data handling. Cerebras receives nothing while `FAST_RESPONSE_ENABLED` is
> False, and would again the moment it is not — so its terms still have to be
> read before that flag moves, not after.
> Historically:
> Cerebras receives an eligible turn's prompt content when the fast-response
> stage is enabled (§1). An earlier revision of this warning said Cerebras "is
> gone" and told a reader to DELETE its policy material — that instruction is
> withdrawn, and §4a is live rather than historical.
>
> Closing this means reading <https://www.baseten.co/privacy-policy/>,
> <https://www.baseten.co/terms-and-conditions/>,
> <https://openrouter.ai/privacy>, <https://openrouter.ai/terms>,
> <https://trust.cerebras.ai> and <https://cerebras.ai/privacy-policy>, and
> writing what each actually commits to for the tier we run on. Tracked in
> `TODO.md`.
>
> **OpenRouter is the hardest of the three to close**, and the reason is
> structural rather than a matter of reading one more page: it is a router, so
> its own terms are not the end of the question — the endpoint that actually
> serves a failed-over turn has terms too, and `sort: "latency"` means which one
> that is varies per request (measured: Modal, Makora, Together and Parasail all
> answered during one afternoon's testing). Under the retired routing policy this
> server sent `data_collection: "deny"` to bound exactly that. Nothing sends it
> today. Whether the fallback should carry it back is a decision that belongs
> with the reading, not ahead of it.
>
> **The executor host CHANGED, so this is not merely still-unverified — it is
> unverified about a different company.** Any reading previously done against
> Together AI's terms says nothing about Baseten's, and the model vendor behind
> the weights changed too (Z.ai rather than DeepSeek). Start over.

**What the promise WAS, and why its basis moved.** Under OpenRouter the
no-training floor was something this server *enforced* per request, by sending
`data_collection: "deny"` and filtering the eligible endpoint pool. A single host
has no pool, no filter, and no per-request data-handling parameter to send. So
what Daintree can say rests entirely on the serving host's own published policy
— which makes WHICH HOST a privacy fact, not just an operational one, and is
exactly why a host change invalidates this section rather than merely dating it.

> **The intended shape of the promise** (unverified for Baseten, see above):
> a no-training guarantee, deliberately NOT a no-retention guarantee.

---

### 4a. Cerebras — the fast-response host

**This host serves Daintree requests again** (the fast-response role, section 1),
so this section is live rather than historical. What has NOT changed is that the
position below is published-and-unverified in exactly the way section 4 describes
for Baseten: nothing here has been confirmed against the account tier actually in
use, and this code cannot confirm it. Verify both hosts before external testing.

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
that needs a human to verify the serving host's terms against Daintree's actual account
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
  CLI  ──(hop 1: CLI → backend)──►  backend  ──(hop 2: backend → Baseten)──►  model
        cli/internal/backend/retry.go          providers/retry.py
        ≤10 attempts / 2-minute window         MAX_UPSTREAM_RETRIES = 1
```

### Hop 2 — backend → Baseten (`MAX_UPSTREAM_RETRIES`, default 1)

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

⚠️ **The verdict exclusion is narrower than it looks, and one half of it has been
closed.** It applies only when the error arrives as an HTTP response — a verdict
delivered as a terminal SSE event is still retried, provided no visible content
has committed.

What no longer applies is the deterministic case. The CLI now keys those on the
code rather than the transport (`deterministicUpstreamCodes` in
`internal/backend/retry.go`), and refuses to replay six of them on either path:
`provider_invalid_api_key`, `provider_insufficient_credits`,
`provider_key_forbidden`, `upstream_no_compliant_provider`,
`upstream_request_rejected` and `upstream_protocol_error`. A revoked key or a
malformed parameter mix reaching the CLI as an SSE error is therefore surfaced
immediately rather than replayed through the full backoff to re-derive the same
refusal. `upstream_unavailable` and `upstream_timeout` are deliberately excluded
from that set — they are the genuinely transient half, and giving up on them
would trade one wrong behaviour for another.

A residual catch-all `upstream_error` delivered over SSE can still be retried,
which is the remaining argument for the taxonomy work in §6.

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
| `preparation` | validation, state decode, runbook selection — before the model is opened | absolute | 25 s |
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

## 5b. Runbook selection — the no-task fast path, and why shortlisting waits

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
selector benchmark corpus and fails on any case that declares a required runbook.

**Not shipped: candidate shortlisting.** Sending 6–10 lexically-retrieved
candidates instead of all 31 runbooks would cut the ~11k-token selector prompt
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

## 5c. Cost reporting — built for a number that may not arrive

> **Baseten does NOT report a billed cost — measured, not assumed.** `usage`
> carries no `cost` field on either the streaming or the non-streaming path, so
> `ProviderUsage.cost` is `None` on every turn and every `cost_usd` below is
> null in practice. This is the one capability lost in the move off Together,
> which did report it. It is not a regression to route around: the rules below
> were written so that "we do not know what this cost" is a first-class answer,
> and a client that implemented them correctly behaves correctly either way.
> Per-token pricing IS published in Baseten's `/models` listing (`prompt`,
> `completion`, `input_cache_read`), so a computed figure is possible — and
> deliberately not done, because that would be this server inventing a number the
> host declined to state. Historical note: confirming the old behaviour needed
> the live contract suite `TODO.md` carries.

Two failure modes are unacceptable and stay unacceptable: reporting a cost we did
not observe, and reporting **zero** for one we could not observe. The second is
the one that bites — a zero reads as "this turn was free", which is a claim about
someone's money with nothing behind it.

**Where it would come from.** `ProviderUsage.cost` is whatever the provider says
it billed, never a token-price estimate computed here. Anything we calculated
ourselves would be a guess presented as a bill, and would be wrong in the
direction that looks authoritative. When the provider sends no billed amount,
`cost` parses to `None` and every consumer treats that as "unknown" rather than
zero — see the two unacceptable failure modes above.

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

(Runbook selection is local and bills nothing; `cost.selector` is null on
production turns and populated only by the scripted test seam.)

`cost.total` sums every one it can observe. It no longer lands on the caller's
card — Daintree holds the credential and pays (see `docs/KEY_HANDLING.md`) — and
the figure is reported anyway, because a client that cannot see what a turn cost
cannot budget, warn, or explain a slow expensive round to its user. The breakdown
is kept because it answers the follow-up question ("why was that turn
expensive?").

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
  (This was common when speculative streaming existed. It is now limited to a
  discarded bare-intent re-roll and, on a turn that showed a preamble, a fast
  call whose usage never arrived — which is exactly why `fast_response` is its
  own cost component rather than being folded into `main`.)
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
being down. `providers/openrouter.py` maps upstream status codes onto the stable
codes in [`DAINTREE_API.md`](DAINTREE_API.md#errors); the CLI branches on
`error.code`, never on prose.

### Current mapping

The first column is what the provider said; the second is what a caller is told.
They differ on the first row and only there: an upstream 401 is about the key
THIS SERVER sent, so forwarding it would tell a caller to re-authenticate over a
credential they do not hold and cannot replace. 503 says the deployment is not
currently able to serve, which is the true statement. It is still not retried —
the code, not the status, is what `deterministicUpstreamCodes` keys on.

| Upstream | Daintree | Code | Retried? |
|---|---:|---|---|
| 401 | 503 | `provider_invalid_api_key` | no |
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
- **An in-band stream error may carry a symbolic code, not a numeric one.** Some
  hosts send `"wrong_api_key"` rather than `401`, so the streaming path matches
  numeric codes first and falls back to prose; without that a rejected credential
  would fall through to a generic retryable error and the CLI would replay a
  request that
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

`/readyz` validates the server's own secrets, the prompt files, the runbook catalog
and provider-object construction. `BASETEN_API_KEY` being **present** is part of
that (`missing_secrets`) — without it nothing can be answered at all, and
discovering that on the first turn surfaces as an upstream 401 that reads like the
caller's fault.

**`OPENROUTER_API_KEY` is deliberately NOT part of it**, and the asymmetry is the
whole difference between the two credentials. Without it every turn is still
answered; what is lost is the ability to survive a Baseten outage. A deployment
without it is degraded in its ROBUSTNESS, not in its function, and failing
readiness over a spare tyre would take the server down for a fault it does not
have. `main._build_provider` returns the bare Baseten adapter in that case —
exactly the behaviour this service had before §1a existed — and
`/v1/daintree/doctor` reports `provider.fallback.armed: false` so the absence is
visible rather than silent. But presence is not validity: `/readyz` **cannot** prove Baseten
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

Everything else here is either a host behaviour or a measurement, and is
recorded as a **dated observation** rather than a timeless truth. Specifically:

- what the host retains and for how long (§4) — their published policy, not
  something this repository can assert or enforce;
- which of the two documented cached-token shapes arrives — nested under
  `prompt_tokens_details` or flat at the top level. The parser reads both
  (`providers/base.py`) and records which one it saw, so this is unverified
  rather than unhandled;
- that the disable object actually suppresses reasoning (§2);
- whether the host reports a billed cost (§5c);
- every latency and token figure quoted above.

**No live suite pins any of it.** There used to be one — it checked which
parameters the upstream accepted, which it rejected, and that the serving model
reported zero reasoning tokens — but it was written against Cerebras and was
deleted with that adapter. So the facts most likely to break silently are now
remembered rather than checked, which is a real gap and not a small one;
`TODO.md` carries the replacement. The rest comes from this repository's
benchmark suites, which write their results to a gitignored directory, so those
numbers cannot be traced back to a committed run. Treat a quoted figure as "what
we saw on the date given", re-measure before relying on it, and prefer citing the
host's own documentation (with the date read) for provider semantics.

## 9. Changing the pinned model

A model change requires, before merge: the full deterministic suite, a
runbook-selection comparison, an injection comparison, a latency comparison, a
tool-call correctness comparison, a refreshed `benchmarks/baseline.json`, and an
explicit release note. Do not switch to a floating alias during internal testing.

**There is no live suite to run as part of that list** — it was deleted with the
Cerebras adapter and has no replacement (§8, `TODO.md`). Write it before the next
model change rather than as part of one: a live suite authored to make a
candidate pass is not the same artifact as one authored to describe the incumbent.

Two things beyond quality have to be re-decided when the model moves:

- **Thinking posture.** This is the item the last model change actually broke,
  so treat it as the worked example. The posture was never absent — it was
  actively held, by sending `reasoning: {"enabled": false}` to a model that
  reasons by default. The incoming model, GLM-5.3-Flash, could not honour that at
  all: reasoning is always on, `none` is rejected, and an omitted field means
  `high`, so the inherited posture silently became the most expensive one
  available rather than failing loudly. A replacement must be asked three
  questions, not one: can reasoning be turned off, what does OMITTING the field
  buy, and what is the field called (§2). Nothing checks any of that today.
- **Prompt budgets.** The per-file budget tests and `benchmarks/baseline.json`
  are sized against this model's context window and this model's behaviour under
  the current prompts.
