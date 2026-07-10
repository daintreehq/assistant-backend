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
  on: a prompt/skill/assembler edit that busts the cache, weakens a guard, adds a
  per-request CPU regression, or breaks the orchestrator spine reddens here.
- the **live** suites (`prompt_cost` live, `workflow_intel`, `injection_live`) add
  real, paid DeepSeek calls and only run when `DEEPSEEK_API_KEY` is present.

Each suite is isolated: if one raises, its slot records the error and the rest
still run. Every suite keeps its own `summary` verbatim in the merged doc; the
dashboard layers a small `headline` per suite for a one-glance read, and `compare`
diffs two runs' headlines.

The existing live suites `skill_selection` and `utility_tasks` are run on their own
(they predate the dashboard and are expensive); wire them in the same way if you
want them in the roll-up.
