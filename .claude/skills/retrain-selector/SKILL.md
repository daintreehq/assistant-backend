---
name: retrain-selector
description: "Retrain the local skill-selection classifier after adding, removing or renaming skills, or after editing any skill's title/summary/whenToUse. Generates synthetic training data for the new skills, retrains, exports ONNX with a decision-parity gate, and scores the shipped artifact against the held-out benchmark. Use when skills change in skills/files/, when the server logs local_selector_stale_head, or when the user says retrain the selector."
argument-hint: "(optional) space-separated skill ids that are NEW, so data generation focuses there"
allowed-tools:
  - Bash
  - Read
  - Grep
  - Glob
  - Write
  - Edit
  - Workflow
---

# Retrain the local skill selector

The selector is a **multi-label classifier**: one bge-base encoder (109M), one
head with a row per skill plus a docs output plus four cardinality outputs
(`skills/local/classifier.py`). The head's weight matrix IS the skill
embeddings, so **a skill that was not in the training run has no row and can
never be selected**. Serving guards this with a provenance stamp: the trainer
records `routing_revision` (a hash of every selectable skill's
id/title/summary/whenToUse) and `LocalSelector` refuses to serve — degrading
open to the prior active set — whenever the live catalog's routing surface no
longer matches.

## When a retrain is needed

- A skill was added, removed or renamed in `src/daintree_assistant_server/skills/files/`.
- A skill's `title`, `summary` or `whenToUse` changed **at all** — these are the
  classifier's training text, and any edit moves `routing_revision`.
- The server logs `local_selector_stale_head` (it is already degrading open).

A **body-only** edit does NOT need a retrain: `routing_revision` deliberately
excludes bodies, so skill-learning's usual edits leave the selector serving.

## The shipped recipe (do not improvise — measured in training/EXPERIMENTS.md)

```bash
PY=.venv-train/bin/python
export HF_HUB_DISABLE_PROGRESS_BARS=1 TQDM_DISABLE=1
```

### 1. Confirm the gap

```bash
$PY - <<'EOF'
import json, sys; sys.path.insert(0, 'src')
from pathlib import Path
from daintree_assistant_server.skills.catalog import load_catalog, default_skills_dir
from daintree_assistant_server.skills.local.documents import routing_revision
cat = load_catalog(default_skills_dir())
art = Path("src/daintree_assistant_server/skills/local/artifacts/classifier")
meta = json.loads((art / "meta.json").read_text())
trained = set(json.loads((art / "labels.json").read_text()))
live = set(cat.selectable_ids)
print("live routing   :", routing_revision(cat)[:32])
print("artifact stamp :", meta.get("routing_revision", "<missing>")[:32])
print("no head row for:", sorted(live - trained) or "none")
print("orphan rows for:", sorted(trained - live) or "none")
EOF
```

### 2. Generate data for the new skills

Regenerate the digest, then run one Workflow agent per new skill. The agent
prompts and JSON schemas are in the session history of the original build and
follow one pattern: read `training/data/catalog_digest.md` + the skill's own
file; return `positives` (situation-invented, NOT whenToUse paraphrase),
`boundary_negatives` mined from the "do not use for X" clauses, and `followups`.

```bash
$PY - <<'EOF'
import sys, pathlib; sys.path.insert(0, 'src')
from daintree_assistant_server.skills.catalog import load_catalog, default_skills_dir
cat = load_catalog(default_skills_dir()); sel = set(cat.selectable_ids)
out = ["# Daintree skill catalog — routing digest", ""]
for s in cat.skills:
    if s.meta.id not in sel: continue
    f = " [FOUNDATION]" if getattr(s.meta, "foundation", False) else ""
    out += [f"## {s.meta.id}{f}", f"title: {s.meta.title}",
            f"summary: {s.meta.summary}", f"whenToUse: {s.meta.when_to_use}", ""]
pathlib.Path("training/data/catalog_digest.md").write_text("\n".join(out))
print("digest refreshed:", len(sel), "skills")
EOF
```

**Hard rules for generating agents:** never read `benchmarks/` or `tests/`;
only real skill ids; label the specific skill alone (never "specific +
foundation" — the backend pairs the foundation downstream); `expect_ids: []` is
a first-class answer.

### 3. Assemble (cumulative — pass the old journals too)

```bash
$PY -m training.assemble_data --journal <new-journal.jsonl> <all-old-journals...>
```

Watch the printed stats: `dropped_contaminated` (benchmark collisions),
`dropped_conflicting_label` (same rendered input, different labels — a noisy
generator), and the per-source counts.

### 4. Train + export

```bash
$PY -m training.train_classifier \
    --epochs 10 --lr 5e-5 --batch-size 48 --balance-power 0.0 \
    --base-model BAAI/bge-base-en-v1.5 --seed 41
$PY -m training.export_classifier
```

Watch: `card_acc` ~0.95 (this is what makes "zero skills" work) and
`val exact-match` ~0.85. The export **fails** if the fp32 graph changes any
decision vs torch, and stamps the artifact with the trainer's
`routing_revision` — it will refuse a checkpoint that lacks one.

### 5. Score the SHIPPED artifact

```bash
$PY -m training.eval_classifier
```

This is the canonical evaluator: it loads `SkillClassifier` from the exported
ONNX (both precisions), applies the production decision procedure, verifies the
provenance stamp against the live catalog, and prints the fp32-vs-int8
**decision diff**. int8 is the shipped precision. Compare per-category numbers
against `training/EXPERIMENTS.md`; the family's honest error bar is ±2 cases.

### 6. Commit

Model graphs and the tokenizer are Git LFS. Verify before committing:

```bash
git add -A .
git diff --cached --numstat | awk '{a+=$1} END {print a" staged lines"}'  # thousands, not 100k+
```

Commit the regenerated `training/data/*.jsonl` with the weights — the corpus is
the record of what the shipped model was trained on.

## Scaling

One new skill costs one 768-float head row; retraining costs ~8 minutes on an
M4 (MPS). The intended workflow is batched: accumulate 10-20 skills, generate
their data, retrain once. There is no incremental path, and adding one skill
costs the same as adding twenty. The design target of 100-200 skills changes
inference cost by microseconds.
