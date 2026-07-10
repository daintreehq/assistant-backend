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
- **Upstream:** DeepSeek (OpenAI-format API spoken *internally*, behind a provider abstraction) — `deepseek-v4-flash` for the main/selector/utility calls and the skill-learning gate; the local skill-learning builder/improver run on OpenAI `gpt-5.5` (a second provider)
- **Target:** Google Cloud Run (stateless; signed state token carries the active skills)

## Key invariants

- Client `system`/`developer` messages are rejected (server owns all system text).
- Every respond request carries required structured `startup` context. Its curated project,
  agent-roster, and project-instruction data reaches only the main model in a stable framed
  block; selector and skill-learning transcripts remain exactly `input.messages`.
- `skill.find` / `skill.load` are never offered to the main model; selection runs
  as a separate small-model call before the main completion.
- Only the active skill bodies (max 3) are injected — never the full catalog.
- The selector does **not** re-run on ordinary tool-continuation rounds; it re-runs
  on a new turn, a mid-turn interjection, an invalid/missing state token, a catalog
  change, or `selection.force`.
- Selector failure fails *open* (keeps the prior active set) rather than blocking
  the assistant.
- Streamed selector turns with verified, catalog-current active skills race the
  selector against a private main stream built from the exact ordered prior set.
  An exact resolved match reuses that stream; any set/revision change closes it
  and starts the authoritative prompt. Empty/untrusted state stays serial, and no
  speculative model event is exposed before the first `meta` event.
- Skill state is portable across instances via a signed, opaque token.
- Prompt order is cache-critical: stable systems/tools → stable startup user block → visible
  conversation → one volatile runtime/turn user tail. Runtime worktree state is typed and
  fresh per request; stable project/agent facts are not duplicated there.

## Layout

```
src/daintree_assistant_server/
  api/            # FastAPI routers (chat, models, capabilities, skills, tasks, health)
  contracts/      # Pydantic wire models (OpenAI subset, extension, tasks, errors)
  providers/      # ChatProvider protocol + DeepSeek adapter + SSE parser
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

# Live provider-contract tests (skipped by default; make real DeepSeek calls).
# Pins the things mocks can't see — e.g. that the selector's non-thinking
# json_object call succeeds upstream and doesn't degrade open.
RUN_LIVE_DEEPSEEK=1 DEEPSEEK_API_KEY=sk-... pytest tests/live -q
```

CI (`.github/workflows/ci.yml`) runs ruff + `mypy --strict` + pytest on every
push/PR. Runtime dependencies are pinned in `requirements.lock` (a constraints
file the Docker build installs against for reproducibility).

### Required configuration

Set these (see `.env.example` for the full list):

```bash
export DAINTREE_API_KEY=...               # public bearer key (dev)
export DEEPSEEK_API_KEY=...               # upstream provider key
export STATE_SIGNING_KEY_CURRENT=...      # HMAC key for state tokens
```

Missing required secrets do not crash the process — they fail **readiness**
(`/readyz` returns 503) so liveness stays green.

### Run locally

```bash
# Copy the env scaffold and add your DeepSeek key; it binds HOST/PORT from .env
# (default 127.0.0.1:8473). In development, leaving DAINTREE_API_KEY unset means
# unauthenticated access.
cp .env.example .env   # then edit DEEPSEEK_API_KEY (+ PORT if you like)
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

It is invisible to the CLI. The main assistant model is never told it exists.
Learning runs **off the response path, in the background, after** the turn's answer
is already sent, so the triggering turn is not slowed at all; the new/improved skill
simply shows up as an ordinary dynamic skill load on a **later** relevant turn. Keep
pointing your CLI at the same server — nothing about the wire protocol changes.

**How it works (fire-and-forget, after a turn completes with a final answer):**

1. A cheap **gate** (`deepseek-v4-flash`) reads the just-completed workflow and
   decides: nothing to learn / improve an existing skill / create a new one. Strong
   triggers are you signalling a problem ("what happened here?", "this shouldn't
   have happened") or the assistant visibly struggling; a cleanly completed,
   repeatable workflow can also justify a *new* skill.
2. If warranted, **OpenAI `gpt-5.5`** (high reasoning) first diagnoses the workflow — what
   the assistant (and the current skill, if improving) did that worked and where it failed —
   then writes the new skill or the improved one. The diagnosis is kept in the audit
   record so you can review *why* each change landed.
3. The backend validates it through the real skill loader (frontmatter, tool-name
   rules, size caps) plus a learning-layer gate (house-style checks + a secret
   scan), writes the `.md` file, and reloads the catalog. The created/improved
   skill is **never** folded into the turn it learned from (that answer is already
   sent) — it becomes live for the **next** relevant turn's selection.

**Enable it locally.** The builder/improver run on OpenAI `gpt-5.5`, so this mode
needs an `OPENAI_API_KEY` (readiness fails without one when learning is enabled).
The fastest way is to prefix the run command — no `.env` edit needed (a real env
var wins over `.env` anyway):

```bash
OPENAI_API_KEY=sk-... SKILL_LEARNING_MODE=apply python -m daintree_assistant_server
```

That is the everything-on-defaults mode: the gate (`deepseek-v4-flash`) runs after
each completed turn, the GPT-5.5 builder writes/improves a skill when warranted,
changes land in the real `skills/files/` (so they show up in `git diff`), and the
catalog reloads live. Point your CLI at the server as usual and just work. For a
safe dry run that never touches the skill files, swap `apply` for `propose`.

Or set it in `.env` to make it the default for every run:

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
  a unified diff, and the model's what-worked / what-failed diagnosis) under
  `.daintree/skill-learning/`. Watch the server logs for
  `skill_learning_applied` events. Learned skill changes do **not** touch the
  golden snapshots (those render against a pinned fixture catalog) — the test
  suite stays green; just review the `git diff` and commit what's good.
- Learning fires **after** a turn that ends in a final answer — not after tool-call
  continuation rounds (those aren't a finished workflow) — and runs in the
  background, so it never delays the response. Because it triggers on completion
  rather than on your *next* message, even the **last** workflow of a session is
  learned from; no throwaway follow-up is needed. What it writes lands for a later
  turn, never the one it learned from.

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
(non-streaming) or as the first SSE `meta` event (streaming). The streaming event
is emitted as soon as selection completes and always precedes model output. On an
eligible later turn, the backend may already have privately prefetched the matching
main stream, so useful output can follow immediately after `meta`.

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
