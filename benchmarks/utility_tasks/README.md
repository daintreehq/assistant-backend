# Utility-task accuracy benchmark

Measures the **accuracy and latency** of the server-owned utility-task
classifiers/judges (`services/task_runner.py`) against the live DeepSeek API,
using the real production path end-to-end: the real `TaskRunner`, the real
per-task `TaskProfile` (user-message builder, output parser, token cap, and the
one repair retry), the packaged task prompts, `sanitize`/validation, the
DeepSeek adapter, and the retry policy. The only injectable seam is a prompts
**overlay** — a variant directory that overrides individual `tasks/<id>.md`
bodies — which is how candidate prompts are benchmarked before being promoted
into `src`.

Scope is the four tasks with a single, unambiguous ground-truth field, so they
can be scored as strict accuracy (the text/compaction tasks need a judge or
reference scorer and are out of scope here):

| task | ground truth | why it matters |
|---|---|---|
| `terminal_judge` | `matched` (bool) | drives whether a watcher fires on a yes/no question |
| `watcher_classify` | `classification` (enum) | the supervisor-queue label for a terminal |
| `extraction_verdict` | `pass` (bool) | pass/fail gate over an extracted result |
| `skill_step_consistency` | `matched` (bool) | flags a bad skill-run advance |

## Quick start

```bash
# from the repo root, venv active, DEEPSEEK_API_KEY in .env (or exported)
python -m benchmarks.utility_tasks.bench list             # validate cases, no API calls
python -m benchmarks.utility_tasks.bench run              # baseline (packaged prompts)
python -m benchmarks.utility_tasks.bench run --variant my-idea
python -m benchmarks.utility_tasks.bench run --filter watcher_classify
python -m benchmarks.utility_tasks.bench compare results/a.json results/b.json
```

A full default run is `cases x trials` real task calls per variant — cheap on
deepseek-v4-flash, but not free.

## Cases (`cases/*.yaml`)

Each case freezes one task invocation — the `task` id, the `input` payload
(exactly what a client sends `/v1/daintree/tasks`), and the expected output
fields:

```yaml
- id: watch-rate-limited-429
  task: watcher_classify
  category: watch-throttle
  input:
    goal: Have the sub-agent refactor the module.
    terminal_state: {agent_state: working, runtime_status: running}
    tail: |
      HTTP 429 Too Many Requests: rate limit exceeded. retry-after: 30s
  expect: {classification: rate_limited}
```

Scoring is strict per trial: a trial passes only when **every** `expect` field
matches and the task did not error. A scalar `expect` value must match exactly;
a **list** is an acceptable set (used where two neighbouring classes are both
defensible, e.g. `permission_prompt` vs `waiting_for_input`). Every case is
validated at load: the `task` must be supported, the `input` must validate
against the task's real Pydantic input model, and each `expect` key must be a
real output field — a typo fails immediately.

Categories deliberately over-sample the failure modes each prompt is likeliest
to get wrong: prompt-injection in the (untrusted) tail/result, over-eager
"yes/pass", neighbour-class confusion (`rate_limited` vs `command_failed`), and
the idempotent-retry / deliberate-skip traps the consistency prompt warns about.

## Prompt variants (`prompts/<name>/tasks/<id>.md`)

`--variant baseline` uses the packaged production prompts. Any other name
resolves to an **overlay**: a `prompts/<name>/tasks/` directory whose
`<id>.md` files override those tasks (every other task falls back to the
packaged prompt), so a variant can change just one task prompt. To promote a
winner:

1. Copy it over `src/daintree_assistant_server/prompts/tasks/<id>.md`.
2. Regenerate golden snapshots: `DAINTREE_UPDATE_GOLDEN=1 pytest tests/golden/`.
3. Re-run `bench run` (baseline now IS the new prompt) to confirm.

## Results

Runs are saved to `results/<timestamp>-<variant>.json` (gitignored). The
document carries the per-task prompt SHA-256, model id, per-trial
output/mismatch/latency/tokens, and the summary block (strict cases, trial pass
rate, per-task and per-category breakdown, latency percentiles). Latency is
end-to-end per task call (render + API + parse + any repair retry) — exactly the
cost `/v1/daintree/tasks` pays.

## Findings (2026-07-07, deepseek-v4-flash, 46 cases x 3 trials)

| run | strict cases | trial pass | p50 | p90 | notes |
|---|---|---|---|---|---|
| baseline | 43/46 (93.5%) | 132/138 (95.7%) | 2.14s | 3.68s | all 6 misses in `watcher_classify` |
| **+ watcher precedence fix** | **46/46 (100%)** | **138/138 (100%)** | 2.50s | 4.44s | 3 cases fixed, 0 regressions |

Per task, all misses were `watcher_classify` (85.7% → 100%); `terminal_judge`,
`extraction_verdict`, and `skill_step_consistency` were 100% at baseline.

What moved the needle — **the watcher prompt enumerated the classes but gave no
precedence for overlapping ones**, so the small model picked a defensible-but-wrong
neighbour:

- **`merge_conflict` → `waiting_for_input` / `command_failed`** (0–1/3): a rebase
  conflict was downgraded to "waiting for someone to resolve it" or a generic
  command failure.
- **`tests_failed` → `command_failed`** (2/3): a `pytest` failure that also
  tripped `make: *** [test] Error 1` read as a generic non-zero exit.
- **`no_change` → `still_working`** (0/3): routine forward progress (a training
  run ticking `epoch 3/50`) was re-reported as fresh activity instead of "nothing
  meaningfully changed since I last said still_working".

The fix (`tasks/watcher_classify.md`) adds a **most-specific-class-wins** rule
naming those precedences, and a **still_working vs no_change** rule (first report
= `still_working`; continued routine progress of the same activity = `no_change`).
It took the suite to 100% with no regressions across 70 trials.

- **A tighter rewrite lost accuracy for no token saving.** `v3-tight` compressed
  the same rules into two dense sentences: it dropped a *clear* `tests_passed`
  trial (92.9% strict) while completion tokens barely moved (292 → 301). As with
  the selector, the thinking model uses the verbal room; compressing the rules
  costs reliability without buying latency. The fuller phrasing was promoted.
- **Cost of the win**: the longer watcher prompt adds ~340 prompt tokens and
  ~0.4s p50 on that task. Watchers run on the background supervisor queue (60s
  timeout), not the user's first-byte path, so accuracy is worth far more than
  the latency here — the opposite of the selector's trade.
