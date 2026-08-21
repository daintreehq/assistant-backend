# Daintree Assistant Server

A Daintree-operated model-orchestration service. It moves the orchestration
boundary off the local CLI: the server owns the permanent system prompt, the
skill catalog and bodies, automatic skill selection, prompt assembly, and the
small-model utility prompts. The CLI keeps local state and local tool execution.

**The server holds the upstream credential** (`CEREBRAS_API_KEY`) and funds every
turn from it, so a request needs no API key of its own. That is a stage, not a
destination: account authentication and sign-in are next, and `auth.py` is the
seam they land in.

It exposes a **Daintree-native** API (`POST /v1/daintree/respond`) — purpose-built,
not OpenAI-compatible. The session/skill envelope is first-class, skills are surfaced
as their own response block, and streaming uses named SSE events (`meta`/`delta`/`done`/
`error`). The full contract is in [`docs/DAINTREE_API.md`](docs/DAINTREE_API.md).

- **Runtime:** Python 3.12+, FastAPI, async `httpx`
- **Upstream:** Cerebras Inference (OpenAI-format API spoken *internally*, behind a provider abstraction) — `gemma-4-31b` for the main completion and every utility task; skill selection is LOCAL, an in-process two-stage ONNX selector making no upstream call. Cerebras is a model *host*, not a router: one endpoint per model, so there is no endpoint-selection policy and no per-request routing preference. Requests are not collected for training ([`docs/PROVIDER_POLICY.md`](docs/PROVIDER_POLICY.md) — a no-training guarantee, deliberately *not* a zero-retention claim <!-- privacy-copy-ok -->)
- **Target:** Google Cloud Run (stateless; signed state token carries the active skills)

## Authoritative documents

This README is a summary. Where it disagrees with one of these, the document wins:

| Document | Authority for |
|---|---|
| [`docs/INTERNAL_BETA.md`](docs/INTERNAL_BETA.md) | What the beta is: included/excluded scope, required tester disclosure, environments, invariants |
| [`docs/PROVIDER_POLICY.md`](docs/PROVIDER_POLICY.md) | Model ids, upstream wire shape, privacy posture, thinking posture, both retry layers, failure mapping |
| [`docs/KEY_HANDLING.md`](docs/KEY_HANDLING.md) | What a key is, how it moves, where it must never appear, verification, rotation |
| [`docs/DAINTREE_API.md`](docs/DAINTREE_API.md) | The wire contract |

`tests/unit/test_docs_drift.py` pins the highest-risk facts in those documents —
privacy wording, thinking posture, model ids, credential ownership, retry layers,
routing policy — against the code that implements them, so those postures cannot
change without the prose moving in the same commit. It is a targeted gate, not a
proof that the whole corpus is accurate.

## Key invariants

- Client `system`/`developer` messages are rejected (server owns all system text).
- Every respond request carries required structured `startup` context. Its curated project,
  agent-roster, and project-instruction data reaches only the main model in a stable framed
  block. The selector never sees `startup`: its evidence is sanitized
  `input.messages` plus bounded turn/workflow hints and tool names.
- `skill.find` / `skill.load` are never offered to the main model; selection runs
  server-side as a local two-stage forward pass before the main completion.
- Only the active skill bodies (max 3) are injected — never the full catalog.
- The selector does **not** re-run on ordinary tool-continuation rounds; it re-runs
  on a new turn, a mid-turn interjection, an invalid/missing state token, a catalog
  change, or `selection.force`.
- Selector failure fails *open* (keeps the prior active set) rather than blocking
  the assistant.
- The same selector call also classifies whether the turn is a question about
  Daintree itself. When it is, the server searches the public documentation MCP
  and folds the matching pages into the volatile prompt tail — the client is not
  handed a docs tool to run. Best-effort: any failure leaves the turn to answer
  without documentation, never to fail.
- Skill selection is LOCAL and two-stage: a bi-encoder shortlists the catalog by
  cosine, then a set-wise head judges those candidates together. No upstream
  call, no credential spent, and the turn's flow is strictly select → assemble →
  one main completion. A failing selector degrades open to the prior active set,
  never blocks the turn.
- Neither stage owns per-skill weights — the head reads skill *embeddings* — so a
  skill added to the catalog is embedded on load and reachable on the next turn,
  with no retrain and no degraded window. Measured, a never-trained skill wins
  requests only it plausibly answers and still loses contested ones to trained
  neighbours, so retraining is what makes a skill *competitive*, not what makes
  it *selectable*.
- Skill state is portable across instances via a signed, opaque token.
- Prompt order is cache-critical, and every row below the schemas is ordered widest-shared
  first: two static system blocks (base prompt → response contract) → tool schemas → user-role
  skill bodies → user-role integrations → stable startup user block → visible conversation →
  one volatile runtime/display/turn user tail. Runtime worktree state is typed and fresh per
  request; stable project/agent facts are not duplicated there.

## Layout

```
src/daintree_assistant_server/
  api/            # FastAPI routers (chat, models, capabilities, skills, tasks, health)
  contracts/      # Pydantic wire models (OpenAI subset, extension, tasks, errors)
  providers/      # ChatProvider protocol + Cerebras adapter + SSE parser
  prompts/        # Server-owned prompt text (.md / .md.j2) + loader
  skills/         # Skill models, loader, catalog + CatalogManager, selector, load events
  state/          # Signed skill-state token codec
  services/       # validation, prompt_assembler, chat_orchestrator, task_runner, docs_mcp
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

# Live provider-contract tests (skipped by default; make real Cerebras calls).
# Pins the things mocks can't see — which parameters Cerebras accepts and which
# it rejects with a 400, and that the serving model reports no reasoning tokens.
RUN_LIVE_CEREBRAS=1 CEREBRAS_API_KEY=csk-... pytest tests/live -q
```

CI (`.github/workflows/ci.yml`) runs ruff + `mypy --strict` + pytest on every
push/PR. Runtime dependencies are pinned in `requirements.lock` (a constraints
file the Docker build installs against for reproducibility).

### Required configuration

Set this (see `.env.example` for the full list):

```bash
export STATE_SIGNING_KEY_CURRENT=...      # HMAC key for state tokens
export CEREBRAS_API_KEY=...               # the upstream credential; funds every turn
```

Those two are the whole list, and both are the **server's own**. There is no
inbound API key to configure: callers send no bearer token today, so the door is
open — deliberately and temporarily, until account authentication lands. A token
that *is* sent is still validated for shape and used as that request's upstream
credential.

Missing required secrets do not crash the process — they fail **readiness**
(`/readyz` returns 503) so liveness stays green. `CEREBRAS_API_KEY` is gated
there precisely so its absence surfaces as "this deployment is not ready" rather
than as a first-turn upstream 401 that reads like the caller's fault.

### Run locally

```bash
# Copy the env scaffold; it binds HOST/PORT from .env (default 127.0.0.1:8473).
cp .env.example .env   # then set STATE_SIGNING_KEY_CURRENT and CEREBRAS_API_KEY
./dev                  # exports .env, then runs the server
```

`./dev` is the convenience wrapper: it exports `.env` into the environment (so a
`curl` typed in the same terminal agrees with the server), warns about missing
required secrets, and turns on human-readable logs. Plain
`python -m daintree_assistant_server` still works — `Settings` reads `.env`
either way.

Point your CLI at `http://127.0.0.1:8473` (or whatever `PORT` you set). For
auto-reload during development: `uvicorn daintree_assistant_server.main:app
--reload --port 8473`.

## Endpoints

| Method | Path | Auth | Purpose |
|---|---|---|---|
| `POST` | `/v1/daintree/respond` | yes | Native generation (+ first-class `skills` block; stream or JSON) |
| `GET`  | `/v1/daintree/capabilities` | yes | Protocol range, limits, stream events, task ids, catalog revision |
| `POST` | `/v1/daintree/auth/verify` | open | Credential probe: is the key this request would spend usable? (no body; no inference — a model listing). With no bearer that is the *server's* key, which answers "can this backend serve me?" |
| `GET`  | `/v1/daintree/skills` | yes | Skill metadata (ETag/304); never the body |
| `POST` | `/v1/daintree/skills/resolve` | yes | Manual find / load / clear |
| `POST` | `/v1/daintree/tasks` | yes | Named utility-model tasks |
| `GET`  | `/healthz` | no | Liveness |
| `GET`  | `/readyz` | no | Readiness (config, secrets, prompts, catalog, provider) |
| `GET`  | `/version` | no | Build SHA, version, protocol range |

Skill/selection metadata is delivered in the response body's `skills` block
(non-streaming) or as the first SSE `meta` event (streaming). The streaming event
is emitted as soon as selection completes — local selection makes that ~10ms in —
and always precedes model output.

## Deployment

The service runs on Cloud Run in `us-central1`, and **every push to `main`
deploys it**. Nothing is built on a developer machine — there is no local Docker
step in the release path.

```
push to main → Cloud Build trigger `deploy-main` → cloudbuild.yaml
   build the image (BUILD_SHA = commit sha)
   push to Artifact Registry
   deploy a no-traffic revision behind the `candidate` tag
   smoke-test it: /readyz and /version == the released commit
     (NOT /healthz: Cloud Run's frontend answers that path itself with an
      HTML 404 before the container sees it — /health is the reachable alias)
   promote to 100%
```

A revision that cannot answer `/readyz` never takes traffic, which is what turns
a bad secret, a malformed prompt template, or an unparseable skill file into a
red build instead of an outage. The commit SHA is the only version this system
has (see CLAUDE.md, "Versioning"): it tags the image, is baked in as
`BUILD_SHA`, and is what `/version` reports back.

One-time GCP setup — Artifact Registry, the two Secret Manager secrets, the
runtime and build service accounts, the GitHub connection, the trigger, and the
custom domain mapping — lives in `infra/gcp/bootstrap.sh`. It is idempotent, so
re-running it is how you finish setup rather than something to avoid:

```bash
./infra/gcp/bootstrap.sh
```

`.claude/skills/release` drives an actual release: it gates on GitHub CI being
green, merges to `main`, pushes, watches the build, and verifies that the live
`/version` reports the commit it just shipped.

`infra/gcp/smoke.sh` checks a deployed instance. By default it exercises only
the free surface — `/health`, `/readyz`, `/version`, and the 401 auth gate.
Export `OPENROUTER_API_KEY` to add the authenticated routes: the metadata reads,
the `/auth/verify` sign-in probe (free — a key introspection, not a model call),
the 426 protocol gate on both content endpoints, request-id echo, and the typed
error envelope. Pass `--full` to send two real model turns (streaming and
non-streaming), which spend money on that key:

```bash
./infra/gcp/smoke.sh
OPENROUTER_API_KEY=sk-or-v1-... ./infra/gcp/smoke.sh --full
./infra/gcp/smoke.sh --url https://assistant.daintree.org
```

`infra/gcp/rollback.sh` moves traffic back when a revision deploys cleanly and is
nonetheless **wrong** — a prompt change that degrades answers, a skill that routes
badly. The build gate cannot catch those: it only proves the candidate starts and
reports the right commit. Rolling forward again is the same command with the
revision you left.

```bash
./infra/gcp/rollback.sh --list        # ready revisions, with the serving one marked
./infra/gcp/rollback.sh --dry-run     # the plan, and stop
./infra/gcp/rollback.sh               # ask, then move traffic, then verify
./infra/gcp/rollback.sh --to daintree-assistant-00042-abc
```

It targets the revision *behind the one serving*, not simply the newest one that
is not serving — after a first rollback those differ, and the naive reading rolls
straight forward into the bad build. It verifies `/readyz` and `/version` after
the switch and exits non-zero if they disagree, because a traffic split that
applied while leaving the service unhealthy is the worst outcome: the operator
believes they have recovered and stops looking.

The deployment mounts state-signing material (`STATE_SIGNING_KEY_CURRENT`,
`STATE_SALT`) and the upstream credential (`CEREBRAS_API_KEY`), which funds every
turn. In production (`ENVIRONMENT=production`) the service fails readiness if a
secret is weak or default (the packaged `STATE_SALT`, a signing key under 32
characters), or if `CEREBRAS_BASE_URL` is not the real `https://api.cerebras.ai`
origin — the server key rides every request to that origin, so a typo'd or
plain-http host would leak the one credential all of Daintree runs on.

`infra/terraform/` remains a Terraform description of the same topology plus the
deferred external-load-balancer edge; `infra/gcp/bootstrap.sh` is what actually
built the running deployment.

## License

Proprietary — Daintree
