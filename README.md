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
- **Upstream:** DeepSeek V4 Flash (OpenAI-format API spoken *internally*, behind a provider abstraction)
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
  providers/      # ChatProvider protocol + DeepSeek adapter + SSE parser
  prompts/        # Server-owned prompt text (.md / .md.j2) + loader
  skills/         # Skill models, loader, immutable catalog, selector, load events
  state/          # Signed skill-state token codec
  services/       # validation, prompt_assembler, chat_orchestrator, task_runner
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
