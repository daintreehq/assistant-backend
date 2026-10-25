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
| 5a | 133 | the 47 runbooks added 2026-09-28 (catalog 40 → 87): each one's own requests, traps toward every neighbour, plain code work under the orchestration principle | **development** |
| 5b | 133 | the same population, split at random | was **sealed** until the catalog-expansion decision (2026-09-28); scored once: 126/133. Treat as development from here |
| 6a | 82 | the 18 runbooks added in round 6 (catalog 87 → 105) plus the restore operation of `migrate-worktree-database`: own requests, traps toward every neighbour, plain code work, pairs | **development** |
| 6b | 83 | the same population, split at random | **sealed** until the round-6 retrain decision |
| refresh-2026-10-25 | 48 | the next refresh's signoff: local vs remote continuation, one host vs several, held and draining work, the seven runbooks untrained at 146; criteria fixed in its README | **sealed** until the next retrain decision |

Round 4 (2026-09-28) was written by four agents on the same read-only rule, and each case
labelled blind by gpt-6-sol and gpt-6-astra: 319 of 320 were unanimous with the writer. Cases
near-duplicating a training row (difflib > 0.8) were removed; the remaining 277 were split at
random into 4a and 4b.

Round 5 (2026-09-28) was written the same way against the grown catalog, by four writers
covering 11-12 new runbooks each; gpt-6-sol and gpt-6-astra agreed with the writer on all
270, four near-duplicates of training rows were removed, and the rest split at random.

Round 6 (2026-09-28) was written the same way by four writers against the 105-runbook catalog;
gpt-6-sol and gpt-6-astra agreed with the writer on 169 of 170, five near-duplicates of training
rows (queries and history lines) were removed, and the rest split at random. A blind audit of
every existing case and corpus row that the new runbooks could plausibly own (1,050 candidates:
the shipped selector ranking a new runbook top-3, or its vocabulary) changed no case and moved 9
corpus rows, each where sol and astra agreed on a new runbook.

The same day, every single-message case in rounds 1-4b (and in `../cases/`) was re-labelled
blind against the grown catalog: gpt-6-sol first, gpt-6-astra on every disagreement, and a
label changed only where both agreed on an answer that a NEW runbook owns, or that moved
project work from `[]` to the runbook that delegates it. 26 cases changed.

A suite stops being held out the moment someone chooses anything by looking at it. The
next retrain that wants a clean check needs a fresh round written the same way.

## Reading a result

Judge a RECIPE across seeds, never one artifact: at 217 benchmark cases this family's
single-run spread was ~7 cases (198/205/202/198 for one recipe on one corpus). Score with
`DAINTREE_BENCH_CANONICAL_ACTIVE=1` to present active sets as the server holds them
(every specific paired with its domain foundation). See `training/EXPERIMENTS.md`,
"The retrain lottery".
