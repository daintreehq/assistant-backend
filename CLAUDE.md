# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this service is

**Daintree runbooks**: an MCP server at `POST /v1/daintree/mcp` with two read-only
tools. `search_runbooks` ranks the runbook catalog against a described task and returns the
top runbooks as JSON: selected ones with their full procedures, unselected neighbours by
id and title only; `get_runbook` returns one by id. Both add the prerequisite
instructions (domain foundation, plus `requires`) of each runbook they deliver in full,
once, as `supporting_runbooks` — and only once per MCP session: a later reply in the same
session names them with a note (`mcp_sessions.py`). That is all it does. Anyone can add
it — there is no authentication, no account, no credential held, and no upstream call.
The session record only saves bytes: no request depends on it. The wire contract is
`docs/MCP.md`.

**Who follows the runbooks:** Claude Code and Codex, acting through the **Daintree app's own
MCP** (`agent.launch`, `terminal.getStatus`, `terminal.notifyWhenIdle`, …) and their native
tools. This server supplies instructions only; it never touches a workspace. Asynchronous
runbooks launch or prompt an agent with `notify`, end the turn, and resume on the notice
Daintree types into the caller's pane. `docs/agents/DAINTREE_RUNBOOK_CLIENT.md` is the
host-side setup.

**The caller is the orchestration layer.** Runbooks are Daintree workflows: the caller
reads Daintree state, runs Daintree operations and verifies. The work itself (code, git,
environment, writing) goes to a visible agent in the right worktree, briefed with the
runbook's know-how for that work. The caller does it directly only when the user asks.
A proposed runbook with no Daintree role once framed this way does not belong in the
catalog (`docs/RUNBOOKS.md`).

This repository used to be the Daintree Assistant Server (model orchestration, prompt
assembly, accounts, entitlement, bring-your-own-key, utility tasks). All of that was
retired and deleted; git history has it. **Do not reintroduce any of it incidentally** —
no provider adapters, no prompt layers, no auth modes, no state tokens.

**The main ongoing work is growing the runbook catalog** and keeping search good as it
grows. Real sessions are the main input: `/learn-from-transcript <path>` takes a Claude Code
or Codex transcript, measures what matched and what should have, and lands the lessons as
runbooks, cases and training rows on the current branch. It replaced the retired
`assistant-lab` missions; no issue or PR is involved. Issue-driven work, a new runbook or an
improvement to one, goes through `/runbook <issue>` (`.claude/skills/runbook`): baseline,
measure, ground every action in the app, author, trial live in an isolated Daintree, review,
and a PR into `develop`. Runbook issues follow `references/issue-format.md` there.

## Layout

| path | what |
|---|---|
| `src/daintree_assistant_server/runbook_mcp.py` | JSON-RPC dispatch: both MCP protocol eras, the two tool definitions, argument validation, result rendering. Transport-agnostic. |
| `src/daintree_assistant_server/api/mcp.py` | The HTTP transport: Origin check, content negotiation, parse errors, session ids, 405 for GET/DELETE. |
| `src/daintree_assistant_server/mcp_sessions.py` | Per-session record of supporting runbooks already sent in full; bounded, and every gap errs toward resending. |
| `src/daintree_assistant_server/api/health.py`, `api/root.py` | `/health`, `/healthz`, `/readyz`, `/version`, `/` redirect. |
| `src/daintree_assistant_server/main.py` | App factory and lifespan: loads both catalogs and the selector; body-size limit; request ids. |
| `src/daintree_assistant_server/config.py` | Tiny `Settings`: environment, log level, build sha, bind address, body cap. |
| `src/daintree_assistant_server/runbooks/` | Catalog (incl. dependency validation and `supporting_for`), the per-model catalog pair (`load_catalogs`), loader, frontmatter model, `host_state.py` (the caller's headers), and `local/` — the selector. |
| `src/daintree_assistant_server/runbooks/files/`, `runbooks/inbox/` | The runbooks: `files/` is the control catalog, `inbox/` the frozen v0.44 copies the inbox catalog swaps in. |
| `training/` | The selector's training pipeline, corpus and experiment log. |
| `benchmarks/runbook_selection/` | The routing benchmark and its quality gate. |
| `tests/fixtures/daintree_mcp/` | The Daintree app's MCP surfaces per caller (pane, sidebar, what a host offers inside `host.call`, API-key), captured from one app commit: the authority on which actions runbooks may name. `inbox/` is the v0.44.0 capture, frozen with the inbox catalog. |
| `selector.lock`, `training/selector_store.py` | The selector's model files are not in Git: the lock names each by SHA-256 and the bucket holding it; the tool fetches, verifies, publishes and prunes. |
| `infra/gcp/`, `cloudbuild.yaml`, `Dockerfile` | The Cloud Run deployment. |

## Commands

```bash
# Setup
training/fetch_selector.sh               # the selector model, as selector.lock names it (needs gcloud)
python3.12 -m venv .venv && source .venv/bin/activate
pip install -e ".[dev]" -c requirements.lock

# Test / lint / types (the full check: run it locally; there is no hosted CI)
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
missing/corrupt artifact fails **readiness** (`/readyz` 503) without crashing liveness,
so Cloud Run never promotes it.

## Two catalogs, one per host model

Daintree drives Remote Hosts one of two ways, and both are installed: v0.44 messages a
host's assistant through an inbox (hub, lanes, tasks), and later builds control the host
directly (`host.call`) and delegate to its assistant as to any agent. The server serves
each its own catalog, picked per request by `Daintree-Host-Control` (`1`: control; absent
or anything else: inbox, failing closed) before search, `get_runbook`, supporting delivery
and `catalog_revision` (`runbooks/catalog.py`, `load_catalogs`; `docs/MCP.md`).
`runbooks/files/` is the control catalog and what every tool's `load_catalog()` reads. The
inbox catalog is `files/` minus the hosts namespace and whatever `inbox/` overrides, plus
`inbox/`: frozen v0.44 copies of every hosts runbook and of each shared runbook whose text
differs between the models (`test_catalogs.py` pins the directory). Shared runbooks keep
improving for both; copy one into `inbox/` before changing it in a way v0.44 cannot follow.
A control caller reporting `Daintree-Hosts: connected` gets `daintree.hosts.foundation`
with every runbook it is served in full. One classifier keeps a table per catalog; `bench
local` and `bench coverage` qualify each against its own suite (a case's `host_model`).

## Runbooks

**Where they live:** `src/daintree_assistant_server/runbooks/files/<id>.md` — one file per
runbook, **filename stem must equal the `id`** (enforced at load). Auto-discovered; no
registration. `docs/RUNBOOKS.md` is the authoring guide.

**Format** — YAML frontmatter + Markdown body. The body is operational prose in the house
style: an `On a host:` line naming where its steps run when the work's machine is a Remote
Host (there, per the hosts foundation; `gh`, naming the repo, from your own shell;
`test_runbook_host_execution.py`), then a numbered procedure (no `Use when:` or
`Availability:` line; the served document
already carries `when_to_use` and the tool lists), `On notice or resume:` and
`Stop and recovery:` sections for a runbook that ends its turn on a notice, ending with a
"Report back" line. Shared rules (caller differences, ownership, launching, batch calls,
`waitForReply` and notices, long-run checkpoints, proving a finish) live once,
in `daintree.foundation`, which both tools deliver with every runbook they deliver in full.
Anything Daintree's own assistant prompt already says stays out of it. Frontmatter is validated
by `RunbookMeta` in `runbooks/models.py` (`extra="forbid"` — unknown keys are rejected):

| key | required | notes |
|---|---|---|
| `id` | yes | must match filename stem |
| `title` | yes | |
| `summary` | yes | routing signal; one line |
| `whenToUse` | yes | the *primary* routing signal (→ `when_to_use`) |
| `foundation` | no | `true` marks a domain foundation (base judgment for a dotted namespace) |
| `risk` | no | enum `read local ui terminal project external git system` — descriptive only |
| `requiredTools` | no | Daintree app MCP action ids the baseline path calls; must be on the caller's surface: `pane-core`, or for `daintree.hosts.*` one of `help-core`/`help-full` (`CALLER_POLICY`) (→ `required_tools`) |
| `optionalTools` | no | action ids a conditional branch or caller variant calls (→ `optional_tools`); never also required |
| `requires` | no | other runbook ids whose instructions must be read first; validated for existence, visibility and cycles (the implicit foundation edge included), and delivered as `supporting_runbooks` |
| `hostEligibility` | `daintree.hosts.*` only, required there | `connected` `configured` `any` (`lane` in the inbox catalog only): the caller host state (`Daintree-Hosts` header, and `Daintree-Lane` for an inbox caller; `runbooks/host_state.py`) the runbook fits; search excludes it before the shortlist when the reported state does not fit, and an unreported caller is offered everything |

The routing document the ranker embeds is title + summary + `whenToUse`, **not the body**.
Editing routing text re-embeds that runbook and must be measured with `bench local`.
Every selected result carries the full body under `instructions`, plus the foundation (once
per session), and a caller that prints the whole result pays for both the text block and
`structuredContent`, so body size is paid twice on every hit
(`tests/unit/test_runbook_budgets.py` holds the ceilings).

**Runbooks name Daintree app MCP actions**, and `tests/fixtures/daintree_mcp/` is the
authority on which exist and what each caller is offered: an agent pane (owned tools such as
`terminal.sendCommandOwned`), the sidebar assistant (the unscoped `terminal.sendCommand`, and
alone `host.list`/`host.call`), what a Remote Host offers inside `host.call` (the relayed
surface: owned tools, no `host.*`), and an API-key client (no `notify`, no
`terminal.notifyWhenIdle`). A `host.call` example is validated against both the call's surface
and the relayed one of the same tier. The hygiene test checks
every declared tool against them, `test_runbook_examples.py` validates tagged
```` ```json tool=<action> [surface=<caller>] ```` examples against that caller's schema, and
`test_runbook_migration.py` fails any action-shaped name no caller is offered — which is
what keeps the retired Assistant CLI's tools (`agentTask.*`, `terminal.awaitAll`,
`queue.*`, `timer.*`, …) out. The captures come from one app commit; refresh them
together with `tests/fixtures/daintree_mcp/capture/capture.sh`. They prove what is listed,
not that a notice is delivered: only a live trial in a Daintree pane shows that.

**To add a runbook:**
1. Create `runbooks/files/<id>.md`.
2. **Write carve-outs by fully-qualified id** — "do not use for X, use
   `daintree.other.runbook`" — never a bare leaf or prose. `bench coverage` parses declared
   neighbours out of `whenToUse`, and the hygiene test fails a dotted reference that
   resolves to nothing. `bench lint` ranks the most confusable pairs.
3. Add a case under `benchmarks/runbook_selection/cases/` that REQUIRES it (and a routing
   trap if it declares neighbours). `bench coverage` exits non-zero otherwise.
4. Run `pytest tests/unit` (~2 min; the runbook test files alone take ~2s). Do **not**
   narrow it with `-k`; concern-named filters silently deselect the loader tests.
5. Run `bench local`. Its floors are a ratchet; a new runbook that drops recall elsewhere
   is a routing problem to fix in the text or a retrain to name as owed, never a floor to
   loosen.

**Retraining** is one command: `training/retrain.sh` trains, installs and gates the
new artifact (`.claude/skills/retrain-selector` for data generation and deeper work). It is
cheap, a few dollars on a rented Lambda GPU (about two hours on an A10), but **only the user
starts one**: they merge a batch of runbook work, then retrain once on `develop`. Never retrain
on a branch or inside a skill run, even to turn a red gate green; name it as owed
(`/retrain-selector <ids>`) in the PR and the report, with the cases it should win back. Retrains swing
several cases by seed, so judge a recipe over several seeds. `TODO.md` carries the
retrain-triggered entries (reservation retention, renamed routing text, `k=64` at ~200
runbooks, domain foundations).

**The model is not in Git.** The ONNX graphs, tokenizer vocabularies and `vectors.npz` live in a
private bucket (`gs://daintree-research-selector`), one object per distinct content, named by
SHA-256; `selector.lock` (committed) lists each file's path, hash and size, and is the model's
identity. `training/fetch_selector.sh` puts them in place (files that already match are left
alone, and downloads are cached in `~/.cache/daintree-selector`, so a worktree is a copy, not a
transfer); Cloud Build's `fetch-selector` step does the same and checks every byte. After a
retrain passes `bench local`, `python3 training/selector_store.py publish` uploads what the
bucket lacks and rewrites the lock: commit the lock, and nothing else about the model, with the
retrain. `test_selector_lock.py` fails a lock that disagrees with the files on disk.
`selector_store.py prune` lists (and with `--delete` removes) bucket objects no lock names,
keeping the current model, the three before it (read from the committed locks), what
`origin/main` names, and anything under a day old. The files under the selector directory are
git-ignored local copies; Git never holds a model version.

**Training on a remote machine.** Training is heavy (stage 1 re-embeds the whole catalog with
gradients every step), so prefer an idle Apple-silicon box over this one. Everything lives in a
throwaway directory there; only the exported artifact comes back. Over SSH (non-interactive
shells have no Homebrew on `PATH`, so export it):

```bash
H=studio-01.local                       # any Apple-silicon Mac you can ssh to
git push origin develop                 # the remote clones from GitHub, so push first
ssh $H 'export PATH=/opt/homebrew/bin:$PATH
  brew install python@3.12              # once per machine
  git clone --branch develop git@github.com:daintreehq/assistant-backend.git ~/tmp/daintree-retrain
  cd ~/tmp/daintree-retrain
  python3.12 -m venv .venv-train
  .venv-train/bin/pip install -r training/requirements-train.lock
  .venv-train/bin/pip install --no-deps -e .'
# train and export; detached so the ssh session can drop (hours on an M1 Max)
ssh $H 'export PATH=/opt/homebrew/bin:$PATH; cd ~/tmp/daintree-retrain; mkdir -p .tmp
  RETRIEVER_ARGS=--grad-checkpoint EXPORT_ONLY=1 nohup training/retrain.sh > .tmp/retrain.out 2>&1 &'
# when .tmp/retrain.out says "exported to .tmp/retrain/artifact": copy it back and gate here
rsync -a --delete $H:tmp/daintree-retrain/.tmp/retrain/artifact/ \
  src/daintree_assistant_server/runbooks/local/artifacts/selector/
.venv/bin/python -m benchmarks.runbook_selection.bench local   # the gate; restore the old artifact
                                                               # with training/fetch_selector.sh if it fails
```

`--grad-checkpoint` is required below ~64 GB of unified memory: the retriever's peak passed
42 GiB at 155 runbooks and fails with `MPS backend out of memory` on a 32-36 GB Mac. It is the
same recipe at lower peak memory and costs about 30% more step time. `EXPORT_ONLY=1` stops
after the export because the install-and-gate step needs the dev venv. Seeds and experiments run
the same way, one per machine, each in its own `.tmp/<tag>` (`--seed`, `--out`); delete the
checkout when done.

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

There is no hosted CI: GitHub runs nothing on a push or a PR. The full check runs locally
before work lands: install from lock, ruff, `mypy --strict src`, pytest with coverage
(`pytest -q --cov --cov-report=term:skip-covered`, `fail_under = 90`), `bench coverage`,
`bench local`. It needs the model fetched (`training/fetch_selector.sh`). It is not a release gate either (Cloud Build's
candidate smoke test guards production). `bench local` is the routing verdict: it attributes every
miss to the stage that lost it, and `candidate_miss` (a required runbook that never
reached the head) is gated at zero — it is the first number to move as the catalog
grows, and `bench scale` measures when.

## Deploying (Cloud Run)

Nothing is built or pushed from a dev machine. **Pushing to `main` is the deploy** — work
lands on `develop`, and a release fast-forwards `main`. `cloudbuild.yaml`: substitution
guard → model fetch by lock → build → push → deploy a **no-traffic** revision tagged `rc-<build>`
(env replaced wholesale, `--clear-secrets`, readback verified) → `infra/gcp/smoke.sh`
against the tag's URL, including a real search → promote to 100% → `release-gate` retires
the tag and renders the verdict. The first-ever deploy of a service goes out at 100% behind
an identity-token `/readyz` check. Project `daintree-research`, region `us-central1`,
service `daintree-assistant`.

**Where it is live:** `https://assistant.daintree.org`, a Cloud Run domain mapping onto
`daintree-assistant`, so the MCP endpoint is `https://assistant.daintree.org/v1/daintree/mcp`.
The raw Cloud Run URL (`https://daintree-assistant-aijjq62rdq-uc.a.run.app`) serves the same
revision. Every release reaches both; check the domain with
`./infra/gcp/smoke.sh --url https://assistant.daintree.org --expect-build-sha <sha>`.

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
