# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this service is

The Daintree Assistant Server moves the model-orchestration boundary off the local CLI and onto the server. **The server owns** the permanent system prompt, the skill catalog and bodies, automatic skill selection, prompt assembly, the small-model utility prompts, and upstream credentials. **The CLI owns** local state and local tool execution.

The public API is **Daintree-native** (`POST /v1/daintree/respond`), not OpenAI-compatible — the session/skill envelope is first-class and skills are their own response block. The upstream is **OpenRouter** (OpenAI wire shape, spoken *internally* behind the `ChatProvider` abstraction in `providers/`): `deepseek/deepseek-v4-flash-0731` for the main/selector/utility calls and the skill-learning gate. OpenRouter fronts ~24 endpoints serving those weights and every request pins an endpoint-selection policy (`_PROVIDER_ROUTING` in `providers/openrouter.py`): **throughput-sorted, filtered to endpoints that neither store nor train on request data**, `require_parameters` on, fallbacks on. The local skill-learning **builder/improver name a stronger model, `openai/gpt-5.6-sol`** (the hard "write a whole skill" task) — same provider, same key, same adapter; only the model id differs. All model ids hardcoded. The full wire contract is `docs/DAINTREE_API.md`; code comments cite it as "section X.Y" — read it when changing wire shapes or selector/task behavior.

**The CLI source is vendored at `cli/`** — a git submodule tracking `github.com/daintreehq/assistant` (the Go CLI that talks to this server). It's checked in so the two repos can evolve together and so this repo can read the CLI's actual tool/function definitions as we move toward sourcing them from there; it is **not** part of the Python package, build, or test scope (ruff `extend-exclude`, pytest `testpaths`, and mypy/coverage are all pinned to `src`/`tests`, and CI does not fetch submodules). After cloning, run `git submodule update --init cli` to populate it; it is otherwise pinned to a commit and does not update on its own.

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
mypy --strict src

# Prompt golden snapshots — REQUIRED after any intentional change to a prompt
# template or the assembler (see "Golden tests" below); review the diff before
# committing. Skill-catalog edits no longer churn these: the snapshots render
# against a pinned fixture catalog, not the live one.
DAINTREE_UPDATE_GOLDEN=1 pytest tests/golden/

# Live OpenRouter contract tests (skipped by default; make real API calls). Also
# prove the routing policy matches an endpoint for every parameter mix we send.
RUN_LIVE_OPENROUTER=1 OPENROUTER_API_KEY=sk-or-v1-... pytest tests/live -q

# Run locally (binds HOST/PORT from .env; default 127.0.0.1:8473)
cp .env.example .env   # then set OPENROUTER_API_KEY
python -m daintree_assistant_server
uvicorn daintree_assistant_server.main:app --reload --port 8473

# Skill learning (LOCAL DEV ONLY): let the server write/improve skills from your
# own sessions. Off by default; fails readiness in production. In .env set:
#   SKILL_LEARNING_MODE=apply   # off | propose (dry-run JSON) | apply (writes skill files)
# apply edits the real skills/files/*.md so changes show up in `git diff`; review,
# then regenerate golden snapshots before committing. See "Skill learning" below.
```

## Request flow (`/v1/daintree/respond`)

`ChatOrchestrator.complete` (`services/chat_orchestrator.py`) is the spine:

1. **Verify state token** (`state/codec.py`) — a signed, opaque token carries the prior active-skill set across stateless instances. Invalid/expired/wrong-session tokens are ignored (not an error).
2. **Selector cadence** (`_should_run_selector`) — the selector does **not** run on ordinary tool-continuation rounds. It runs on `selection.force`, `policy=="always"`, an invalid token, a catalog-revision change, a new `turn_id`, or a changed `instruction_revision`. Otherwise the prior active set is reused.
3. **Skill selection** (`skills/selector.py`) — a separate **small-model** call classifies the conversation against the catalog and returns ≤ `max_active_skills` (default 3) IDs as a JSON object (`response_format` json_object, **not** a forced tool call). It sits on the turn's first-byte path, so it is **hardcoded non-thinking** — the one exception to the server's thinking-high posture (posture is a product decision hardcoded next to the model ids in `config.py`, deliberately not env-configurable). The prompt's reason-first key order (`task_type`/`reason` before `skill_ids`) is the inline reasoning step (same picks as thinking-high in benchmarks, ~1.5–2.5s vs a 4–14s+ tail). Output is validated and repaired once; on failure it **fails open** (degrades to the prior active set, never blocks the user).
4. **Resolve active set** (`resolve_active_skills`) — a **successful** selector run is authoritative: its ids become the complete active set (topic-shift replacement; an empty result clears it), capped at the limit. On an authoritative run each selected specific is **paired with its domain foundation** (`_pair_domain_foundations` + `Catalog.domain_foundation`, keyed on the id namespace minus its final segment) so a foundation loads on the SAME turn its specific is selected rather than a turn late — front-loading is deterministic, not left to the classifier; the paired foundation is appended after the explicit picks, so it only fills spare capacity under the cap. A no-run continuation round or a degraded selector reuses the prior set unchanged (no re-pairing — a prior set already carries its foundation, so continuation never surfaces a surprise load). (`merge_active_skills` stays additive for the explicit `resolve` `load` path only.)
5. **Assemble** the upstream request (`services/prompt_assembler.py`).
   Required structured `startup` data becomes one stable framed user-role block before the
   visible conversation; typed `runtime.worktree` and other live state stay in the final
   volatile user-role tail.
6. **Respond** — the skills/selection metadata is delivered first: as the response body's `skills` block (non-streaming) or the first SSE `meta` event (streaming). Streaming `meta` is yielded as soon as selection completes, **before** the upstream model is opened, so the CLI can surface a loaded skill during the remaining generation wait. Open/prefetch retries happen after metadata but before any model delta; an exhausted open or mid-stream failure emits a terminal `error` event, never a fabricated `done`.
7. **Skill learning** (`services/skill_learning.py`, local dev only — see below) — *after* the response, on a turn that finished with a **final answer** (no pending tool calls), the orchestrator fires the learner **fire-and-forget** (`_maybe_learn_after_turn`) with the just-generated answer appended to the transcript, so it learns from the workflow it actually completed. It may write/improve a skill and reload the catalog *in place*, live for the **next** turn (it never touches THIS response — already sent — so it adds no latency and is not folded in). A turn ending in a tool call (a continuation round) is skipped: the workflow isn't done. Off by default; fails open — a learning error can never break the turn.

**Invariants to preserve** (also in README): client `system`/`developer` messages are rejected; `skill.find`/`skill.load` are never offered to the main model; only active skill *bodies* are injected (never the full catalog); selector failure fails open; state is portable via the signed token; required structured `startup` context reaches the main model only — selector and learner evidence remains exactly `input.messages`.

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
- **Off by default** (`SKILL_LEARNING_MODE=off`) and **impossible in production**: an enabled mode is a `local_only_problems()` entry that fails `/readyz` when `ENVIRONMENT=production`.
- **Invisible to the CLI**: no new wire fields, no capabilities flag, no dev endpoints. The main assistant model is **never** told learning exists — nothing is appended to its prompt and it is offered no feedback tool. A created/improved skill simply becomes live in the catalog and appears — on the **next** relevant turn — in the existing `skills` block (`newly_loaded` + prelude) as an ordinary dynamic load. Because learning runs fire-and-forget *after* the response, the triggering turn is not slowed and shows nothing new.
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
- `main/{startup_context,runtime_context,integrations,loaded_skills,response_contract,turn_footer}.md.j2` — the structured main-prompt layers. `response_contract.md.j2` is order-critical: the golden suite asserts it renders as the **final system message**, after the base/integrations/active-skill systems and before the conversation.
- `selector/system.md.j2` — the skill-selector classifier prompt.
- `tasks/<task>.md` — utility-task system prompts.

**Two things that are easy to get wrong:**

- **Assembly order is load-bearing — and role choice IS position.** The DeepSeek chat template (unchanged by routing through OpenRouter — it is the model's template, not the API's) serializes [ALL system messages, in array order] → [tools] → [non-system conversation], so a volatile system message ANYWHERE in the array lands before the ~18k-token tool schemas and busts their cache (measured 2026-07-08: 36% hit on byte-identical turns → 99% after the fix). `PromptAssembler.build_main_request` therefore orders stable systems (base → integrations → active skills → response contract), then a stable **user-role** startup block, the append-only visible `input.messages`, and ONE trailing **user-role** "[Injected context …]" block carrying per-request runtime + turn state. Only the final tail re-encodes each round. Startup is required even when empty, stays outside the visible transcript, and is never sent to selector/learning calls. A skill-set change (selector topic shift) re-encodes tools + conversation — rare and accepted. Don't reorder casually, and never add a volatile system message. All client-provided local context is treated as untrusted: sanitized and byte-capped (`text_utils.py`, the `cap_*` settings in `config.py`); exact agent ids are JSON-quoted and rows are admitted whole.
- **Prompt bytes are behavior.** On a meaningful prompt change, regenerate golden snapshots (below) — that diff is the reviewable record. Do **not** bump version strings: `MAIN_PROMPT_VERSION` is a fixed family label (`"main"`), not a per-edit version (see "Versioning" below).

## Versioning (pre-release)

This service is **pre-release and running locally** — there is no deployed version to stay
compatible with. **This is not a versioned system.** Nothing embedded in it — prompts,
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

`config.py` `Settings` is parsed once, immutable, env-driven (`.env`). Models are **hardcoded** (`OPENROUTER_MODEL`, `SKILL_BUILDER_MODEL`/`SKILL_IMPROVER_MODEL` = `openai/gpt-5.6-sol`, `PUBLIC_MODEL_ID`) — a product decision, not config — and so are the **thinking posture** (`OPENROUTER_REASONING_EFFORT`, `SKILL_LEARNING_REASONING_EFFORT`, both module constants beside the model ids: main/utility/gate think-high, the selector is non-thinking) and the **endpoint-selection policy** (`_PROVIDER_ROUTING` in `providers/openrouter.py`). There are deliberately **no env toggles** for any of it; the server ships one optimum. Only the single OpenRouter key/base-URL comes from the environment. Secrets are `SecretStr`. Missing required secrets (and, in `production`, weak/default ones) **fail readiness** (`/readyz` → 503) rather than crashing the process — liveness (`/healthz`) stays green. Local-only features also gate readiness: `local_only_problems()` fails `/readyz` if skill learning is enabled in production. `main.py` builds the immutable service `Container` and validates it during lifespan startup; a malformed prompt or skill prevents readiness without taking down liveness.

## Conventions

- Python 3.12, `from __future__ import annotations` everywhere, `mypy --strict`, ruff (line length 100; rule set + ignores in `pyproject.toml`).
- Pydantic v2 for wire/config models; `extra="forbid"` and `frozen=True` are used deliberately (reject unknown fields, keep things immutable).
- Fail-open (selector) vs fail-closed (oversized body, malformed skill) choices are intentional — preserve the existing posture when editing those paths.
- Runtime deps are pinned in `requirements.lock` (a constraints file the Docker build and CI install against).
