# Local skill selection — experiment log

> ## ⭐ SHIPPED CONFIGURATION — two-stage set-wise selector
> ```
> stage 1    sentence-transformers/all-MiniLM-L6-v2 (22.7M), fine-tuned, k=32
> stage 2    BAAI/bge-base-en-v1.5 (109M) + set-wise head (2.4M), ordering=equivariant
> corpus     training/data/selection.jsonl @ 8,640 rows
> train      stage 1: --epochs 6 --lr 3e-5 --batch-size 48 --seed 41
>            stage 2: --epochs 10 --ordering equivariant --seed 41
> serve      ONNX int8 encoders + fp32 head, ~410 MB whole-process RSS,
>            BONUS_FLOOR 0.45 / NEWCOMER_FLOOR 0.65 (re-calibrated, see below)
> ```
> **Current benchmark: shipped int8 measures 158/169 recall-pass (93.5%),
> fp32 158/169; int8 p50 11.6 ms. Synthetic held-out exact-match 0.910.**
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

At 2,000 skills the table is 2,000 x 384 floats; one normalised matrix-vector
product is ~1.5M FLOPs — microseconds, and **exact**. HNSW/IVF would add a
dependency, a build step and approximate recall for nothing measurable. The
crossover is around 10^5 vectors.

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
  applies to Architecture C's numbers too: the 169-case benchmark has been
  inspected across many iterations and has drifted toward a dev set. The
  measured claims here are honest, but a final generalisation claim needs ~50
  cases written blind.
