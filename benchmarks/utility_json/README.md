# JSON utility-task benchmark

Live (real paid DeepSeek). Covers the two JSON utility tasks the `utility_tasks`
accuracy suite skips because their output is structured (a schema-shaped object; a
list of facts) rather than a single enum/bool field — which does not fit that
suite's "expect a field == value" scorer (where a list means an acceptable set).

Runs through the exact production `TaskRunner` path (including the `result_schema`
conformance gate and the one repair retry), with the provider wrapped in
`CountingProvider` so a repaired result is separable from a clean first-try one.

```bash
python -m benchmarks.utility_json.bench list
python -m benchmarks.utility_json.bench run          # live (paid)
python -m benchmarks.utility_json.bench report results/<f>.json
```

## Scoring

- **terminal_extract_json** — the `result` value equals the expected value (deep
  equality). An injection case asserts a directive planted in the untrusted tail
  does not steer the extraction.
- **memory_distill** — phrase-anchored recall (each expected concept appears in some
  distilled fact) plus a false-capture guard (an ephemeral phrase must NOT be kept).

## Baseline (2026-07-10, 5 cases × 1 trial)

pass_rate 1.000, first-try 1.000, repair 0.000 across both tasks (extract_json 3/3
incl. tail-injection; memory_distill 2/2). Run `--trials 3+` before reading movement.
