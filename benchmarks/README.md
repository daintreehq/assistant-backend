# Benchmarks

One suite, `runbook_selection/`: routing quality of the shipped runbook selector —
the search behind the MCP server. Offline, deterministic and free. It is outside the
package, pytest's `testpaths` and the type gate, but CI runs `coverage` and `local`
on every push.

```bash
python -m benchmarks.runbook_selection.bench coverage   # every runbook has a case that requires it
python -m benchmarks.runbook_selection.bench local      # the shipped ONNX selector over every case, gated
python -m benchmarks.runbook_selection.bench scale      # recall with the catalog padded to 200/500/1,000
python -m benchmarks.runbook_selection.bench lint       # the most confusable runbook pairs
```

`local` attributes every missing runbook to the stage that lost it
(`candidate_miss` / `rank_miss` / `floor_reject` / `cardinality_short` /
`cap_full`) and grades the run against measured floors in
`runbook_selection/quality.py`. Three rules those floors rest on:

- **A missing metric fails.** A renamed key would otherwise silently disable exactly
  the gate watching it.
- **A rate over an empty denominator fails.** `0/0 == 0.0` reads as a perfect score.
- **Floors are a ratchet.** Tightening one as the selector improves is the point;
  loosening one wants a reason in the commit message.

The cases run conversations, not bare queries: they are the corpus the classifier
was trained and measured on, so they carry history, loaded runbooks and background
wakes that a search never sends. See `runbook_selection/README.md`.
