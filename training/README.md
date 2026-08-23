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
- **Catalog size does not reach the head.** It always sees `k` (32) candidates.
  Growth costs one table row (~4.5KB) and one cosine, not a wider head.
- **The shortlist's order is not information.** The head has no positional
  encoding, so it is permutation-equivariant; retrieval evidence arrives as
  per-candidate features (cosine, normalised rank) instead. `--ordering
  positional` is the measured alternative, not the default.
- The cardinality head makes "zero runbooks" a first-class prediction; there is no
  tuned threshold to go stale.
- Served as ONNX **int8** for both encoders and fp32 for the head (~2.4M
  parameters — 1% of the weights, so quantising it buys nothing). The export
  gate fails any export whose int8 changes a *decision*, not just its logits.

## Environment

```bash
python3.12 -m venv .venv-train
.venv-train/bin/pip install -r training-requirements.txt
.venv-train/bin/pip install -e ".[dev]"
```

Training auto-selects MPS on Apple silicon (measured 5.7x faster than CPU for
training; inference stays CPU/ONNX regardless).

## Pipeline

```bash
# 1. Generate synthetic data (Workflow agents; see .claude/skills/retrain-selector
#    for prompts + schemas). Journals land under the session's workflows dir.

# 2. Assemble + validate. Drops benchmark collisions (reports the count),
#    validates every id against the live catalog, unpairs foundation labels.
.venv-train/bin/python -m training.assemble_data --journal <journal.jsonl> [...]

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

**`benchmarks/runbook_selection/cases/` is held out.** Generating agents are
forbidden from reading it; `assemble_data` normalises every generated query and
drops collisions (8 caught so far); thresholds are tuned on held-out *synthetic*
splits only. After many inspect-iterate cycles the benchmark has drifted toward
a dev set — before any final adoption claim, write a fresh ~50-case signoff set
blind.

## Retraining when runbooks change

`.claude/skills/retrain-selector` is the runbook, and the first thing it says is
that **a retrain is no longer what makes a runbook selectable**. A new runbook is
embedded on load and can be selected that turn. What a retrain buys is that the
runbook becomes *competitive*: measured against a both-stages holdout, a
never-trained runbook takes 4 of 14 of its own benchmark cases against 13 of 14
trained, because it wins only the requests no neighbour also claims. If a new
runbook lands in a crowded neighbourhood, it needs data — in both directions.

## Files

| file | role |
|---|---|
| `train_retriever.py` | stage 1: the contrastive bi-encoder that shortlists |
| `sweep_retriever.py` | how small stage 1 can be — trains each backbone and measures |
| `setwise_model.py` | stage 2's architecture, shared by trainer and exporter |
| `train_setwise.py` | stage 2: the set-wise head over a frozen shortlist |
| `export_two_stage.py` | ONNX export (both encoders + head) + decision-parity gate |
| `assemble_data.py` | journals → validated corpus; contamination gate |
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
