# Local runbook selection — training pipeline

Everything here produces the selector the server ships in
`src/daintree_assistant_server/runbooks/local/artifacts/selector/`. None of it
runs in production, ships in the image, or is importable by the server.

The goal was latency, and that is settled: the prompt-based selector
(`runbooks/selector.py`) is a DeepSeek call on the turn's first-byte path
(measured p50 2.04s, p90 2.81s); the local selector answers the same question in
milliseconds. The goal **now** is scale — a catalog of 200 to 1,000 runbooks, with
runbooks appearing between builds.

**Read `EXPERIMENTS.md` first** — it pins the best-known configuration at the top
and records every architecture tried, including two that were built in full and
abandoned on evidence. Do not re-try things it already scores.

## The shipped architecture

```
                      ┌─ retriever encoder (MiniLM-L6, fine-tuned) ─┐
conversation ─────────┤                                             ├─► cosine vs
                      └─ ranking  encoder (bge-base,  fine-tuned) ──┘   runbook table
                                            │                              │
                                            │                        top-k candidates
                                            ▼                              │
                                     set-wise head  ◄──── cached runbook embeddings
                                            │
                    ┌───────────────────────┼────────────────────────┐
                k runbook logits         1 docs logit         4 cardinality logits
```

- **No stage owns per-runbook weights.** The head reads a runbook's *embedding*, so a
  runbook added after the build is embedded on catalog load and can be selected
  that turn — no retrain to unblock it, and no staleness gate degrading every
  OTHER turn open behind it. It is not yet as good as a trained runbook (4/14 vs
  13/14; see "Retraining when runbooks change" below), but the catalog keeps
  working, which is the part that used to break.
- **One current catalog.** Training and serving read `runbooks/catalog/`.
  Retired inbox examples are archived, excluded from compilation and refused if
  reintroduced. Archived evaluation text remains protected from leakage.
- **Catalog size does not reach the head.** It always sees `k` (32) candidates.
  Growth costs one table row (~4.5KB) and one cosine, not a wider head.
- **The shortlist's order is not information.** The head has no positional
  encoding, so it is permutation-equivariant; retrieval evidence arrives as
  per-candidate features (cosine, normalised rank) instead. `--ordering
  positional` is the measured alternative, not the default.
- The cardinality head makes "zero runbooks" a first-class prediction; there is no
  tuned threshold to go stale.
- Served as ONNX **int8** for the ranking encoder, **fp32** for the retriever
  (quantising it never gained a case and cost up to 6; fp32 costs 67MB more) and fp32 for
  the head (~2.4M parameters — 1% of the weights, so quantising it buys
  nothing). The export gate compares fp32 and int8 *decisions* over the whole
  benchmark, not logits: it fails when int8 changes the core decision on more than
  10 cases or moves recall-pass by more than 3.

## Environment

```bash
python3.12 -m venv .venv-train
.venv-train/bin/pip install -r training-requirements.txt
.venv-train/bin/pip install -e ".[dev]"
```

Training auto-selects MPS on Apple silicon (measured 5.7x faster than CPU for
training; inference stays CPU/ONNX regardless).

## The corpus is three planes

The training data is not one file. It is a raw plane, a curated plane and a
compiled artifact, and keeping them apart is what makes the corpus something you
can delete from.

```
training/journals/   RAW      what a generating agent returned. Append-only, committed.
training/corpus/     CURATED  hand-edited YAML, one file per runbook. SOURCE OF TRUTH.
training/data/       COMPILED selection.jsonl + corpus-lock.json. Generated; test-gated.
```

`training/corpus/README.md` is the authoring reference — read it before writing
examples. The short version: a row answering exactly one runbook lives in
`corpus/runbooks/<id>.yaml`, anything else in `corpus/shared/<source>.yaml`, and
`selection.jsonl` is a build output that nobody edits by hand.

This replaced a single `selection.jsonl` that was both the assembler's output
and its own input — `assemble_data.py` took the committed corpus as `--base` and
appended to it. Nothing could be removed by regenerating; provenance was one
flat `source` string; and re-running a generator grew the corpus instead of
reproducing it (the retired `augment_context.py` measured 9,903 rows becoming
10,405, and shipped a guard against its own re-run whose documented recovery was
`git show <commit>:…jsonl >`). Compiling is now a pure function: run it twice,
get the same bytes.

## Pipeline

```bash
# 1. Generate synthetic data (Workflow agents; see .claude/skills/retrain-selector
#    for prompts + schemas). Journals land under the session's workflows dir.

# 2. Fold the journal into the CURATED corpus, and archive the journal itself.
#    Idempotent: examples already present are skipped, so re-running is a no-op.
.venv-train/bin/python -m training.ingest_journal <journal.jsonl> --run <run-id>

#    Then edit training/corpus/**.yaml by hand — that is the point of the plane.
#    Delete what the generator got wrong; it will stay deleted.

# 2b. Compile. Drops benchmark collisions (reports the count), validates every id
#     against the live catalog, unpairs foundation labels, and writes the lockfile.
.venv-train/bin/python -m training.compile_corpus

# 3. Train stage 1, THEN stage 2. The order is load-bearing: stage 2 trains
#    against shortlists from a FROZEN stage 1, so retraining stage 1 afterwards
#    invalidates stage 2's training distribution.
.venv-train/bin/python -m training.train_retriever
.venv-train/bin/python -m training.train_setwise --epochs 10 --ordering equivariant

# 4. Export both encoders + the head to ONNX, with the decision-parity gate.
.venv-train/bin/python -m training.export_two_stage

# 5. Score the SHIPPED artifact under the SHIPPED policy (fp32 + int8 + diff),
#    and check the equivariance claim against the real graph.
.venv-train/bin/python -m training.eval_two_stage --ablate
```

## The one rule that matters

**Evaluation cases are excluded from training.** Generating agents must not read
`benchmarks/` or `tests/`. `compile_corpus` protects both
`benchmarks/runbook_selection/cases/` and every `holdout/` round, including sealed
rounds, by normalising every query and history line and dropping exact collisions.
The corpus guards assert that the curated sources contain none. Keep evaluation
wording frozen; remove collisions from training. Near-duplicates still need a
separate screening pass, including history, before new data lands.

The synthetic train/validation split groups normalised latest questions together,
including their history and active-set variants. Adding context must not place a
question already used for training into validation. This changes the split from
the pre-October recipe, so old synthetic validation scores are not comparable.
Thresholds are tuned on held-out *synthetic* splits only. After many inspect-iterate
cycles the benchmark has drifted toward
a dev set — before any final adoption claim, write a fresh ~50-case signoff set
blind.

## Re-embedding and retraining are different operations

Moving files or editing procedure bodies preserves routing and needs no new
weights. Editing title, summary or `whenToUse` invalidates that document's vectors;
serving re-embeds it on startup. Build a derived index explicitly with the runtime
venv, without PyTorch or a network call:

```bash
.venv/bin/python -m training.refresh_index --out .tmp/runbooks/vectors.npz
```

Caches are bound to the encoder graphs, tokenizers, document cap, rendering variant
and precision. A cache without that identity is ignored and rebuilt. The command
leaves `selector.lock` and the locked artifact alone. To package a refreshed cache,
include it in a newly published selector artifact and update the lock together;
never overwrite a locked file without its matching release metadata.

Retraining changes the encoders or head. It is warranted by measured retrieval or
ranking failures, new contested procedures, or a corpus correction whose lesson
must change in the weights. The set-wise head has no per-runbook parameters, so
adding a procedure remains searchable before training. Train stage 1 before stage 2
when both change; re-embedding cannot remove lessons already learned from retired
corpus rows. New training runs stamp the exact input corpus hash and row count in
both stages' metadata, carried through export. Historical weights without that
stamp keep unknown corpus provenance; do not fill it with today's corpus hash.

## Files

| file | role |
|---|---|
| `train_retriever.py` | stage 1: the contrastive bi-encoder that shortlists |
| `sweep_retriever.py` | how small stage 1 can be — trains each backbone and measures |
| `setwise_model.py` | stage 2's architecture, shared by trainer and exporter |
| `train_setwise.py` | stage 2: the set-wise head over a frozen shortlist |
| `export_two_stage.py` | ONNX export (both encoders + head) + decision-parity gate |
| `corpus_source.py` | the curated plane: schema, loader, writer |
| `ingest_journal.py` | raw journal → curated YAML; idempotent, archives the journal |
| `compile_corpus.py` | curated YAML → `selection.jsonl` + lockfile; every gate lives here |
| `prune_run.py` | removes every example a named generation run contributed |
| `eval_two_stage.py` | **canonical** eval: shipped artifact, shipped policy, fp32+int8+diff |
| `calibrate_floors.py` | re-derives the admission floors against a fresh export |
| `try_runbook_text.py` | scores a proposed whenToUse/summary edit — no retrain, no writes |
| `eval_zero_shot.py` | what a retrain actually bought, vs a both-stages holdout |
| `common.py` | wraps the SERVING renderer for training rows |
| `EXPERIMENTS.md` | **the log — read first** |

The abandoned pipelines (bi-encoder + **cross-encoder** reranker; hosted
embeddings) and the single fixed-head classifier that shipped before this one
were deleted after measurement — their scripts, modules and artifacts live in git
history, and every number they produced is transcribed in `EXPERIMENTS.md`.
