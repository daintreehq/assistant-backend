# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this service is

The Daintree Assistant Server moves the model-orchestration boundary off the local CLI and onto the server. **The server owns** the permanent system prompt, the skill catalog and bodies, automatic skill selection, prompt assembly, the small-model utility prompts, and — since the move off caller-supplied keys — **the upstream credential**. **The CLI owns** local state and local tool execution.

**The credential model inverted, and it is the fact most likely to be misremembered.** `TOGETHER_API_KEY` is the server's OWN key and it funds every turn. `auth.py` **ignores the `Authorization` header entirely** — not parsed, not validated, not rejected — and `Principal` carries no credential at all.

That is stronger than "a key is optional", and the difference is the whole point: a CLI mid-migration may still be sending its old OpenRouter key, and a door that *read* the header would thread that key to the provider and forward it upstream, where a credential the host never issued is rejected. The caller would see `provider_invalid_api_key` — their key blamed for a header they were right to still be sending. Refusing to look is what makes an old client work rather than fail confusingly. The same reasoning retires the old structural check: rejecting a malformed bearer only meant something while a bearer meant something.

It is a *stage*, not a destination — account authentication is next and `auth.py` is the seam (`TODO.md`). `ProviderChatRequest.api_key` stays live and tested as the field an account-mapped credential will occupy; benchmarks and live tests are its only callers today.

The public API is **Daintree-native** (`POST /v1/daintree/respond`), not OpenAI-compatible — the session/skill envelope is first-class and skills are their own response block.

The upstream is **Together AI** (OpenAI wire shape, spoken *internally* behind the `ChatProvider` abstraction in `providers/`): **one model, `deepseek-ai/DeepSeek-V4-Flash-0731`** (`config.SERVING_MODEL`), for the main completion and every utility task. The public response says `daintree-assistant`.

**A two-mode split briefly lived here** — a `pro` tier on Cerebras/`gemma-4-31b`, with a `modes.py` registry, per-mode credentials and per-turn route threading. It is gone: Cerebras's pricing structure could not be made to work for this product, so there was no second route to choose between, and a registry over one entry is a lie about the shape of the thing. Git history has the whole of it, and a genuine second tier lands there again rather than as a flag in `config.py`. Do not reintroduce mode plumbing speculatively.

**The host is the latency decision, not the model:** these are the weights `context_compact` reached through OpenRouter for months, and the same 62 utility-task payloads measured p50 1100ms on Together against 3414ms through OpenRouter's default routing. **Skill selection is LOCAL** (an in-process ONNX classifier, `skills/local/`) and makes no upstream call.

**Together is a model HOST rather than a router**, and almost everything that used to live in this paragraph followed from the opposite. One endpoint serves the model, so there is no endpoint-selection policy, no privacy filter to send, no fallback pool, no output-quality denylist, no sticky-affinity cohort, and no caller-facing `routing` preference — all of it was deleted rather than neutered, along with the `upstream_no_compliant_provider` taxonomy branch (`providers/policy.py` is gone; git history has it). Prefix caching is automatic, org-scoped and matched on the literal prompt prefix, so there is **no cache-affinity field on the wire either**: `cache_session_id` is still computed (it names which calls share a growing prefix, which nothing can recover after the fact) and `_build_body` drops it, under both vendors' spellings. `tests/unit/test_provider_openrouter.py::test_no_routing_or_affinity_field_is_ever_sent` asserts that, and literal prefix stability is therefore the only lever there is.

The Together adapter is the OpenRouter one with three class attributes moved (`providers/together.py`) — same body, same SSE, and the unified `reasoning: {"enabled": false}` object is honoured, measured. **There is no live contract suite for it** (`tests/live/` is empty): the one that existed pinned the CEREBRAS wire and went with that adapter. What Together accepts, what it 400s, and whether it reports `cost` are therefore unpinned against the real API — `TODO.md` carries it.

All model ids hardcoded. The full wire contract is `docs/DAINTREE_API.md`; code comments cite it as "section X.Y" — read it when changing wire shapes or task behavior.

`TODO.md` holds deliberately-deferred work, each entry gated on a trigger.

**The CLI source is vendored at `cli/`** — a git submodule tracking `github.com/daintreehq/assistant` (the Go CLI that talks to this server). It's checked in so the two repos can evolve together and so this repo can read the CLI's actual tool/function definitions as we move toward sourcing them from there; it is **not** part of the Python package, build, or test scope (ruff `extend-exclude`, pytest `testpaths`, and mypy/coverage are all pinned to `src`/`tests`). CI **does** check it out (`submodules: true`), because several gates read the CLI's own source as the authority — the typed-wrapper denylist, the credential path, the sign-in ordering — and without it each of those skips rather than runs. It still does not BUILD it, so the tool fixtures refresh by hand. After cloning, run `git submodule update --init cli` to populate it; it is otherwise pinned to a commit and does not update on its own. **It tracks `develop`, not `main`** (`branch = develop` in `.gitmodules`) — the CLI integrates there and `main` trails it, so `--remote` against the default would silently rewind. Because it does not float, the pin goes stale quietly and takes the tool fixtures with it: when a bump removes or renames a tool, `tests/unit/test_skill_catalog_hygiene.py` fails against skills still declaring the old name, which is the intended alarm. Refresh `benchmarks/_harness/fixtures/cli_tool_inventory.json` (recipe in its README) whenever you move the pin.

## Commands

```bash
# Setup
git submodule update --init cli          # populate the vendored CLI source (Go)
python3.12 -m venv .venv && source .venv/bin/activate
pip install -e ".[dev]"

# Test / lint / types (CI runs all three on push + PR to main)
pytest                                   # whole suite
pytest tests/unit/test_selector.py       # one file
pytest tests/unit/test_selector.py::test_name   # one test
pytest -k selector                       # by keyword
ruff check .
mypy --strict src tools    # tools/ holds the CI gate logic

# Prompt golden snapshots — REQUIRED after any intentional change to a prompt
# template or the assembler (see "Golden tests" below); review the diff before
# committing. Skill-catalog edits no longer churn these: the snapshots render
# against a pinned fixture catalog, not the live one.
DAINTREE_UPDATE_GOLDEN=1 pytest tests/golden/

# Run locally. `./dev` exports .env into the environment (so a curl in the same
# terminal agrees with the server), warns about missing secrets, and turns on
# console logs. Two required env vars, BOTH server-owned: STATE_SIGNING_KEY_CURRENT
# and TOGETHER_API_KEY — the server holds the upstream credential now, so a missing
# key fails /readyz rather than surfacing as a caller-looking 401 on the first turn.
cp .env.example .env   # then set both
./dev
uvicorn daintree_assistant_server.main:app --reload --port 8473
```

## Request flow (`/v1/daintree/respond`)

`ChatOrchestrator.complete` (`services/chat_orchestrator.py`) is the spine. It keeps the two sequential flows that genuinely are the orchestration — `_prepare_inner` and `_stream_body`, whose shared local state (the bare-intent hold, retry counters, accumulated usage) is what makes them long — and delegates the separable concerns to their own modules: `skills/active_set.py` (resolving the active set from a selector decision, including domain-foundation pairing), `services/turn_accounting.py` (usage, cost and the complete-vs-floor rules), `services/deadlines.py`, and `services/output_policy.py`. The steps:

1. **Verify state token** (`state/codec.py`) — a signed, opaque token carries the prior active-skill set across stateless instances. Invalid/expired/wrong-session tokens are ignored (not an error).
2. **Selector cadence** (`_should_run_selector`) — the selector does **not** run on ordinary tool-continuation rounds. It runs on `selection.force`, `policy=="always"`, an invalid token, a catalog-revision change, a new `turn_id`, or a changed `instruction_revision`. Otherwise the prior active set is reused.
3. **Skill selection** (`skills/local/`) — **LOCAL, not a model call**, and **two-stage**: a fine-tuned bi-encoder embeds the conversation and takes the `k` (32) nearest skills by cosine from a table of embedded skill documents; then a **set-wise head** reads the conversation embedding and those `k` cached skill embeddings *together* and returns a logit per candidate plus a docs logit and a 4-way cardinality head that makes "zero skills" a first-class answer (`setwise.py`). `LocalSelector` (`skills/local/selector.py`) is the only selector; conversation state enters both as a `[loaded: …]` query prefix and as a per-candidate `active` feature, and a skill not already active needs p≥0.65 to join a loaded set (adding mid-conversation is the expensive false positive). **The ballot is not a plain top-`k`** (`skills/local/candidates.py`): an active skill the cosine shortlist missed is seated anyway, displacing the weakest non-active candidate and carrying its true cosine at `rank_norm` 1.0. The head cannot keep a runbook it never sees, and a successful selection is authoritative enough to evict one — so at 500 skills a vague follow-up would otherwise drop a live workflow. It is inert on today's catalog (the `[loaded: …]` prefix keeps one active skill at rank ~0, measured) and the mechanism, the trainer and `eval_two_stage` all share the one assembler; what the head DOES with a reserved candidate is a weights question the current artifact answers badly, and `TODO.md` carries the owed retrain. **Neither stage owns per-skill weights** — the head consumes skill *embeddings*, not skill ids — and three things follow that are the whole reason for the design: a skill added to the catalog at runtime is embedded by `sync()` on load and is **reachable on the next turn with no retrain** — measured, that means it wins requests only it plausibly answers but still loses contested turns to trained neighbours (4/14 held-out benchmark cases against 13/14 trained; `training/EXPERIMENTS.md`), so a retrain is no longer what makes a skill *possible*, only what makes it *competitive*; there is consequently **no `routing_revision` staleness gate and no degrade-open window** (the stamp is still recorded in the artifact, as a drift signal for a human, and gates nothing); and catalog growth costs one 4.5KB table row rather than a wider head, so the head's job is the same size at 38 skills and at 1,000. The shortlist's ORDER is deliberately not information: the head has no positional encoding, so it is permutation-equivariant, and retrieval evidence reaches it as per-candidate features (cosine, normalised rank) instead — which is what stops a newly added skill being penalised for landing in a slot the head never trained on. Retraining is a **quality** step, not an unblocking one (`.claude/skills/retrain-selector`); the training pipeline, corpus, and full experiment log live in `training/` (READ `training/EXPERIMENTS.md` before re-trying any idea — the obvious retrieve-then-**cross-encoder**-rerank design was built, measured, and abandoned there, and this design is not it: nothing re-reads a skill's text at request time). **There is no other selector.** The prompt-based `Selector` — a billed model call on the turn's first-byte path — is deleted: it was slower and more expensive than the local one on every axis measured, and nothing in the product reached it. Its one remaining consumer was a test fixture, and that lives in `tests/fakes.py::ScriptedSelector` now. `SelectionDecision` outlived the class (`skills/decision.py`) because no consumer ever cared how the ids were arrived at.
4. **Resolve active set** (`resolve_active_skills`) — a **successful** selector run is authoritative: its ids become the complete active set (topic-shift replacement; an empty result clears it), capped at the limit. On an authoritative run each selected specific is **paired with its domain foundation** (`_admit_with_domain_foundations` + `Catalog.domain_foundation`, which walks UP the dotted id namespace nearest-first, so a top-level `daintree.foundation` backs every `daintree.*` skill) so a foundation loads on the SAME turn its specific is selected rather than a turn late — front-loading is deterministic, not left to the classifier. Admission is by ATOMIC UNIT (specific + its foundation), admitted in rank order only while the whole unit fits under the cap — deliberately *not* append-then-truncate, which was dead code at full capacity because the selector already caps its own output, so the appended foundation was always the first id discarded. A no-run continuation round or a degraded selector reuses the prior set unchanged, absent caller pins (no re-pairing — a prior set already carries its foundation, so continuation never surfaces a surprise load). A caller can also PIN ids (`selection.pinned_skill_ids`, for exercising a runbook under development): those are admitted FIRST and foundation-paired, with the selection budgeted against what they leave — the mirror of the server's own docs pin, which takes the leftover slot unpaired because it is an inference rather than an instruction. Pins ride alongside selection rather than replacing it, count against the cap, and every refusal is reported in `warnings` (`unknown_skill_id_ignored`, `pinned_skill_not_executable` for an id outside a sub-agent's narrowed menu, `pinned_skill_over_cap`) — a pin that silently no-ops is the one failure the feature cannot have. (`merge_active_skills` stays additive for the explicit `resolve` `load` path only.)
4b. **Documentation lookup** (`services/docs_mcp.py`) — the set-wise head's docs output answers a
   SECOND question beside `skill_ids`: should documentation be retrieved? That question is
   decided on a DIFFERENT string from the skill one — the cleaned last user message, bare,
   rather than the ranking query with its `[loaded: …]` prefix and history tail. The two are
   not interchangeable: all 207 docs rows in the corpus carry no history and no active set,
   so any context at all reads to the head as "not a docs turn" and drove recall to 5/120 on
   turns with a skill loaded (the full measurement, and the corpus work that would retire the
   workaround, are at the foot of `training/EXPERIMENTS.md`). `DOCS_THRESHOLD` sits at 0.40
   rather than 0.5 for the same reason — retrieval is the cheapest thing the classifier can
   ask for and the most expensive thing for it to miss. That same string is then the search
   query, and the orchestrator
   calls the public docs MCP (`https://daintree.org/api/mcp`) itself — one stateless
   JSON-RPC `tools/call`, no `initialize` handshake — inside its own slice of the
   `preparation` budget (minus a reserve, enforced as wall-clock), and folds the top sections
   into the **volatile tail**. Best-effort throughout: no client, no budget, a failed call or
   an empty result all leave the turn to answer without docs (`_lookup_docs` never raises).
   The search is surfaced to the CLI as a `docs__search` prelude execution — which
   the CLI currently DECODES AND DISCARDS (its own contracts call the prelude block
   vestigial), so nothing is shown to the user today — and
   the query rides the signed state token, scoped to the `turn_id`, so a usage question that
   takes a tool call to answer still has its sources on the continuation round. **The base
   base prompt is written for this** (`main/base/35-daintree-docs.md`) — it tells the model
   documentation arrives retrieved and to cite the `URL:` given, never to reach for a docs
   tool. Those rules live in the ALWAYS-PRESENT base prompt rather than a skill on purpose:
   a docs block can arrive on a turn where some other skill is loaded, and a skill only
   applies when it is selected. For a standalone documentation
   question the docs RUNBOOK skill is pinned server-side (`DOCS_SKILL_ID`) on the
   classification, alongside whatever the retrieved pages supply.
5. **Assemble** the upstream request (`services/prompt_assembler.py`).
   Required structured `startup` data becomes one stable framed user-role block before the
   visible conversation; typed `runtime.worktree`, retrieved documentation and other live
   state stay in the final volatile user-role tail.
5b. **Context budget** (`services/context_budget.py`) — the one limit that asks
   whether the WHOLE assembled prompt fits. Every other cap bounds a piece the
   client sent (8 MiB of body, 1 MiB of message text, 128 tools); none of them
   counts the base prompt, the ~21.5k tokens of tool schemas, the skill bodies,
   startup or the injected tail, so a request can satisfy all of them and still
   be several times too large. Measured LAST, after projection has settled what
   the tools array actually is. **It measures every turn and refuses almost
   none**: the estimate is characters/4 against a `SERVING_CONTEXT_TOKENS` that
   is unverified on the live host, so `exceeds_window` fires only when the input
   alone clears the window by `_REFUSAL_MARGIN` (1.25x) — everything short of that
   is left to the upstream, whose own refusal maps to the same code. Two
   measurement details are load-bearing rather than tidy: `ensure_ascii=False`
   (the default escapes a CJK character to six, so a Japanese session read 5.2x
   its size and would have been refused at a fifth of the window), and images
   charged as a flat per-image constant rather than by byte (a base64 payload is
   not text; counting it as text made the 6 MiB `cap_image_encoded_bytes` read as
   1.57M tokens, so every legal image was an automatic 413). The refusal is a
   typed 413 `context_window_exceeded` naming the dominant layer, and the SAME
   code is mapped from the host's own length rejection (`_CONTEXT_LENGTH_MARKERS`
   in `providers/openrouter.py`), which used to collapse into
   `upstream_request_rejected` and tell a caller with a long session to file a
   bug about the server. It does NOT shed context to make a turn fit; that is a
   producer of prompts rather than a check on one and belongs beside compaction
   (`TODO.md`). `turn_usage.estimated_prompt_tokens` logs the estimate beside the
   upstream's real `prompt_tokens` — the pair that calibrates the constant.

5c. **Per-layer attribution** (`PromptSegment` in `services/prompt_assembler.py`) —
   `build_main_request_with_segments` returns, beside the request, a digest and a byte
   count for every layer it built (`base`, `response_contract`, `skills`,
   `integrations`, `startup`, `conversation`, `tail`). The assembler HOLDS THE `tools` SLOT
   OPEN at the index the chat template puts it — after the system rows — and the
   orchestrator fills that slot in place from the POST-projection request, then logs the
   lot on `respond` as `prompt_segments`. Position is stated rather than recovered: an
   orchestrator counting segments whose ids looked system-ish put the tools row ahead of
   the persona on every sub-agent turn. Both profiles use the SAME layer names, so a
   manifest is never keyed by message index — a positional id renames every row below an
   optional one the moment it appears, and on a long sub-agent transcript would emit one
   row per message, up to `cap_messages` of them, in a single log record. The layout above is gated deterministically, but in
   production the assembled array was opaque — only `tools_*_bytes` was ever logged — so a
   cache ratio that dropped could be attributed to nothing, and "the cache seems worse" is
   not a diagnosis. Two consecutive turns in one session share a segment's digest or they
   do not, and the FIRST one that differs is the layer that paid. Content-free by
   construction: a 12-hex digest (matching `PromptLoader.content_hashes()`) and a byte
   count, both taken over the WIRE encoding of the row rather than its `content` string —
   compact separators, **no key sorting**, `ensure_ascii=False`. All three are
   load-bearing. Canonical sorting is the obvious thing to do to a digest and the wrong
   thing to do to this one: prefix caching matches literal bytes, so two tool schemas
   carrying the same keys in a different insertion order are a cache MISS that a sorted
   digest reports as identical — a false negative on the exact question the manifest
   answers (`benchmarks/_harness/wire.py` refuses sorting for the same reason). Measuring
   the row rather than its content is what makes `prompt_bytes` a real total instead of a
   sum of two units, and makes the `tools` entry agree with `tools_sent_bytes`.
   The same record now also carries `ContextMeasurement.log_fields()` on EVERY turn — it
   was computed every turn and read only on the refusal path, so the one request that
   never needed attributing was the only one that got it — and the loaded-skill render
   cache's `hits`/`misses`/`entries`/`chars`, which had been incremented since the class
   was written and read by nothing but a unit test. `build_main_request` stays as the
   narrow entry point for callers that want only the wire bytes (the golden suite, the
   cost benchmark); the manifest is diagnostics, never payload, and never reaches a
   client.

6. **Respond** — the skills/selection metadata is delivered first: as the response body's `skills` block (non-streaming) or the first SSE `meta` event (streaming), which local selection makes effectively immediate (~10ms in). The flow is strictly sequential now — select → assemble → **one** upstream call; the speculative-stream machinery that used to race a guessed prompt against the selector was deleted when selection stopped costing 2s (git history has it). Open/prefetch retries happen after metadata but before any model delta; an exhausted open or mid-stream failure emits a terminal `error` event, never a fabricated `done`.

6b. **Turn-finality declaration and compaction** (`services/response_declaration.py`,
   `services/compaction.py`) — when compaction is live the model opens its reply with
   one control line, `[[DAINTREE:FINAL]]` or `[[DAINTREE:TOOLS]]`
   (`main/base/15-response-declaration.md`), and the backend strips it before any byte
   reaches the client. **That rule is CONDITIONAL and today it is off**: the container
   builds the assembler with `response_declaration=compactor.available`, and every
   deployment holds `NullCompactor`, so the partial is omitted from the base prompt
   (646 bytes — the partial plus its join separator) and no reply is asked for a
   control line. Everything below describes
   the mechanism as it behaves once a producer exists. The rule is in the
   ASSISTANT base prompt only — a sub-agent is never asked for it — and carves out
   a bare-JSON reply, which a control line would invalidate. Position is the whole
   point: a marker at the end says nothing the stream terminating did not, while one at
   the front buys the entire remaining generation for a compaction call to run beside it.
   `DeclarationScanner` mirrors `bare_intent.py`'s monotonic-probe shape and sits AHEAD
   of the commit gate rather than duplicating it, so `content_parts` — and with it the
   runaway guard, the bare-intent predicate and the output lint — sees exactly what the
   user sees. It is prefix-only by design: a marker quoted mid-reply is left alone.
   On FINAL the orchestrator starts compaction as a `_background_tasks` task, but
   DELIVERY is gated at the terminal event on what the reply actually did (a clean
   `stop`, no tool calls) — the model contradicting itself is expected, and a discarded
   block costs one call while a wrongly delivered one freezes history mid-task, which
   append-only cannot undo. The block rides a `compaction` SSE event emitted immediately
   BEFORE `done`, never after: `done` stays terminal because readers modelled on the
   `data: [DONE]` convention break their loop on one. Nothing reaches the state token —
   the span start is re-derived each turn from the reserved `daintree_compaction` message
   name the client splices back in, so a frozen segment is never re-compacted without any
   server-side state. **The PRODUCER does not exist yet** (issue #60): `Compactor` is a
   Protocol and every deployment holds `NullCompactor` — which is now what SWITCHES THE
   RULE OFF rather than merely idling behind it. It used to ship regardless, paying its
   bytes and a marker token on every turn to buy the `response_declaration` compliance
   log; that trade stopped making sense once the serving model was measured emitting the
   marker on 0 of 20 tool-calling turns, so the telemetry only reported that an unread
   contract was also unhonoured. The scanner and stripper stay wired — they cost a
   prefix probe, and a model that emits the marker unprompted should still not show it
   to a user. Compaction is streaming-only; the non-streaming path strips and stops,
   having no generation left to overlap.

**The transcript is validated as a state machine, not a membership test**
(`services/validation.py`). An assistant turn opens a BATCH of tool calls, every
call is answered exactly once, and only then may a user/assistant message follow;
order *within* a batch is free, because the CLI dispatches a parallel group
concurrently. That strictness is load-bearing rather than tidy:
`tool_projection._unresolved_tool_calls` keys `{call_id: tool_name}`, so a
transcript reusing an id while the first call is still open silently OVERWRITES
the first entry — and under enforcement the projection then withholds the tool a
pending call needs. Global id uniqueness is deliberately NOT required: ids come
from the MODEL, and one reused in a later turn (every call answered before the
next began) is a coherent conversation a global `seen` set would 400. A TRAILING
unresolved batch stays legal — it is exactly the input `_unresolved_tool_calls`
was written for, and the rule now guarantees it can only ever be the last one.
Two smaller rules ride along, both because ids and roles reach the wire RAW:
only an `assistant` message may carry `tool_calls` (the schema types `role` as a
bare string so `system`/`developer` get a precise rejection, and the assembler
forwards the field for every non-tool role), and an id surrounded by whitespace
is REJECTED rather than trimmed — pairing on a stripped value would validate
` c1 ` against `c1` here and hand the provider a call nothing answers.
`tests/unit/test_tool_projection.py` pins the property this module depends on
(every distinct open call keeps its tool) plus the validator rule that
guarantees it.

**Shape caps bound counts, not bytes** (`cap_messages`, `cap_content_parts`,
`cap_tool_calls_per_message`, `cap_stop_sequences`, `cap_pinned_skill_ids`,
`cap_images`). The 8 MiB body ceiling does not cover object count: a request of
100,000 two-byte messages is small on the wire and expensive everywhere after it.
Every default is far above real traffic on purpose — crossing one means malformed,
not busy. Image data URLs get a real policy too (media-type allowlist, `;base64`
required, base64 validated, encoded size capped so nothing oversized is decoded);
until now any `data:` URL was accepted, including `image/svg+xml` and `text/html`.

**Invariants to preserve** (also in README): client `system`/`developer` messages are rejected; `skill.find`/`skill.load` are never offered to the main model; only active skill *bodies* are injected (never the full catalog); selector failure fails open; state is portable via the signed token; required structured `startup` context reaches the main model only — the selector's evidence is sanitized `input.messages` plus bounded turn/workflow hints and tool names, and it never sees `startup`.

## Skills — the main area of ongoing work

Skills are Markdown runbooks that become task-specific operating instructions for the main model.

**Where they live:** `src/daintree_assistant_server/skills/files/<id>.md` — one file per skill, **filename stem must equal the `id`** in the frontmatter (enforced at load).

**File format** — YAML frontmatter + Markdown body. The body is operational runbook prose; match the house style of the existing files (when-to-use line, numbered procedure, "Report back" line). Frontmatter is validated by `SkillMeta` in `skills/models.py` (`extra="forbid"` — unknown keys are rejected):

| key | required | notes |
|---|---|---|
| `id` | yes | must match filename stem |
| `title` | yes | |
| `summary` | yes | selector signal |
| `whenToUse` | yes | the *primary* selector signal (camelCase key → `when_to_use`) |
| `foundation` | no | `true` marks a domain **foundation** skill; the selector pairs it with the specific workflow skill(s) it picks (default `false`) |
| `risk` | no | enum: `read local ui terminal project external git system` — **descriptive, not policy** (see below) |
| `requiredTools` | no | camelCase key → `required_tools` |

`risk` enforces nothing. It gates no code path, is not shown to the selector, and never crosses the wire; real risk gating is **per-tool**, in the CLI's confirmation matrix. It is kept as documentation — a reader skimming a runbook can see at a glance what tier of mutation it authorizes. (It used to have a second consumer: the skill-learning builder wrote it. That feature is gone.) There is no `tags` key and no `priority` key — both were deleted once `selector_view()` narrowed to `{id, summary, when_to_use, foundation}` and left them with no consumer.

**Pipeline:** `loader.py` parses/validates (strict — a malformed skill fails `/readyz`, not a request) → `catalog.py` builds the **immutable** catalog and a deterministic `revision` hash → the selector sees only `Skill.selector_view()`, the public `/skills` endpoint sees only `public_view()`, and **neither ever exposes the body** except via injection into the main prompt for active skills. Selection is automatic and server-side: the guarantee that `skill.find`/`skill.load` are never *offered* is enforced at the request boundary by the reserved-tool-names check in `services/validation.py` (`RESERVED_TOOL_NAMES`, which also covers the `skill__find`/`skill__load` wire spellings and the `daintree_internal__` prefix), and the base prompt separately tells the model it does not find, load, or manage skills itself (`main/base/30-skills.md` — it never names the tools).

**To add a skill:**
1. Create `skills/files/<id>.md` (it is auto-discovered, sorted by id — no registration).
2. Run `pytest tests/unit` (~5s, no API calls). A new skill trips more than the loader/catalog tests: `test_prompt_budgets.py` (body size ceilings, no pipe tables, a Report-back line), `test_skill_catalog_hygiene.py` (every `requiredTools` name exists in the CLI tool-registry snapshot at `tests/fixtures/cli_tool_names.txt` — its header has the regeneration recipe against the pinned `cli/` submodule), and `test_prompt_budgets.py`'s house-style sweep over the packaged catalog. Do **not** try to narrow this with `pytest -k`: the obvious concern-named filter (`-k "catalog or budget or house"`) matches no loader test name or module and silently deselects all 14 of them. Run the directory, or list the files without a filter.
3. **Run the golden tests** (`pytest tests/golden/`). The prompt snapshots render against a **pinned fixture catalog** (`tests/golden/fixtures/skills/`), not the live one, so adding or changing a real skill no longer churns `selector_system.md` / `loaded_skills_*` — only editing a prompt template or the assembler does. A structural smoke test (`test_selector_prompt_renders_real_catalog`) still verifies the real catalog renders through the selector prompt.
4. **Check routing coverage** (`python -m benchmarks.skill_selection.bench coverage`) — a per-skill case-coverage matrix, offline and free (no API calls). It exits non-zero when a skill has no benchmark case that requires it, so a new skill does not land unrouted and untested. **Write carve-outs by fully-qualified ID** — "do not use for X, use `daintree.other.skill`" — never as a bare leaf (`use competition`) or as prose (`use the competition skill`). Coverage parses declared neighbours out of the `whenToUse` text, so an unqualified carve-out reads to it as a skill claiming no confusion at all; `tests/unit/test_skill_catalog_hygiene.py` holds the count at zero and also fails a dotted reference that resolves to no skill, namespace or tool. `bench lint` ranks every skill pair by how alike the retriever finds them and flags the ones with no id-level edge — which is how the 27 unqualified carve-outs were found: they were the catalog's most confusable pairs, invisible to its own tooling. Rewriting them left every one of the 177 benchmark cases returning a byte-identical answer. Note coverage gates per SKILL, not per edge: naming a second neighbour makes the claim readable, not tested.

## Prompts / system messages

All prompt text the SERVER owns lives in `src/daintree_assistant_server/prompts/` (skill bodies are prompt text too, and live in `skills/files/`; tool descriptions are the CLI's) (`.md` = raw bodies, `.md.j2` = Jinja templates), loaded via `prompts/loader.py`:

- `main/base/NN-*.md` — the permanent assistant system prompt (persona + hard rules). It is a **directory of ordered partials**, not a single file: `PromptLoader.base_prompt()` globs `main/base/*.md`, sorts by filename, and joins them with blank lines, so the `NN-` prefix is load-bearing ordering (`00-persona`, `10-hard-rules`, `15-response-declaration` — omitted unless compaction is live, see 6b —, `20-tool-discipline`, `30-skills`, `35-daintree-docs`, `40-communication`, `50-formatting`). Edit the partial that owns the rule; a new section is a new numbered file. **There is no `main/base.md`** — writing one creates a stray file the loader ignores. This directory is the highest-leverage prompt text in the repo.
- `main/{startup_context,runtime_context,integrations,loaded_skills,response_contract,turn_footer,docs_context}.md.j2` — the structured main-prompt layers. `response_contract.md.j2` is order-critical: the golden suite asserts it renders as the **final system message** — the second and last one, directly after the base prompt and so immediately ahead of the tool schemas. The skill, integration and startup layers are **user-role rows downstream of those schemas**, not system messages.
- `selector/system.md.j2` — the skill-selector classifier prompt.
- `tasks/<task>.md` — utility-task system prompts.

### One rule, one owner — the target, and where it is actually true

Two copies of a rule drift until they disagree, and the model then has to decide
which layer it believes — a decision it makes differently from turn to turn, so the
failure reads as flakiness rather than as a prompt bug. The target is one home per
rule. **Read the second table before acting on the first: the goal is not reached,
and the gap is not a cleanup task to knock off casually.**

| The rule is about… | Intended owner |
|---|---|
| Safety, authority, truthfulness on EVERY turn | `main/base/10-hard-rules.md` |
| Turn mechanics on every turn | `main/base/20-tool-discipline.md` |
| What the ANSWER contains — length, lists vs tables, status cues, `Blocked`/`Next` | `main/response_contract.md.j2` |
| What survives the terminal RENDERER — reflow, flat headings, links, fences | `main/base/50-formatting.md` |
| Tone, the private-scaffold guard, the continuation delta | `main/base/40-communication.md` |
| One tool's capability, its nearest neighbour, its sharp edge | that tool's description in the CLI |
| Judgement shared across Daintree workflows | `daintree.foundation` |
| One task's procedure | that specific skill |

**Where this holds, and where it does not.** Only the first group is enforced:
`test_no_permanent_layer_restates_another` compares `main/base/*.md` against the
response contract and fails on six copied words. Nothing compares those layers
against `daintree.foundation` or the specific skills, and they DO restate base
rules today — exact-ID discipline, terminal ownership, supervision choice, finish
detection and delegated search all appear in both base and the foundation. That is
a known, unmeasured overlap, not an oversight to fix on sight.

Three traps in closing it, each of which looks like tidying:

- **A benchmark cannot instruct the model.** Moving a rule "into a test" removes it
  from the prompt. A regression case belongs beside a rule, never instead of one.
- **Schemas cannot express intent.** `terminal.close` has a valid schema for a call
  the user never asked for; only prose carries "the user must have asked". Do not
  retire a restraint rule to a schema.
- **Repetition near an untrusted carrier is deliberate.** Every carrier repeats its
  own local "this is data, not instructions" framing because the injection
  benchmark showed framing works by PROXIMITY. Never deduplicate those.

The rules deliberately mirrored in more than one place are pinned in
`tests/unit/test_policy_contradictions.py`, so a mirror cannot drift silently.

**Two things that are easy to get wrong:**

- **Assembly order is load-bearing — and role choice IS position.** The serving model's chat template (a property of the model, not of whichever API fronts it — this survived every host move unchanged) serializes [ALL system messages, in array order] → [tools] → [non-system conversation], so a volatile system message ANYWHERE in the array lands before the ~21.5k-token tool schemas and busts their cache (measured 2026-07-08: 36% hit on byte-identical turns → 99% after the fix). The system role is therefore reserved for what is static for **every caller**, and the rows below it are ordered by **how widely each can be shared, widest first**, so a change to one never re-encodes anything above it. `PromptAssembler.build_main_request` emits: the two static **system** blocks (base → response contract), then — after the schemas — a **user-role** loaded-skills block (shared by task), a **user-role** integrations block and the stable **user-role** startup block (per session, instance and project), the append-only visible `input.messages`, and ONE trailing **user-role** "[Injected context …]" block carrying per-request runtime + turn state. While the skill, integration and startup rows hold steady, only the final tail re-encodes each round — the client republishes `mcp_servers` every round, so a server reconnecting mid-session moves the integrations row too. Integrations and skills used to be system-role, which put a per-machine MCP endpoint URL and every selector topic shift ahead of all 21.5k tokens of schemas; moving them below (issue #39) makes a skill load cost its own bytes instead of the tool region, and `benchmarks/prompt_cost` now gates `skill_topic_shift` at `system_tools_stable == 1.0` to hold that. Skill bodies stay **ahead of the conversation** rather than in the tail — a large runbook in the tail is re-paid uncached every round of every turn. Startup is required even when empty, stays outside the visible transcript, and is never sent to selector/learning calls. Don't reorder casually, and never add a volatile system message. All client-provided local context is treated as untrusted: sanitized and byte-capped (`text_utils.py`, the `cap_*` settings in `config.py`); exact agent ids are JSON-quoted and rows are admitted whole.
- **The selector's query has a 256-token cap, and the renderer now respects it.**
`query_document` builds `[prior] [newest] [turn_context]`; the encoder truncates
at 256 tokens from the RIGHT, so the tail — the turn context, and with dense
enough history the newest instruction itself — was being deleted with no error
anywhere, because a truncated query still scores. `documents.fit_query` shrinks
deliberately instead: prior history first, then head-and-tail on the newest
message, never the turn context. A query that already fits is returned
byte-identical, which is why this needed no retrain — the training corpus peaks
at 155 tokens and the benchmark at 130, so neither ever reaches the shrink path.
Serving and `training/common.py::build_query` share the function.
`tests/unit/test_selector_query_truncation.py` pins it, and no character
heuristic can replace it: 320 characters of one unbroken word is 3 tokens, the
same 320 of path-like output is 320.

**Prompt bytes are behavior.** On a meaningful prompt change, regenerate golden snapshots (below) — that diff is the reviewable record. Do **not** bump version strings: `MAIN_PROMPT_VERSION` is a fixed family label (`"main"`), not a per-edit version (see "Versioning" below).

## Versioning (pre-release)

This service is **pre-release** — it deploys to Cloud Run on every push to `main`, but its
only client is the CLI in this repo's submodule, moving in lockstep, so there is no
*external* version to stay compatible with. **This is not a versioned system.** Nothing embedded in it — prompts,
skills, task identifiers, prompt-family labels — carries a version; everything on disk is
simply "the current version." **Do not add version numbers, SemVer, or date stamps to any
of it, and do not bump anything on a change.** Edit freely and let the golden-snapshot diff
be the record. The one `v1` Daintree itself owns is the API URL path prefix
(`/v1/daintree/...`), kept solely so the URL can change shape in the future — it is not a
claim that anything behind it is versioned.

**The whole system — this backend AND the CLI — is a pre-release work in progress. Never
engineer for backward compatibility.** There are no external users: the only client is the
vendored CLI, and both repos move together. Concretely:

- **Do not create parallel versioned API surfaces to protect old clients.** A protocol
  change edits the existing endpoints in place; the CLI is updated in lockstep.
- **No compatibility shims, deprecation cycles, or migration paths.** Wire contracts,
  storage schemas, event shapes, and prompts can all be broken freely.
- **Throwing code away is fine.** If a design is wrong, delete and replace it — don't
  preserve, wrap, or route around it for continuity's sake.

What this means concretely:

- **Skills** have **no `version` field** — frontmatter carries `id`/`title`/`summary`/etc.
  only. Change busting rides on the skill content hash (which feeds `catalog_revision`), not
  a hand-bumped number.
- **The prompt-family label** (`MAIN_PROMPT_VERSION` in `prompts/loader.py`) is a fixed
  name — `"main"` — surfaced as `prompt_version`. It identifies *which* prompt, not a
  revision; leave it alone.
- **Task identifiers are unversioned** — `terminal_summarize`, not `terminal_summarize.v1`,
  with matching `tasks/<task>.md` prompt files. A task id names *which* task, never a
  revision of it; a task's behavior changes by editing its prompt/profile in place (CLI
  updated in lockstep).
- The **only `v1` that stays** is the API URL path (`/v1/daintree/...`) — a URL-shape
  escape hatch for the future, not a version claim over anything behind it.
- Separately, **build/protocol versioning is not in scope here and stays as-is**: the
  `/version` endpoint, `server_version`, `protocol_version` / `X-Daintree-Protocol`, and the
  signed-state-token format version (`TOKEN_VERSION`) are real wire/build metadata, unrelated
  to prompt churn.

If real release versioning is ever wanted, that is a deliberate, separate decision — not
something to reintroduce incidentally.

## CI gates

CI runs install-from-lock, ruff, `mypy --strict src tools`, pytest with coverage,
the coverage floors, and the deterministic benchmark dashboard. Three of those are
newer and easy to misread:

**Coverage is two gates, not one.** `fail_under = 90` in `pyproject.toml` is the
global floor (measured 93% at introduction). `tools/coverage_floors.py` carries
PER-MODULE floors for the security and reliability surfaces — auth, error mapping,
the state token, retries, provider parsing, validation, deadlines, redaction —
because a global percentage stays green while exactly that code regresses: a
thousand covered lines elsewhere absorb the loss. Each floor is the measurement
when it was added, rounded down, so it can only ratchet up. Lowering one is a
deliberate act that needs a reason in the commit message.

**`benchmarks/baseline.json` records what the server actually sends** — base and
selector prompt bytes, catalog and tool counts, the pinned model and privacy
posture — with declared movement bands (`tools/baseline.py`), gated through
`tests/unit/test_baseline.py`. It catches what the per-file budget tests cannot:
those assert CEILINGS, which a slow 4%-a-month growth sits under for a year. This
asserts MOVEMENT. Refresh it deliberately with
`python -m tools.baseline capture > benchmarks/baseline.json` and explain the move.

**The two CLI fixtures are one capture.** `tests/fixtures/cli_tool_names.txt` must
be exactly the name projection of `benchmarks/_harness/fixtures/cli_tool_inventory.json`;
`test_skill_catalog_hygiene.py` fails if they disagree. Refreshing one and
forgetting the other is the obvious mistake, and nothing else would notice — both
files still parse, and the damage only surfaces as a skill declaring a tool the CLI
dropped. CI checks the submodule out but does not BUILD it — regenerating needs a
Go toolchain — so this offline equality check remains the available gate.

**Routing quality IS gated here, and for free** — `benchmarks/skill_selection`
`bench local` drives the SHIPPED ONNX artifact through the production decision
procedure and runs in the deterministic dashboard on every push. It grades
recall-pass (the local selector buys recall with precision on purpose, so the
paid suite's exact-set contract is the wrong bar for it) and, more usefully,
attributes every missing skill to the STAGE that lost it: `candidate_miss` /
`rank_miss` / `floor_reject` / `cardinality_short` / `cap_full`. `candidate_miss`
is gated at zero — no floor and no better head can recover a skill that never
reached the ballot, and it is the first number to move as the catalog grows.
There is no paid comparator any more: `bench run` went with the prompt selector
it drove, and `bench local` is the only routing verdict.

**`bench scale` answers the question the live catalog is too small to ask.** It
pads the catalog with derived distractors (paraphrases, scope splits, siblings,
exclusion-clause collisions) and re-measures oracle recall at 100/200/500/1,000
skills; the numbers are in `training/EXPERIMENTS.md`. `k=32` takes its first
candidate miss at 500 skills (2 of 154 target-bearing cases) and `k=48` is clean
there, so the width is a `TODO.md` entry with a trigger rather than a standing
worry. Read that as a re-measurement trigger and not a capacity limit: it is one
synthetic pool and 154 cases. The same harness also says candidate reservation
has **not** rescued anything measurable — dense retrieval alone keeps 13 of 13
active-and-required skills at every size out to 1,000 — so it is insurance
against a failure no floor could repair, not a fix for an observed loss. CI runs
a 200-skill smoke (~24s).

Not gated here, deliberately: anything needing a paid call. Live latency,
injection results and the prompt-selector comparison belong in a nightly run
against a low-spend key — a baseline that cannot be checked offline either gets
skipped or makes every push cost money.

## Skill-aware tool projection

Tool schemas are the largest region of a request (~65% of the deterministic
fixture), and most of them are irrelevant to what a turn is doing.
`skills/tool_projection.py` narrows them to the active skills' declared
`requiredTools`, plus their domain foundations', plus a core, plus anything a
pinned `tool_choice` or an unresolved tool call needs. Against the pinned 79.3 KiB inventory: 20.8 KiB on a skill-less turn (74% saved),
46.8 KiB with a workflow skill and its foundation loaded (41%). Issue #64's ~56%
median sits between the two — the saving is a function of how much a turn loaded,
so a single number was always going to be a blend.

**`TOOL_PROJECTION_MODE` is `disabled` | `shadow` | `enforced`, and it defaults to
`shadow`** — every deployment measures, none enforces until somebody says so.
`TOOL_PROJECTION_ROLLOUT_PERCENT` buckets on the SESSION (never the request: a
percentage re-rolled per request would offer a continuation round a different
inventory than the round that started the work). `TODO.md` carries the staged
rollout, its promotion gates and its rollback triggers.

**Three things about the design are load-bearing and easy to undo.**

**The core is DERIVED, not chosen.** `ALWAYS_AVAILABLE` holds every tool the
ALWAYS-PRESENT base prompt instructs, because withholding one is a contradiction
the model cannot resolve — no skill is loaded to explain the absence, and on a
skill-less turn there is no metadata in play at all. `10-hard-rules.md` says file
changes go through `agentTask.spawnForEdits`; `20-tool-discipline.md` says every
oversized result is paged through `artifact.read` and every expensive search is
delegated with `subagent.run`. A projection that dropped those would leave a turn
commanded to do something it has no legal move for.
`tests/unit/test_prompt_hygiene_tools.py` re-derives the set from the prompt text
and fails on drift — so the fix for a new instruction is to add the tool, or to
stop naming it in the permanent prompt. The same applies to
`SKILL_SCAFFOLD_TOOLS`: `loaded_skills.md.j2` instructs `skill.step.advance` and
`skill.run.get` and no skill declares them, so no `requiredTools` audit could ever
see that gap. **This is the single biggest hazard in the whole feature** — the
core looks like an optimisation to be trimmed and is actually a correctness
constraint.

**A tool no skill declares is never withheld.** Withholding argues from metadata,
and there is none about a tool the catalog has never heard of — a CLI release that
ships a tool ahead of the skill using it, an integration contributing its own.
Silence is not evidence a turn does not need something, and the failure would be
invisible until a user noticed their own tool had stopped working.
`Catalog.declared_tools` is that universe.

**Withholding is FINAL for the turn.** The CLI reports an unoffered tool as
`callable: false` — "calling them would do nothing" — and dynamic invocation of a
withheld tool does not exist (CLI #368). `tool.search` discovers names; it does
not recover capability. So every gate fails OPEN to the full inventory, and
`EnforcementDecision.fail_open` is deliberately narrower than `not enforce`:
shadow mode, an unselected cohort and a sub-agent turn all decline without
anything having gone wrong, and counting them would bury the reasons a rollback
triggers on.

Enforcement is a post-assembly `replace(provider_request, tools=...)` in
`_prepare_inner`, never a parameter on `build_main_request` — the full request
already exists as the fallback, the names matched are the normalized ones actually
going upstream, and the assembler keeps its one job. Everything downstream
(retries, the bare-intent re-roll, both response paths) reads the same prepared
request, so the filter is applied exactly once.

**It costs prompt cache on a topic shift, and that is measured rather than
assumed.** Under enforcement the tools array is a function of the ACTIVE SET, so a
skill topic shift changes the schemas — which the `skill_topic_shift` transition
in `benchmarks/prompt_cost` cannot see, because it calls the assembler directly
and never reaches the orchestrator. `projected_tool_continuation` (gated at 1.0)
and `projected_skill_topic_shift` (gated on the SYSTEM messages, which must not
move) are the enforced twins. The trade: a one-time miss on a region already ~42%
smaller, against a saving taken on every round in between — and continuation
rounds measure byte-identical, which is where most rounds are.

Sub-agents are held out of the first rollout: their menu is already narrowed and
few skills survive `executable_with`, so most of their turns are skill-less —
exactly the shape where a core tuned for the ASSISTANT base prompt is the wrong
core.

## Deploying (Cloud Run)

Nothing is ever built or pushed from a dev machine. **Pushing to `main` is the deploy** —
work lands on `develop`, and a release fast-forwards `main` (Cloud Build trigger pattern is
`^main$`; `develop` runs CI but never deploys). `cloudbuild.yaml` builds → pushes → deploys
a **no-traffic** revision tagged `candidate` → smokes `/readyz` and asserts `/version`
`build_sha` equals the commit → promotes to 100%, so a bad revision fails the build and
never takes traffic. Project `daintree-research`, region `us-central1`, service
`daintree-assistant`.

Use the **`/release` skill** (`.claude/skills/release/`) rather than doing it by hand: it
gates on green GitHub CI, fast-forwards and pushes `main`, then watches the build and
confirms live `/version` reports the released commit. `infra/gcp/smoke.sh` checks a
deployment afterwards (free by default; `--full` sends two real turns that cost money on the
SERVER's key), and `infra/gcp/bootstrap.sh` is the idempotent one-time GCP setup. Note Cloud
Run reserves `/healthz` — probe `/health` or `/readyz` externally.

## Golden tests

`tests/golden/` snapshots rendered prompts and the fully-assembled upstream message array into `tests/golden/snapshots/`. Any intentional change to a **prompt template or the assembler** will fail these until you regenerate with `DAINTREE_UPDATE_GOLDEN=1 pytest tests/golden/` — treat the resulting snapshot diff as the human-reviewable record of what the model will actually see. The catalog-dependent snapshots render against a **pinned fixture catalog** (`tests/golden/fixtures/skills/`), deliberately decoupled from the live catalog so ordinary skill edits never redden the suite; `test_selector_prompt_renders_real_catalog` keeps a structural check over the real catalog.

## Utility tasks (`POST /v1/daintree/tasks`)

`services/task_runner.py` runs named small-model tasks (checkpoint, memory distill, watcher classify, terminal summarize/extract/judge, etc.). Each is a server-owned `TaskProfile` that picks the prompt, output mode (`json`/`text`), temperature, and token cap; clients send only task *data* (never `messages`/`system`). JSON tasks get one repair retry on parse failure, and that retry is told what actually went wrong. Without it "respond again" had no referent — the model was told its previous response was wrong in a conversation that did not contain one — so the rejected output goes back inside the correction, in a delimited, escape-neutralized data block rather than as a fabricated `assistant` turn (the wire role would have been honest, but a model reading its own prior words does not read them sceptically, and a reply induced by injected terminal output is exactly what lands there). The instruction names the failure by KIND — `syntax` (never parsed), `schema` (parsed, wrong shape, with the field paths Pydantic objected to) or `semantic` (well-formed and unusable, e.g. a dependency cycle). The kind is carried on `TaskParseError`, not sniffed from its message. Detail that reaches the prompt is sanitized, framed and byte-capped for the same reason, because the useful parts of an error are the parts the MODEL wrote — quoted node ids, quoted field paths; Pydantic's `input` is dropped outright, being the payload we just refused rather than a description of it. `terminal_extract_json` is the only task that accepts a client `result_schema` (validated for size/depth/keys by `services/result_schema.py`). Adding a task = new `TaskProfile` + input/output models in `contracts/tasks.py` + a `tasks/<id>.md` prompt + a golden snapshot.

## Workflow intelligence (stateless backend half)

The CLI owns durable **workflow execution graphs** (typed DAGs of nodes/resources/blockers, stored in its local SQLite); the backend contributes only stateless intelligence — it never persists workflow state (Cloud Run safe). Three utility tasks (`services/task_runner.py`, semantic checks in `services/workflow_validation.py`, contracts in `contracts/tasks.py`):

- `workflow_plan` — goal + runtime summary + tool inventory (+ compact **outlines** of the active skills, via the task runner's catalog access — step headings only, never bodies) → a typed plan (≤20 nodes / ≤40 edges).
- `workflow_reconcile` — graph snapshot + recent events → a typed patch + next action.
- `workflow_resume_digest` — workflow snapshots → ranked, prompt-ready resume package ("where were we?").

Output posture: **structural defects** (cycle, duplicate/unknown node id, moving a `done`/`skipped`/`cancelled` node) are treated like parse failures — one repair retry, then 502; **unknown tool names** degrade instead (`next_action` nulled + warning) so a missing capability never sinks the task. Plans/patches are recommendations only — the client validates and applies them through its own dispatch/confirmation policy; nothing here executes tools.

`turn.workflow_state` (contracts/extensions.py `WorkflowDigest`) carries bounded digests of those client graphs into the turn footer — hard caps: `CAP_WORKFLOW_DIGESTS` (5), one 512-byte sanitized line per string, `CAP_WORKFLOW_STATE_BYTES` (16 KiB) total, head-of-list wins. `/v1/daintree/capabilities` advertises the feature under `workflow_intelligence` (the CLI gates on it during rollout).

## Config & readiness

`config.py` `Settings` is parsed once, immutable, env-driven (`.env`). The model is **hardcoded** (`config.SERVING_MODEL`, `config.PUBLIC_MODEL_ID`) — a product decision, not config — and so is the **thinking posture**: `INTERACTIVE_THINKING_ENABLED = False`. On Together that is a flag genuinely held down, not a property of the model: DeepSeek V4 Flash reasons by default and the unified `reasoning: {"enabled": false}` is what switches it off, measured. Nothing in this server thinks; the flag and `INTERACTIVE_REASONING_EFFORT` stay as the decision record and the switch a future reasoning-capable serving model would need. The server ships **one optimum** and there are deliberately no env toggles for PRODUCT decisions — which model, which posture, which prompt, which skills: things with one right answer we have already benchmarked. That rule is about quality, not about operations, and the handful of env-driven booleans beside it (`MAIN_OUTPUT_LINT_ENABLED`, `EXPOSE_DEBUG_SELECTOR_REASON`, `TOOL_PROJECTION_MODE`) are the other kind: switches whose right value is whatever production says this week. `TOOL_PROJECTION_MODE` is the sharpest case — it gates a behaviour that WITHHOLDS capability from the model, so being able to answer "turn it off" in the time Cloud Run takes to roll a config-only revision is the point of it existing (see "Skill-aware tool projection" below).

The environment supplies exactly two secrets, **both server-owned**: `STATE_SIGNING_KEY_CURRENT` and `TOGETHER_API_KEY`. Both are in `missing_secrets()` — the upstream key gates readiness because without it nothing can be answered at all, and discovering that on the first turn surfaces as an upstream 401 that reads like the caller's fault. There is still **no inbound API key** to configure: the door is open until account auth lands. Secrets are `SecretStr`. Missing required secrets — and, in a hardened environment (`production`/`staging`), weak/default ones, including a serving base URL that is not the real origin (`config.SERVING_ORIGIN`), because the server key is forwarded there — **fail readiness** (`/readyz` → 503) rather than crashing the process; liveness (`/healthz`) stays green. `local_only_problems()` adds two more, both development-only: prompt dumping must be off in production, and so must `DAINTREE_SKILLS_OVERLAY_DIR` — a scratch directory of skill files merged over the packaged catalog so a skill can be authored against a running server (`POST /v1/daintree/skills/reload` rebuilds it live; that route is mounted only when the overlay is configured and the environment is not hardened). `main.py` builds the immutable service `Container` and validates it during lifespan startup; a malformed prompt or skill prevents readiness without taking down liveness.

## Conventions

- Python 3.12, `from __future__ import annotations` everywhere, `mypy --strict`, ruff (line length 100; rule set + ignores in `pyproject.toml`).
- Pydantic v2 for wire/config models; `extra="forbid"` and `frozen=True` are used deliberately (reject unknown fields, keep things immutable).
- Fail-open (selector) vs fail-closed (oversized body, malformed skill) choices are intentional — preserve the existing posture when editing those paths.
- Runtime deps are pinned in `requirements.lock` (a constraints file the Docker build and CI install against).

## Sub-agent profile (`profile: "subagent"`)

`RespondRequest.profile` selects which persona answers a request. Omitted (the CLI
default on every ordinary turn) means `"assistant"` — the orchestrator. The CLI's
delegation loop (`internal/subagent` in the cli repo) sends `"subagent"`: a bounded,
READ-ONLY worker running in its own isolated conversation so the main thread can
learn one answer without paying the context cost of finding it.

It is not a hint — it selects which of two assembly paths runs:

|                        | `assistant`                     | `subagent`                                  |
|------------------------|---------------------------------|---------------------------------------------|
| System prompt          | `prompts/main/base/`            | `prompts/main/subagent/` (4 partials)        |
| Skill selection        | full catalog                    | menu narrowed by `Catalog.executable_with`   |
| Active-set cap         | `settings.max_active_skills`    | `min(that, 2)`                               |
| Docs lookup            | on                              | off (it researches the PROJECT)              |
| Integrations / runtime / turn footer / display | rendered   | dropped                                      |
| Startup block          | yes                             | yes (it must know which project it is in)    |

**The narrowed menu is the load-bearing part.** A skill is a PROCEDURE, and one
whose steps call tools the caller does not hold is worse than no skill: it spends
context and then steers the model toward calls that can only fail. A sub-agent is
offered ~36 read-only tools while most of this catalog drives spawning, worktrees
and terminals, so `Catalog.executable_with(tools)` keeps only skills whose declared
`requiredTools` the caller actually has. That reuses metadata skills already
declare, so a research-shaped skill written tomorrow reaches sub-agents the day it
lands with no second list to maintain. Today 2 of 39 skills pass — an honest
statement about the catalog, not the filter.

Both selector implementations honour a caller-supplied catalog. `LocalSelector`
must NOT ignore it: ignoring it would let a locally-selected run load an
orchestration runbook the sub-agent cannot follow, and the two implementations
would disagree about what a profile may see.

Two boundaries are enforced server-side rather than left to the client. The signed
state token records the profile that resolved its active set (`StatePayload.profile`),
and a cross-profile token forces a fresh selection — the profiles choose from
different menus, so a sub-agent's set can hold a specific whose domain foundation was
filtered out of its menu, and continuation reuse deliberately does not re-pair
foundations.

One performance property the narrowed menu depends on: `SetwiseClassifier` holds a
single table slot, so syncing a narrowed catalog used to evict the rest of the
catalog's vectors and make the NEXT orchestrator turn re-embed them on the first-byte
path — measured at 5.3s for 37 skills, once per alternation, against a ~10ms selection
budget. A digest-keyed embedding memo (`_vec_memo`) is what makes profile alternation
free; `test_a_narrowed_catalog_does_not_evict_the_full_catalog_s_vectors` holds it.

`profile` is `omitempty` on the CLI's wire and defaults to `"assistant"` here, so
an ordinary turn stays byte-identical and the prompt cache does not split.

**Biasing the orchestrator toward delegating.** A delegation primitive reached for
only after the expensive search has happened is worth nothing, so the bias lives in
three places, split by the base prompt's own trigger rule: one bullet in
`main/base/20-tool-discipline.md` (fires even on a zero-skill turn), the
elaboration as item 8 of `daintree.foundation`, and — the one that actually moved
behaviour — a rewritten decision step in `daintree.explore.repository`, which
previously said "POINTER — do it yourself" and knew no delegate but the heavyweight
visible agent.

Full design, bounds and the CLI side: `docs/SUBAGENTS.md` in the cli repo.
