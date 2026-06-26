# Daintree Assistant Server

A Daintree-operated model-orchestration service. It moves the orchestration
boundary off the local CLI: the server owns the permanent system prompt, the
skill catalog and bodies, automatic skill selection, prompt assembly, the
small-model utility prompts, and upstream credentials. The CLI keeps local
state and local tool execution.

It exposes a largely **OpenAI-compatible** Chat Completions API plus a
negotiated **Daintree extension** (request fields + response metadata). A
generic OpenAI client can talk to it and ignore the extension; a Daintree-aware
client uses the extension for skill-state continuity and the skill-load prelude.

- **Runtime:** Python 3.12+, FastAPI, async `httpx`
- **Upstream:** DeepSeek V4 Flash (OpenAI-format API), behind a provider abstraction
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
uvicorn daintree_assistant_server.main:app --reload --port 8080
```

## Endpoints

| Method | Path | Auth | Purpose |
|---|---|---|---|
| `POST` | `/v1/chat/completions` | yes | Main OpenAI-compatible completion (+ Daintree extension) |
| `GET`  | `/v1/models` | yes | Advertised model alias |
| `GET`  | `/v1/daintree/capabilities` | yes | Protocol, limits, task profiles, catalog revision |
| `GET`  | `/v1/daintree/skills` | yes | Skill metadata (ETag/304); never the body |
| `POST` | `/v1/daintree/skills/resolve` | yes | Manual find / load / clear |
| `POST` | `/v1/daintree/tasks` | yes | Named utility-model tasks |
| `GET`  | `/healthz` | no | Liveness |
| `GET`  | `/readyz` | no | Readiness (config, secrets, prompts, catalog, provider) |
| `GET`  | `/version` | no | Build SHA, version, protocol range |

Daintree response metadata is delivered in the `X-Daintree-Meta` header
(base64url JSON, available before the first streamed token) and, for
non-streaming responses, also in a top-level `daintree` field.

## Deployment

A multi-stage `Dockerfile` produces a minimal non-root image listening on
`$PORT`. The recommended production edge is a global external Application Load
Balancer with a serverless NEG to Cloud Run, a Google-managed TLS certificate,
and Cloud Armor. Secrets come from Secret Manager. See `infra/terraform/` for a
skeleton; secret values are never committed to Terraform state.

## License

Proprietary — Daintree
