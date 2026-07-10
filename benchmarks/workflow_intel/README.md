# Workflow-intelligence live benchmark

Live (real paid DeepSeek). Drives the three workflow-intelligence tasks
(`workflow_plan` / `workflow_reconcile` / `workflow_resume_digest`) through the
exact production `TaskRunner` path — real profiles, the real semantic validators
(`validate_plan`/`validate_reconcile`: id-uniqueness, DAG-ness, terminal-status
immutability, tool grounding), and the one repair retry — with the provider wrapped
in `CountingProvider` so a clean first-try success is separable from a repaired one.

```bash
python -m benchmarks.workflow_intel.bench list          # validate cases (no calls)
python -m benchmarks.workflow_intel.bench run           # live (paid)
python -m benchmarks.workflow_intel.bench run --trials 3
python -m benchmarks.workflow_intel.bench report results/<f>.json
```

## Metrics

- **first_try_valid_rate** — parsed + passed the structural validators on the FIRST
  model call. This is the direct prompt lever: a repair doubles latency and cost.
- **repair_rate** / **error_rate (502)** — needed the repair call, or failed even
  after it.
- plan: node/edge counts vs the 20/40 caps; `next_action_grounded` (an ungrounded
  tool degrades to a warning, never a fabricated capability).
- resume: **focus_accuracy** — the workflow blocked on the user is the suggested
  focus (objectively scorable, no labels).

## Baseline (2026-07-10, deepseek-v4-flash, 7 cases × 1 trial)

| task | n | first-try valid | repair | 502 | focus acc |
|---|---|---|---|---|---|
| workflow_plan | 3 | 1.000 | 0 | 0 | — |
| workflow_reconcile | 2 | 1.000 | 0 | 0 | — |
| workflow_resume_digest | 2 | 1.000 | 0 | 0 | 1.000 |

Latency p50 9.4 s / p90 39.8 s — thinking-high; these are background (supervisor)
tasks off the user's first-byte path, so structural correctness outweighs latency.
Re-run with `--trials 3+` before reading first-try-rate movement as signal (DeepSeek
is not deterministic at temperature 0).
