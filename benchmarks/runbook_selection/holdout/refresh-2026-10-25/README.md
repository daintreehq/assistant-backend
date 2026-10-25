# Selector refresh signoff — 2026-10-25

Forty-eight sealed cases for the next selector refresh (#189). They were written,
labelled and frozen before any candidate exists, and their pass/fail criteria below
were fixed before even the locked model was scored on them. `training/REFRESH-2026-10-25.md`
is the protocol they belong to.

## How they were made

- **Writer:** one agent that could read only the runbook files under
  `src/daintree_assistant_server/runbooks/catalog/` and `docs/RUNBOOKS.md`, never
  `benchmarks/`, `training/`, `tests/` or `archive/`, and never ran a search.
- **Labels:** the writer's, from each runbook's current `whenToUse`, with the deciding
  clause quoted in every `note`. A second labeller (gpt-6.1-sol, read-only, same reading
  rule) labelled the shuffled texts blind, with only the caller's host state shown.
  It agreed with all 48 and marked none uncertain.
- **Leakage:** `training.compile_corpus` protects everything under `holdout/`, so no
  training row may now near-duplicate these texts (difflib > 0.8). The 38 rows added for
  this refresh were written before this set was read and screened against it.

| group | cases | covers |
|---|---|---|
| 1 | 10 | continuing work on this machine against work already sent to Remote Hosts; several local projects; after a restart; after a usage limit |
| 2 | 10 | one unit on one host, the same work on every host, different units over hosts, a host assistant's answer, a staged plan, a race, assistants readied with no work |
| 3 | 8 | held and draining work: stop new starts, park, reprioritise, maintenance, steer, drain over hosts |
| 4 | 14 | two own requests each for `hosts.configure-settings`, `hosts.prepare-project-for-work`, `hosts.drain-issue-backlog`, `hosts.answer-assistant-questions`, `hosts.sync-with-origin`, `agents.choose-agent-model`, `agents.resume-after-usage-limit`; 11 are traps with the neighbour forbidden |
| 5 | 6 | plain code work delegated to a visible agent (4), and two that need no runbook |

Groups are delimited by comments in `01-signoff.yaml`. 35 cases report connected hosts,
6 report none, 7 are unreported.

## Criteria (fixed 2026-10-25, before any scoring)

Score with the same runtime on both sides, Mac and the production Linux image reported
separately, each candidate against the locked baseline measured on that platform:

```bash
python -m training.eval_two_stage --artifact <dir> \
  --cases-dir benchmarks/runbook_selection/holdout/refresh-2026-10-25
```

A candidate passes this set only if its **int8** result has, on each platform:

1. recall-pass at least the locked baseline's, and at least 42 of 48;
2. group 4 recall-pass at least the baseline's, and at least 12 of 14;
3. no forbidden load (`expect.forbid`) at all;
4. no case in group 5 failing that the baseline passes.

The preselected shipping seed (41) must pass on its own; the mean over the recipe's
seeds must also meet 1. No seed is chosen by its score here, and no admission floor is
tuned against this set.

## If the catalog moves first

Labels follow the catalog at `routing:773cd654500efe211cee7be68d9536a0`. #167, #172,
#184 and #186 may change routing text before the retrain. When one lands, re-run the
blind label over this set against the new text *before any candidate is scored*;
re-label only a case whose deciding clause changed and both labellers agree on, and
record each change here. Once a candidate has been scored the set is spent: like
every round in `../README.md`, it is development data from then on.

## Baseline (locked model)

Recorded after the criteria above were written; see `training/REFRESH-2026-10-25.md`.
