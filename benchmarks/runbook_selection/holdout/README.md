# Held-out routing suites

Case suites written OUTSIDE the benchmark, for judging a selector retrain on
evidence the corpus and the benchmark were never tuned against. Same case format as
`../cases/`; not part of CI, because none of them should become a gate somebody tunes to.

```bash
.venv-train/bin/python -m training.eval_two_stage --cases-dir benchmarks/runbook_selection/holdout/round3
```

## How they were made (2026-09-24)

Written by agents that could read ONLY the catalog digest and the runbook files — never
`benchmarks/`, `training/` or `tests/`. Every case was then labelled blind (answers
stripped, order shuffled) by two further annotators on different models, and kept only
where at least two of the three agreed; that majority is the label. Rows in the training
corpus that near-duplicated any case here (difflib ratio > 0.8 on the final user turn) were
removed from the corpus; five round-3 cases that near-duplicated new training rows were
removed from round 3.

| round | cases | focus | status |
|---|---|---|---|
| 1 | 100 | per-domain: plain requests, traps, state, no-task | **development** — its failures were read |
| 2 | 189 | confusable fences, conversation state, messy/pasted turns | **development** — its failure SHAPES informed the 2026-09-24 data-generation briefs |
| 3 | 197 | three personas, goal-phrased requests, long sessions | was **sealed** until the 2026-09-24 decision; scored once. Treat as development from here |
| 4a | 138 | single messages shaped like `search_runbooks` traffic: Claude- and Codex-written task descriptions, users' own words, both sides of every fence | **development** — its failures were read (2026-09-28) |
| 4b | 139 | the same population, split at random | was **sealed** until the 2026-09-28 decision; scored once. Treat as development from here |

Round 4 (2026-09-28) was written by four agents on the same read-only rule, and each case
labelled blind by gpt-6-sol and gpt-6-astra: 319 of 320 were unanimous with the writer. Cases
near-duplicating a training row (difflib > 0.8) were removed; the remaining 277 were split at
random into 4a and 4b.

A suite stops being held out the moment someone chooses anything by looking at it. The
next retrain that wants a clean check needs a fresh round written the same way.

## Reading a result

Judge a RECIPE across seeds, never one artifact: at 217 benchmark cases this family's
single-run spread was ~7 cases (198/205/202/198 for one recipe on one corpus). Score with
`DAINTREE_BENCH_CANONICAL_ACTIVE=1` to present active sets as the server holds them
(every specific paired with its domain foundation). See `training/EXPERIMENTS.md`,
"The retrain lottery".
