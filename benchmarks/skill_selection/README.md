# Skill-selection benchmark

Measures the **accuracy and latency** of the automatic skill selector
(`skills/selector.py`) against the live DeepSeek API, using the real production
path end-to-end: the real `Selector` class, the real selector prompt template, the
real catalog, `sanitize_for_selector`, the DeepSeek adapter, and the retry policy.
The only injectable seam is the prompts directory, which is how candidate prompts
are benchmarked before being promoted into `src`.

## Quick start

```bash
# from the repo root, venv active, DEEPSEEK_API_KEY in .env (or exported)
python -m benchmarks.skill_selection.bench list          # validate cases, no API calls
python -m benchmarks.skill_selection.bench run           # baseline (production prompt)
python -m benchmarks.skill_selection.bench run --variant my-idea
python -m benchmarks.skill_selection.bench compare results/a.json results/b.json
```

A full default run is `cases x trials` (~55 x 3 = ~165) real selector calls per
variant — cheap on deepseek-v4-flash, but not free.

## Cases (`cases/*.yaml`)

Each case freezes a conversation, the prior active-skill set, optional
`turn_context` / `available_tools`, and the expected selection:

```yaml
- id: rename-terminals-basic
  category: single-skill
  active: []                    # prior active set carried by the state token
  messages:
    - role: user
      content: Rename my terminals so I can tell the agents apart.
  expect:
    required: [daintree.terminals.rename]        # must ALL be picked (recall)
    optional: [daintree.orchestration.basic]     # allowed, not demanded
```

Scoring is strict per trial: `missing = required - picked` (recall failure),
`extra = picked - (required + optional)` (precision failure), pass = neither, not
degraded. `required: []` means the selector must **clear** the active set. Every id
is validated against the real catalog at load, so a typo fails immediately.

The `optional` slot exists mainly for the domain foundation
(`daintree.orchestration.basic`): pairing it with a specific skill is the
documented default, but a lean pick of just the specific also passes. Expectations
encode **product intent** — when a case's ground truth is debatable, the case
carries a `note` saying why.

Categories: `single-skill`, `routing-trap` (neighbor-skill confusion),
`no-task` (must return empty), `continuation` (keep the in-progress skill),
`topic-shift` (must evict the old skill), `foundation-only` (novel workflow, no
specific match), `adversarial` (instruction injection + keyword bait),
`multi-skill`, `background-wake` (turn_context-driven, no new user message).

## Prompt variants (`prompts/<name>/selector/system.md.j2`)

`--variant baseline` renders the packaged production template. Any other name
resolves to `prompts/<name>/selector/system.md.j2` — a full, self-contained
template (same Jinja context: `catalog`, `max_active`). To promote a winner:

1. Copy it over `src/daintree_assistant_server/prompts/selector/system.md.j2`.
2. Regenerate golden snapshots: `DAINTREE_UPDATE_GOLDEN=1 pytest tests/golden/`.
3. Re-run `bench run` (baseline now IS the new prompt) to confirm.

## Results

Runs are saved to `results/<timestamp>-<variant>.json` (gitignored). The document
carries the prompt SHA-256, catalog revision, model id, per-trial
picks/latency/tokens/model-stated reason, and the summary block. Latency is
end-to-end per selector call (render + API + validation + any repair/retry), i.e.
exactly the first-byte cost `/respond` pays.

## Findings (2026-07-07, deepseek-v4-flash, 55 cases x 3 trials per run)

| run | strict cases | mean | p50 | p90 | completion tok |
|---|---|---|---|---|---|
| baseline, pre catalog fix | 54/55 | 1.78s | 1.73s | 2.12s | 83 |
| **baseline + catalog fix** | **55/55 (x2 runs)** | **1.76-1.80s** | **1.73-1.79s** | **2.03-2.15s** | **83** |
| lean-reason (140-char reason) | 54/55 | 1.57s | 1.53s | 1.82s | 64 |
| lean-evict / lean-v3 / lean-v4 | 53/55 | ~1.6s | ~1.55s | ~1.83s | ~65 |
| mid-reason (200-char + evict slot) | 52/55 | 1.80s | 1.78s | 2.11s | 77 |

What moved the needle:

- **The one baseline miss was catalog text, not the prompt**: `multi-agent`
  listed "vote" as a `whenToUse` trigger (and a `vote` tag), colliding with
  `competition`'s "voting/peer review". Removing the collision and adding
  "including the agents reviewing and voting on each other's submissions" to
  `competition` took the suite to 100% with no regressions. When a routing miss
  is consistent across trials, fix the `whenToUse` routing first.
- **The 300-char `reason` budget is load-bearing.** Output length drives latency
  (corr ≈ 0.7, ~10ms/token), and compressing `reason` to ~140 chars saves
  ~0.2s mean — but every compressed variant flaked on marginal keep-vs-evict
  calls (keeping a finished workflow's skill "for context", over/under-loading
  the foundation), including variants with explicit rules forbidding exactly
  that. The non-thinking model needs the verbal workspace; rules don't
  substitute for it. `prompts/lean-reason/` is kept as the best latency
  candidate if that trade is ever wanted.
- Non-thinking baseline latency (p50 ~1.75s, p90 ~2.1s, max <3s over 660 calls)
  is near the floor for this output length; the remaining levers are output
  tokens (accuracy-coupled, see above) or a faster model.
