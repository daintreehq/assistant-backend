# Daintree Assistant Server

A Daintree-operated model-orchestration service. It moves the orchestration
boundary off the local CLI: the server owns the permanent system prompt, the
skill catalog and bodies, automatic skill selection, prompt assembly, the
small-model utility prompts, and upstream credentials. The CLI keeps local
state and local tool execution.

It exposes a **Daintree-native** API (`POST /v1/daintree/respond`) — purpose-built,
not OpenAI-compatible. The session/skill envelope is first-class, skills are surfaced
as their own response block, and streaming uses named SSE events (`meta`/`delta`/`done`/
`error`). The full contract is in [`docs/DAINTREE_API.md`](docs/DAINTREE_API.md).

- **Runtime:** Python 3.12+, FastAPI, async `httpx`
- **Upstream:** Fireworks (OpenAI-format API spoken *internally*, behind a provider abstraction) — `deepseek-v4-flash` for the main/selector/utility calls, `glm-5p2` for the local skill-learning builder/improver
- **Target:** Google Cloud Run (stateless; signed state token carries the active skills)

## Key invariants

- Client `system`/`developer` messages are rejected (server owns all system text).
- `skill.find` / `skill.load` are never offered to the main model; selection runs
  as a separate small-model call before the main completion.
- Only the active skill bodies (max 3) are injected — never the full catalog.
- The selector does **not** re-run on ordinary tool-continuation rounds; it re-runs
  on a new turn, a mid-turn interjection, an invalid/missing state token, a catalog
  change, or `selection.force`.
- Selector failure fails *open* (keeps the prior active set) rather than blocking
  the assistant.
- Skill state is portable across instances via a signed, opaque token.

## Layout

```
src/daintree_assistant_server/
  api/            # FastAPI routers (chat, models, capabilities, skills, tasks, health)
  contracts/      # Pydantic wire models (OpenAI subset, extension, tasks, errors)
  providers/      # ChatProvider protocol + Fireworks adapter + SSE parser
  prompts/        # Server-owned prompt text (.md / .md.j2) + loader (incl. skill_learning/)
  skills/         # Skill models, loader, catalog + CatalogManager, selector, load events
  state/          # Signed skill-state token codec
  services/       # validation, prompt_assembler, chat_orchestrator, task_runner, skill_learning
  config.py auth.py errors.py observability.py main.py
tests/            # unit / integration / contract / golden
infra/terraform/  # Cloud Run + load balancer skeleton
```

## Development

```bash
python3.12 -m venv .venv && source .venv/bin/activate
pip install -e ".[dev]"

# Tests, lint, types
pytest
ruff check src/ tests/
mypy src/

# Regenerate prompt golden snapshots after an intentional change
DAINTREE_UPDATE_GOLDEN=1 pytest tests/golden/

# Live provider-contract tests (skipped by default; make real Fireworks calls).
# Pins the things mocks can't see — e.g. the selector's forced tool_choice.
RUN_LIVE_FIREWORKS=1 FIREWORKS_API_KEY=fw_... pytest tests/live -q
```

CI (`.github/workflows/ci.yml`) runs ruff + `mypy --strict` + pytest on every
push/PR. Runtime dependencies are pinned in `requirements.lock` (a constraints
file the Docker build installs against for reproducibility).

### Required configuration

Set these (see `.env.example` for the full list):

```bash
export DAINTREE_API_KEY=...               # public bearer key (dev)
export FIREWORKS_API_KEY=...              # upstream provider key
export STATE_SIGNING_KEY_CURRENT=...      # HMAC key for state tokens
```

Missing required secrets do not crash the process — they fail **readiness**
(`/readyz` returns 503) so liveness stays green.

### Run locally

```bash
# Copy the env scaffold and add your Fireworks key; it binds HOST/PORT from .env
# (default 127.0.0.1:8473). In development, leaving DAINTREE_API_KEY unset means
# unauthenticated access.
cp .env.example .env   # then edit FIREWORKS_API_KEY (+ PORT if you like)
python -m daintree_assistant_server
```

Point your CLI at `http://127.0.0.1:8473` (or whatever `PORT` you set). For
auto-reload during development: `uvicorn daintree_assistant_server.main:app
--reload --port 8473`.

## Skill learning (local development only)

The server can **learn skills from your own sessions**: after a workflow, a
backend-internal pipeline can write a brand-new skill or improve an existing one,
so the catalog gets better as you use it. It is **off by default** and **cannot run
in production** (an enabled mode fails `/readyz` when `ENVIRONMENT=production`).

It is invisible to the CLI. The main assistant model is never told it exists; the
CLI just sees a slightly slower turn when a skill is being generated, and then the
new/improved skill shows up as an ordinary dynamic skill load. Keep pointing your
CLI at the same server — nothing about the wire protocol changes.

**How it works (per turn, at a genuine new-instruction boundary):**

1. A cheap **gate** (`deepseek-v4-flash`) reads the just-completed workflow and
   decides: nothing to learn / improve an existing skill / create a new one. Strong
   triggers are you signalling a problem ("what happened here?", "this shouldn't
   have happened") or the assistant visibly struggling; a cleanly completed,
   repeatable workflow can also justify a *new* skill.
2. If warranted, **GLM 5.2** writes the new skill body or the improved one.
3. The backend validates it through the real skill loader (frontmatter, house
   style, tool-name rules, size caps, secret scan), writes the `.md` file, and
   reloads the catalog. A created skill is used **this same turn**.

**Enable it locally** (in `.env`):

```bash
SKILL_LEARNING_MODE=apply        # off (default) | propose (dry-run) | apply (writes files)
# SKILL_LEARNING_DIR=            # empty = edit the real skills/files/ (git-diff-able); or
                                 # point at e.g. .daintree/skills to keep source untouched
```

- **`apply`** rewrites/creates real skill files under `src/.../skills/files/` (the
  default), so improvements show up in `git diff` — review them and commit the good
  ones. Bad ones are just `git checkout`-ed away.
- **`propose`** runs the whole pipeline but writes only the proposed skill markdown
  + an audit JSON under `.daintree/skill-learning/proposals/` (gitignored) — a safe
  dry run that never touches real skill files or the catalog.
- Every applied change also drops an audit record (old/new hash, gate verdict,
  diff) under `.daintree/skill-learning/`. Watch the server logs for
  `skill_learning_applied` events. Changed skills change the catalog revision, so
  regenerate golden snapshots before committing (`DAINTREE_UPDATE_GOLDEN=1 pytest
  tests/golden/`).

## Endpoints

| Method | Path | Auth | Purpose |
|---|---|---|---|
| `POST` | `/v1/daintree/respond` | yes | Native generation (+ first-class `skills` block; stream or JSON) |
| `GET`  | `/v1/daintree/capabilities` | yes | Protocol range, limits, stream events, task profiles, catalog revision |
| `GET`  | `/v1/daintree/skills` | yes | Skill metadata (ETag/304); never the body |
| `POST` | `/v1/daintree/skills/resolve` | yes | Manual find / load / clear |
| `POST` | `/v1/daintree/tasks` | yes | Named utility-model tasks |
| `GET`  | `/healthz` | no | Liveness |
| `GET`  | `/readyz` | no | Readiness (config, secrets, prompts, catalog, provider) |
| `GET`  | `/version` | no | Build SHA, version, protocol range |

Skill/selection metadata is delivered in the response body's `skills` block
(non-streaming) or as the first SSE `meta` event (streaming), before any token.

## Deployment

A multi-stage `Dockerfile` produces a minimal non-root image listening on
`$PORT`. `infra/terraform/` is **staging-first**: the defaults expose the direct
Cloud Run URL (`INGRESS_TRAFFIC_ALL`, app bearer auth as the gate) for smoke
testing, with the production external load balancer / managed cert / DNS / Cloud
Armor edge documented as a deferred step. Secrets come from Secret Manager and
are never committed to Terraform state. See `infra/terraform/README.md` for the
staging and production paths and a curl smoke-test script.

In production (`ENVIRONMENT=production`) the service fails readiness if a secret
is weak or default (e.g. the default `STATE_SALT`, a short signing key).

## License

Proprietary — Daintree
