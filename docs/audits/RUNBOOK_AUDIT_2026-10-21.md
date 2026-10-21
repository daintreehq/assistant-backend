# Runbook system audit and restructure — 2026-10-21

The inbox directory was an actively served compatibility overlay for the retired assistant messaging protocol. It was not a queue, a place to drop new runbooks, or a second authoring source. The restructure removes that parallel product model from serving, authoring, validation and training, and gives the current catalog a clear home.

The current system now has **140 public runbooks in 23 domain folders**, one catalog, one selector binding and one readiness revision. Daintree v0.44 inbox clients are no longer supported. Historical procedures, captures, evaluation cases and trial plans remain available outside the package.

This audit covers the backend's runtime and model pipeline, every current runbook through its structural and routing checks, and the Daintree integration paths that provision assistants, expose tools, control hosts and deliver agent notices. It includes a targeted review of the foundations and communication, ownership, host execution and recovery procedures. Offline checks cannot establish successful live execution of every procedure; no live desktop trial or production deployment was performed.

The machine-readable measurements are in [runbook-audit-2026-10-21.json](runbook-audit-2026-10-21.json).

## What the two directories actually meant

Before this change, `runbooks/files/` contained 141 files: 140 selectable procedures and a hidden, obsolete documentation stub. `runbooks/inbox/` contained 69 frozen overrides. The loader constructed a 155-runbook inbox catalog by replacing the current hosts namespace and selected shared procedures with those overrides.

Requests with `Daintree-Host-Control: 1` used the current catalog. Every other request, including a manually configured MCP connection with no header, used the inbox catalog. Startup embedded both catalogs. Health responses, benchmark qualification, schema fixtures, authoring instructions and the trial planner all maintained that split.

The current app is 0.45.0 and has already retired the inbox protocol. It controls a Remote Host with `host.call`; a host's assistant is addressed as an agent terminal, with a terminal id and generation. Keeping the backend's inbox model active made the newer protocol harder to maintain and made headerless clients default to obsolete instructions.

## System boundaries

| Component | Responsibility | Source of truth |
|---|---|---|
| Daintree help-session provisioning | MCP connections, runbooks-first instruction, caller tier, host-state snapshot | App `HelpSessionService.ts` and `helpSessionRunbooks.ts` |
| Daintree actions and MCP server | Actual operations, schemas, ownership, tier admission, confirmation and delivery outcomes | App action registry, `sessionServer.ts`, host runtime and their contract tests |
| Runbook service | Find and deliver procedures; expand instruction dependencies | Backend `runbook_mcp.py`, `runbooks/catalog.py` and Markdown catalog |
| Retrieval | Embed a task and shortlist 32 eligible procedures | Fine-tuned MiniLM encoder and current document vectors |
| Ranking | Score candidates together and admit up to three | BGE encoder, set-wise head and admission policy |
| Training corpus | Examples and authored confusion boundaries | Curated YAML; raw journals are historical evidence |
| Compiled data | Reproducible trainer input and corpus lock | `training.compile_corpus` |
| Model release | Exact binary identities and distribution | `selector.lock` and content-addressed artifact store |
| Deployment | Build, no-traffic candidate, smoke checks, promotion | Dockerfile, Cloud Build and `infra/gcp/smoke.sh` |

The service supplies instructions, not workspace access. Runbook metadata does not grant permission. Current host calls still use the app's tier, ownership and confirmation rules. A relayed call cannot chain into another `host.*` call. The general foundation and hosts foundation remain separate because those rules belong to different execution boundaries.

## Changes made

| Finding | Resolution |
|---|---|
| Two serving catalogs kept the retired messaging model alive | One current catalog and selector; inbox composition and model selection removed |
| Generic `files/` hid the authoring structure | Procedures moved to `runbooks/catalog/<domain>/<id>.md`; recursive discovery and packaging verified |
| Frozen overrides looked like active content | All 69 moved byte-for-byte to `archive/inbox-v0.44/runbooks/` |
| Headerless clients received inbox instructions | Every client receives the current catalog; host state only narrows search |
| Lane eligibility remained part of the current schema | Current eligibility is `any`, `configured` or `connected`; malformed and repeated host-state headers are unreported |
| Old routing and validation obligations followed the inbox catalog | 417 inbox-only cases archived; active benchmark retains the same 1,730 case objects |
| Training sources still taught lane enrollment, exchange records and removed APIs | 256 examples quarantined with origin, section, defaults and reason, including 27 found in follow-up; compiler rejects reintroduction |
| Retiring cases could accidentally permit evaluation leakage | Active, holdout and archived case text remains protected, including history and turn context, without loading an old catalog |
| Embedding caches identified precision but not the encoder | Cache identity now includes encoder graphs, tokenizer files, document cap and rendering variant; precision is checked separately |
| Re-embedding was entangled with the training/export workflow | `training.refresh_index` builds a derived cache using the runtime stack; `training.audit_catalog` reports drift |
| Model metadata did not identify its input corpus | Future retriever and set-wise runs capture the corpus hash and row count at load time; export preserves them |
| Captured schemas trailed the app | All seven caller surfaces refreshed from app commit `1006cbca719950c122d995c7b378138a3968aaa2` |
| Deployment smoke required an inbox readiness revision | Smoke now verifies headerless and app-configured requests against the same current revision |
| Active trial tooling described hub/lane messaging | Planner, register and scenarios archived; current help/relay evidence requirements documented |
| An obsolete docs stub claimed active data still required it | Stub archived under `archive/assistant-cli/`; the id remains reserved and unavailable |
| Client and authoring documentation described older capabilities | Backend guides, repository instructions and Daintree's assistant README updated |
| Local Docker context included training-only material | Training environment, training sources and archive excluded from Docker context |

No selectable id, title, summary, routing text or procedure body was changed by the folder migration. The selectable routing revision remains `routing:2a6b266aebae55c7eca30a75dd7f10a4`. Removing the hidden docs stub changes the whole-catalog content revision intentionally. Foundation pairing, explicit dependencies and their size bounds remain enforced.

## Catalog structure and consolidation decisions

| Domain | Runbooks | Main responsibility |
|---|---:|---|
| `core` | 2 | General foundation and diagnostics |
| `agents` | 12 | Questions, steering, handover, recovery, model choice and usage |
| `orchestration` | 30 | Local multi-agent workflows and run lifecycle |
| `hosts` | 33 | Placement, execution, coordination and recovery across machines |
| `git` | 11 | Branch integration, attribution and recovery |
| `forge` | 6 | Issues, PRs and release-note workflows |
| `workflow` | 5 | Issue-to-work and branch-to-review lifecycle |
| `worktree` | 6 | Checkout placement, environment and resource management |
| `review` | 6 | Reviews, audits and independent verification |
| `release` | 5 | Readiness, publication, deployment and rollback |
| `runtime` | 5 | Processes, previews, performance and databases |
| `ci` | 4 | Local checks, CI diagnosis and test execution |
| `context`, `debug`, `edits`, `terminals` | 2 each | Context, investigation, delegated edits and terminal operations |
| `deps`, `explore`, `memory`, `plugins`, `recipe`, `security`, `skills` | 1 each | Specific deliverables with their own acceptance conditions |

The local and host procedures often share a deliverable, but they have different callers and evidence paths. The closest retrieved pairs include local versus host timeboxes, undo, reprioritization and collision handling. Most already declare their relationship. Merging them solely because their titles resemble each other would hide who can execute the baseline and where paths, ownership and verification apply. The folders make those boundaries visible while preserving the working routing space.

One useful follow-up is a measured boundary review of `hosts.open-project-on-host` versus `hosts.verify-project-placement`: cosine similarity is 0.5231 and neither declares an id-level fence to the other. This is a routing hypothesis, not proof of redundant procedures. The audit leaves their text unchanged rather than tuning it against an inspected benchmark.

The document cap is now a practical constraint: median routing-document length is 370 tokens, the 95th percentile is about 504, and the maximum is 511 against a 512-token cap. Every document fits both shipped tokenizers, and an existing test catches truncation. New routing text needs careful budgeting; directory moves do not buy token capacity.

## What re-embedding revealed

The initial Mac comparison found that the original cache has no encoder fingerprint. For 138 documents whose text digest still matches, ranking embeddings rebuilt on Apple Silicon differ from the stored vectors by a median relative L2 of about **15.2%**, reaching **22.3%**. Retrieval-vector differences are below `8e-7` L2. Follow-up on Linux x86 established the cause: all 138 fresh ranking vectors match the old cache exactly. The dynamic-int8 inference kernels differ across architectures; this was not evidence of mismatched model weights. Cache identity now includes ONNX Runtime version, operating system, CPU architecture and the session optimization/threading contract as well as encoder/tokenizer bytes and rendering settings.

The new cache is rebuilt through the serving implementation, one document per encoder call. Loading that rebuilt cache and rebuilding it cold produce identical selected sets on all 1,730 active cases. The cache is keyed to the actual encoder/tokenizer bytes and rendering settings, so a future retrain cannot silently borrow vectors from another encoder of the same precision.

| Initial Mac measurement | Linux-built locked cache | Mac-rebuilt cache |
|---|---:|---:|
| Recall-pass | 1,684 / 1,730 (97.34%) | 1,675 / 1,730 (96.82%) |
| Strict set match | 1,564 / 1,730 (90.40%) | 1,565 / 1,730 (90.46%) |
| Required runbooks absent from retrieval shortlist | 0 | 0 |
| Forbidden procedure loads | 0 | 0 |
| Docs classification | 30 / 31 | 30 / 31 |
| Existing quality gate | Pass | Pass |

Fifty-four selected sets change in that cross-platform Mac comparison; one recall failure is repaired and ten are introduced. On Linux, the original cache and a fresh rebuild both pass recall on **1,683 / 1,730**, with **identical selected sets on every case**. Thus the reported nine-case cost did not reproduce on the production architecture. Mac and Linux results must be reported separately, and a cache from another inference runtime is rebuilt locally. No benchmark expectation or gate threshold was changed.

## Training decision

**There is no evidence that this restructure requires a different selector architecture or retrieval width.** Retrieval loses no required procedure on the active suite. File movement and body changes do not train a model, and re-embedding does not update weights.

There is evidence for a future controlled ranking/data refresh:

- The artifact's training labels cover 139 of the current 140 procedures. `daintree.agents.choose-agent-model` is the new label; it already passes 8 of its 10 required cases zero-shot, leaving two concrete misses.
- Its recorded training routing revision differs from the current one. Existing labels therefore do not establish that current routing text was present at training time.
- The curated corpus now has 21,559 rows after removing 256 obsolete-protocol examples and one evaluation near-collision. Removing source rows does not remove any lesson already learned by historical weights.
- The existing weights have no corpus fingerprint. Their precise relationship to today's curated data is unknown; that provenance cannot honestly be reconstructed by stamping the current hash onto them.
- Remaining misses are predominantly ranking and admission/cardinality failures. They do not justify changing a retriever that currently supplies all required candidates.

Keep the locked weights as the comparison baseline. The user wants further changes merged before retraining; training remains deferred until explicitly requested. For that future run, train a candidate against the cleaned corpus, preserve the retriever/head pairing, export through the same cache implementation and evaluate both current routing and an independently authored signoff set. If both stages are retrained, train retrieval first and freeze it before training the set-wise head. Do not remove the active-state features or docs head incidentally: production search uses a bare query, but the existing weights, corpus and evaluation machinery still share those contracts.

The complete exact/near leakage scan also found one existing usage-query near-collision
(similarity 0.811321) against protected evaluation text. That training row is quarantined
separately in `archive/training-audit-2026-10-21.jsonl`; no evaluation wording changed.
The final scan reports no exact or near overlaps above the existing 0.8 threshold.
Obsolete host-generation briefs in curated YAML headers now point to the current
procedure instead of teaching retired lane and exchange mechanics.

The initial restructure re-embedded unchanged weights. During follow-up, the assistant incorrectly treated the general completion request as authorization to retrain. Training and export finished with the unchanged recipe and seed 41, but model work was stopped when the user objected. The rented GPU is confirmed terminated. That candidate was never installed, published or deployed and did not change the committed model lock. It is not a qualified release artifact.

An additional 27 examples describing retired messaging settings or state were archived, leaving **21,559 rows**; the routing digest was refreshed to all 140 current procedures. The new 50-case signoff set was authored and frozen before candidate scoring, with no exact or near overlap above 0.8 against training or prior evaluation inputs. Its predeclared acceptance conditions are recorded alongside it. The unchanged model passes 46/50 on both Mac and Linux. No candidate result is claimed by this audit.

## Remaining operational limits

- Host-state headers describe the snapshot taken when the help session launches. The server reads them per request, but the CLI's configured snapshot can become stale until provisioning runs again. This audit documents that behavior rather than pretending search has live host state.
- Notices can be missed. Status reconciliation remains necessary, and neither a stop heuristic nor an agent's handback proves successful work.
- Current search submits a bare task, while historical training and evaluation also exercise active sets, conversation history and the docs head. Simplifying those learned contracts requires its own training and evaluation decision.
- Existing aggregate routing thresholds can permit individual regressions. Review comparisons on the same inference runtime as well as the passing gate; thresholds were not relaxed.

## Verification and release state

The archive integrity check reconstructs all **2,147 original case objects** from Git and compares them with the active/archive union. No id, message, history, expectation or other case value changed. All **69 frozen override files** also match their original bytes. Raw journals remain unchanged.

The app surface capture passed its capture test and **190 host-control contract tests**. Daintree's help-session runbook wiring passed **17 tests**. Backend verification covers catalog loading, dependencies, examples against both outer and relayed schemas, ownership/caller constraints, corpus compilation, cache identity, transport, readiness and deployment smoke behavior. An isolated wheel contains exactly 140 current Markdown procedures, contains no old `files/` or `inbox/` catalog, and loads independently of the checkout. The initial staged-artifact backend run passed **818 tests with 96.13% coverage**. Follow-up verification of the final code and original weights in an isolated worktree passed **820 tests with 96.14% coverage**, above the unchanged 90% floor. The isolated worktree used a matching, locally rebuilt cache and temporary lock; the primary checkout retained its original artifact and lock throughout follow-up. A local HTTP server passed **13 smoke checks**, including readiness, version, MCP initialization, search, get, and headerless/app-configured catalog consistency. Lint, strict source type checking, corpus reproducibility, exact/near leakage screening and shell syntax checks also pass.

**The committed `selector.lock` and installed locked artifact remain unchanged**, and artifact verification passes. Nothing was deployed or pushed by this work. An initial cache publication attempt failed because the CLI login needed reauthentication; follow-up verified application-default credentials, but no artifact was published. With this code, an unstamped cache or one from a different inference runtime is ignored and the current catalog is embedded at startup. Correctness does not depend on publishing a rebuilt cache first. Scratch caches made before the runtime-identity fix must be regenerated before any future publication; a Mac-generated cache must not be treated as a warm Linux cache. Model publication and deployment remain deferred.

For a fresh audit or re-embed:

```bash
.venv/bin/python -m training.audit_catalog
.venv/bin/python -m training.refresh_index --out .tmp/runbooks/vectors.npz
.venv/bin/python -m training.audit_catalog --index .tmp/runbooks/vectors.npz
.venv/bin/python -m benchmarks.runbook_selection.bench coverage
.venv/bin/python -m benchmarks.runbook_selection.bench local
```
