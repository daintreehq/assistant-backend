# Benchmarks

Measurable numbers for the quality- and cost-critical paths this server owns.
Everything here is **development tooling**: it is not part of the Python package, the
test suite, or CI (pytest `testpaths` and mypy are pinned to `src`/`tests`). The
deterministic suites are free and safe to run anywhere; the live suites make **real,
paid API calls**. Results land in each suite's `results/` (gitignored); the JSON
document is the record.

## Suites

| suite | mode | measures | entry point |
|---|---|---|---|
| `prompt_cost/` | deterministic + live | cacheable-prefix stability per transition, per-layer token budget, selector/state size; live warm cache ratio + first-byte latency | `python -m benchmarks.prompt_cost.bench run [--live]` |
| `safety/` | deterministic | zero-tolerance false-accept counts across every wire/schema/repair/sanitization/state-token/workflow guard | `python -m benchmarks.safety.bench run` |
| `microperf/` | deterministic | cold-start, catalog build, codec throughput, concurrency-under-load p99 inflation | `python -m benchmarks.microperf.bench run` |
| `session_trace/` | deterministic | selector cadence, active-set trajectory, learning trigger, state continuity through the real orchestrator | `python -m benchmarks.session_trace.bench run` |
| `workflow_intel/` | live | first-try structural validity + tool grounding + resume ranking of the workflow tasks | `python -m benchmarks.workflow_intel.bench run` |
| `injection_live/` | live | canary-graded injection obedience floor of the untrusted local-context carriers | `python -m benchmarks.injection_live.bench run` |
| `utility_json/` | live | terminal_extract_json + memory_distill accuracy/repair (the JSON tasks `utility_tasks` skips) | `python -m benchmarks.utility_json.bench run` |
| `skill_selection/` | live | accuracy + latency of the automatic skill selector | `python -m benchmarks.skill_selection.bench` |
| `utility_tasks/` | live | accuracy + latency of the 4 classifier/judge tasks | `python -m benchmarks.utility_tasks.bench` |
| `dashboard/` | roll-up | runs the portfolio and emits one JSON report (deterministic-only is CI-able) | `python -m benchmarks.dashboard.bench run` |

## One command

```bash
python -m benchmarks.dashboard.bench run --deterministic-only   # free, CI-able, no key
python -m benchmarks.dashboard.bench run                        # + live suites if a key is present
```

## Shared harness (`_harness/`)

Reused by every suite: settings + live-provider bootstrap (`env`), a byte-identical
call-counting provider decorator (`counting`), the exact httpx wire serialization +
DeepSeek-cacheable-region model (`wire`), stats, the results-doc writer (`results`),
a YAML case loader (`cases`), and the captured 72-tool CLI inventory fixture
(`fixtures/cli_tool_inventory.json`, from `Registry.OpenAITools(nil)`).

## Conventions for adding a suite

- One subdirectory, runnable as `python -m benchmarks.<name>.bench run`.
- Drive the **real production code path** (the same classes `/respond` and `/tasks`
  use), with the prompt directory / a fixture corpus as the only injectable seam.
- Cases are data (`cases/*.yaml`), validated against real server state at load.
- Label each metric deterministic / live / hybrid; token numbers from the bytes/4
  proxy are **trend-only** — only live `prompt_tokens`/`cached_tokens` are real.

## Not yet built

- **text-compaction faithfulness judge** — the summarize/extract-text/checkpoint
  quality suite needs a calibrated LLM judge (kappa ≥ 0.7 against human labels) before
  its numbers can be trusted. The judge-free slices (checkpoint id-preservation,
  injection canary) fit the `utility_json` pattern and can be added when wanted.
- **skill-learning gate/closed-loop** — needs the OpenAI builder key + a labeled
  transcript corpus; the deterministic `_validate`/secret-scan half is already covered
  inside `safety/`.
