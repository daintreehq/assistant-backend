# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this service is

The Daintree Assistant Server moves the model-orchestration boundary off the local CLI and onto the server. **The server owns** the permanent system prompt, the skill catalog and bodies, automatic skill selection, prompt assembly, the small-model utility prompts, and upstream credentials. **The CLI owns** local state and local tool execution.

The public API is **Daintree-native** (`POST /v1/daintree/respond`), not OpenAI-compatible — the session/skill envelope is first-class and skills are their own response block. The main upstream is **DeepSeek** (OpenAI wire shape, spoken *internally* behind the `ChatProvider` abstraction in `providers/`): `deepseek-v4-flash` for the main/selector/utility calls and the skill-learning gate. The local skill-learning **builder/improver run on OpenAI `gpt-5.5`** (a stronger reasoning model for the hard "write a whole skill" task, via a second `ChatProvider` in `providers/openai.py`) — all model ids hardcoded. The full wire contract is `docs/DAINTREE_API.md`; code comments cite it as "section X.Y" — read it when changing wire shapes or selector/task behavior.

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

# Live DeepSeek contract tests (skipped by default; make real API calls)
RUN_LIVE_DEEPSEEK=1 DEEPSEEK_API_KEY=sk-... pytest tests/live -q

# Run locally (binds HOST/PORT from .env; default 127.0.0.1:8473)
cp .env.example .env   # then set DEEPSEEK_API_KEY
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
3. **Skill selection** (`skills/selector.py`) — a separate **small-model** call classifies the conversation against the catalog and returns ≤ `max_active_skills` (default 3) IDs via a forced `select_skills` tool call. Output is validated and repaired once; on failure it **fails open** (degrades to the prior active set, never blocks the user).
4. **Resolve active set** (`resolve_active_skills`) — a **successful** selector run is authoritative: its ids become the complete active set (topic-shift replacement; an empty result clears it), capped at the limit. A no-run continuation round or a degraded selector reuses the prior set unchanged. (`merge_active_skills` stays additive for the explicit `resolve` `load` path only.)
5. **Assemble** the upstream request (`services/prompt_assembler.py`) and call the provider.
6. **Respond** — the skills/selection metadata is delivered first: as the response body's `skills` block (non-streaming) or the first SSE `meta` event (streaming), before any token. Retries only happen *before* any public bytes are emitted; a mid-stream failure emits a terminal `error` event, never a fabricated `done`.
7. **Skill learning** (`services/skill_learning.py`, local dev only — see below) — *after* the response, on a turn that finished with a **final answer** (no pending tool calls), the orchestrator fires the learner **fire-and-forget** (`_maybe_learn_after_turn`) with the just-generated answer appended to the transcript, so it learns from the workflow it actually completed. It may write/improve a skill and reload the catalog *in place*, live for the **next** turn (it never touches THIS response — already sent — so it adds no latency and is not folded in). A turn ending in a tool call (a continuation round) is skipped: the workflow isn't done. Off by default; fails open — a learning error can never break the turn.

**Invariants to preserve** (also in README): client `system`/`developer` messages are rejected; `skill.find`/`skill.load` are never offered to the main model; only active skill *bodies* are injected (never the full catalog); selector failure fails open; state is portable via the signed token.

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
| `tags` | no | lowercased, deduped |
| `priority` | no | 0–1000 |
| `risk` | no | enum: `read local ui terminal project external git system` |
| `requiredTools` | no | camelCase key → `required_tools` |

**Pipeline:** `loader.py` parses/validates (strict — a malformed skill fails `/readyz`, not a request) → `catalog.py` builds the **immutable** catalog and a deterministic `revision` hash → the selector sees only `Skill.selector_view()`, the public `/skills` endpoint sees only `public_view()`, and **neither ever exposes the body** except via injection into the main prompt for active skills. The main model is explicitly told (in `base.md`) not to emit `skill.find`/`skill.load` — selection is automatic and server-side.

**To add a skill:**
1. Create `skills/files/<id>.md` (it is auto-discovered, sorted by id — no registration).
2. Run `pytest tests/unit/test_skills_loader.py tests/unit/test_catalog.py`.
3. **Run the golden tests** (`pytest tests/golden/`). The prompt snapshots render against a **pinned fixture catalog** (`tests/golden/fixtures/skills/`), not the live one, so adding or changing a real skill no longer churns `selector_system.md` / `loaded_skills_*` — only editing a prompt template or the assembler does. A structural smoke test (`test_selector_prompt_renders_real_catalog`) still verifies the real catalog renders through the selector prompt.

## Skill learning (local development only)

A backend-internal harness (`services/skill_learning.py`) that watches completed local workflows and, when warranted, **writes a new skill or improves an existing one** so the catalog gets better as you use it. It is the newest area of ongoing work alongside skills/prompts.

**Hard boundaries** (do not weaken):
- **Off by default** (`SKILL_LEARNING_MODE=off`) and **impossible in production**: an enabled mode is a `local_only_problems()` entry that fails `/readyz` when `ENVIRONMENT=production`.
- **Invisible to the CLI**: no new wire fields, no capabilities flag, no dev endpoints. The main assistant model is **never** told learning exists — nothing is appended to its prompt and it is offered no feedback tool. A created/improved skill simply becomes live in the catalog and appears — on the **next** relevant turn — in the existing `skills` block (`newly_loaded` + prelude) as an ordinary dynamic load. Because learning runs fire-and-forget *after* the response, the triggering turn is not slowed and shows nothing new.
- **Fails open**: `SkillLearner.run` swallows every exception and returns a no-op. Learning must never break `/respond`.
- The **skill loader is the authority for disk**: a proposal is rendered to markdown and run through `parse_skill(expected_id=...)`; it only lands on disk if that (plus an id-format guard, a path-containment check, and a conservative secret scan) passes.

**Pipeline** (fire-and-forget *after* a turn that completed with a final answer — `_maybe_learn_after_turn` in the orchestrator; a turn ending in a tool call is a continuation round and is skipped, so learning only ever sees a finished workflow — including the last one of a session, which no next instruction would ever trigger):
1. **Gate** (`prompts/skill_learning/gate.md`, small model, non-thinking JSON) reads the whole workflow transcript (with the just-generated final answer appended) and returns none / improve / create. Strong triggers: the user signalling a problem ("what happened here?", "this shouldn't have happened") or the assistant visibly struggling; a cleanly completed repeatable workflow can justify a *create*. Only a `general_daintree_workflow` lesson above `SKILL_LEARNING_MIN_CONFIDENCE`, backed by concrete evidence, writes.
2. **Build/improve** (`build.md` / `improve.md`, OpenAI GPT-5.5, high reasoning) diagnoses what worked and what failed in the workflow, then returns a `SkillProposal`; the diagnosis plus a unified diff land in the audit record. The prompts require **generic, project- and agent-agnostic** skills — no hardcoded repo/path/CLI-agent; a step that needs an agent tells the model to pick the best-fit one from the configured-agents roster it sees at runtime — because the catalog must transfer across projects, users, and CLI agents.
3. **Validate → write → reload**: atomic write + reload as one cancellation-shielded unit (`CatalogManager.reload()`, rollback on failure). The reloaded catalog is live for the next turn's selection; nothing is folded into the just-answered turn (its response is already sent).

**Modes** (`SKILL_LEARNING_MODE`): `off` | `propose` (run the pipeline, write only a proposal + audit JSON under `SKILL_LEARNING_AUDIT_DIR`, never touch skill files) | `apply` (write the skill file + reload).

**Write target** (`SKILL_LEARNING_DIR`): empty → the packaged `skills/files/` (edit-in-place; improvements land in `git diff`). Set to a separate dir to overlay instead — the catalog then loads packaged + overlay (`load_catalog(primary, overlay)`), overlay winning on id collision, leaving the source tree untouched.

**Prompts have no version suffix** (`gate.md`, not `gate.v1.md`) — pre-release internal prompts, unlike wire-contract `tasks/<task>.v1.md`. They are snapshotted in `tests/golden/` like every other prompt. Because apply-mode writes real skill files, an interactive session will dirty the catalog (`git diff` under `skills/files/`) — but **not** the golden snapshots, which render against a pinned fixture catalog. Regenerate goldens only when you change a prompt template or the assembler.

## Prompts / system messages

All prompt text is **server-owned** and lives in `src/daintree_assistant_server/prompts/` (`.md` = raw bodies, `.md.j2` = Jinja templates), loaded via `prompts/loader.py`:

- `main/base.md` — the permanent assistant system prompt (persona + hard rules). This is the highest-leverage file in the repo.
- `main/{runtime_context,integrations,loaded_skills,turn_footer}.md.j2` — the dynamic main-prompt layers.
- `selector/system.md.j2` — the skill-selector classifier prompt.
- `tasks/<task>.v1.md` — utility-task system prompts.

**Two things that are easy to get wrong:**

- **Assembly order is load-bearing.** `PromptAssembler.build_main_request` keeps the large **append-only conversation** in the provider's cached prefix (base prompt → integrations → conversation), then appends the small per-request tail — **active skill bodies → runtime → turn footer** — *after* it. The active skill set changes by wholesale replacement on a topic shift, so the bodies sit behind the conversation: a skill load/evict then re-encodes only that bounded tail, never the conversation (a mutable block in front of the conversation is the one layout prefix caching can't tolerate). It also lands the runbook near generation, which the small model follows better. Don't reorder casually. All client-provided local context is treated as untrusted: sanitized and byte-capped (`text_utils.py`, the `cap_*` settings in `config.py`).
- **Prompt bytes are behavior.** On a meaningful prompt change, regenerate golden snapshots (below) — that diff is the reviewable record. Do **not** bump version strings: `MAIN_PROMPT_VERSION`/`SELECTOR_PROMPT_VERSION` are fixed family labels (`"main"`/`"selector"`), not per-edit versions (see "Versioning" below).

## Versioning (pre-release)

This service is **pre-release and running locally** — there is no deployed version to stay
compatible with. Treat everything on disk as simply "the current version." **Do not add
version numbers, SemVer, or date stamps to prompts, skills, or prompt-family labels, and do
not bump them on a change.** Edit freely and let the golden-snapshot diff be the record.

What this means concretely:

- **Skills** have **no `version` field** — frontmatter carries `id`/`title`/`summary`/etc.
  only. Change busting rides on the skill content hash (which feeds `catalog_revision`), not
  a hand-bumped number.
- **Prompt-family labels** (`MAIN_PROMPT_VERSION` / `SELECTOR_PROMPT_VERSION` in
  `prompts/loader.py`) are fixed names — `"main"` and `"selector"` — surfaced as
  `prompt_version`. They identify *which* prompt, not a revision; leave them alone.
- The **only `v1` that stays** is the wire identifier: the API URL path (`/v1/daintree/...`)
  and task identifiers (`<task>.v1`, e.g. `terminal_summarize.v1`, with their matching
  `tasks/<task>.v1.md` prompt files). Those are part of the request contract — keep them.
- Separately, **build/protocol versioning is not in scope here and stays as-is**: the
  `/version` endpoint, `server_version`, `protocol_version` / `X-Daintree-Protocol`, and the
  signed-state-token format version (`TOKEN_VERSION`) are real wire/build metadata, unrelated
  to prompt churn.

If real release versioning is ever wanted, that is a deliberate, separate decision — not
something to reintroduce incidentally.

## Golden tests

`tests/golden/` snapshots rendered prompts and the fully-assembled upstream message array into `tests/golden/snapshots/`. Any intentional change to a **prompt template or the assembler** will fail these until you regenerate with `DAINTREE_UPDATE_GOLDEN=1 pytest tests/golden/` — treat the resulting snapshot diff as the human-reviewable record of what the model will actually see. The catalog-dependent snapshots render against a **pinned fixture catalog** (`tests/golden/fixtures/skills/`), deliberately decoupled from the live catalog so the self-evolving skill files (skill learning) never redden the suite; `test_selector_prompt_renders_real_catalog` keeps a structural check over the real catalog.

## Utility tasks (`POST /v1/daintree/tasks`)

`services/task_runner.py` runs named small-model tasks (checkpoint, memory distill, watcher classify, terminal summarize/extract/judge, etc.). Each is a server-owned `TaskProfile` that picks the prompt, output mode (`json`/`text`), temperature, and token cap; clients send only task *data* (never `messages`/`system`). JSON tasks get one repair retry on parse failure. `terminal_extract_json.v1` is the only task that accepts a client `result_schema` (validated for size/depth/keys by `services/result_schema.py`). Adding a task = new `TaskProfile` + input/output models in `contracts/tasks.py` + a `tasks/<id>.md` prompt + a golden snapshot.

## Config & readiness

`config.py` `Settings` is parsed once, immutable, env-driven (`.env`). Models are **hardcoded** (`DEEPSEEK_MODEL`, `SKILL_BUILDER_MODEL`/`SKILL_IMPROVER_MODEL` = `gpt-5.5`, `PUBLIC_MODEL_ID`) — a product decision, not config; only the provider keys/base-URLs (DeepSeek, plus OpenAI for skill learning) come from the environment. Secrets are `SecretStr`. Missing required secrets (and, in `production`, weak/default ones) **fail readiness** (`/readyz` → 503) rather than crashing the process — liveness (`/healthz`) stays green. Local-only features also gate readiness: `local_only_problems()` fails `/readyz` if skill learning is enabled in production, or if it is enabled without `OPENAI_API_KEY` (which its GPT-5.5 builder/improver need). `main.py` builds the immutable service `Container` and validates it during lifespan startup; a malformed prompt or skill prevents readiness without taking down liveness.

## Conventions

- Python 3.12, `from __future__ import annotations` everywhere, `mypy --strict`, ruff (line length 100; rule set + ignores in `pyproject.toml`).
- Pydantic v2 for wire/config models; `extra="forbid"` and `frozen=True` are used deliberately (reject unknown fields, keep things immutable).
- Fail-open (selector) vs fail-closed (oversized body, malformed skill) choices are intentional — preserve the existing posture when editing those paths.
- Runtime deps are pinned in `requirements.lock` (a constraints file the Docker build and CI install against).
