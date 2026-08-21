# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this service is

The Daintree Assistant Server moves the model-orchestration boundary off the local CLI and onto the server. **The server owns** the permanent system prompt, the skill catalog and bodies, automatic skill selection, prompt assembly, and the small-model utility prompts. **The CLI owns** local state, local tool execution, and **the upstream credential** — the server holds none. Every request's bearer token **IS** the caller's own OpenRouter key: it authenticates the request and funds that turn's upstream calls, threaded explicitly to every provider call (`auth.py` `Principal.api_key`; `main.py` builds the serving provider with `api_key=None` so a propagation bug fails loudly instead of quietly billing a shared account).

The public API is **Daintree-native** (`POST /v1/daintree/respond`), not OpenAI-compatible — the session/skill envelope is first-class and skills are their own response block. The upstream is **OpenRouter** (OpenAI wire shape, spoken *internally* behind the `ChatProvider` abstraction in `providers/`): `deepseek/deepseek-v4-flash-0731` for the main/utility calls and the skill-learning gate — **skill selection is LOCAL** (an in-process ONNX classifier, `skills/local/`) and makes no upstream call. OpenRouter fronts ~24 endpoints serving those weights and every request pins an endpoint-selection policy (`_PROVIDER_ROUTING` in `providers/openrouter.py`): **throughput-sorted, filtered to endpoints that do not collect or train on request data** (`docs/PROVIDER_POLICY.md` — a no-training floor, deliberately *not* a zero-retention claim <!-- privacy-copy-ok -->), `require_parameters` on, fallbacks on. **One policy serves every call** — there is deliberately no per-call-shape routing — and it ranks on one axis while flooring the other: `sort: throughput` picks the fastest generator, `preferred_max_latency` (p50/p90/p99) stops that being an endpoint which streams fast and takes seconds to START. The bound deprioritises rather than filters, so it can never empty the pool and is safe everywhere. A `latency_critical` flag (flips `sort` to `latency`) remains in the policy as a call-site property, but with the upstream selector replaced by the local classifier nothing on the serving path sets it today. Every call a *user waits on* also carries a **cache-affinity `session_id`** so its prefix stays on one endpoint (§3a): per session for a turn, per task id for a utility task. Skill learning still sends none — it runs after the response. A caller who overrides routing gets their own cohort suffix, so one caller's `zdr`/`only` can never re-point everyone else's sticky record once the credential is server-held. The local skill-learning **builder/improver name a stronger model, `openai/gpt-5.6-sol`** (the hard "write a whole skill" task) — same provider, same key, same adapter; only the model id differs. All model ids hardcoded. The full wire contract is `docs/DAINTREE_API.md`; code comments cite it as "section X.Y" — read it when changing wire shapes or task behavior.

`TODO.md` holds deliberately-deferred work, each entry gated on a trigger (currently: move the serving path off OpenRouter to Novita direct, if and when there is a paid tier).

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

# Live OpenRouter contract tests (skipped by default; make real API calls). Also
# prove the routing policy matches an endpoint for every parameter mix we send.
RUN_LIVE_OPENROUTER=1 OPENROUTER_API_KEY=sk-or-v1-... pytest tests/live -q

# Run locally (binds HOST/PORT from .env; default 127.0.0.1:8473). STATE_SIGNING_KEY_CURRENT
# is the only required env var: requests authenticate with the CALLER's own OpenRouter key as
# the bearer token, and OPENROUTER_API_KEY is benchmark/live-test tooling only (unread by the
# serving path).
cp .env.example .env   # then set STATE_SIGNING_KEY_CURRENT
python -m daintree_assistant_server
uvicorn daintree_assistant_server.main:app --reload --port 8473

# Skill learning (LOCAL DEV ONLY): let the server write/improve skills from your
# own sessions. Off by default; fails readiness in production. In .env set:
#   SKILL_LEARNING_MODE=apply   # off | propose (dry-run JSON) | apply (writes skill files)
# apply edits the real skills/files/*.md so changes show up in `git diff`; review,
# then regenerate golden snapshots before committing. See "Skill learning" below.
```

## Request flow (`/v1/daintree/respond`)

`ChatOrchestrator.complete` (`services/chat_orchestrator.py`) is the spine. It keeps the two sequential flows that genuinely are the orchestration — `_prepare_inner` and `_stream_body`, whose shared local state (the bare-intent hold, retry counters, accumulated usage) is what makes them long — and delegates the separable concerns to their own modules: `skills/active_set.py` (resolving the active set from a selector decision, including domain-foundation pairing), `services/turn_accounting.py` (usage, cost and the complete-vs-floor rules), `services/deadlines.py`, and `services/output_policy.py`. The steps:

1. **Verify state token** (`state/codec.py`) — a signed, opaque token carries the prior active-skill set across stateless instances. Invalid/expired/wrong-session tokens are ignored (not an error).
2. **Selector cadence** (`_should_run_selector`) — the selector does **not** run on ordinary tool-continuation rounds. It runs on `selection.force`, `policy=="always"`, an invalid token, a catalog-revision change, a new `turn_id`, or a changed `instruction_revision`. Otherwise the prior active set is reused.
3. **Skill selection** (`skills/local/`) — **LOCAL, not a model call**, and **two-stage**: a fine-tuned bi-encoder embeds the conversation and takes the `k` (32) nearest skills by cosine from a table of embedded skill documents; then a **set-wise head** reads the conversation embedding and those `k` cached skill embeddings *together* and returns a logit per candidate plus a docs logit and a 4-way cardinality head that makes "zero skills" a first-class answer (`setwise.py`). `LocalSelector` (`skills/local/selector.py`) keeps the upstream `Selector`'s exact signature; conversation state enters both as a `[loaded: …]` query prefix and as a per-candidate `active` feature, and a skill not already active needs p≥0.65 to join a loaded set (adding mid-conversation is the expensive false positive). **Neither stage owns per-skill weights** — the head consumes skill *embeddings*, not skill ids — and three things follow that are the whole reason for the design: a skill added to the catalog at runtime is embedded by `sync()` on load and is **reachable on the next turn with no retrain** — measured, that means it wins requests only it plausibly answers but still loses contested turns to trained neighbours (4/14 held-out benchmark cases against 13/14 trained; `training/EXPERIMENTS.md`), so a retrain is no longer what makes a skill *possible*, only what makes it *competitive*; there is consequently **no `routing_revision` staleness gate and no degrade-open window** (the stamp is still recorded in the artifact, as a drift signal for a human, and gates nothing); and catalog growth costs one 4.5KB table row rather than a wider head, so the head's job is the same size at 38 skills and at 1,000. The shortlist's ORDER is deliberately not information: the head has no positional encoding, so it is permutation-equivariant, and retrieval evidence reaches it as per-candidate features (cosine, normalised rank) instead — which is what stops a newly added skill being penalised for landing in a slot the head never trained on. Retraining is a **quality** step, not an unblocking one (`.claude/skills/retrain-selector`); the training pipeline, corpus, and full experiment log live in `training/` (READ `training/EXPERIMENTS.md` before re-trying any idea — the obvious retrieve-then-**cross-encoder**-rerank design was built, measured, and abandoned there, and this design is not it: nothing re-reads a skill's text at request time). The prompt-based `Selector` class remains only as the scriptable test seam (`tests/conftest.py install_scripted_selector`) and for its evidence helpers, which skill learning shares.
4. **Resolve active set** (`resolve_active_skills`) — a **successful** selector run is authoritative: its ids become the complete active set (topic-shift replacement; an empty result clears it), capped at the limit. On an authoritative run each selected specific is **paired with its domain foundation** (`_admit_with_domain_foundations` + `Catalog.domain_foundation`, which walks UP the dotted id namespace nearest-first, so a top-level `daintree.foundation` backs every `daintree.*` skill) so a foundation loads on the SAME turn its specific is selected rather than a turn late — front-loading is deterministic, not left to the classifier. Admission is by ATOMIC UNIT (specific + its foundation), admitted in rank order only while the whole unit fits under the cap — deliberately *not* append-then-truncate, which was dead code at full capacity because the selector already caps its own output, so the appended foundation was always the first id discarded. A no-run continuation round or a degraded selector reuses the prior set unchanged (no re-pairing — a prior set already carries its foundation, so continuation never surfaces a surprise load). (`merge_active_skills` stays additive for the explicit `resolve` `load` path only.)
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
   The search is surfaced to the CLI as a `docs__search` prelude execution, and
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
6. **Respond** — the skills/selection metadata is delivered first: as the response body's `skills` block (non-streaming) or the first SSE `meta` event (streaming), which local selection makes effectively immediate (~10ms in). The flow is strictly sequential now — select → assemble → **one** upstream call; the speculative-stream machinery that used to race a guessed prompt against the selector was deleted when selection stopped costing 2s (git history has it). Open/prefetch retries happen after metadata but before any model delta; an exhausted open or mid-stream failure emits a terminal `error` event, never a fabricated `done`.
7. **Skill learning** (`services/skill_learning.py`, local dev only — see below) — *after* the response, on a turn that finished with a **final answer** (no pending tool calls), the orchestrator fires the learner **fire-and-forget** (`_maybe_learn_after_turn`) with the just-generated answer appended to the transcript, so it learns from the workflow it actually completed. It may write/improve a skill and reload the catalog *in place*, live for the **next** turn (it never touches THIS response — already sent — so it adds no latency and is not folded in). A turn ending in a tool call (a continuation round) is skipped: the workflow isn't done. Off by default; fails open — a learning error can never break the turn.

**Invariants to preserve** (also in README): client `system`/`developer` messages are rejected; `skill.find`/`skill.load` are never offered to the main model; only active skill *bodies* are injected (never the full catalog); selector failure fails open; state is portable via the signed token; required structured `startup` context reaches the main model only — the selector's evidence is sanitized `input.messages` plus bounded turn/workflow hints and tool names, the learner's is `input.messages` plus the just-generated final answer and tool names, and neither ever sees `startup`.

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

`risk` enforces nothing. It gates no code path, is not shown to the selector, and never crosses the wire; real risk gating is **per-tool**, in the CLI's confirmation matrix. It is kept because the skill-learning builder writes it, so a human reviewing a generated skill in `git diff` can see at a glance what tier of mutation the runbook authorizes. There is no `tags` key and no `priority` key — both were deleted once `selector_view()` narrowed to `{id, summary, when_to_use, foundation}` and left them with no consumer.

**Pipeline:** `loader.py` parses/validates (strict — a malformed skill fails `/readyz`, not a request) → `catalog.py` builds the **immutable** catalog and a deterministic `revision` hash → the selector sees only `Skill.selector_view()`, the public `/skills` endpoint sees only `public_view()`, and **neither ever exposes the body** except via injection into the main prompt for active skills. Selection is automatic and server-side: the guarantee that `skill.find`/`skill.load` are never *offered* is enforced at the request boundary by the reserved-tool-names check in `services/validation.py` (`RESERVED_TOOL_NAMES`, which also covers the `skill__find`/`skill__load` wire spellings and the `daintree_internal__` prefix), and the base prompt separately tells the model it does not find, load, or manage skills itself (`main/base/30-skills.md` — it never names the tools).

**To add a skill:**
1. Create `skills/files/<id>.md` (it is auto-discovered, sorted by id — no registration).
2. Run `pytest tests/unit` (~5s, no API calls). A new skill trips more than the loader/catalog tests: `test_prompt_budgets.py` (body size ceilings, no pipe tables, a Report-back line), `test_skill_catalog_hygiene.py` (every `requiredTools` name exists in the CLI tool-registry snapshot at `tests/fixtures/cli_tool_names.txt` — its header has the regeneration recipe against the pinned `cli/` submodule), and `test_skill_learning.py`'s house-style sweep over the packaged catalog. Do **not** try to narrow this with `pytest -k`: the obvious concern-named filter (`-k "catalog or budget or house"`) matches no loader test name or module and silently deselects all 14 of them. Run the directory, or list the files without a filter.
3. **Run the golden tests** (`pytest tests/golden/`). The prompt snapshots render against a **pinned fixture catalog** (`tests/golden/fixtures/skills/`), not the live one, so adding or changing a real skill no longer churns `selector_system.md` / `loaded_skills_*` — only editing a prompt template or the assembler does. A structural smoke test (`test_selector_prompt_renders_real_catalog`) still verifies the real catalog renders through the selector prompt.
4. **Check routing coverage** (`python -m benchmarks.skill_selection.bench coverage`) — a per-skill case-coverage matrix, offline and free (no API calls). It exits non-zero when a skill has no benchmark case that requires it, so a new skill does not land unrouted and untested.

## Skill learning (local development only)

A backend-internal harness (`services/skill_learning.py`) that watches completed local workflows and, when warranted, **writes a new skill or improves an existing one** so the catalog gets better as you use it. It is the newest area of ongoing work alongside skills/prompts.

**Hard boundaries** (do not weaken):
- **Off by default** (`SKILL_LEARNING_MODE=off`) and **impossible in a hardened environment**: an enabled mode is a `local_only_problems()` entry that fails `/readyz` when `ENVIRONMENT` is `production` *or* `staging` (`Settings.is_hardened`).
- **Invisible to the CLI**: no new wire fields, no capabilities flag, no dev endpoints. The main assistant model is **never** told learning exists — nothing is appended to its prompt and it is offered no feedback tool. A created/improved skill simply becomes live in the catalog and appears — on the **next** relevant turn — in the existing `skills` block (`newly_loaded` + prelude) as an ordinary dynamic load. **That claim only holds at all because the selector has no per-skill weights**: the two-stage selector embeds the new skill's document on catalog reload and can route to it that turn. Under the fixed-head classifier that preceded it, a written skill had no head row AND moved `routing_revision`, so learning a skill degraded selection open until someone retrained — the feature and the selector were in direct conflict. Temper it with the measurement, though: a freshly written skill wins only unambiguous requests until a retrain gives it boundary data against its neighbours. Because learning runs fire-and-forget *after* the response, the triggering turn is not slowed and shows nothing new.
- **Fails open**: `SkillLearner.run` swallows every exception and returns a no-op. Learning must never break `/respond`.
- The **skill loader is the authority for disk**: a proposal is rendered to markdown and run through `parse_skill(expected_id=...)`; it only lands on disk if that (plus an id-format guard, a path-containment check, and a conservative secret scan) passes.

**Pipeline** (fire-and-forget *after* a turn that completed with a final answer — `_maybe_learn_after_turn` in the orchestrator; a turn ending in a tool call is a continuation round and is skipped, so learning only ever sees a finished workflow — including the last one of a session, which no next instruction would ever trigger):
1. **Gate** (`prompts/skill_learning/gate.md`, the small main model, thinking-high JSON) reads the whole workflow transcript (with the just-generated final answer appended) and returns none / improve / create. Strong triggers: the user signalling a problem ("what happened here?", "this shouldn't have happened") or the assistant visibly struggling; a cleanly completed repeatable workflow can justify a *create*. Only a `general_daintree_workflow` lesson above `SKILL_LEARNING_MIN_CONFIDENCE`, backed by concrete evidence, writes.
2. **Build/improve** (`build.md` / `improve.md`, GPT-5.6 Sol, high reasoning) diagnoses what worked and what failed in the workflow, then returns a `SkillProposal`; the diagnosis plus a unified diff land in the audit record. The prompts require **generic, project- and agent-agnostic** skills — no hardcoded repo/path/CLI-agent; a step that needs an agent tells the main assistant to pick the best-fit one from the available-agent registry supplied in structured startup context — because the catalog must transfer across projects, users, and CLI agents. **Foundation tiering** (see "Skills" above): the gate routes base, domain-general lessons into a domain's existing `[foundation]` skill (improve it) rather than spawning a narrow specific; the learner may always grow an existing foundation, but a `SkillProposal` that would MINT a new foundation or PROMOTE a specific one is honored only when `SKILL_LEARNING_ALLOW_FOUNDATION_CREATE=true` (default off — `_validate` never demotes, and drops+logs an unauthorized promotion). The landed tier and the requested tier are both in the audit record.
3. **Validate → write → reload**: atomic write + reload as one cancellation-shielded unit (`CatalogManager.reload()`, rollback on failure). The reloaded catalog is live for the next turn's selection; nothing is folded into the just-answered turn (its response is already sent).

**Modes** (`SKILL_LEARNING_MODE`): `off` | `propose` (run the pipeline, write only a proposal + audit JSON under `SKILL_LEARNING_AUDIT_DIR`, never touch skill files) | `apply` (write the skill file + reload).

**Write target** (`SKILL_LEARNING_DIR`): empty → the packaged `skills/files/` (edit-in-place; improvements land in `git diff`). Set to a separate dir to overlay instead — the catalog then loads packaged + overlay (`load_catalog(primary, overlay)`), overlay winning on id collision, leaving the source tree untouched.

**Prompts have no version suffix** (`gate.md`, not `gate.v1.md`) — like every prompt in the system, task prompts included (see "Versioning" below). They are snapshotted in `tests/golden/` like every other prompt. Because apply-mode writes real skill files, an interactive session will dirty the catalog (`git diff` under `skills/files/`) — but **not** the golden snapshots, which render against a pinned fixture catalog. Regenerate goldens only when you change a prompt template or the assembler.

## Prompts / system messages

All prompt text is **server-owned** and lives in `src/daintree_assistant_server/prompts/` (`.md` = raw bodies, `.md.j2` = Jinja templates), loaded via `prompts/loader.py`:

- `main/base/NN-*.md` — the permanent assistant system prompt (persona + hard rules). It is a **directory of ordered partials**, not a single file: `PromptLoader.base_prompt()` globs `main/base/*.md`, sorts by filename, and joins them with blank lines, so the `NN-` prefix is load-bearing ordering (`00-persona`, `10-hard-rules`, `20-tool-discipline`, `30-skills`, `35-daintree-docs`, `40-communication`, `50-formatting`). Edit the partial that owns the rule; a new section is a new numbered file. **There is no `main/base.md`** — writing one creates a stray file the loader ignores. This directory is the highest-leverage prompt text in the repo.
- `main/{startup_context,runtime_context,integrations,loaded_skills,response_contract,turn_footer,docs_context}.md.j2` — the structured main-prompt layers. `response_contract.md.j2` is order-critical: the golden suite asserts it renders as the **final system message** — the second and last one, directly after the base prompt and so immediately ahead of the tool schemas. The skill, integration and startup layers are **user-role rows downstream of those schemas**, not system messages.
- `selector/system.md.j2` — the skill-selector classifier prompt.
- `tasks/<task>.md` — utility-task system prompts.

**Two things that are easy to get wrong:**

- **Assembly order is load-bearing — and role choice IS position.** The DeepSeek chat template (unchanged by routing through OpenRouter — it is the model's template, not the API's) serializes [ALL system messages, in array order] → [tools] → [non-system conversation], so a volatile system message ANYWHERE in the array lands before the ~21.5k-token tool schemas and busts their cache (measured 2026-07-08: 36% hit on byte-identical turns → 99% after the fix). The system role is therefore reserved for what is static for **every caller**, and the rows below it are ordered by **how widely each can be shared, widest first**, so a change to one never re-encodes anything above it. `PromptAssembler.build_main_request` emits: the two static **system** blocks (base → response contract), then — after the schemas — a **user-role** loaded-skills block (shared by task), a **user-role** integrations block and the stable **user-role** startup block (per session, instance and project), the append-only visible `input.messages`, and ONE trailing **user-role** "[Injected context …]" block carrying per-request runtime + turn state. While the skill, integration and startup rows hold steady, only the final tail re-encodes each round — the client republishes `mcp_servers` every round, so a server reconnecting mid-session moves the integrations row too. Integrations and skills used to be system-role, which put a per-machine MCP endpoint URL and every selector topic shift ahead of all 21.5k tokens of schemas; moving them below (issue #39) makes a skill load cost its own bytes instead of the tool region, and `benchmarks/prompt_cost` now gates `skill_topic_shift` at `system_tools_stable == 1.0` to hold that. Skill bodies stay **ahead of the conversation** rather than in the tail — a large runbook in the tail is re-paid uncached every round of every turn. Startup is required even when empty, stays outside the visible transcript, and is never sent to selector/learning calls. Don't reorder casually, and never add a volatile system message. All client-provided local context is treated as untrusted: sanitized and byte-capped (`text_utils.py`, the `cap_*` settings in `config.py`); exact agent ids are JSON-quoted and rows are admitted whole.
- **Prompt bytes are behavior.** On a meaningful prompt change, regenerate golden snapshots (below) — that diff is the reviewable record. Do **not** bump version strings: `MAIN_PROMPT_VERSION` is a fixed family label (`"main"`), not a per-edit version (see "Versioning" below).

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

Not gated here, deliberately: anything needing a paid call. Live latency,
selection accuracy and injection results belong in a nightly run against a
low-spend key — a baseline that cannot be checked offline either gets skipped or
makes every push cost money.

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
caller's key), and `infra/gcp/bootstrap.sh` is the idempotent one-time GCP setup. Note Cloud
Run reserves `/healthz` — probe `/health` or `/readyz` externally.

## Golden tests

`tests/golden/` snapshots rendered prompts and the fully-assembled upstream message array into `tests/golden/snapshots/`. Any intentional change to a **prompt template or the assembler** will fail these until you regenerate with `DAINTREE_UPDATE_GOLDEN=1 pytest tests/golden/` — treat the resulting snapshot diff as the human-reviewable record of what the model will actually see. The catalog-dependent snapshots render against a **pinned fixture catalog** (`tests/golden/fixtures/skills/`), deliberately decoupled from the live catalog so the self-evolving skill files (skill learning) never redden the suite; `test_selector_prompt_renders_real_catalog` keeps a structural check over the real catalog.

## Utility tasks (`POST /v1/daintree/tasks`)

`services/task_runner.py` runs named small-model tasks (checkpoint, memory distill, watcher classify, terminal summarize/extract/judge, etc.). Each is a server-owned `TaskProfile` that picks the prompt, output mode (`json`/`text`), temperature, and token cap; clients send only task *data* (never `messages`/`system`). JSON tasks get one repair retry on parse failure. `terminal_extract_json` is the only task that accepts a client `result_schema` (validated for size/depth/keys by `services/result_schema.py`). Adding a task = new `TaskProfile` + input/output models in `contracts/tasks.py` + a `tasks/<id>.md` prompt + a golden snapshot.

## Workflow intelligence (stateless backend half)

The CLI owns durable **workflow execution graphs** (typed DAGs of nodes/resources/blockers, stored in its local SQLite); the backend contributes only stateless intelligence — it never persists workflow state (Cloud Run safe). Three utility tasks (`services/task_runner.py`, semantic checks in `services/workflow_validation.py`, contracts in `contracts/tasks.py`):

- `workflow_plan` — goal + runtime summary + tool inventory (+ compact **outlines** of the active skills, via the task runner's catalog access — step headings only, never bodies) → a typed plan (≤20 nodes / ≤40 edges).
- `workflow_reconcile` — graph snapshot + recent events → a typed patch + next action.
- `workflow_resume_digest` — workflow snapshots → ranked, prompt-ready resume package ("where were we?").

Output posture: **structural defects** (cycle, duplicate/unknown node id, moving a `done`/`skipped`/`cancelled` node) are treated like parse failures — one repair retry, then 502; **unknown tool names** degrade instead (`next_action` nulled + warning) so a missing capability never sinks the task. Plans/patches are recommendations only — the client validates and applies them through its own dispatch/confirmation policy; nothing here executes tools.

`turn.workflow_state` (contracts/extensions.py `WorkflowDigest`) carries bounded digests of those client graphs into the turn footer — hard caps: `CAP_WORKFLOW_DIGESTS` (5), one 512-byte sanitized line per string, `CAP_WORKFLOW_STATE_BYTES` (16 KiB) total, head-of-list wins. `/v1/daintree/capabilities` advertises the feature under `workflow_intelligence` (the CLI gates on it during rollout).

## Config & readiness

`config.py` `Settings` is parsed once, immutable, env-driven (`.env`). Models are **hardcoded** (`OPENROUTER_MODEL`, `SKILL_BUILDER_MODEL`/`SKILL_IMPROVER_MODEL` = `openai/gpt-5.6-sol`, `PUBLIC_MODEL_ID`) — a product decision, not config — and so are the **thinking posture** (`INTERACTIVE_THINKING_ENABLED`, `OPENROUTER_REASONING_EFFORT`, `SKILL_LEARNING_REASONING_EFFORT`, module constants beside the model ids: main/utility/selector are **non-thinking**, only skill learning still thinks — it runs after the response, so its latency never reaches a user. There is no middle tier: DeepSeek remaps `low`/`medium` up to `high`, so off is the only step down) and the **endpoint-selection policy** (`_PROVIDER_ROUTING` in `providers/openrouter.py`). There are deliberately **no env toggles** for any of it; the server ships one optimum. The environment supplies **no provider credential at all** — only the OpenRouter base URL plus the server's own state-signing material, since every turn's upstream key is the caller's bearer token. Secrets are `SecretStr`, and `missing_secrets()` is down to **`STATE_SIGNING_KEY_CURRENT` alone** (there is no inbound API key to configure and no unauthenticated development mode — auth no longer varies by environment). Missing required secrets — and, in a hardened environment (`production`/`staging`), weak/default ones, now including an `OPENROUTER_BASE_URL` that is not the `https://openrouter.ai` origin, because caller keys are forwarded there — **fail readiness** (`/readyz` → 503) rather than crashing the process; liveness (`/healthz`) stays green. Local-only features also gate readiness: `local_only_problems()` fails `/readyz` if skill learning is enabled in production. `main.py` builds the immutable service `Container` and validates it during lifespan startup; a malformed prompt or skill prevents readiness without taking down liveness.

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
