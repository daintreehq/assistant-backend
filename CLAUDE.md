# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this service is

**Daintree runbook search**: a stateless MCP server with one tool, `search_runbooks`, at
`POST /v1/daintree/mcp`. A caller describes a task; the server ranks the runbook catalog
against it and returns the top runbooks as JSON, full procedures included. That is all it
does. Anyone can add it — there is no authentication, no account, no session, no
credential held, and no upstream call. The wire contract is `docs/MCP.md`.

This repository used to be the Daintree Assistant Server (model orchestration, prompt
assembly, accounts, entitlement, bring-your-own-key, utility tasks). All of that was
retired and deleted; git history has it. **Do not reintroduce any of it incidentally** —
no provider adapters, no prompt layers, no auth modes, no state tokens.

**The main ongoing work is growing the runbook catalog** and keeping search good as it
grows. Real sessions are the main input: `/learn-from-transcript <path>` takes a Claude Code
or Codex transcript, measures what matched and what should have, and lands the lessons as
runbooks, cases and training rows on the current branch. It replaced the retired
`assistant-lab` missions; no issue or PR is involved.

## Layout

| path | what |
|---|---|
| `src/daintree_assistant_server/runbook_mcp.py` | JSON-RPC dispatch: both MCP protocol eras, the tool definition, argument validation, result rendering. Transport-agnostic. |
| `src/daintree_assistant_server/api/mcp.py` | The HTTP transport: Origin check, content negotiation, parse errors, 405 for GET/DELETE. |
| `src/daintree_assistant_server/api/health.py`, `api/root.py` | `/health`, `/healthz`, `/readyz`, `/version`, `/` redirect. |
| `src/daintree_assistant_server/main.py` | App factory and lifespan: loads the catalog and selector; body-size limit; request ids. |
| `src/daintree_assistant_server/config.py` | Tiny `Settings`: environment, log level, build sha, bind address, body cap. |
| `src/daintree_assistant_server/runbooks/` | Catalog, loader, frontmatter model, and `local/` — the selector. |
| `training/` | The selector's training pipeline, corpus and experiment log. |
| `benchmarks/runbook_selection/` | The routing benchmark and its quality gate. |
| `cli/` | Vendored Daintree CLI source (submodule), read by runbook hygiene tests. |
| `infra/gcp/`, `cloudbuild.yaml`, `Dockerfile` | The Cloud Run deployment. |

## Commands

```bash
# Setup
git lfs pull                             # the selector artifact is Git LFS
git submodule update --init cli
python3.12 -m venv .venv && source .venv/bin/activate
pip install -e ".[dev]" -c requirements.lock

# Test / lint / types (CI runs all of these on push + PR to main and develop)
pytest                                   # whole suite
pytest tests/unit/test_catalog.py -k <name>
ruff check .
mypy --strict src
python -m benchmarks.runbook_selection.bench coverage   # every runbook has a case requiring it
python -m benchmarks.runbook_selection.bench local      # shipped selector over every case, gated
python -m benchmarks.runbook_selection.bench search "<task>"   # one query, ranked as the MCP ranks it

# Run locally (settings from .env; see .env.example — nothing is required)
./dev
```

## How a search works

`LocalSelector.search` (`runbooks/local/selector.py`) renders the query as a lone user
message with nothing loaded (`documents.fit_query`, which shrinks deliberately to the
encoder's 256-token cap instead of letting the tokenizer cut the tail), then runs
`SetwiseClassifier.select` on the inference pool:

1. **Shortlist.** A fine-tuned bi-encoder embeds the query and takes the `k` (32) nearest
   runbooks by cosine from a table of embedded runbook documents.
2. **Set-wise head.** Reads the query embedding and those `k` runbook embeddings together
   and returns a probability per candidate, plus a 4-way cardinality head that makes
   "zero runbooks" a first-class answer, plus admission floors.

The search returns the head's **ranking** (up to the caller's `max_results`), and marks
the classifier's admitted set as `selected`. A caller asking for five gets five even
where the classifier would have loaded one or none.

**Neither stage owns per-runbook weights** — the head consumes runbook *embeddings*, not
runbook ids. So a runbook added to the catalog is embedded by `sync()` at startup and is
**searchable with no retrain**; measured, a new runbook wins requests only it plausibly
answers but loses contested ones to trained neighbours (`training/EXPERIMENTS.md`). A
retrain is what makes it *competitive*, not what makes it *reachable*. The head has no
positional encoding, so shortlist order carries no information; retrieval evidence reaches
it as per-candidate features (cosine, normalised rank). READ `training/EXPERIMENTS.md`
before re-trying any design idea — the obvious retrieve-then-cross-encoder design was
built, measured and abandoned there.

The classifier was trained on multi-turn conversations with an active runbook set (the
old assistant's turns), so it still understands a `[loaded: …]` query prefix, a per-candidate
`active` feature, candidate reservation and a docs head. Search uses none of those; they
remain because the training corpus, the benchmark and a future retrain all depend on the
same code (`runbooks/active_set.py`, `local/candidates.py`, `setwise.py`). Don't delete them
as dead code without deciding to retrain on bare queries.

`daintree.docs.answer-usage-question` is hidden from the selector (`catalog.DOCS_RUNBOOK_ID`):
it governed answering from documentation the old assistant retrieved, and it must never
come back from a search. It stays because the corpus and benchmark name it.

Failure posture: a search the pool refuses or the graph fails is `None`, which the tool
reports as a retryable `isError` result — never an empty ranking. A malformed runbook or a
missing/LFS-pointer artifact fails **readiness** (`/readyz` 503) without crashing liveness,
so Cloud Run never promotes it.

## Runbooks

**Where they live:** `src/daintree_assistant_server/runbooks/files/<id>.md` — one file per
runbook, **filename stem must equal the `id`** (enforced at load). Auto-discovered; no
registration. `docs/RUNBOOKS.md` is the authoring guide.

**Format** — YAML frontmatter + Markdown body. The body is operational prose in the house
style (when-to-use line, numbered procedure, "Report back" line). Frontmatter is validated
by `RunbookMeta` in `runbooks/models.py` (`extra="forbid"` — unknown keys are rejected):

| key | required | notes |
|---|---|---|
| `id` | yes | must match filename stem |
| `title` | yes | |
| `summary` | yes | routing signal; one line |
| `whenToUse` | yes | the *primary* routing signal (→ `when_to_use`) |
| `foundation` | no | `true` marks a domain foundation (base judgment for a dotted namespace) |
| `risk` | no | enum `read local ui terminal project external git system` — descriptive only |
| `requiredTools` | no | Daintree CLI tools the steps use (→ `required_tools`) |

The routing document the ranker embeds is title + summary + `whenToUse`, **not the body**.
Editing routing text re-embeds that runbook and must be measured with `bench local`.
Every result carries the full body under `instructions`, so body size is paid in the
calling agent's context on every hit (`tests/unit/test_runbook_budgets.py` holds the
ceilings).

**Runbooks name Daintree CLI tools**, and the vendored `cli/` is the authority on which
exist: `tests/unit/test_runbook_catalog_hygiene.py` checks every `requiredTools` name
against `tests/fixtures/cli_tool_names.txt`, and `test_runbook_examples.py` validates
tagged ```` ```json tool=<name> ```` examples against `tests/fixtures/cli_tool_inventory.json`.
The two fixtures are one capture; refresh them together (`tests/fixtures/README.md`).
The submodule tracks the CLI's `develop` and does not float — refresh the fixtures when
you move the pin.

**To add a runbook:**
1. Create `runbooks/files/<id>.md`.
2. **Write carve-outs by fully-qualified id** — "do not use for X, use
   `daintree.other.runbook`" — never a bare leaf or prose. `bench coverage` parses declared
   neighbours out of `whenToUse`, and the hygiene test fails a dotted reference that
   resolves to nothing. `bench lint` ranks the most confusable pairs.
3. Add a case under `benchmarks/runbook_selection/cases/` that REQUIRES it (and a routing
   trap if it declares neighbours). `bench coverage` exits non-zero otherwise.
4. Run `pytest tests/unit` (~5s). Do **not** narrow it with `-k`; concern-named filters
   silently deselect the loader tests.
5. Run `bench local`. Its floors are a ratchet; a new runbook that drops recall elsewhere
   is a routing problem to fix in the text or with a retrain, not a floor to loosen.

**Retraining** is a quality step (`.claude/skills/retrain-selector`). Retrains swing
several cases by seed, so judge a recipe over several seeds. `TODO.md` carries the
retrain-triggered entries (reservation retention, renamed routing text, `k=64` at ~200
runbooks, domain foundations).

## Versioning and compatibility

Pre-release. Nothing carries a version: runbooks have no `version` field, and change is
tracked by content hash (`Catalog.revision`, returned as `catalog_revision`). **Never
engineer for backward compatibility** — no shims, no deprecation paths, no parallel
versioned surfaces. The `/v1/daintree/` URL prefix is a shape escape hatch, not a version
claim. The build SHA (`BUILD_SHA`, reported by `/version`) is the only release version.

The MCP protocol versions in `runbook_mcp.py` are the exception: they are the external
protocol's revisions and are negotiated with clients, so they are kept current with the
MCP spec rather than frozen.

## CI

`.github/workflows/ci.yml`: install from lock, ruff, `mypy --strict src`, pytest with
coverage (`fail_under = 90`), `bench coverage`, `bench local`. CI checks out the `cli/`
submodule and the LFS artifact. `bench local` is the routing verdict: it attributes every
miss to the stage that lost it, and `candidate_miss` (a required runbook that never
reached the head) is gated at zero — it is the first number to move as the catalog
grows, and `bench scale` measures when.

## Deploying (Cloud Run)

Nothing is built or pushed from a dev machine. **Pushing to `main` is the deploy** — work
lands on `develop`, and a release fast-forwards `main`. `cloudbuild.yaml`: substitution
guard → LFS fetch → build → push → deploy a **no-traffic** revision tagged `rc-<build>`
(env replaced wholesale, `--clear-secrets`, readback verified) → `infra/gcp/smoke.sh`
against the tag's URL, including a real search → promote to 100% → `release-gate` retires
the tag and renders the verdict. The first-ever deploy of a service goes out at 100% behind
an identity-token `/readyz` check. Project `daintree-research`, region `us-central1`,
service `daintree-assistant`.

Use the **`/release` skill** rather than doing it by hand. `infra/gcp/bootstrap.sh` is the
idempotent one-time setup; making a new service public is a deliberate
`PUBLIC_CUTOVER=<service>` run, gated on the smoke test. `infra/gcp/rollback.sh` moves
traffic back. Cloud Run reserves `/healthz` externally — probe `/health` or `/readyz`.

## Conventions

- Python 3.12, `from __future__ import annotations` everywhere, `mypy --strict`, ruff (line
  length 100).
- Pydantic v2 for models; `extra="forbid"` and `frozen=True` are deliberate.
- Logs carry metadata, never content: a search query is user content and stays out of logs.
- Runtime deps are pinned in `requirements.lock` (a constraints file).
