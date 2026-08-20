# Local skill selection — experiment log

> ## ⭐ SHIPPED CONFIGURATION — two-stage set-wise selector
> ```
> stage 1    sentence-transformers/all-MiniLM-L6-v2 (22.7M), fine-tuned, k=32
> stage 2    BAAI/bge-base-en-v1.5 (109M) + set-wise head (2.4M), ordering=equivariant
> corpus     training/data/selection.jsonl @ 9,774 rows
> train      stage 1: --epochs 6 --lr 3e-5 --batch-size 48 --seed 41
>            stage 2: --epochs 10 --ordering equivariant --seed 41
> serve      ONNX int8 encoders + fp32 head, ~410 MB whole-process RSS,
>            BONUS_FLOOR 0.45 / NEWCOMER_FLOOR 0.65 (re-calibrated, see below)
> ```
> **Current benchmark: shipped int8 measures 164/177 recall-pass (92.7%),
> fp32 163/177; docs flag 19/21; int8 p50 11.5 ms. Synthetic held-out
> exact-match 0.931.**
>
> The denominator moved 169 → 172 → 177 across two rounds on 2026-08-20, as docs
> and context cases were added to cover failures the suite could not see (see the
> two docs sections at the foot of this file). Compare artifacts on ONE suite,
> never a number against a number recorded under a different denominator: the
> previous artifact re-scored on this 177-case suite measures 161/177 with the
> docs flag at 14/21, so the context retrain is **+3 recall-pass and +5 docs**.
>
> That is **parity** with the fixed-head classifier it replaced (158/169, p50
> 9.0 ms), and parity is the honest headline — the number did not improve and
> the family's error bar is ±2. What did move is everything around it:
>
> | | Architecture B (fixed head) | Architecture C (two-stage) |
> |---|---|---|
> | recall-pass (int8) | 158/169 | 158/169 |
> | strict pass | 74/169 | **132/169** |
> | precision extras | 94 | **32** |
> | mean skills loaded/turn | 2.29 | **1.17** |
> | fp32 ↔ int8 decision diff | 32 cases | **5 cases** |
> | p50 latency | 9.0 ms | 11.6 ms |
>
> Same routing accuracy reached by being right rather than by over-loading and
> letting the recall bonus cover it. Half the skill bodies per turn is a direct
> saving in prompt bytes on every round of every turn.
>
> **The reason to switch is structural, not the accuracy.** Neither stage owns a
> per-skill weight: stage 1 embeds skill documents, stage 2's head consumes those
> embeddings. So
>
> 1. **a skill added at runtime routes without a retrain** — embedded by
>    `SetwiseClassifier.sync()` on catalog load, competing on equal terms. Under
>    Architecture B this was structurally impossible (no head row) *and* the
>    `routing_revision` gate degraded every turn open until someone retrained,
>    which put the selector in direct conflict with skill learning;
> 2. **there is no staleness gate at all** — the hash is still stamped, but as a
>    drift signal for a human, gating nothing;
> 3. **catalog growth costs one table row (~4.5KB), not a wider head.** The head
>    always sees `k` candidates, so its job is the same size at 38 skills and at
>    2,000; only the cosine search grows, and that is a matmul.
>
> Rebuild:
> ```
> .venv-train/bin/python -m training.train_retriever
> .venv-train/bin/python -m training.train_setwise --epochs 10 --ordering equivariant
> .venv-train/bin/python -m training.export_two_stage
> .venv-train/bin/python -m training.eval_two_stage --ablate
> ```

Every configuration tried, what it scored, and what it taught. Kept because the
path here was not a straight line: the obvious architecture (retrieve then
rerank) was built, trained, measured and **abandoned on evidence**, and without a
record the same ideas get re-tried.

## The two numbers everything is measured against

| | value |
|---|---|
| DeepSeek selector, benchmark | **138/146 (94.5%)** |
| DeepSeek selector, latency | **p50 2,044 ms / p90 2,806 ms** |

Benchmark = `benchmarks/skill_selection`, 150 held-out cases, exact-match
(all required ids present, no extras; the domain foundation is allowed-not-required).

**Caveat that grows with this document:** the benchmark has now been inspected
across many iterations. It has drifted from a clean holdout toward a development
set. Numbers below are honest measurements, but a final claim of generalisation
needs a fresh holdout.

---

## Architecture A — bi-encoder shortlist + cross-encoder reranker

The conventional design. Built in full, then abandoned.

| # | configuration | benchmark | synthetic | note |
|---|---|---|---|---|
| A1 | zero-shot MiniLM-L6 + Ettin-17M | — | — | recall@10 0.860; go/no-go passed |
| A2 | fine-tuned bi-encoder, top-1 only | **108/150** | — | no reranker at all |
| A3 | + reranker, `margin` admission | 65/150 | 0.607 | 137 precision-extras — admission rule wrong |
| A4 | + `relative` admission | 90/150 | 0.641 | pos_weight 1→8 fixed score compression |
| A5 | + `full` documents (routing clauses) | 90/150 | 0.731 | +9pp synthetic, flat on benchmark |
| A6 | + serving cap 256→512 | 89/150 | — | fixed a 75%-truncation bug; **no gain**, 1.8x latency |
| A7 | bi ordering + reranker empty-gate | 111/150 | — | sentinels inert: 111 at every threshold 0.3–0.7 |

### Why it was abandoned

On an **identical shortlist**, over the 136 cases needing ≥1 skill:

| ordering | recall@1 | recall@3 |
|---|---|---|
| bi-encoder | **0.779** | 0.941 |
| cross-encoder | 0.699 | 0.926 |

The reranker pushed the correct answer **off** rank 1 in 20 cases and pulled it
on in 9 — net −11. It was also 309ms of a 312ms pipeline. It cost the entire
latency budget to make ranking worse.

Independently confirmed by Codex (gpt-5.6-sol), which added that the
`sigmoid(top) < 0.5` admission floor alone was rejecting **30 valid turns**, and
that bi-ordering with *oracle* cardinality caps at 123/150 — i.e. the residual
error is cardinality, not ranking.

---

## Architecture B — multi-label classifier (REPLACED by C)

One encoder, one head: 32 skill logits + 1 docs logit + 4 cardinality logits.
The head's weight matrix **is** the skill embeddings, so a bigger catalog costs
one row (384 floats), not a wider input.

| # | configuration | benchmark | synthetic | note |
|---|---|---|---|---|
| B1 | MiniLM-L6, threshold rule, 8 epochs | 113/150 | 0.441 | undertrained; threshold-sensitive |
| B2 | + 30 epochs | 113/150 | 0.816 | |
| B3 | + fusion with bi-encoder (α=0.8) | 113/150 | 0.859 | |
| B4 | + cardinality head, fusion α=0.4 | **115/150** | 0.871 | routing-trap 52/58, single-skill 31/33 |
| B5 | + head init from skill embeddings | — | 0.804 | **worse**; constrains training. Kept opt-in |
| B6 | + full inverse-frequency balancing | 114/150 | 0.816 | topic-shift up, routing-trap 52→46 |
| B7 | classifier ALONE via ONNX fp32 | 114/150 | — | **2.05 ms p50**; beats fusion on topic-shift |
| B8 | + rebalanced corpus (topic-shift 88→223) | 114/150 | 0.849 | topic-shift 2/9→6/9, docs flag 15/16 |
| B9 | + round-2 diversity corpus (4,768) | 117/150 | 0.850 | data alone: +3 |
| B10 | + bge-base backbone (109M) | 121/150 | 0.874 | model alone: +4. p50 9.8ms |
| B11 | + foundation-label fix (see below) | — | 0.850 | 104 rows unpaired; card-2 rows 171→83 |
| B12 | + round-3 contrastive corpus (5,224) fp32 | 126/150 | 0.841 | |
| **B13** | **same weights, int8** | **130/150** | — | **86.7%, p50 8.9ms** |
| B15 | + wake corpus & hardened gates (6,399), review fixes | **130/150 int8** | 0.850 | canonical serving-path eval; topic-shift 8/9, foundation 5/6, adversarial 4/5. fp32 129, diff 7 cases |
| B14 | + round-4 session corpus (6,285) | 129/150 | 0.852 | fp32 = int8 = 129: decisions stabilised. foundation-only 2/6→**5/6**, multi-skill 2/5→**4/5** (both now match DeepSeek), no-task 6/6 |

### The foundation-label bug (found by Codex)

104 training rows labelled `daintree.foundation` ALONGSIDE a specific skill, and
96 of 171 cardinality-2 rows were really "specific + auto-paired foundation".
But the backend pairs the foundation downstream and the benchmark puts it in
`optional`, never `required`. So most of the cardinality head's evidence for
"this turn needs TWO skills" meant something the classifier is never asked to
produce. Stripping foundation from any multi-label target was worth several
cases.

### Backbone capacity (B8 corpus, 30 epochs)

| backbone | params | synthetic exact-match |
|---|---|---|
| all-MiniLM-L6-v2 | 22.7M | 0.849 |
| all-MiniLM-L12-v2 | 33M | **0.872** |
| BAAI/bge-small-en-v1.5 | 33M | **0.872** |

Capacity helps, modestly.

---

## Architecture C — retrieved shortlist + set-wise head (current)

    conversation ─► MiniLM-L6 (fine-tuned) ─► cosine vs the embedded catalog ─► top-32
    conversation ─► bge-base  (fine-tuned) ─► q ──┐
    skill table  ────────────────────────────► s_i ┴─► set-wise head ─► 32 logits
                                                                      + docs + cardinality

The head is 2 transformer layers over `[CLS(q), cand_1 … cand_32]`, where
`cand_i = W([s_i ; q⊙s_i ; |q−s_i|]) + W_f(features_i)`. It owns **no per-skill
parameters**, which is the whole point — see the header for what that buys.

This is **not** Architecture A. Nothing re-reads a skill's text at request time;
skill vectors are computed once and looked up. A's cross-encoder cost 309ms of a
312ms pipeline scoring (query, document) *pairs*; this head reads cached vectors
and costs well under a millisecond.

### Stage 1 — how small can the filter be

Stage 1 is a **filter**, not a scorer: its only job is to put the answer inside
the top `k`. So `r@32` must be 1.000, and among backbones that clear it the
choice is cost. `r@8` is reported as the scale proxy — 8 of 38 is the same
*fraction* that `k=32` would be of a 150-skill catalog.

| backbone | params | int8 | p50 | r@3 | r@8 | r@32 |
|---|---|---|---|---|---|---|
| prajjwal1/bert-tiny | 4.4M | 4.5MB | 0.25ms | 0.781 | 0.928 | **0.999** ✗ |
| paraphrase-MiniLM-L3-v2 | 17.4M | 17.4MB | 1.15ms | 0.921 | 0.973 | 1.000 |
| **all-MiniLM-L6-v2** | **22.7M** | **22.9MB** | **2.23ms** | 0.959 | **0.990** | **1.000** |
| BAAI/bge-small-en-v1.5 | 33.4M | 33.8MB | 4.46ms | 0.966 | 0.990 | 1.000 |

**MiniLM-L6 strictly dominates bge-small** — identical on both metrics that
matter, 32% fewer parameters, half the latency. bge-small was the initial pick
and buys nothing. bert-tiny is 9x cheaper again and is rejected on `r@32`: a
shortlist that drops the answer caps the entire selector below where it already
is, and no amount of head quality recovers a candidate that never arrived.

Reproduce with `training/sweep_retriever.py` (5 epochs each, ONNX int8, single
intra-op thread, a realistic ~35-token query).

### Stage 2 — the encoder does NOT need to be big

| stage-2 backbone | synthetic exact-match | benchmark int8 | int8 size |
|---|---|---|---|
| BAAI/bge-base-en-v1.5 | 0.904 | **158/169** | 110MB |
| BAAI/bge-small-en-v1.5 | **0.911** | 155/169 | 34MB |

bge-small scored **better on synthetic val and worse on the benchmark** — the
clearest instance yet of finding 6 below (synthetic val overstates). It is a
genuine 76MB / ~6ms option if 3 cases are ever worth trading; it is not shipped
because parity with Architecture B was the bar.

### The ordering question — settled by measurement

Does the shortlist's ORDER carry information the head should use? Three arms,
same shortlist, same corpus, each read at its own calibrated floor:

| arm | recall-pass | extras | mean set | order ablation |
|---|---|---|---|---|
| **equivariant** (no position; cosine+rank as features) | **158/169** | **32** | **1.17** | **0/165 changed** |
| positional (cosine order + learned rank embedding) | 154/169 | 34 | 1.11 | **151/165 changed** |
| positional at its own best floor (0.25) | 159/169 | 65 | 1.34 | 151/165 changed |
| blind (equivariant, retrieval features zeroed) | 158/169 | 33 | — | 0/165 changed |

**Shuffle, do not order.** At matched precision the equivariant head wins
outright (158 vs 154); positional only reaches 159 by loading roughly twice the
extras. And the ablation shows what it learned to get there: reversing the
shortlist changes **151 of 165** answers, i.e. it is leaning on "rank 0 usually
wins" rather than on content. That prior is exactly wrong for the skill this
architecture exists to serve — one added after training, whose cosine rank the
head never trained against.

The `blind` arm is the one to read carefully, because it is easy to misread.
It zeroes the cosine/rank **features fed into the head**; both arms get the
IDENTICAL shortlist, so it says nothing about the filter. What it shows is that
the head decides from the embeddings alone, and its accuracy is **independent of
stage 1's scores**. That is a property worth having rather than a defect: when
the catalog reaches thousands and the filter's scores get noisier, only `r@k`
degrades — accuracy does not ride on the scores. The features are kept because
they cost 3 floats and the path stays trained, not because they are currently
earning anything measurable at 38 skills.

### Zero-shot: what "no retrain needed" actually buys — MEASURED

The architecture's headline claim is that a skill added after the build routes
without a retrain. It is true in the sense that matters operationally, and it is
much weaker than it sounds. Measured by holding three skills out of **both**
stages' training data and scoring only the benchmark cases that require them
(`training/eval_zero_shot.py`):

| | zero-shot | same recipe, trained |
|---|---|---|
| cases requiring a held-out skill | **4/14** | 13/14 |
| the other 141 cases | 130 | 131 |

Per skill, the pattern is the whole finding:

- `daintree.terminals.rename` — **4 of 7**. Distinctive vocabulary ("rename",
  "titles", "tabs") that no neighbour claims, so its document wins on content.
- `daintree.forge.manage-issue` — **0 of 4**. Loses every case to
  `start-work-on-issue` / `find-issue-work`.
- `daintree.runtime.debug-web-app` — **0 of 3**. Loses every case to
  `diagnose-and-fix`.

This is the asymmetry recorded at the top of this file, hit from the third
direction: **a new skill's strongest words are stolen from its neighbours**, and
here the neighbours are trained while the newcomer is not, so the newcomer loses
every *contested* turn and wins only the unambiguous ones.

So state the property precisely, and do not let it drift in the docs:

- Before (Architecture B): a new skill was **structurally unselectable** — no
  head row — *and* it moved `routing_revision`, so the selector degraded EVERY
  turn open until someone retrained. The catalog broke, not just the new skill.
- Now: the catalog keeps working, and the new skill is reachable and wins
  requests only it plausibly answers. It will still lose to established
  neighbours until it has boundary data of its own.

**A retrain is no longer what makes a skill possible; it is what makes it
competitive.** That is a large operational improvement and not a licence to stop
retraining. `.claude/skills/retrain-selector` says the same thing.

### Cross-learning: what a shared head does and does not buy

With no per-skill parameters, every training row teaches the SHARED reader —
"how does a request map onto a whenToUse fence" — rather than filling in one
skill's row. So competence transfers across skills in a way it structurally
could not before. Two claims come out of that, and only one of them is measured.

**Proven.** A skill held out of both stages still takes 4 of 14 of its own
benchmark cases (see the zero-shot section above). Under a per-skill head that
number is **0 by construction** — there is no output to activate. Every one of
those 4 is transfer from other skills' data, so the mechanism is real and
demonstrated, not inferred.

**Not yet measured: that it COMPOUNDS.** The appealing version — each skill
added makes every other skill route better, so the catalog stabilises as it
grows — does not follow from the above and is not established here. The one
adjacent data point is mildly against a large effect: removing 411 rows (three
skills, ~5% of the corpus) moved the remaining 141 cases by 130 vs 131, i.e.
nothing outside the noise band.

The experiment that would settle it, when there is a reason to run it: train
stage 2 on data for N of the catalog's skills for N in {10, 20, 30, all}, and
each time score the SAME held-out skills. If cross-learning compounds, zero-shot
performance on the held-out set rises with N even though their own data is never
present. That is a learning curve over *breadth*, not volume, and it is the only
version of the question the benchmark can answer.

**And note where transfer is weakest, because it is exactly where the catalog is
heading.** The zero-shot breakdown is not uniform: `terminals.rename` scored 4/7
with no training, `manage-issue` 0/4 and `debug-web-app` 0/3. Distinctive
vocabulary transfers; **discrimination between two subtly different neighbours
does not**, because that distinction lives in the PAIR and not in either
document alone. A shared reader can learn "read the fence"; it cannot learn a
fence nobody wrote. As overlapping skills accumulate, boundary data — in both
directions — is the thing cross-learning does not give you for free.

### Skill TEXT is now the fast routing lever

A corollary worth using. Editing a `whenToUse` used to move `routing_revision`
and require a retrain before it took effect, so text tuning was a ~20-minute
loop. Now the edited skill is re-embedded on catalog load — **one** skill, one
forward pass — and the change is live. Measured on the shipped artifact: adding
one clause to `review.review-change`'s whenToUse (that "tell me what you'd push
back on" is a request for FINDINGS even when framed as pre-push) took the
benchmark 158 -> 159, one case fixed and none broken, with 1 of 38 skills
re-embedded and nothing retrained.

`training/try_skill_text.py` scores a proposed edit against the whole suite from
an in-memory overlay, writing nothing. Use it, and read both columns: a
locally-correct carve-out that fences on words two skills legitimately share is
a regression, and this file already records one that shipped and had to be
reverted. Tuning text against a suite this heavily inspected also fits it — a +1
inside a ±2 error bar is not evidence on its own, and the edit should be
defensible as a statement about the skill before the number is consulted.

### Why brute-force cosine and not an ANN index

The shortlist's whole contract is `r@k = 1.000`: a candidate that never reaches
the head cannot be recovered downstream, so a shortlist miss caps the entire
selector. Approximate search sells exactly the property this stage cannot spend
— IVF scans only the `nprobe` nearest k-means buckets, so a skill in an unprobed
bucket is invisible; HNSW's recall is a tunable below 1.

And the speed it buys is not needed. `M @ q` + `argpartition` over float32,
measured (see the latency breakdown above): 0.015 ms at 2,000 skills, 1.22 ms at
100,000. Where the crossovers actually sit:

| skills | tables | search | search as % of turn |
|---|---|---|---|
| 2,000 (the design target) | 9 MB | 0.02 ms | 0.2% |
| 10,000 | 46 MB | 0.12 ms | 0.9% |
| 100,000 | 461 MB | 1.22 ms | 8.2% |
| 875,000 | 4 GB | 10.7 ms | 44% |

- search reaches **10% of the turn** at ~111,000 skills
- search reaches parity with the ranking encoder at ~862,000 skills

At 2,000 skills IVF would be self-defeating anyway: with the usual
`nlist ~ sqrt(N)` it compares ~45 centroids and then scans ~45 vectors — about 90
comparisons against our 2,000, saving ~0.013 ms — while adding an index build, a
large binary dependency, and approximate recall.

**The axis that binds first is MEMORY, not latency.** The tables are 4.5 KB per
skill (384-d retrieval + 768-d ranking, float32) and fill 1 GB at ~217,000
skills, while search does not become meaningful until ~111,000. So if this
catalog ever reached that scale the lever is **compressing the vectors** —
float16 halves the tables outright, Matryoshka truncation or PQ goes further —
not bucketing them for speed. If a vector library ever earns its place here it
will be for compression, not for approximate search; those are separable, and
compression is the one that would actually be needed.

Caveat on the scaling row: it is a synthetic table of random unit vectors, which
measures the arithmetic honestly but not real embedding geometry. That is fine
for brute force, whose cost is fixed by shape rather than content — but it is
another reason not to reach for an ANN index speculatively, since ITS recall
depends entirely on how clustered the real vectors turn out to be.

### `k` is a tunable, not a constant

The head is self-attention over candidate slots, so its cost is O(k²) — k=32 is
~1k pairs and sub-millisecond. Scoring *all* N candidates would be more accurate
in principle, but at N=2,000 that is 4M attention pairs plus a 2,000x768 gather,
and it moves the trained distribution hard (the head learned "about one positive
among 32"). The scalable response to a bigger catalog is to **raise k** — 64 or
128 are still cheap — not to abandon the filter. Note the exported head graph is
STATIC in k (see below), so changing it means re-exporting.

### Traps hit while building this, all of which failed silently

1. **`nn.MultiheadAttention` bakes the sequence length into its traced
   reshapes.** Exporting the head with a "dynamic" candidate axis produces a
   graph that loads happily and throws at run time on any k but the traced one.
   The head is therefore exported FULLY STATIC and serving pads to k with
   `mask=0`. Caught only by running a k=12 input against a k=32 export.
2. **Skill documents are longer than they look**: p50 262 tokens, max 368. A
   192-token cap — a reasonable-looking number borrowed from the query side —
   truncates exactly the `whenToUse` exclusion clauses that carry the routing
   signal. `doc_max_length` is 384 and the exporter asserts both stages agree.
3. **A vector table must be built by the same precision that queries it.**
   `vectors.npz` was built with int8 encoders and reused by an fp32 session, so
   documents and queries came from two different functions. It moved the
   benchmark by 2 cases between otherwise identical exports and read as run
   noise. `SkillTable` now carries a `precision` stamp and a mismatch re-embeds.
4. **Per-tensor int8 is not enough for two stacked encoders.** Per-tensor
   quantisation moved the CORE decision on 14/169 cases and cost 5 recall-pass;
   `per_channel=True` — same size, same speed — took that to 1/169.
5. **A rare head needs its own `pos_weight`.** Only 2.4% of the corpus wants a
   docs lookup, so unweighted BCE answers "no" to everything and scores 97.6%
   accuracy while the benchmark docs flag falls 12/16 → 9/16. Weighted: 11/16.
6. **Admission floors do not survive an architecture change.**
   `BONUS_FLOOR` was 0.55 under Architecture B and is 0.45 here: this head is
   trained with a slot-level BCE over k candidates rather than a 38-way
   per-label one, which compresses its probabilities downward. Measured on the
   shipped artifact, recall-pass is FLAT at 158 for every floor from 0.20 to
   0.45 and only extras move (55 → 41); 0.55 gives up a case. 0.45 is the knee —
   the lowest floor still buying recall — deliberately not the benchmark's peak.
   `training/calibrate_floors.py` exists so this is re-derived, not remembered.

---

## Latency (single thread, matching a 1-vCPU container)

| approach | p50 | vs DeepSeek |
|---|---|---|
| DeepSeek selector (network) | 2,044 ms | 1x |
| cross-encoder pipeline, full docs (Arch A) | 309 ms | 7x |
| classifier + bi-encoder fusion (Arch B era) | 3.1 ms | 661x |
| classifier alone, MiniLM (Arch B era) | 2.1 ms | 955x |
| fixed-head classifier, bge-base int8 (Arch B, shipped before) | 9.0 ms | 227x |
| **two-stage set-wise, shipped (Arch C)** | **11.6 ms** | **176x** |

Architecture C costs ~2.6ms more than the fixed head it replaced: a second
encoder pass over the conversation (MiniLM-L6, 2.2ms) plus the cosine search and
the head (both sub-millisecond). Against the 2,044ms call this whole line of work
replaced, the difference is not worth a decision. |

Concurrency, through the real worker pool: 1 worker with 8 simultaneous rankings
peaked at 280ms on the *reranker* design. At 2ms the question is moot.

---

## Findings worth not re-learning

1. **The reranker was the wrong instrument.** The residual errors are cardinality
   (return 0, return 2), which is a property of the query, not of a
   (query, document) pair. A pairwise scorer has no second argument for it.
2. **A threshold cannot express both "zero" and "two."** Sweeping it moved the
   benchmark between 75 and 113 without getting both right. A cardinality head
   (96% accurate) does it in one prediction.
3. **Conversation history trades continuation against topic-shift.** With 320
   chars: continuation 9/9, topic-shift 1/9. With none: 4/9 and 6/9. No setting
   wins both, because what separates them is *which skills are loaded* — state,
   not words. Fixed by putting the active set into the input (`[loaded: …]`).
4. **Corpus balance shows up directly as category failures.** 272 follow-ups vs
   88 topic-shifts taught "loaded → keep", and the model ranked stale skills at
   0.98 confidence. Generating 135 more topic-shifts moved it 2/9 → 6/9.
5. **int8 changes decisions on the classifier** (which skill ranks first).
   *(MiniLM-era finding — bge-base int8 passes the tiered parity gate and IS
   the shipped precision; see header.)*
6. **Synthetic val overstates generalisation by ~10pp** (0.85 vs 0.76). The
   split is row-random over paraphrase families. Confirmed again under
   Architecture C from the other direction: bge-small scored HIGHER on synthetic
   val than bge-base (0.911 vs 0.904) and 3 cases LOWER on the benchmark. When
   the two disagree, the benchmark decides.
7. **A cheap filter does not need to be an accurate ranker.** Stage 1 is scored
   on `r@32` alone; its `r@1` (0.835) is irrelevant, because the head re-decides
   from content. Choosing a retriever on ranking quality would have bought
   bge-small's extra layers for nothing.

---

### Seed / precision variance (the honest error bar)

Round-4 corpus, same recipe: seeds 17/23/41 scored 129/127/130 (torch fp32);
int8 exports moved individual runs by ±2 more. Synthetic val varies only ±0.002
— the benchmark spread is 20-odd near-tie cases flipping, not model quality
moving. Report the family as **129 ± 2**; do not chase single-run peaks.

3-seed logit-mean ensemble: 131/150. Weight soup: 127 (heads unaligned across
seeds — random init per seed — so averaging them is meaningless).

## Serving-policy decisions (measured, settled)

- **int8 encoders, fp32 head.** The encoders carry ~99% of the parameters and
  all of the memory pressure; the head is 2.4M parameters (9.5MB fp32), so
  quantising it would trade numerical slack for memory nobody is short of.
  Quantisation must be PER-CHANNEL — see Architecture C trap 4. Measured
  whole-process RSS with both encoders loaded: ~410MB.
- **Cloud Run: --cpu=1 --memory=2Gi --concurrency=8.** 1Gi would run today at
  ~55% but leaves no headroom for the 100-200 skill catalog target plus
  concurrent turn buffers; the second GiB costs ~$7/mo at min-instances=1.
- **Earn-your-place admission** (setwise.py NEWCOMER_FLOOR_WHEN_ACTIVE=0.65):
  with skills already loaded, a skill NOT in the active set needs p>=0.65 to
  join; active skills and fresh-turn picks ride on the model's ranking alone.
  Encodes the asymmetric cost of adding mid-conversation (re-encodes the skill
  block + dilutes running runbooks). Benchmark-neutral: 130/150 with and without.
- **Integration design (deferred to tear-out phase)**: at ~9ms the selector
  completes before speculation is worth starting — select first, assemble once,
  ONE upstream call. The speculative-stream machinery is unnecessary on this
  path.

## Open / untried

- **Hosted embedding model** (e.g. Qwen3-Embedding-8B via OpenRouter) with a
  frozen base and a trained head. Trades ~2ms local for a network call, but a
  much stronger representation. The question is whether the accuracy gain is
  worth reintroducing a round trip — at ~200ms it would still be ~10x faster
  than the call being replaced.
- **A bigger `k` as the catalog grows.** Untested above 32. The head is O(k²) in
  its attention, so 64 and 128 are cheap; the open question is whether a head
  trained at one `k` transfers to another or needs retraining at the target.
- **Stage 2 on bge-small** to save 76MB and ~6ms, at a measured cost of 3
  benchmark cases. Worth revisiting if the memory ever binds.
- **A genuinely fresh holdout suite.** The caveat at the top of this file now
  applies to Architecture C's numbers too: the benchmark (172 cases) has been
  inspected across many iterations and has drifted toward a dev set. The
  measured claims here are honest, but a final generalisation claim needs ~50
  cases written blind. The docs-head bug at the foot of this file is the sharp
  argument for it — the suite mirrored the corpus's assumptions, so it scored a
  broken feature as healthy for as long as both were wrong the same way.

## The docs head learned "is anything in the context?" (2026-08-20)

Found by diagnosing a live session, not by the benchmark — which had the same
blind spot and reported the feature healthy throughout.

**Symptom.** A plain usage question ("How do you create a worktree in
Daintree?") asked as the second turn of a conversation retrieved no
documentation, and the turn answered a product question with no product
documentation. The selector ran clean: `ran=true`, `degraded=false`, zero skills,
no docs query.

**Cause — a lossless shortcut in the corpus, not the architecture.** Of 8,640
rows, all 207 `is_docs=true` positives carry an empty active set AND no history,
while all 1,956 rows carrying an active set are labelled `is_docs=false`. The
separation is perfect and never once contradicted, so "anything in the context
means this is not a docs turn" is free accuracy and the head took it. Every
context channel triggers it independently:

| the same question, plus… | docs_p |
|---|---|
| nothing (as trained) | 0.9998 |
| `[loaded: daintree.foundation]` | 0.0020 |
| one turn of ordinary history | 0.00017 |
| a conversational lead-in inside the message | 0.001 |

Measured over 120 docs positives: **113/120 asked cold, 5/120 with a skill
loaded.** Since `query_document` always prepends a 320-char history tail, the
practical reading is that documentation lookup worked on the FIRST message of a
conversation and almost nowhere else.

**Shipped mitigation** (`setwise.py`): the docs logit is re-read from a SECOND
COMPLETE PASS over the bare newest user message — its own retrieval, shortlist,
cosine/rank features, and an empty active set. Cost is +6.7 ms, skipped entirely
when the docs string and the ranking query agree.

A cheaper version reused the primary shortlist and swapped only the query
vector, and the measurement that justified it (113/120 either way) was worthless:
k is 32 against 38 skills, so any two shortlists must share ≥26 candidates and
retrieval is nearly a no-op. At 64 skills two top-32 sets can be disjoint, and
the seam would have opened silently on exactly the topic-shift turns this feature
exists for. **A measurement taken where the mechanism cannot operate is not
evidence about the mechanism.**

`DOCS_THRESHOLD` moved 0.5 → 0.40 at the same time, and the honest justification
is only the cheap half: the head is bimodal, so on the corpus the two are
indistinguishable (199/207 recall, the SAME 6 false positives, 0 on chatter down
to 0.15), which makes 0.40 a free standing bias toward retrieving. It was
originally chosen to rescue second-person phrasings measured at 0.4471/0.4783 —
**that justification did not survive.** Those same two queries disagree between
fp32 and int8 on one machine, and between arm64 and x86-64 on the same artifact;
asserting one of them in a unit test passed locally and reddened CI. A case
decided by quantisation noise is not owned by any threshold.

**This is a workaround pinned to a data artifact.** It strips context from
OUTSIDE the message; it cannot strip framing INSIDE one, which is why
`docs-question-mid-workflow` ("Hang on, before you push — what does Daintree
actually do with…") still reads 0.001 where the same question without the
lead-in reads 0.9994.

**The durable fix is corpus, and it is the next retrain's main job:**

1. **Docs positives carrying context.** Every one currently has neither history
   nor an active set. They need both, in the proportions real turns have them.
   Until then the head is only in-distribution on a bare question.
2. **Second-person phrasing.** 66 of the 207 docs rows contain the token "I"
   (76 counting my/me/mine) against 4 mentioning "you", and none is of the form
   "how do you <do X>" — a user asking how the PRODUCT does something, which is
   one of the commonest ways the question is actually asked.
3. **Conversational framing.** Lead-ins, hedges and mid-sentence questions
   ("hang on —", "quick q,", "before you push"). Exactly ONE of the 207 rows
   opens with a lead-in and one is compound — near-total absence, which is what
   makes "Hang on, before you push — what does Daintree…" read as 0.001.
4. **Compound questions.** "What actually is a watcher, and when would I want
   one?" reads 0.0142 cold, against 0.9997 for "what is a watcher".

After that retrain, re-measure `DOCS_THRESHOLD` — like the admission floors it
is calibrated to one artifact's sigmoid and does not transfer.

**And take the tuning lesson, which is the second time this file has met it:** a
constant justified by the single case it rescues is only as stable as that case.
0.4471 sat 0.047 above the floor and moved under quantisation alone. Justify a
threshold by what it COSTS across the corpus — that is stable — and treat any
case within a few hundredths of it as unowned until the data moves it.

**Benchmark lesson, worth more than the bug.** Of the 9 docs positives, NONE
carried more than one message and only one carried an active set. The suite
mirrored the corpus, so it could not see a failure the corpus had caused: it
scored 11/16 on the docs flag while the feature was dead in the field. Three
cases were added that put an ordinary usage question somewhere other than the
opening message — after an unrelated exchange, with a skill loaded, and in the
second person. All three fail their DOCS EXPECTATION before the mitigation and
meet it after; the docs flag goes 11/19 → 14/19.

Be precise about what that does and does not say. `recall_pass_flags` scores
skill ids only and ignores `expect.docs` entirely, so the routing headline
(158/172) is not evidence for this fix and never could be — the docs score is
the separate 14/19. Nor are the added cases free of routing content: `required:
[]` means the selector must come back empty, and
`docs-usage-question-after-unrelated-exchange` deliberately records a skill
extra it should not pick.

When a benchmark is generated from the same assumptions as the training data,
agreement between them is not evidence.

---

# Context-bearing turns: a greeting erased the task (2026-08-20)

Reported from session `ses_3a3cd1e7`. Two turns in — "How are you?", then "How do
I create worktrees in this app?" — the server loaded **no skill and retrieved no
documentation**, and the model answered from the tool schemas alone. Since the
only worktree-creation tool the CLI exposes is `worktree__createWithRecipe`,
whose description is entirely about recipes, the answer confidently explained
that worktrees are made from recipes. The correcting sentence ("omit `recipeId`
and it creates only the worktree") lives in `create-plain-worktree`'s body — the
skill that did not load.

The selector logged `ran=true, degraded=false, cardinality=0`. Nothing failed.
It answered, and the answer was "no task, no docs".

## Two independent causes, both in the corpus

**1. A social lead-in is under-represented, and the conversational ones absent.**

An earlier draft of this section said the corpus held no greeting histories at
all. **That was wrong, and the correction is worth more than the claim was.** Of
2,116 history-bearing rows, 43 DO open on a greeting — but every one is terse and
answered in a single clause: `user: hey` / `assistant: Hi — workspace is idle,
nothing running.` That is 2.0%, and it does not contain the shape that failed: a
conversational opener answered with a real status paragraph.

The error came from a regex sweep that looked for "hi there" and "hey there" and
never for bare "hey" or "hi", and from writing off its 26 hits as incidental
without reading them. It was caught by the double-application guard in
`augment_context.py`, which used a greeting as its signature and reported 43
false positives against a corpus it should have called pristine — a check written
for something else entirely. The measurement below was always right; the
explanation attached to it was not. Measured on the pre-retrain artifact:

| history in front of "How do I create worktrees in this app?" | top skill | cardinality |
|---|---|---|
| none | 1.000 | 1 |
| `check on the fleet` / `Four running…` (task) | 1.000 | 1 |
| 105 chars of meaningless padding | 0.998 | 1 |
| `How are you?` / `Good — thanks. Everything's quiet…` | **0.248** | **0** |
| `How are you?` / `Good, thanks. How can I help?` | **0.005** | **0** |

**It is not dilution.** Padding of the same length in the same slot leaves the
score at 0.998. It is the pleasantry, and the head answered it with the
zero-cardinality prior it had learned for chatter — a prior 43 terse examples
were never going to move, and which nothing in the corpus contradicted at the
length and register real sessions actually open with.

**2. Sixty-four documentation questions were labelled `is_docs=False`.** They sat
in `no_task_stateful`, whose `assemble_data` branch hardcodes the flag, so every
usage question that arrived with history or an active set trained as NOT a docs
turn: "what actually is a watcher", "where does daintree put worktrees on disk",
"how do i change which coding agent daintree uses by default".

This is a sharper diagnosis than the one recorded above it. The previous entry
read the 113/120-cold vs 5/120-loaded split as *absence* — docs positives never
carrying context. It was worse than absence: the same question was in the corpus
labelled both ways, and the contradictions all sat on the contextful side. A
perfect correlation the head can exploit is bad; a perfect correlation that is
also **wrong** is what produced a confident 'no'.

Deixis was the third: `"in this app"` appeared in **0 of 207** docs rows, and
`"how do you …"` in **0 of 207**. Cold, "How do I create a worktree?" answered
docs=true while "How do I create worktrees in this app?" answered false.

## What was done

`training/augment_context.py` — deterministic, every row a transformation of a
row already in the corpus, so a label can only be as wrong as its source. It
relabels the 60 curated contradictions (4 of the 64 excluded: a pasta recipe that
matched only on the word "recipe", and three general-git concept questions the
product documentation does not own), then propagates the docs bundle's label to
every matching query and ASSERTS that no query is left carrying both — curation
is a list, and three contradictions survived it, one of them in a `topic_shift`
row that no source-specific gate could ever have reached.
`assemble_data`'s docs branch now carries `history`; a docs row that cannot
express context cannot teach its way out of a context shortcut, and its dedupe
signature now includes `is_docs`, so two identical inputs disagreeing on the flag
are a conflict rather than a silent duplicate.

Corpus 8,640 → 9,774. Docs positives 207 → 499. `"this app"` 0 → 38,
`"how do you"` 0 → 39.

**Four rules the generator has to follow, each of which it broke first.** They
are the transferable part of this round; the row counts are not.

1. **Carry the source row's answer, never `[]`.** 21 of the 207 docs positives
   legitimately require skills — a docs turn and an operational turn are not
   exclusive, which is the entire premise of the pinned docs runbook arriving
   *alongside* another one. The first draft hardcoded `skill_ids: []` in every
   family and generated 25 rows training "answer with nothing" for requests whose
   own source row demands `diagnostics` or `watch-outcome`.
2. **Never emit into a bundle that cannot express the label you need.** The
   routing families go through the branch that hardcodes `is_docs=False`, so
   sampling a docs positive into them produced a row contradicting its own
   source — the exact defect being repaired, reintroduced by the repair.
3. **A borrowed history must not move the referent.** "where are the logs kept"
   after a walk through five monorepo services is a question about those
   services. Task histories are now grafted only onto questions that NAME the
   product; social lead-ins carry no referent and can go anywhere.
4. **Rephrase questions, not statements.** `_deixis` suffixed free text into
   "the mcp says disconnected, what now in this app", and a `_compound` tail
   built "…pinned and configured agents, and when would I want one?". Both
   transforms now require a question-shaped opener.

**No active-set docs family**, though the previous round's notes called for one.
It was built (60 rows) and removed on two independent grounds: serving takes the
docs decision from a second pass with the active set EMPTIED, so those rows could
never reach the path they were meant to teach; and an active set with an empty
answer trains eviction, because an authoritative empty result CLEARS the active
set. The corpus already holds 318 rows of that shape under `no_task_stateful`,
and this round declines to add 60 more to a convention it has not measured.

`can i …` is deliberately NOT converted to second person. "can you pin an agent"
is a request to pin one at least as often as a question about whether pinning
exists, and a docs positive that reads as an imperative teaches the head to raise
the docs flag on ordinary work.

## An augmentation is not free of the balance it lands in

The first pass added 320 routing rows against ~570 zero-cardinality ones and was
exported and scored before the skew was noticed. It moved the corpus from 13.8%
zero-answer to 17.5% and thinned the two-skill class from 3.3% to 3.0%, and the
cardinality head followed the prior it was given: **docs 20/21 with multi-skill
at 2/5**, against 4/5 before. Pass 2 raised the routing families to 600
single-skill rows and added 95 two-skill lead-in rows, landing the two-skill
class at 3.8%, and took multi-skill back to 4/5 for one docs case.

**Adding rows to fix one head moves the prior of another.** The two are trained
from one corpus, and nothing but the per-category table showed it — the headline
recall-pass moved by one case while a fifth of the multi-skill category fell out.

## Result

int8 **164/177 (92.7%)**, docs flag **19/21**, `card` 0.969, val exact-match
0.931, r@32 1.000, shortlist-order ablation 0/173.

Scored against the PREVIOUS artifact re-run on the SAME 177-case suite, rather
than against its own smaller one:

| | recall-pass | docs flag |
|---|---|---|
| previous artifact | 161/177 | 14/21 |
| this artifact | **164/177** | **19/21** |

The two numbers measure different heads, and only one of them can see this
round's point: `recall_pass_flags` scores skill ids and ignores `expect.docs`
entirely, so the docs gain is invisible to the routing figure and vice versa.

Seven cases fixed, four broken:

```
+ status-overview                              - wake-the-box-worktree-already-exists
+ recover-spawn-no-terminal-id                 - plain-worktree-not-an-agent-spawn
+ explore-not-docs                             - search-closed-prs-is-find-work
+ docs-negative-continuation-work              - issue-plus-poll
+ docs-usage-question-after-unrelated-exchange
+ docs-deictic-product-reference
+ novel-resolve-one-queue-item
```

`docs-usage-question-after-unrelated-exchange` is the case the PREVIOUS round
added and could not pass. `search-closed-prs-is-find-work` is not a casualty of
the augmentation: `find-issue-work` gained its pull-request vocabulary in a skill
edit that landed AFTER both stages had trained, so the encoders never saw the
words the case is about and the corpus has no PR-search rows at all. Its own note
asks for a retrain, which is the follow-up, not this round.

Every phrasing from the reported session now answers docs=true — including "in
this app" behind a greeting, and with the user's original typo — and every
operational instruction routes identically with or without a pleasantry in front
of it.

## What the benchmark could and could not do

Three cases were added, and one of them was worthless when first written. An
early draft of `docs-usage-question-after-greeting` reused the canonical cold
question, which the BROKEN artifact already answered docs=true on and already
returned no skills for: it passed on both artifacts and scored the mitigation
rather than the bug. A regression case has to be a question the old artifact gets
WRONG, and checking that requires actually running the old artifact — which is
why the old one was kept and re-scored rather than trusted from its recorded
numbers. It is now the deictic question, twinned against
`docs-deictic-product-reference`, and the two differ only by the greeting.

Note also what these cases are and are not. The augmentation deliberately
generates `"in this app"` rows, so these are targeted regression guards, not
independent generalization evidence — the same warning this file already records
about benchmarks generated from the training data's assumptions. Their value is
provenance: the wording is a real session's, not invented to match the corpus.

And they carry a booby trap for whoever adds the next one. `assemble_data`'s
contamination gate drops any training row whose query **or history** collides
with a benchmark string, and the social lead-in rows carry their greeting in the
history — so wording a benchmark greeting like a corpus one silently deletes the
rows that teach the lesson, leaving the case failing for a reason invisible in
the diff. `tests/unit/test_training_corpus_guards.py` now asserts the two
vocabularies stay disjoint, and asserts the no-contradictory-docs-label
invariant alongside it.

**`routing_revision_matches_catalog` is now `false` in the baseline**, and that
is the drift signal working rather than a fault. The stamp is taken at TRAIN
time; two skills' `whenToUse` lines moved after stage 2 finished. Nothing gates
on it — the head has no per-skill weights and `sync()` re-embeds the moved
documents on load — but it is the honest record that the next retrain has
PR-search vocabulary to pick up.

**Still open.**

1. **The docs loss is trained on the wrong render.** `train_setwise` computes all
   three losses on the contextual `build_query()`, while serving reads the docs
   logit from a second pass over the BARE question with an empty active set. So
   the contextful docs rows added here cannot directly teach the path that is
   actually served — the gain measured above comes from the bare rows. Training
   the docs loss on its own bare view is the change that would make the second
   pass unnecessary, and it deserves its own measured round.
2. **`DOCS_THRESHOLD` is calibrated against a corpus that no longer exists** —
   199/207 recall and 6 false positives were measured at 207 positives, and there
   are now 499. The sweep is owed; the stale figures in `setwise.py` are marked
   as historical rather than current.
3. `docs-question-mid-workflow` still misses its skill: the mitigation strips
   context from OUTSIDE the message and cannot strip framing INSIDE one.
   `docs-negative-do-the-thing` remains.
