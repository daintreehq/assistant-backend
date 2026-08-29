# The curated corpus — source of truth for the selector's training data

Everything under this directory is **hand-editable and reviewable**. It is the
input to `python -m training.compile_corpus`, which produces
`training/data/selection.jsonl`. That file is a **build artifact**: do not edit
it, and do not treat it as the record. CI recompiles from here and fails if the
two disagree.

```
training/journals/   RAW      what a generating agent returned, append-only, committed
training/corpus/     CURATED  this directory — the source of truth
training/data/       COMPILED selection.jsonl + corpus-lock.json, both generated
```

## Why it is split up

Until this existed there was one file — `selection.jsonl` — which was both the
assembler's output and its own input: a retrain read the committed corpus back
in as `--base` and appended. Three things followed.

- **Nothing could be removed by regenerating**, only by hand-editing 2.8 MB of
  JSONL.
- **Provenance was one flat `source` string.** "Which run added this row" and
  "drop everything from the run with the bad prompt" were both unanswerable.
- **Re-running a generator grew the corpus rather than reproducing it.** The
  retired `augment_context.py` measured itself taking 9,903 rows to 10,405 on a
  second application, and shipped a guard against its own re-run whose
  documented recovery was `git show <commit>:…jsonl >`. Git was the undo stack.

## Where a row goes

One rule, and it is mechanical so that two people fanning out the same journal
produce the same tree:

| the row's answer | its home |
|---|---|
| exactly one runbook | `runbooks/<that runbook id>.yaml` |
| none, or several | `shared/<source>.yaml` |

A boundary case therefore lives with the runbook that **wins** it, which is
where somebody asking "why does this runbook lose to its neighbour" will look.
`compile_corpus` refuses a runbook file holding a row that answers anything else.

## The two file shapes

**A runbook file.** Every example answers the one runbook named at the top, so
no example repeats it. Sections say what kind of evidence a row is.

```yaml
runbook: daintree.worktree.relocate-agents
run: 2026-08-29-worktrees

positives:
  # A bare string is a query and nothing else — most positives are this.
  - split these agents into their own worktrees
  - query: move them out, and tell each one where it went
    history:
      - "user: what's running?"
      - "assistant: Three agents in one checkout."

boundaries:
  - query: make me a worktree for the auth branch
    confusable_with: [daintree.worktree.create-plain-worktree]
```

Sections, and the `source` each compiles to: `positives`, `boundaries`,
`followups`, `topic_shifts`, `wakes`, `near_misses`, `docs`, `foundation`,
`foundation_boundaries`.

**A shared file.** For rows answering no runbook or several. `source`, `docs`
and `empty_kind` at the top are defaults every example inherits.

```yaml
source: no_task
empty_kind: no_task
examples:
  - thanks
  - query: nice one
```

Per-example keys: `query` (required), `history`, `active`, `expect`,
`confusable_with`, `docs`, `empty_kind`, `run`. Anything else is refused — a
silently-ignored key is a row that does not teach what its author thinks it does.

## `confusable_with` is authored, not mined

It records **which runbooks a human thinks this query could be mistaken for**,
usually straight out of a runbook's own "do not use for X" clause. That
judgement is stable across retrains.

It is deliberately not the same thing as the negatives `train_retriever` mines
each epoch from the live encoder, which are what *this checkpoint* gets wrong
and go stale the moment the model moves. The trainer unions the two. Do not
paste model output in here.

The old corpus's `hard_negatives` field became this one. It was never mined —
the assembler hardcoded the id of the runbook whose generation block produced
the row — and **no line of code ever read it**, though two docstrings claimed it
was used. Naming it for what it actually is was half the fix; the other half was
giving it a consumer.

## Authoring rules worth knowing

These are measured findings, not preferences. Fuller detail in
`training/EXPERIMENTS.md`.

- **A social lead-in erases the task.** "How do I create worktrees?" ranked its
  runbook at 1.000 cold and 0.005 behind a short greeting, with cardinality
  collapsing to zero — so the turn loaded nothing. It is the greeting itself,
  not dilution: 105 characters of meaningless padding left the score at 0.998.
  Rows that open on chatter and then state a task are worth writing.
- **The docs label is not the docs section.** A section says where a row came
  from; `docs:` says what the classifier should learn. Conflating them is how 64
  documentation questions got labelled `is_docs: false`, which measured 113/120
  docs recall cold against 5/120 with a runbook loaded.
- **Label the specific runbook alone.** Never "specific + `daintree.foundation`":
  the backend pairs the domain foundation downstream, and a row teaching the
  classifier to do it as well is teaching a job something else already does.
  `compile_corpus` unpairs it and counts the correction.
- **Write reverse-direction boundaries.** A new runbook's `whenToUse` fences
  against its neighbours, but their `whenToUse` predates it and names it
  nowhere, so without rows where the ESTABLISHED runbook is correct and the new
  one is `confusable_with`, every fence runs one way only.
- **Never read `benchmarks/runbook_selection/cases/`.** It is the held-out
  evaluation set and the only evidence any of this works. `compile_corpus`
  measures the overlap rather than trusting anyone about it, and drops
  collisions — including ones in `history`.

## Removing a bad generation run

Every example carries the `run` that produced it, so a batch generated
against a broken prompt can be taken back out — the thing the cumulative
assembler made impossible.

```bash
python -m training.prune_run <run-id> --dry-run   # report only
python -m training.prune_run <run-id>
python -m training.compile_corpus
```

The journal stays in `training/journals/`. That plane is the raw record and
is append-only: pruning says what the corpus should teach, it does not
rewrite what a generator returned.

**Two consequences worth knowing before you rely on either tool.**

- *Ingest's no-op property is about the corpus, not the journal.* It skips
  what is already on disk, so re-ingesting a journal whose rows you have
  since pruned or deleted brings them back. That is the recovery path if you
  pruned by mistake, and a footgun otherwise — after a deliberate prune,
  don't re-ingest that run's journal.
- *A rewrite drops your comments.* `ingest` and `prune` rewrite a whole file
  through the YAML dumper, so hand-written comments in a file they touch do
  not survive. Put durable reasoning in this README or in `EXPERIMENTS.md`;
  a comment on a specific example is fine but treat it as provisional.

## Changing anything here

```bash
python -m training.compile_corpus          # rebuild selection.jsonl + corpus-lock.json
python -m training.compile_corpus --check  # what CI runs
```

Commit the YAML edit, the recompiled `selection.jsonl` and the updated
`corpus-lock.json` together. The lockfile records per-file row counts and
hashes, so the reviewable part of a data change is the YAML diff plus a few
lines of counts — not several hundred lines of JSONL.
