# Skill-selection benchmark

Two suites over one corpus, because there are two selectors and only one of them
serves traffic.

**`bench local` is the release authority.** It drives the shipped ONNX artifact
(`skills/local/artifacts/selector/`) through the production decision procedure —
the visible-active projection, the structural background-wake rule, the
earn-your-place admission floors, the bare-question docs pass — offline,
deterministically, and for free. It is gated in CI through
`dashboard bench run --deterministic-only`.

**There is no paid comparator any more.** `bench run` drove the prompt-based
`Selector` against a live API (~450 calls per variant). Both are deleted: that
selector had not been on the serving path since selection moved local, and a
comparator whose baseline nobody serves measures a road not taken at ~450 paid
calls a run. `local` is the routing verdict, and it is free.

### What `local` reports that nothing else can

A single end-to-end miss rate collapses four unrelated failures, and they want
four different fixes. Every missing skill is attributed to the stage that lost it:

| stage | meaning | where the fix is |
|---|---|---|
| `candidate_miss` | never reached the head | stage 1: `k`, evidence, retrieval |
| `rank_miss` | scored, and scored too low | stage 2: the head, training data |
| `floor_reject` | out-scored the cut, refused by a floor | calibration |
| `cardinality_short` | the count head asked for fewer | the count head |
| `cap_full` | the answer was already at `max_active_skills` | the cap |

`candidate_miss` is the one to watch. It is nearly unreachable at 38 skills
against `k=32` — 84% of the catalog is on the ballot regardless — and it is the
first number to move as the catalog grows, which is exactly why it is gated at
zero now rather than after it starts happening.

Latency is decomposed the same way (`sync`, `retriever`, `search`, `ranker`,
`head`, `docs`), because one aggregate cannot separate a tokenizer regression
from a catalog re-embed. Measured today: `ranker_ms` p50 ~9ms dominates,
`search_ms` p50 0.03ms is noise — which is the evidence behind not reaching for
an ANN index.

### Graded on recall-pass, not on the exact set

`local` gates on **recall-pass** (every required skill present); `run` gates on
the exact set. That is not a softer bar chosen to make numbers work — it is each
selector's actual contract. The local selector's `BONUS_FLOOR` admits a further
skill above 0.45 on purpose, because a turn that misses its runbook answers
without it while a spare runbook costs prompt bytes. Extras are designed
behaviour here and a defect there. The strict rate is reported and gated too,
well below the measurement, so a precision collapse still reddens the suite.

## Quick start

```bash
# from the repo root, venv active
python -m benchmarks.skill_selection.bench local         # THE gate: shipped ONNX, free
python -m benchmarks.skill_selection.bench local --fp32  # same, unquantised
python -m benchmarks.skill_selection.bench list          # validate cases, no API calls
python -m benchmarks.skill_selection.bench coverage      # per-skill coverage matrix, no API calls

python -m benchmarks.skill_selection.bench compare results/a.json results/b.json
```

Only `run` costs money. `local`, `list`, `coverage`, `report`, and `compare` are
offline and free — they work with `TOGETHER_API_KEY=` blanked. `report` reads
which suite wrote the artifact and regrades it under that suite's gate, so a
saved local run and a saved paid run are not graded against each other's
thresholds.

A full default run is `cases x trials` (currently 150 x 3 = 450) real selector
calls per variant — cheap on deepseek-v4-flash, but not free, and it is the bulk
of what `dashboard qualify-release` spends. Run `bench list` for the current
count rather than trusting this number.

`run` and `report` exit NON-ZERO on a quality failure, graded by
`skill_selection_verdict()` in `_harness/quality.py`. Both used to return 0
unconditionally, so a release job could watch cases fail on screen and still get
a successful exit.

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
failures are invisible to `bench local` by construction: an untested skill's
misses never appear in any `missing` list. Five skills sat at zero recall coverage for
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

## Results

Runs are saved to `results/<timestamp>-<variant>.json` (gitignored). The document
carries the catalog revision, the selector artifact's revision, per-case picks
and stage attribution, and the summary block. Latency is end-to-end per
selection (embed + shortlist + head + admission), i.e. exactly the first-byte
cost `/respond` pays — which is now in-process and free.

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
> expensive to learn. They describe a selector that no longer exists — the
> prompt-based one, deleted with its comparator — so nothing here can be
> re-measured. Read them as design history, never as a figure.

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
- **LEXICAL shortlisting was measured and rejected — do not re-propose it.** The
  idea (BM25-shortlist the catalog, send only the top-K onward) was scored over
  the exact metadata the selector sees: `recall@3 = 67%`, `recall@5 = 81%`,
  `recall@10 = 85%`, and it **never reaches 100% short of shipping the full
  catalog**. The cause is structural, not a tuning problem: routing in this
  catalog is carried by **negative** clauses ("do not use for X — use the Y
  skill"), which lexical scoring reads as strong *positive* evidence for exactly
  the wrong skill. On the `foundation-only` cases the required foundation ranked
  **dead last**. Anything bag-of-words inherits this, whatever the weighting.

  A **learned** shortlist does not: the shipped selector's stage 1 is a
  fine-tuned bi-encoder and it measures `recall@32 = 1.000` on held-out synthetic
  rows, precisely because training teaches it to read "do not use for X" as
  evidence AGAINST X rather than for it. The rejection above is about lexical
  scoring, not about shortlisting — do not read it as an argument against the
  architecture that shipped.
- **Shortlisting SHIPPED** (see `training/EXPERIMENTS.md`, Architecture C), which
  supersedes the "revisit trigger" this bullet used to carry. The trigger asked
  for a measured, catalog-size-attributable regression before shortlisting was
  worth it; what actually motivated it was not accuracy but the fixed head's
  coupling to the catalog — a skill added between builds could not be selected at
  all. The shortlist is a **learned** bi-encoder, and its job is filtering, not
  ranking: it is scored on `recall@32` alone (1.000), because the set-wise head
  re-decides from content afterwards. Deterministic namespace routing was never
  needed.
- Non-thinking baseline latency (p50 ~1.75s, p90 ~2.1s, max <3s over 660 calls)
  is near the floor for this output length; the remaining levers are output
  tokens (accuracy-coupled, see above) or a faster model.
