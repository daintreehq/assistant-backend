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
| **executor** | OpenAI, Responses API (`https://api.openai.com/v1/responses`) | `gpt-6-luna` | every turn, and every utility task |
| **executor fallback** | OpenRouter (`https://openrouter.ai/api/v1`) | `openai/gpt-6-luna` | only when OpenAI cannot answer (§1a) |
| **fast response** | Cerebras (`https://api.cerebras.ai/v1`) | `gpt-oss-120b` | **NEVER — the stage is OFF** (`config.FAST_RESPONSE_ENABLED`) |

The fallback is a second HOST for the same weights, not a second role in the
sense the other two are: it answers the identical request, at the identical
reasoning effort, and its existence changes nothing about an ordinary turn.

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
prompt content, and section 4 applies to it exactly as it applies to OpenAI —
**whenever the stage is on. It is currently OFF**, so today Cerebras receives
nothing at all and OpenAI (plus OpenRouter on a failed-over turn, §1a) is the
only host that sees prompt content. Do not
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
| Main completion | `gpt-6-luna` | `config.EXECUTOR_MODEL` |
| Utility tasks | `gpt-6-luna` | `config.EXECUTOR_MODEL` |
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
Previously the executor was `zai-org/GLM-5.3-Flash` on Baseten's Model APIs
(`providers/baseten.py` is deleted; git history has it), and
before that `deepseek-ai/DeepSeek-V4-Flash-0731` on Together — itself a pure HOST
decision, replayed over 62 real utility-task payloads at Together p50 1100ms /
p90 1702ms against OpenRouter's p50 3414ms / p90 6013ms. The move to OpenAI is
not like that one: different weights on a different host, with a different wire
API and a different thinking posture (§2), so every latency, cost and quality
figure recorded anywhere in this repo predating it describes a system that no
longer exists. Treat them as history, not as a baseline, and re-measure before
citing one.

For proportion only: OpenRouter's listing for OpenAI's endpoint prices
`gpt-6-luna` at $0.05/M input, $0.25/M output and $0.005/M cached input, against
$0.15/M, $0.50/M and $0.03/M cache read for GLM-5.3-Flash on Baseten. Those are
list prices read on 2026-09-23, not a bill (§5c); the model also now reasons on
every call (§2), which moves output-token volume the other way.

`config.EXECUTOR_MODEL` names the model and `config.EXECUTOR_ORIGIN`
(`api.openai.com`) the host the credential is sent to; `main._build_provider`
builds the adapter, readiness gates `OPENAI_API_KEY`, and `weak_secrets` pins the
origin in production.

The id is **hardcoded** — a product decision, not deployment config. There is no
environment override, and there must not be one: every prompt and benchmark in
this repository is calibrated against this exact model. Changing it is §9.

> **Runbook selection is not an upstream call.** It runs as a LOCAL in-process
> two-stage selector (`runbooks/local/`, no credential, no endpoint, ~10ms). The
> There is no other selector: the prompt-based one has been deleted, so
> "selector latency" and "selector cost" below describe a path that no longer
> exists on any route.

**Every credential is the server's.** `OPENAI_API_KEY` funds every turn,
`OPENROUTER_API_KEY` funds the ones OpenAI could not, and `CEREBRAS_API_KEY`
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

**Without it, an OpenAI outage is a Daintree outage.** One host holds the only
direct copy of the executor, so a 5xx, a refused connection or a socket that
went quiet becomes a failed turn as soon as `MAX_UPSTREAM_RETRIES` is spent —
and retrying a dead host harder is not robustness, it is the same failure taken
more slowly. The same `gpt-6-luna` weights are also served by Azure and Amazon
Bedrock behind OpenRouter (`openai/gpt-6-luna`), so the single point of failure
is an accident of how the host was picked rather than a property of the model.

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
| `ignore` | `["openai"]` | OpenRouter serves this model from OpenAI, Azure and Amazon Bedrock, and OpenAI's own endpoint is the one guaranteed not to help — this path runs *because* OpenAI just failed. The effective pool is Azure + Bedrock. |
| `allow_fallbacks` | `true` | A fallback that could not fall back would be a new single point of failure. |
| `require_parameters` | `true` | OpenRouter's default is to DROP an unsupported parameter, not to avoid the endpoint. A JSON task whose `response_format` went missing answers prose, burns its repair retry and 502s, and nothing in that chain names the cause. |

The thinking posture is §2's, in OpenRouter's canonical spelling
(`reasoning: {"effort": "medium"}`), sent unconditionally. A fallback that
reasoned differently would answer differently, and the point of this host is to
be a substitute. OpenRouter speaks Chat Completions but handles tools + reasoning
for this model itself, so the fallback does not need the Responses translation
the primary does (§3).

Because `require_parameters` filters out any endpoint that does not support a
parameter we send, the fallback must never send one the MODEL refuses — or the
pool filters to empty and the fallback fails exactly when it is needed. So
`temperature` and `stop` are dropped here just as the primary drops them.

One capability difference is worth knowing before reading a dashboard:
**OpenRouter reports `usage.cost` and OpenAI does not**, so a failed-over turn
is priced while an ordinary one is not (§5c).


## 2. Thinking posture

**The serving model THINKS, on every call, at one fixed effort: `medium`.** That
is a product decision, not a constraint of the host, and it is the opposite of
the two postures that preceded it.

`gpt-6-luna` accepts `none` | `low` | `medium` | `high` | `xhigh`, and `none` is a
genuine off switch (measured: zero reasoning tokens). So unlike the previous
executor, this one CAN be run without reasoning; the server chooses not to.
`config.EXECUTOR_REASONING_EFFORT` is `medium`, injected into the adapter at
construction (`main.py`), and `providers/openai.py` sends it as
`reasoning: {"effort": "medium"}` on **every call** — including the ones the code
marks `thinking_disabled` and the forced-`tool_choice` paths. One posture is the
decision; a caller-reachable second one would be a tier by another name.

| Surface | Thinking | How |
|---|---|---|
| Main completion | `medium` | `reasoning.effort` on the Responses API |
| Utility tasks | `medium` | same, plus `THINKING_HEADROOM_TOKENS` (4000) added to the profile cap |
| Forced tool calls | `medium` | not clamped — this model accepts tools + reasoning + a forced choice on the Responses API |
| Executor fallback | `medium` | `reasoning: {"effort": "medium"}` on OpenRouter (§1a) |
| Runbook selector | n/a | local classifier, no model call |

**Why the Responses API, and not Chat Completions.** OpenAI serves this model on
both, but on `/v1/chat/completions` it refuses function tools combined with any
`reasoning_effort` other than `none` — measured, verbatim: "Function tools with
reasoning_effort are not supported for gpt-6-luna in /v1/chat/completions. To use
function tools, use /v1/responses or set reasoning_effort to 'none'." Every
assistant turn carries tools, so on that endpoint the posture above would be
unreachable on exactly the turns it exists for. The top-level `reasoning_effort`
field is Chat Completions' spelling and is never sent: on the Responses API it is
an unknown parameter, and this host 400s on those (§3).

**Reasoning shares the output cap.** `max_output_tokens` bounds reasoning and
visible output together, so a utility task adds `thinking_headroom_tokens` to its
profile's cap (`services/task_runner.py`) to keep the chain of thought from
starving the answer into a `length` truncation. The small allowance that existed
for the previous executor (`EXECUTOR_REASONING_ALLOWANCE_TOKENS`) is deleted.

**No chain-of-thought reaches the client, and none is replayed.** The adapter
translates only visible text and tool calls out of the Responses stream, so no
`reasoning_content` delta is emitted. Calls are `store: false`, and the CLI
round-trips only content and tool calls, so each round reasons afresh from the
transcript; carrying `reasoning.encrypted_content` across rounds would restore
continuity and is a deliberate follow-up (`TODO.md`).

`INTERACTIVE_THINKING_ENABLED` is `True`, and `/doctor` and
`/v1/daintree/capabilities` publish `thinking_enabled: true`.

**None of this is pinned live.** There is no live contract suite (see §8), so the
refusals and acceptances above rest on measurements taken by hand rather than on
a test that would notice the host changing its mind. `TODO.md` carries the suite.

## 3. Wire shape — what a request carries, and what it must never carry

**OpenAI is a single host, not a router: one endpoint serves the model.** So
there is no endpoint-selection policy and no caller-facing `routing` block — a
request carries the model, the input, and the parameters the call actually
needs, and nothing about where it should go.

The adapter is `providers/openai.py` (`OpenAIProvider`, a subclass of the
OpenRouter adapter). Everything above the provider boundary still speaks the
chat-completions shape; the adapter translates at the wire, in both directions:

| Chat shape (above the boundary) | Responses wire |
|---|---|
| `messages` | `input` items |
| assistant `tool_calls` | one `function_call` item per call |
| `tool` message | `function_call_output`, keyed by the same `call_id` |
| `tools` | flat function tools, each sent with `strict: false` |
| `max_tokens` | `max_output_tokens` (floored at 16, the API minimum) |
| `response_format` | `text.format` |
| streamed `choices[0].delta` | rebuilt from `response.output_text.delta` / function-call events |
| `usage.prompt_tokens_details.cached_tokens` | rebuilt from `input_tokens_details.cached_tokens` |

**The host is STRICT, so the body is built, not patched.** Unknown fields answer
`400`, and this model also refuses `temperature` (other than the default) and
`stop`. So `_build_body` constructs the request from scratch and only the fields
it lists are ever sent: `model`, `input`, `reasoning`, `store: false`, and — when
the call has them — `tools`, `tool_choice`, `max_output_tokens`, `text`,
`prompt_cache_key` and `stream`. `temperature`, `stop` and `max_tokens` never
reach the wire. (`/v1/daintree/respond` still accepts `generation.temperature` /
`generation.stop` from a client; they are dropped here. Removing them from the
wire contract is a lockstep change with the CLI, `TODO.md`.)

**Function tools are sent `strict: false` explicitly.** Omitted, the Responses
API defaults a function tool to strict mode (measured: the echoed tool came back
`"strict": true` with `additionalProperties: false` injected), and strict mode
requires every property to be listed in `required` — which the CLI's schemas,
with their optional fields, are not.

**`store: false` on every call.** Nothing is kept for later retrieval through the
Responses API, and every round is sent whole. See §4 for what that does and does
not mean for retention.

The key probe is `GET /models` (OpenAI has no `/key` route); it answers
`{"object": "list", "data": [...]}` — a LIST — where OpenRouter's `/key` answers
a mapping, so the adapter overrides the decoder. OpenAI runs no public app
ranking, so the parent's attribution headers are not sent.

**The retired hosts' wire rules are not kept as switch-back notes.** Baseten's
`reasoning_effort` spelling and `reasoning_content` un-rename, and Cerebras's
`max_completion_tokens`, were each right for one host and wrong for this one; a
reader who half-remembered one would send a request this host rejects. Git
history has them.

### 3a. Cache affinity — `prompt_cache_key` is sent

OpenAI caches prompt prefixes **automatically**, matched on the literal prompt
prefix, and cached input is billed at a discount. It also documents a routing
hint, `prompt_cache_key`, that keeps requests sharing a prefix on the same cache.
So for the first time the affinity key this server has always computed has a
spelling the host advertises, and **the primary sends it**: every main
conversation call and utility task carries a `cache_session_id` on the
normalized `ProviderChatRequest`, and `OpenAIProvider._build_body` sends it as
`prompt_cache_key`.

| Call | Key | What it names |
|---|---|---|
| Main conversation | `daintree-main-<sha256(session id + salt)>` | that session's growing prefix |
| Main, JSON mode | `daintree-main-json-<sha256(session id + salt)>` | ditto, minus the tool schemas |
| Utility task | `daintree-task-<task id>` | that task's server-owned system prompt |

It is a salted digest, never the client's own session id: the field crosses to a
third party, and a client session id is opaque local text we make no promises
about. The salt is shared by every instance, so the key is stable across a
stateless fleet without any instance storing it.

**The fallback sends neither spelling.** The OpenRouter adapter drops the key
before the body is built — no `prompt_cache_key` and no `session_id` — because
its pool is several hosts with their own caches and a failed-over turn is not
where cache locality is won. `tests/unit/test_docs_drift.py` pins both halves.

The hint does not replace the real lever: prefix caching matches literal bytes,
so prefix stability (§ "Assembly order" in `CLAUDE.md`) is still what decides the
hit rate. (Reuse is observable in `cached_tokens`; its SCOPE on OpenAI — per key,
per organization — has not been verified here.)

### 3b. Cache reporting — two shapes, both read

Prefix caching is automatic and `prompt_cache_key` is only a routing hint
(§3a), so what a request carries says nothing about what it got. The only signal is the count the host reports back,
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

**On the primary the answer is fixed by the adapter**: OpenAI's Responses API
reports `usage.input_tokens_details.cached_tokens`, and `providers/openai.py`
rewrites it into the nested chat spelling before `ProviderUsage.from_wire` sees
it. Which shape the OpenRouter fallback's endpoints use is UNVERIFIED — see §8.
The parser handles both so that the answer is a log query rather than an outage.

## 4. Privacy — UNVERIFIED against the current host

> ⚠️ **READ THIS BEFORE ANY LINE OF THIS SECTION.** Nothing here has been
> verified against any host's terms for the account tiers actually in use.
> **Do not repeat any of it, anywhere, as a statement about the current
> deployment.** That includes the summary in `README.md`.
>
> **There are THREE hosts to verify, and two of them can receive traffic
> today.** OpenAI receives every turn. **OpenRouter receives every turn OpenAI
> could not** (§1a) — the FULL request, not a summary: the assembled prompt, the
> runbook bodies, the tool schemas and the conversation — and forwards it to
> whichever of Azure or Amazon Bedrock answers. That is not a hypothetical path
> waiting on a flag the way Cerebras's is; it runs the moment the primary has an
> incident, which is precisely when nobody is thinking about data handling.
> Cerebras receives nothing while `FAST_RESPONSE_ENABLED` is False, and would
> again the moment it is not — so its terms still have to be read before that
> flag moves, not after.
>
> **What OpenAI publishes** (read 2026-09-23, not yet checked against Daintree's
> own account and agreement): API inputs and outputs are **not used to train
> OpenAI's models by default**; they may be retained for **up to 30 days for
> abuse monitoring**; and `store: false` (which every call sends, §3) means a
> response is not stored for later retrieval through the API — it is **not** a
> retention opt-out. Authoritative sources, which a reader should check rather
> than take from here:
>
> - Enterprise privacy — <https://openai.com/enterprise-privacy/>
> - API data usage — <https://platform.openai.com/docs/guides/your-data>
>
> Closing this means confirming those terms for the account tier we run on, and
> doing the same for <https://openrouter.ai/privacy>, <https://openrouter.ai/terms>
> and the Azure and Amazon Bedrock endpoints behind it, and for
> <https://trust.cerebras.ai> and <https://cerebras.ai/privacy-policy>. Tracked in
> `TODO.md`.
>
> **OpenRouter is the hardest of the three to close**, and the reason is
> structural rather than a matter of reading one more page: it is a router, so
> its own terms are not the end of the question — the endpoint that actually
> serves a failed-over turn has terms too, and `sort: "latency"` means which one
> that is varies per request. Under the retired routing policy this server sent
> `data_collection: "deny"` to bound exactly that. Nothing sends it today.
> Whether the fallback should carry it back is a decision that belongs with the
> reading, not ahead of it.
>
> **The executor host CHANGED, so any earlier reading is void.** Notes taken
> against Baseten's or Together's terms say nothing about OpenAI's. Start over.

**What the promise WAS, and why its basis moved.** Under OpenRouter the
no-training floor was something this server *enforced* per request, by sending
`data_collection: "deny"` and filtering the eligible endpoint pool. A single host
has no pool, no filter, and no per-request data-handling parameter to send. So
what Daintree can say rests entirely on the serving host's own published policy
— which makes WHICH HOST a privacy fact, not just an operational one, and is
exactly why a host change invalidates this section rather than merely dating it.

> **The intended shape of the promise** (unverified for OpenAI, see above):
> a no-training guarantee, deliberately NOT a no-retention guarantee — and on
> OpenAI's published terms a retention claim would be false outright, given the
> 30-day abuse-monitoring window.

---

### 4a. Cerebras — the fast-response host

**This host serves Daintree requests again** (the fast-response role, section 1),
so this section is live rather than historical. What has NOT changed is that the
position below is published-and-unverified in exactly the way section 4 describes
for OpenAI: nothing here has been confirmed against the account tier actually in
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
  CLI  ──(hop 1: CLI → backend)──►  backend  ──(hop 2: backend → OpenAI)───►  model
        cli/internal/backend/retry.go          providers/retry.py
        ≤10 attempts / 2-minute window         MAX_UPSTREAM_RETRIES = 1
```

### Hop 2 — backend → OpenAI (`MAX_UPSTREAM_RETRIES`, default 1)

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

> **OpenAI does NOT report a billed cost.** `usage` carries token counts —
> input, cached input, output, reasoning — and no price, on either the streaming
> or the non-streaming path, so `ProviderUsage.cost` is `None` on every ordinary
> turn and every `cost_usd` below is null in practice. (Only a failed-over turn,
> answered through OpenRouter, is priced — §1a.) It is not a regression to route
> around: the rules below were written so that "we do not know what this cost"
> is a first-class answer, and a client that implemented them correctly behaves
> correctly either way. Per-token list prices are published (§1), so a computed
> figure is possible — and deliberately not done, because that would be this
> server inventing a number the host declined to state.

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
| 404 | 503 | `upstream_unavailable` | yes |
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
- **404 is a host fact, not a malformed body.** It maps to `upstream_unavailable`
  (see `providers/openrouter.py`): the body was not parsed to produce it, and both
  readings — a deployment that is down, or a fallback pool its own filters
  emptied — are about a host, so it is retryable and eligible for failover (§1a).
  The previous stack's marker-matching layer for routing dead-ends is gone, and
  so is the `upstream_no_compliant_provider` code it produced.
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
and provider-object construction. `OPENAI_API_KEY` being **present** is part of
that (`missing_secrets`) — without it nothing can be answered at all, and
discovering that on the first turn surfaces as an upstream 401 that reads like the
caller's fault.

**`OPENROUTER_API_KEY` is deliberately NOT part of it**, and the asymmetry is the
whole difference between the two credentials. Without it every turn is still
answered; what is lost is the ability to survive an OpenAI outage. A deployment
without it is degraded in its ROBUSTNESS, not in its function, and failing
readiness over a spare tyre would take the server down for a fault it does not
have. `main._build_provider` returns the bare OpenAI adapter in that case —
exactly the behaviour this service had before §1a existed — and
`/v1/daintree/doctor` reports `provider.fallback.armed: false` so the absence is
visible rather than silent. But presence is not validity: `/readyz` **cannot** prove OpenAI
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
- the host's refusals and acceptances in §2 and §3 — tools + reasoning refused
  on Chat Completions, `temperature`/`stop` refused, unknown fields 400ed, tools
  defaulting to strict on the Responses API;
- whether the host reports a billed cost (§5c);
- every latency and token figure quoted above.

**No live suite pins any of it.** There used to be one — it checked which
parameters the upstream accepted, which it rejected, and that the serving model
reported zero reasoning tokens — but it was written against Cerebras and was
deleted with that adapter; none was written for Baseten or for OpenAI. So the facts most likely to break silently are now
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

- **Thinking posture.** This is the item model changes actually break, so treat
  the history as the worked example. DeepSeek V4 Flash was held at zero with
  `reasoning: {"enabled": false}`; GLM-5.3-Flash could not honour that at all
  (reasoning always on, `none` rejected, an omitted field meaning `high`), so the
  inherited posture silently became the most expensive one available rather
  than failing loudly. The move to `gpt-6-luna` broke it a third way: the
  posture was reachable only on a different API (Chat Completions refuses tools
  with reasoning). A replacement must be asked four questions, not one: can
  reasoning be turned off, what does OMITTING the field buy, what is the field
  called, and does the endpoint accept it together with tools (§2). Nothing
  checks any of that today.
- **Wire strictness.** This host 400s on unknown fields and refuses parameters
  the previous ones accepted (§3). A replacement's rejection list has to be
  measured, not inherited.
- **Prompt budgets.** The per-file budget tests and `benchmarks/baseline.json`
  are sized against this model's context window and this model's behaviour under
  the current prompts.
