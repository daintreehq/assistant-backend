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
python -m benchmarks.skill_selection.bench coverage      # per-skill coverage matrix, no API calls
python -m benchmarks.skill_selection.bench run           # baseline (production prompt)
python -m benchmarks.skill_selection.bench run --variant my-idea
python -m benchmarks.skill_selection.bench compare results/a.json results/b.json
```

Only `run` costs money. `list`, `coverage`, `report`, and `compare` are offline
and free — they work with `DEEPSEEK_API_KEY=` blanked.

A full default run is `cases x trials` (currently ~95 x 3 = ~285) real selector
calls per variant — cheap on deepseek-v4-flash, but not free. Run `bench list` for
the current count.

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
    optional: [daintree.foundation]     # allowed, not demanded
```

Scoring is strict per trial: `missing = required - picked` (recall failure),
`extra = picked - (required + optional)` (precision failure), pass = neither, not
degraded. `required: []` means the selector must **clear** the active set. Every id
is validated against the real catalog at load, so a typo fails immediately.

The `optional` slot exists mainly for the domain foundation
(`daintree.foundation`): pairing it with a specific skill is the
documented default, but a lean pick of just the specific also passes. Expectations
encode **product intent** — when a case's ground truth is debatable, the case
carries a `note` saying why.

### Coverage (`bench coverage`)

`bench coverage` loads the real catalog and the real cases (offline, free) and
prints a per-skill matrix: `nbr` (declared confusable neighbours), `trap`,
`req` (recall), `alone` (sole required id), `opt` (allowed), `act` (prior-active
continuation pressure), and `neg` (cases where picking it would score as an
`extra`). It **exits 1** on either half of a two-part gate.

**1. Recall — is the skill reachable?** A suite that never demands a skill cannot
detect the selector missing it, so every skill needs at least one case with it in
`expect.required`. A `[foundation]` is gated on `alone` instead: the backend pairs
a foundation with a selected specific *downstream* of the classifier, so a case
whose answer is a specific must list it in `optional` (`cases.py` rejects the
mixed shape), and the only case that tests a foundation for recall is one where
it is the whole answer.

**2. Traps — is it reached for the right reasons?** Recall alone is a weak gate.
Routing quality in this catalog does not live in the positive clause of a
`whenToUse`; it lives in the carve-outs after it — "do not use for X, use
`daintree.<other>`". Each carve-out is a **claim** that a specific confusion
exists and that the catalog text resolves it, and an untested carve-out is an
unverified claim: a suite can be 100% green on recall while every one of those
claims is false, because a case whose bait matches only the positive clause is
passed by a selector that has never once distinguished the neighbours.

So `trap` counts, for skill S, the cases in category `routing-trap` where S is in
`expect.required` **and** at least one of S's declared neighbours is in neither
`required` nor `optional` — i.e. the case actively asserts that neighbour must not
be picked. A skill with `nbr > 0` and `trap == 0` fails. A skill that declares no
neighbours claims no confusion and **owes no trap** — the gate must not pressure
anyone into padding the suite with a manufactured one.

Neighbours are parsed out of the `whenToUse` text by scanning it for other catalog
ids on a token boundary. A carve-out that names its neighbour in **shorthand**
("use the competition skill") is invisible to that parse and its skill reads as
owing nothing — the fix is to write the fully-qualified id in the catalog, not to
loosen the gate.

Run it whenever the catalog changes — that is the moment a new or renamed skill
enters with no coverage, or a freshly-added carve-out enters untested, and both
failures are invisible to `bench run` by construction: an untested skill's misses
never appear in any `missing` list. Five skills sat at zero recall coverage for
three weeks before this existed.

When it reports a gap, the new case has to be **earned**: ground truth decidable
from the routing text on disk (quote the deciding clause in the case `note`), and
user text that reads like a real person rather than the skill's own trigger
phrases echoed back — an echo measures keyword overlap, not routing. If no clause
on disk decides the case, the carve-out itself is unverifiable; fix the catalog
text rather than inventing a case to turn the number green.

It is a bench subcommand, deliberately **not** a pytest test: `benchmarks/` is
outside `testpaths` on purpose and pulling the case tree into CI would make CI
depend on the live-benchmark suite.

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

## Historical findings — 2026-07-07, **13-skill catalog**, superseded

> **These numbers are not evidence about the current selector.** They were
> measured on **2026-07-07** against a **13-skill catalog** and a selector prompt
> that has been **rewritten twice since**. The catalog has since been consolidated
> (skills merged, the foundation renamed to `daintree.foundation`) and the prompt
> template changed, so neither the accuracy column nor the latency column carries
> over. The "55/55 strict" headline in particular says nothing about today —
> when `bench coverage` was first run on 2026-07-28, **5 of the 18 skills** then
> in the catalog had **zero required cases**, so a clean sweep was partly a
> measurement of what the suite declined to ask.
>
> Kept because the *mechanisms* below generalize and the trade-offs were
> expensive to learn. Re-run `bench run` before quoting any figure.

| run (2026-07-07, 13-skill catalog, 55 cases x 3 trials) | strict cases | mean | p50 | p90 | completion tok |
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
- **Two-stage lexical retrieval was measured and rejected — do not re-propose it.**
  The idea (BM25-shortlist the catalog, send only the top-K to the classifier, so
  the selector prompt stops growing with the catalog) was scored over the exact
  metadata the selector sees: `recall@3 = 67%`, `recall@5 = 81%`,
  `recall@10 = 85%`, and it **never reaches 100% short of shipping the full
  catalog** — so any shortlist silently caps the selector's ceiling below where it
  already is. The cause is structural, not a tuning problem: routing in this
  catalog is carried by **negative** clauses ("do not use for X — use the Y
  skill"), which lexical scoring reads as strong *positive* evidence for exactly
  the wrong skill. On the `foundation-only` cases the required foundation ranked
  **dead last**. Anything bag-of-words inherits this, whatever the weighting.
- **Revisit trigger for shortlisting:** a **measured** `bench run` regression that
  is **attributable to catalog size** — not a token-budget estimate, not "the
  catalog feels big". Absent that, the selector prompt growing is a cost, not a
  defect. And the first lever to reach for would be **deterministic namespace
  routing** (the dotted id hierarchy the catalog already carries, used the way
  `Catalog.domain_foundation` already walks it), not lexical scoring — a
  deterministic filter fails predictably and is auditable, where BM25 fails
  exactly on the cases the negative clauses were written to fix.
- Non-thinking baseline latency (p50 ~1.75s, p90 ~2.1s, max <3s over 660 calls)
  is near the floor for this output length; the remaining levers are output
  tokens (accuracy-coupled, see above) or a faster model.
