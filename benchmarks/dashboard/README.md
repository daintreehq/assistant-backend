# Benchmark dashboard

One command that runs the whole portfolio and merges it into a single JSON report
whose headline numbers are the tracked record for "improve everything in one go".

```bash
python -m benchmarks.dashboard.bench run --deterministic-only   # free, CI-able (no key)
python -m benchmarks.dashboard.bench run                        # + live suites if a key is present
python -m benchmarks.dashboard.bench run --live --live-trials 3 # force the paid suites
python -m benchmarks.dashboard.bench compare results/a.json results/b.json
```

- **deterministic-only** runs `prompt_cost` (deterministic), `safety`, `microperf`,
  and `session_trace` — free, no key, safe in CI. This is the mode worth gating PRs
  on: a prompt/runbook/assembler edit that busts the cache, weakens a guard, adds a
  per-request CPU regression, or breaks the orchestrator spine reddens here.
- the **live** suites (`prompt_cost` live, `workflow_intel`, `injection_live`) add
  real, paid DeepSeek calls and only run when `BASETEN_API_KEY` is present.

Each suite is isolated: if one raises, its slot records the error and the rest
still run. Every suite keeps its own `summary` verbatim in the merged doc; the
dashboard layers a small `headline` per suite for a one-glance read, and `compare`
diffs two runs' headlines.

`runbook_selection` IS in the roll-up now, and is most of what a live run costs:
438 selector calls at three trials against 146 cases, versus roughly 80 for
everything else combined. It went in because it measures the one decision the
rest of the product hangs off — which runbook the main model gets — and leaving
it out meant a release could qualify without ever asking whether routing still
worked. It runs the free offline coverage gate as a preflight, so a catalog with
an uncovered runbook fails before any of that is spent.

`utility_tasks` is still run on its own; wire it in the same way if you want it
in the roll-up.
