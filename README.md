# Daintree Assistant Server

A Daintree-operated model-orchestration service. It moves the orchestration
boundary off the local CLI: the server owns the permanent system prompt, the
runbook catalog and bodies, automatic runbook selection, prompt assembly, and the
small-model utility prompts. The CLI keeps local state and local tool execution.

**The server holds the upstream credential** (`BASETEN_API_KEY`) and funds every
turn from it, so a request needs no API key of its own. Account authentication is
built and switched OFF: `AUTH_MODE` selects `open` (the shipped default, under
which the `Authorization` header is not read for authentication at all), `observe`
(a present credential is verified and recorded, and nothing a caller can see
changes) or `enforce` (a verified credential is required). An account is an
ENTITLEMENT to spend Daintree's key, never a mapping to one of yours — and that is
now a check rather than only a description. `ENTITLEMENT_MODE` stages the billing
question on its own axis: `off` (the default) asks nothing, `observe` asks and
records the answer without ever acting on it, and only `enforce` refuses
`/respond` and `/tasks` for an account with no active plan. The question goes to
the Daintree website, which owns the billing relationship — this server stores no
subscription state. What still does not exist is a QUOTA: numeric plan
allowances are an undecided product question, so the server records what a turn
cost against a pseudonymous account and enforces no ceiling. Authentication bounds
WHO may spend Daintree's key, entitlement bounds WHETHER they may, and neither yet
bounds how much.

It exposes a **Daintree-native** API (`POST /v1/daintree/respond`) — purpose-built,
not OpenAI-compatible. The session/runbook envelope is first-class, runbooks are surfaced
as their own response block, and streaming uses named SSE events (`meta`/`status`/`delta`/
`compaction`/`done`/`error`). The full contract is in [`docs/DAINTREE_API.md`](docs/DAINTREE_API.md).

- **Runtime:** Python 3.12+, FastAPI, async `httpx`
- **Upstream:** two internal provider ROLES behind one public model, neither of them selectable by a caller. The **executor** is Baseten's Model APIs (OpenAI-format API spoken *internally*, behind a provider abstraction) — `zai-org/GLM-5.3-Flash` for the main completion and every utility task. It **cannot be run non-thinking** — reasoning is always on and an omitted `reasoning_effort` means `high` — so every call pins the `low` floor ([`docs/PROVIDER_POLICY.md` §2](docs/PROVIDER_POLICY.md)). A **fast-response** stage on Cerebras (`gpt-oss-120b`), which writes a short preamble concurrently with the executor, is **built but currently OFF** (`config.FAST_RESPONSE_ENABLED`) — the new executor answers fast enough that a line announcing work already underway is noise. While it is off, Cerebras receives nothing and `CEREBRAS_API_KEY` is not required; the executor's request is byte-identical either way. The executor also has a **second host**: when Baseten cannot answer, the same turn goes to OpenRouter against the same weights (`z-ai/glm-5.3-flash`) at the same reasoning floor, behind a circuit breaker that stops spending turns on a host that is down and re-probes it every few minutes ([`docs/PROVIDER_POLICY.md` §1a](docs/PROVIDER_POLICY.md)). `OPENROUTER_API_KEY` does not gate readiness — without it every turn is still answered and only the ability to survive an outage is lost.
- **Selection:** runbook selection is LOCAL, an in-process two-stage ONNX selector making no upstream call. Baseten is a model *host*, not a router: one endpoint per model, so there is no endpoint-selection policy and no per-request routing preference. (The fallback above does send one `provider` block to OpenRouter — `sort: latency` over a pool that genuinely exists — but only on the path that runs when Baseten has already failed, and a caller still cannot express a preference.) (The host-latency argument that pinned the PREVIOUS executor — the same weights via OpenRouter's default routing measured 3.1x slower, p50 3414ms against 1100ms — was about DeepSeek V4 Flash on Together and has not been re-run for this host.) **Data handling is currently UNVERIFIED for ALL THREE hosts** — nobody has read Baseten's, OpenRouter's or Cerebras's terms for the account tiers actually in use, so state nothing about how any of them retains, trains on, or deletes what it is sent. OpenRouter is the sharpest of the three, because it is itself a router: the endpoint that serves a failed-over turn has its own terms, and `sort: latency` means which one that is varies per request ([`docs/PROVIDER_POLICY.md` §4](docs/PROVIDER_POLICY.md))
- **Traces:** a restricted training-trace plane (`tracing/`) can store *exact* turn content — messages, tool arguments, the reply — for later model development. It is **off by default** and full-content capture is refused outright in production and staging. Operational logs stay metadata-only and are a separate plane entirely ([`docs/INTERNAL_BETA.md` §9](docs/INTERNAL_BETA.md))
- **Target:** Google Cloud Run (stateless; signed state token carries the active runbooks)

## Authoritative documents

This README is a summary. Where it disagrees with one of these, the document wins:

| Document | Authority for |
|---|---|
| [`docs/INTERNAL_BETA.md`](docs/INTERNAL_BETA.md) | What the beta is: included/excluded scope, required tester disclosure, environments, invariants |
| [`docs/PROVIDER_POLICY.md`](docs/PROVIDER_POLICY.md) | Model ids, upstream wire shape, privacy posture, thinking posture, both retry layers, failure mapping |
| [`docs/KEY_HANDLING.md`](docs/KEY_HANDLING.md) | What a key is, how it moves, where it must never appear, verification, rotation |
| [`docs/DAINTREE_API.md`](docs/DAINTREE_API.md) | The wire contract |
| [`docs/RUNBOOKS.md`](docs/RUNBOOKS.md) | Runbook authoring, contract sources, example validation and catalog checks |
| [`docs/RUNBOOK_AUDIT.md`](docs/RUNBOOK_AUDIT.md) | September 2026 review of all 39 runbooks and remaining runtime limitations |
| [`docs/CONTEXT_COMPILER.md`](docs/CONTEXT_COMPILER.md) | **Future work, not built.** The query-aware context-compiler design, assessed against what already exists here |

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
- `runbook.find` / `runbook.load` are never offered to the main model; selection runs
  server-side as a local two-stage forward pass before the main completion.
- Only the active runbook bodies (max 3) are injected — never the full catalog.
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
- Runbook selection is LOCAL and two-stage: a bi-encoder shortlists the catalog by
  cosine, then a set-wise head judges those candidates together. No upstream
  call, no credential spent, and the turn's flow is strictly select → assemble →
  one main completion. A failing selector degrades open to the prior active set,
  never blocks the turn.
- Neither stage owns per-runbook weights — the head reads runbook *embeddings* — so a
  runbook added to the catalog is embedded on load and reachable on the next turn,
  with no retrain and no degraded window. Measured, a never-trained runbook wins
  requests only it plausibly answers and still loses contested ones to trained
  neighbours, so retraining is what makes a runbook *competitive*, not what makes
  it *selectable*.
- Runbook state is portable across instances via a signed, opaque token.
- Prompt order is cache-critical, and every row below the schemas is ordered widest-shared
  first: two static system blocks (base prompt → response contract) → tool schemas → user-role
  runbook bodies → user-role integrations → stable startup user block → visible conversation →
  one volatile runtime/display/turn user tail. Runtime worktree state is typed and fresh per
  request; stable project/agent facts are not duplicated there.

## Layout

```
src/daintree_assistant_server/
  api/            # FastAPI routers (chat, models, capabilities, runbooks, tasks, health)
  contracts/      # Pydantic wire models (OpenAI subset, extension, tasks, errors)
  providers/      # ChatProvider protocol + Baseten/OpenRouter/Cerebras adapters, executor failover, SSE parser
  prompts/        # Server-owned prompt text (.md / .md.j2) + loader
  runbooks/         # Runbook models, loader, catalog + CatalogManager, selector, load events
  state/          # Signed runbook-state token codec
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

# Live provider-contract tests: NONE ship today. The suite that existed pinned
# the Cerebras wire and was deleted with that adapter, so what Baseten accepts
# and what it 400s are unpinned (see TODO.md). One thing IS now known by hand:
# Baseten reports no `usage.cost` at all, so every cost figure is null.
```

CI (`.github/workflows/ci.yml`) runs ruff + `mypy --strict` + pytest on every
push/PR. Runtime dependencies are pinned in `requirements.lock` (a constraints
file the Docker build installs against for reproducibility).

### Required configuration

Set this (see `.env.example` for the full list):

```bash
export STATE_SIGNING_KEY_CURRENT=...      # HMAC key for state tokens
export BASETEN_API_KEY=...               # the upstream credential; funds every turn
export CEREBRAS_API_KEY=...               # the fast-response stage, which has no off switch
```

Those three are the whole list, and all three are the **server's own**. There is no
inbound API key to configure — account authentication is OAuth, and its settings
(issuer, JWKS, client id, and the website's entitlement URL) are public
configuration rather than secrets, which is why they gate readiness through
`auth_problems()` and `entitlement_problems()` instead of appearing here.

Under the shipped `AUTH_MODE=open` the `Authorization` header is **not read for
authentication** — not parsed, not validated, not rejected. That is stronger than
"a key is optional", and it is what makes an old client work rather than fail
confusingly: a CLI still sending the OpenRouter key it used to authenticate with
would otherwise have that key forwarded upstream and rejected as *theirs*. (The
header IS read for redaction in every mode, so a mis-pasted credential cannot be
logged back to you.)

Missing required secrets do not crash the process — they fail **readiness**
(`/readyz` returns 503) so liveness stays green. `BASETEN_API_KEY` is gated
there precisely so its absence surfaces as "this deployment is not ready" rather
than as a first-turn upstream 401 that reads like the caller's fault.

### Run locally

```bash
# Copy the env scaffold; it binds HOST/PORT from .env (default 127.0.0.1:8473).
cp .env.example .env   # then set STATE_SIGNING_KEY_CURRENT, BASETEN_API_KEY
                       # and CEREBRAS_API_KEY
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
| `POST` | `/v1/daintree/respond` | yes | Native generation (+ first-class `runbooks` block; stream or JSON) |
| `GET`  | `/v1/daintree/capabilities` | yes | Protocol range, limits, stream events, task ids, catalog revision |
| `POST` | `/v1/daintree/auth/verify` | yes | Credential probe: is the key this request would spend usable? (no body; no inference — a key introspection). That key is always the *server's* own, so this answers "can this backend serve me?" rather than anything about the caller. |
| `GET` | `/v1/daintree/account` | yes† | Who you are and what your plan permits. A missing or inactive subscription is 200 data here; it becomes a typed 402 only on a paid route. |
| `GET`  | `/v1/daintree/runbooks` | yes | Runbook metadata (ETag/304); never the body |
| `POST` | `/v1/daintree/runbooks/resolve` | yes | Manual find / load / clear |
| `POST` | `/v1/daintree/tasks` | yes | Named utility-model tasks |
| `GET`  | `/healthz` | no | Liveness |
| `GET`  | `/readyz` | no | Readiness (config, secrets, prompts, catalog, provider) |
| `GET`  | `/version` | no | Build SHA, version, protocol range |

† `/v1/daintree/account` needs a genuinely authenticated principal, not merely the
auth dependency: under `open` and `observe` every caller resolves to the anonymous
one, so it answers `401 auth_required` there. A client should use it once
`/v1/daintree/auth/config` reports `required: true`.

Runbook/selection metadata is delivered in the response body's `runbooks` block
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
   deploy a no-traffic revision behind a tag unique to this build
     (derived from the Cloud Build id, so two overlapping releases can
      never record, smoke, promote or delete each other's revision)
   smoke-test it with infra/gcp/smoke.sh, told what to expect: the released
     commit, the environment, and both posture modes
     (NOT /healthz: Cloud Run's frontend answers that path itself with an
      HTML 404 before the container sees it — /health is the reachable alias)
   promote to 100%
```

A revision that cannot answer `/readyz` never takes traffic, which is what turns
a bad secret, a malformed prompt template, or an unparseable runbook file into a
red build instead of an outage. The commit SHA is the only version this system
has (see CLAUDE.md, "Versioning"): it tags the image, is baked in as
`BUILD_SHA`, and is what `/version` reports back.

One-time GCP setup — Artifact Registry, the four Secret Manager secrets, the
runtime and build service accounts, the GitHub connection, the trigger, and the
custom domain mapping — lives in `infra/gcp/bootstrap.sh`. It is idempotent, so
re-running it is how you finish setup rather than something to avoid:

```bash
./infra/gcp/bootstrap.sh
```

`.claude/skills/release` drives an actual release: it gates on GitHub CI being
green, merges to `main`, pushes, watches the build, and verifies that the live
`/version` reports the commit it just shipped.

`infra/gcp/smoke.sh` checks a deployed instance, and **it is also the
pre-promotion gate** — `cloudbuild.yaml`'s `smoke` step runs this same script
against the no-traffic candidate and refuses to promote it on a non-zero exit.
That is deliberately one definition rather than two: the build step used to inline
a weaker copy of a couple of these checks, and the copy nobody ran by hand was the
one gating production.

**It needs no credential and accepts none.** The server holds the upstream key
(`BASETEN_API_KEY`) and funds every turn from it, so the metadata reads, the
`/auth/verify` sign-in probe (free — a key introspection, not a model call), the
426 protocol gate on both content endpoints, request-id echo and the typed error
envelope answer an anonymous request — under `AUTH_MODE=enforce` the checks that
need a signed-in caller are reported as skipped rather than guessed at, because
this script has no way to obtain a user token and deliberately none to be handed
one. Pass `--full` to send two real model
turns (streaming and non-streaming), which spend **Daintree's** money, not yours.

The `--expect-*` flags turn a survey into an assertion. Without them the script
adapts to whatever it finds, which is what makes it useful against an unknown
host and exactly wrong as a release gate:

```bash
./infra/gcp/smoke.sh
./infra/gcp/smoke.sh --full
./infra/gcp/smoke.sh --url https://assistant.daintree.org
./infra/gcp/smoke.sh --url https://assistant.daintree.org \
  --expect-env staging --expect-posture observe --expect-entitlement off \
  --expect-build-sha "$(git rev-parse HEAD)"
```

`infra/gcp/rollback.sh` moves traffic back when a revision deploys cleanly and is
nonetheless **wrong** — a prompt change that degrades answers, a runbook that routes
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
`STATE_SALT`) and both upstream credentials — `BASETEN_API_KEY`, which funds
every turn, and `CEREBRAS_API_KEY`, which funds the fast-response preamble. Four
Secret Manager secrets, all server-owned; `bootstrap.sh` creates and grants all
four. In production (`ENVIRONMENT=production`) the service fails readiness if a
secret is weak or default (the packaged `STATE_SALT`, a signing key under 32
characters), or if `BASETEN_BASE_URL` is not the real `https://inference.baseten.co`
origin — the server key rides every request to that origin, so a typo'd or
plain-http host would leak the one credential all of Daintree runs on.

`infra/terraform/` remains a Terraform description of the same topology plus the
deferred external-load-balancer edge; `infra/gcp/bootstrap.sh` is what actually
built the running deployment.

## License

Proprietary — Daintree
