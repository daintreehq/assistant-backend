---
name: retrain-selector
description: "Retrain the local two-stage runbook selector to make new or edited runbooks route COMPETITIVELY. Not required to make a runbook selectable at all — the set-wise head has no per-runbook weights, so a runbook added to runbooks/files/ is embedded on load and reachable that turn — but a never-trained runbook loses contested requests to established neighbours (measured 4/14 vs 13/14), so a runbook sharing vocabulary with an existing one still needs data. Run when a new runbook routes badly, when the benchmark regresses, or when the user says retrain the selector."
argument-hint: "(optional) space-separated runbook ids that are NEW, so data generation focuses there"
allowed-tools:
  - Bash
  - Read
  - Grep
  - Glob
  - Write
  - Edit
  - Workflow
---

# Retrain the local runbook selector

## Read this first: a retrain is a QUALITY step, not an unblocking step

The selector is **two stages** (`runbooks/local/setwise.py`):

1. a small bi-encoder that embeds the conversation and takes the `k` nearest
   runbooks by cosine from a table of embedded runbook documents;
2. a set-wise head that reads the conversation embedding and the `k` cached
   runbook embeddings together, and returns a logit per candidate plus the docs
   and cardinality decisions.

**Neither stage has per-runbook parameters.** A runbook added to `runbooks/files/` is
embedded by `SetwiseClassifier.sync()` the moment the catalog loads it, gets a
row in the table, and can be selected that turn — no provenance gate, no
degraded window. Under the fixed-head classifier that preceded it, a new runbook
could never be selected at all AND the whole selector degraded open until
someone ran this runbook.

**But "reachable" is not "competitive", and the gap is measured.** Holding three
runbooks out of both stages and scoring only their benchmark cases:
**4/14 zero-shot against 13/14 trained** (`training/EXPERIMENTS.md`). A new runbook
wins requests that only it plausibly answers, and loses contested ones to
established neighbours — whose training data names them and not it.

So: adding a runbook is no longer *urgent*, because nothing breaks and the runbook
works on unambiguous requests. It is still *worth doing*, and the trigger below
tells you when.

## When a retrain is actually warranted

- The benchmark (`training.eval_two_stage`) drops below its floor, or a
  category regresses.
- **A runbook was added and shares vocabulary with an existing one.** This is the
  common case and the measured weakness: `manage-issue` held out scored 0/4
  against `start-work-on-issue`, `debug-web-app` 0/3 against `diagnose-and-fix`,
  while `terminals.rename` — whose words nothing else claims — scored 4/7 with no
  training at all. If a new runbook sits in a crowded neighbourhood, assume it
  needs data. See the asymmetry lesson in `training/EXPERIMENTS.md`: fences are
  only learned in the direction they are written, and a new runbook's strongest
  words are stolen from its neighbours.
- The corpus has grown enough that the encoders are meaningfully behind it.

A **body-only** runbook edit never warrants one: only `title`/`summary`/
`whenToUse` are embedded.

## Check where you actually stand, before training anything

```bash
PY=.venv-train/bin/python
export HF_HUB_DISABLE_PROGRESS_BARS=1 TQDM_DISABLE=1
$PY -m training.eval_two_stage --ablate
```

That loads the shipped ONNX artifact, applies the production decision procedure,
prints per-category recall-pass and every miss by name, and reports the
shortlist-order ablation. If the runbook you are worried about is not in the miss
list, there is nothing to retrain for.

## The shipped recipe (do not improvise — measured in training/EXPERIMENTS.md)

### 1. Generate data for the new runbooks

Refresh the digest, then run one Workflow agent per new runbook. The agent prompts
and JSON schemas follow one pattern: read `training/data/catalog_digest.md` plus
the runbook's own file; return `positives` (situation-invented, NOT a whenToUse
paraphrase), `boundary_negatives` mined from the "do not use for X" clauses, and
`followups`.

```bash
$PY - <<'EOF'
import sys, pathlib; sys.path.insert(0, 'src')
from daintree_assistant_server.runbooks.catalog import load_catalog, default_runbooks_dir
cat = load_catalog(default_runbooks_dir()); sel = set(cat.selectable_ids)
out = ["# Daintree runbook catalog — routing digest", ""]
for s in cat.runbooks:
    if s.meta.id not in sel: continue
    f = " [FOUNDATION]" if getattr(s.meta, "foundation", False) else ""
    out += [f"## {s.meta.id}{f}", f"title: {s.meta.title}",
            f"summary: {s.meta.summary}", f"whenToUse: {s.meta.when_to_use}", ""]
pathlib.Path("training/data/catalog_digest.md").write_text("\n".join(out))
print("digest refreshed:", len(sel), "runbooks")
EOF
```

**Hard rules for generating agents:** never read `benchmarks/` or `tests/`; only
real runbook ids; label the specific runbook alone (never "specific + foundation" —
the backend pairs the foundation downstream); `expect_ids: []` is a first-class
answer. And generate **reverse-direction** boundary rows too: rows where an
ESTABLISHED runbook is correct and the new one is the hard negative. The new
runbook's whenToUse fences against its neighbours, but their whenToUse predates it
and names it nowhere, so without those rows every fence runs one way only.

**Verify every generated row blind before it lands.** Strip the labels, shuffle,
and have an agent on a DIFFERENT model label the rows from the catalog alone; keep
only the rows where both agree (2026-09-24: 791 of 801 survived). This is the
guard against the failure recorded for the 40th runbook, where 11 of 12 boundary
rows were filed backwards and nothing noticed until routing regressed. The same
round showed where data helps most: generation aimed at MEASURED weak spots —
two-runbook turns, topic shifts that share the loaded runbook's vocabulary, no-task
turns while something is loaded, fences in both directions, pasted output before
the ask — was the only change that moved routing (+8 cases on a sealed suite).

### 2. Ingest into the curated corpus, then compile

The corpus is **three planes** — raw journals, curated YAML, compiled artifact —
and the middle one is the source of truth. `training/corpus/README.md` is the
authoring reference. Read it before writing or editing examples.

```bash
# Folds the journal into training/corpus/**.yaml and archives the journal
# itself under training/journals/. Idempotent: re-running changes nothing.
$PY -m training.ingest_journal <new-journal.jsonl> --run <run-id>
```

Pick a `--run` that names the generation, not the day: `2026-08-29-worktrees`,
not `journal3`. It is stamped on every example the run contributes and is how a
bad batch gets found and removed later.

**Now read what landed and edit it.** This is the step the old cumulative
assembler made impossible — a generator's mistakes were welded into a 2.8 MB
JSONL that only grew. New examples appear in `training/corpus/runbooks/<id>.yaml`
(rows answering that one runbook) and `training/corpus/shared/<source>.yaml`
(rows answering none or several). Delete what is wrong; it stays deleted.

```bash
$PY -m training.compile_corpus
```

Watch `dropped_contaminated` (benchmark collisions) and
`dropped_conflicting_label` (same rendered input, different labels — a noisy
generator). Commit the YAML, the recompiled `training/data/selection.jsonl` and
the updated `corpus-lock.json` together; `tests/unit/test_training_corpus.py`
fails if they disagree.

### 3. Train stage 1, then stage 2

**Order matters and the stages are not interchangeable.** Stage 2 trains against
shortlists produced by a FROZEN stage 1, so stage 1 must be finished first;
retraining stage 1 afterwards invalidates stage 2's training distribution.

```bash
$PY -m training.train_retriever          # ~15 min: watch r@32 stay at 1.000
                                        # and `hard-neg/row` stay around 4
$PY -m training.train_setwise --epochs 10 --ordering equivariant
```

Watch on stage 1: `r@32` (the shortlist must not be the thing that loses a
runbook), `r@8` as the early warning that will matter at 200 runbooks, and
`hard-neg/row` — the authored `confusable_with` entries unioned with what the
encoder mined against its own current weights that epoch. Near zero means the
margin filter is rejecting everything, which is what a corpus whose positives
and negatives have collapsed together looks like. Watch on
stage 2: `card` ~0.95 — this is what makes "zero runbooks" work — and
`val exact-match`.

The trainers' defaults ARE the shipped recipe (2026-09-25): 512-token documents
in both stages, a content-keyed train/val split, and for stage 2 permutation
sampling, sqrt cardinality weights, a 0.1 boundary loss on authored
`confusable_with` pairs and 3 clause-dropout document variants. Measured in
`EXPERIMENTS.md` ("The retrain lottery"): none of these moved routing on its own —
the verified data did — but the 512 cap and the split are correctness fixes, and
`test_every_runbook_document_fits_the_shipped_document_cap` fails a runbook edit
that would push a document past the cap again. `--ema`, `--canonical-active` and
`--teachers` (distillation) exist, were measured, and are off.

Keep `--ordering equivariant`. `positional` and `blind` exist so the choice stays
measured rather than assumed, and both were scored (see `EXPERIMENTS.md`):
`positional` is worse at matched precision (154 vs 158) and gives up
permutation-equivariance — reversing its shortlist changes 151 of 165 answers,
which is a slot prior that would penalise any newly added runbook. `blind` ties,
and is the ablation showing the head does not depend on stage 1's scores.

### 4. Export

```bash
$PY -m training.export_two_stage
```

Exports both encoders (fp32 + int8) and the head, rebuilds `vectors.npz` through
the **shipped** `sync()` path, and gates on decision parity across the whole
benchmark.

**int8 is a lottery**: `quantize_dynamic` is nondeterministic, so each export
draws a slightly different int8 graph (measured spread ~2 recall-pass cases).
The gate scores every draw; if step 5 lands below its floor, export again — and
never re-export an already-measured artifact without re-measuring.

### 5. Score the SHIPPED artifact

```bash
$PY -m training.eval_two_stage --ablate
```

The canonical evaluator: the exported ONNX at both precisions, under the
production decision procedure, with the fp32-vs-int8 decision diff. int8 is the
shipped precision. Compare per-category numbers against `EXPERIMENTS.md`.

**One run is not a result.** The "±2" this file used to quote was too kind: the
same recipe on the same corpus scored 198 / 205 / 202 / 198 across four seeds
(2026-09-24), so "retrain once and keep it if the number went up" is a coin toss —
and that is exactly how a 63-row corpus change once read as a 5-case regression.
When a retrain changes the RECIPE or a lot of DATA, train at least three seeds
(`--seed`, and `--out` per seed) and compare means, on the benchmark AND on the
held-out suites, with active sets presented as production holds them:

```bash
DAINTREE_BENCH_CANONICAL_ACTIVE=1 $PY -m training.eval_two_stage --artifact <dir>
for r in 1 2 3; do DAINTREE_BENCH_CANONICAL_ACTIVE=1 $PY -m training.eval_two_stage \
    --artifact <dir> --cases-dir benchmarks/runbook_selection/holdout/round$r; done
```

Decide which recipe ships BEFORE looking at whichever suite is your final check,
and ship a pre-chosen seed rather than the best-scoring one. The holdout rounds are
development data now (see their README); a clean final check needs a fresh round
written the same way. A routine retrain for one new runbook on the settled recipe
can still be judged on one run — the rule is about choosing between recipes.

`--ablate` reverses the shortlist and re-scores. With `equivariant` this must
report **0 cases changed** — that is the permutation-equivariance claim being
checked against the real graph rather than trusted from the architecture.

If the round was about a new runbook, also re-measure what a retrain actually
bought. Train a probe with the new runbooks held out of BOTH stages and score only
their cases:

```bash
$PY -m training.train_retriever --holdout-runbooks <ids> --out training/models/retriever_probe
$PY -m training.train_setwise --epochs 10 --holdout-runbooks <ids> \
     --retriever training/models/retriever_probe --out training/models/sw_probe
$PY -m training.export_two_stage --setwise training/models/sw_probe --out .tmp/art_probe
$PY -m training.eval_zero_shot --artifact .tmp/art_probe
```

### 6. Commit

```bash
git add -A .
git diff --cached --numstat | awk '{a+=$1} END {print a" staged lines"}'  # thousands, not 100k+
```

Model graphs, tokenizers and `vectors.npz` are Git LFS. Commit the regenerated
`training/data/*.jsonl` with the weights — the corpus is the record of what the
shipped encoders were trained on.

## Scaling

Adding a runbook costs **one row in the embedding table** (~4.5KB) and one
embedding pass at catalog load. It costs nothing at inference: the head sees `k`
candidates whatever the catalog size, and the cosine search over 1,000 runbooks is
a single 1000x384 matrix-vector product — microseconds, and exact, which is why
there is no ANN index here.

What still costs a retrain is *quality*: the encoders only know the vocabulary
they were trained on. The intended workflow is batched — let runbooks accumulate,
generate their data (both directions), retrain once.
