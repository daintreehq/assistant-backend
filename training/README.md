# Local skill selection — training pipeline

Everything here produces the classifier the server ships in
`src/daintree_assistant_server/skills/local/artifacts/classifier/`. None of it
runs in production, ships in the image, or is importable by the server.

The goal is **latency**. The prompt-based selector (`skills/selector.py`) is a
DeepSeek call on the turn's first-byte path: measured p50 2.04s, p90 2.81s.
The local classifier answers the same question in ~9ms.

**Read `EXPERIMENTS.md` first** — it pins the best-known configuration at the
top and records every architecture tried, including the retrieve-then-rerank
design that was built in full and abandoned on evidence. Do not re-try things
it already scores.

## The shipped architecture

```
conversation text ──► bge-base encoder (109M, fine-tuned) ──► pooled vector
                                          │
                    ┌─────────────────────┼─────────────────────┐
              32 skill logits        1 docs logit        4 cardinality logits
               (which skills)      (retrieve docs?)       (how many: 0–3)
```

- The skill head's weight matrix **is** the skill embeddings — one row per
  skill. Adding a skill = one new row + retrain (~2 min).
- The cardinality head makes "zero skills" a first-class prediction; there is no
  tuned threshold to go stale.
- Conversation state (already-loaded skills) enters as a `[loaded: …]` text
  prefix — measured to be the only thing that resolves continuation vs
  topic-shift.
- Served as ONNX **int8** (300MB RSS; fp32 is 665MB and does not fit the memory
  budget). The export gate (`export_classifier.py`) fails any export whose int8
  changes a *decision* — same top skill, same cardinality — not just its logits.

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

# 3. Train (the shipped recipe; see EXPERIMENTS.md header).
.venv-train/bin/python -m training.train_classifier \
    --epochs 10 --lr 5e-5 --batch-size 48 --balance-power 0.0 \
    --base-model BAAI/bge-base-en-v1.5 --seed 41

# 4. Export to ONNX with the decision-parity gate.
.venv-train/bin/python -m training.export_classifier

# 5. Score the SHIPPED artifact under the SHIPPED policy (fp32 + int8 + diff).
.venv-train/bin/python -m training.eval_classifier
```

## The one rule that matters

**`benchmarks/skill_selection/cases/` is held out.** Generating agents are
forbidden from reading it; `assemble_data` normalises every generated query and
drops collisions (8 caught so far); thresholds are tuned on held-out *synthetic*
splits only. After many inspect-iterate cycles the benchmark has drifted toward
a dev set — before any final adoption claim, write a fresh ~50-case signoff set
blind.

## Retraining when skills change

`/.claude/skills/retrain-selector` is the runbook. Short version: the classifier
head has one row per skill, so a new skill cannot be selected until a retrain;
the intended workflow is batched (accumulate 10–20 skills, regenerate their
data, retrain once). The trainer stamps `routing_revision` — a hash of every
selectable skill's id/title/summary/whenToUse — into the artifact, and
`LocalSelector` refuses to serve on mismatch (degrading open), so a stale head
fails safe instead of silently. Body-only skill edits do not move it.

## Files

| file | role |
|---|---|
| `train_classifier.py` | the trainer (multi-label + docs + cardinality heads) |
| `export_classifier.py` | ONNX export + decision-parity gate |
| `assemble_data.py` | journals → validated corpus; contamination gate |
| `eval_classifier.py` | **canonical** eval: shipped ONNX artifact under the shipped serving policy, fp32+int8+decision diff |
| `common.py` | wraps the SERVING renderer for training rows |

| `EXPERIMENTS.md` | **the log — read first** |

The abandoned pipelines (bi-encoder+reranker; hosted embeddings) were deleted
after measurement — their scripts, modules and artifacts live in git history,
and every number they produced is transcribed in `EXPERIMENTS.md`.
