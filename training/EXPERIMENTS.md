# Local runbook selection — experiment log

> ## ⭐ SHIPPED CONFIGURATION — two-stage set-wise selector
> ```
> stage 1    sentence-transformers/all-MiniLM-L6-v2 (22.7M), fine-tuned, k=32
> stage 2    BAAI/bge-base-en-v1.5 (109M) + set-wise head (2.4M), ordering=equivariant
> corpus     training/corpus/**.yaml (SOURCE) → training/data/selection.jsonl @ 16,745 rows
>            trained (16,702 now: 43 leaking rows removed after training, see the foot)
> train      stage 1: --epochs 6 --lr 3e-5 --batch-size 48 --seed 41
>            stage 2: --epochs 10 --ordering equivariant --seed 42 --init-seed 41
>            both: doc_max_length 512, content-keyed split; stage 2 also permutation
>            sampling, sqrt cardinality weights, boundary loss 0.1, 3 doc variants
>            (all now the trainers' DEFAULTS — see "The retrain lottery" at the foot)
> serve      ONNX int8 ranking encoder + fp32 retriever + fp32 head, ~470-480 MB
>            whole-process RSS, BONUS_FLOOR 0.55 / NEWCOMER_FLOOR 0.75
> ```
> **Current (2026-09-28, catalog 87): shipped int8 is the REFINE round's s42 (swapped in
> after CI's Linux runner failed round 1 s41 on the docs gate, see "CI flipped a docs case").
> It measures 662/687 on the benchmark (strict 606, docs 25/26, QUALITY GATE pass,
> candidate_miss 0) and 124/133 on round 5b (no longer sealed), against 112/133 for the
> 40-runbook weights. Over dev + rounds 1-4b + 5a it totals 1518. p50 ~11 ms. See
> "Catalog 40 → 87" at the foot.
> Numbers older than that entry were measured on different suites and corpora.**
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
> | mean runbooks loaded/turn | 2.29 | **1.17** |
> | fp32 ↔ int8 decision diff | 32 cases | **5 cases** |
> | p50 latency | 9.0 ms | 11.6 ms |
>
> Same routing accuracy reached by being right rather than by over-loading and
> letting the recall bonus cover it. Half the runbook bodies per turn is a direct
> saving in prompt bytes on every round of every turn.
>
> **The reason to switch is structural, not the accuracy.** Neither stage owns a
> per-runbook weight: stage 1 embeds runbook documents, stage 2's head consumes those
> embeddings. So
>
> 1. **a runbook added at runtime routes without a retrain** — embedded by
>    `SetwiseClassifier.sync()` on catalog load, competing on equal terms. Under
>    Architecture B this was structurally impossible (no head row) *and* the
>    `routing_revision` gate degraded every turn open until someone retrained,
>    which put the selector in direct conflict with runbook learning;
> 2. **there is no staleness gate at all** — the hash is still stamped, but as a
>    drift signal for a human, gating nothing;
> 3. **catalog growth costs one table row (~4.5KB), not a wider head.** The head
>    always sees `k` candidates, so its job is the same size at 38 runbooks and at
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

Benchmark = `benchmarks/runbook_selection`, 150 held-out cases, exact-match
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

On an **identical shortlist**, over the 136 cases needing ≥1 runbook:

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

One encoder, one head: 32 runbook logits + 1 docs logit + 4 cardinality logits.
The head's weight matrix **is** the runbook embeddings, so a bigger catalog costs
one row (384 floats), not a wider input.

| # | configuration | benchmark | synthetic | note |
|---|---|---|---|---|
| B1 | MiniLM-L6, threshold rule, 8 epochs | 113/150 | 0.441 | undertrained; threshold-sensitive |
| B2 | + 30 epochs | 113/150 | 0.816 | |
| B3 | + fusion with bi-encoder (α=0.8) | 113/150 | 0.859 | |
| B4 | + cardinality head, fusion α=0.4 | **115/150** | 0.871 | routing-trap 52/58, single-runbook 31/33 |
| B5 | + head init from runbook embeddings | — | 0.804 | **worse**; constrains training. Kept opt-in |
| B6 | + full inverse-frequency balancing | 114/150 | 0.816 | topic-shift up, routing-trap 52→46 |
| B7 | classifier ALONE via ONNX fp32 | 114/150 | — | **2.05 ms p50**; beats fusion on topic-shift |
| B8 | + rebalanced corpus (topic-shift 88→223) | 114/150 | 0.849 | topic-shift 2/9→6/9, docs flag 15/16 |
| B9 | + round-2 diversity corpus (4,768) | 117/150 | 0.850 | data alone: +3 |
| B10 | + bge-base backbone (109M) | 121/150 | 0.874 | model alone: +4. p50 9.8ms |
| B11 | + foundation-label fix (see below) | — | 0.850 | 104 rows unpaired; card-2 rows 171→83 |
| B12 | + round-3 contrastive corpus (5,224) fp32 | 126/150 | 0.841 | |
| **B13** | **same weights, int8** | **130/150** | — | **86.7%, p50 8.9ms** |
| B15 | + wake corpus & hardened gates (6,399), review fixes | **130/150 int8** | 0.850 | canonical serving-path eval; topic-shift 8/9, foundation 5/6, adversarial 4/5. fp32 129, diff 7 cases |
| B14 | + round-4 session corpus (6,285) | 129/150 | 0.852 | fp32 = int8 = 129: decisions stabilised. foundation-only 2/6→**5/6**, multi-runbook 2/5→**4/5** (both now match DeepSeek), no-task 6/6 |

### The foundation-label bug (found by Codex)

104 training rows labelled `daintree.foundation` ALONGSIDE a specific runbook, and
96 of 171 cardinality-2 rows were really "specific + auto-paired foundation".
But the backend pairs the foundation downstream and the benchmark puts it in
`optional`, never `required`. So most of the cardinality head's evidence for
"this turn needs TWO runbooks" meant something the classifier is never asked to
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
    runbook table  ────────────────────────────► s_i ┴─► set-wise head ─► 32 logits
                                                                      + docs + cardinality

The head is 2 transformer layers over `[CLS(q), cand_1 … cand_32]`, where
`cand_i = W([s_i ; q⊙s_i ; |q−s_i|]) + W_f(features_i)`. It owns **no per-runbook
parameters**, which is the whole point — see the header for what that buys.

This is **not** Architecture A. Nothing re-reads a runbook's text at request time;
runbook vectors are computed once and looked up. A's cross-encoder cost 309ms of a
312ms pipeline scoring (query, document) *pairs*; this head reads cached vectors
and costs well under a millisecond.

### Stage 1 — how small can the filter be

Stage 1 is a **filter**, not a scorer: its only job is to put the answer inside
the top `k`. So `r@32` must be 1.000, and among backbones that clear it the
choice is cost. `r@8` is reported as the scale proxy — 8 of 38 is the same
*fraction* that `k=32` would be of a 150-runbook catalog.

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
wins" rather than on content. That prior is exactly wrong for the runbook this
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
earning anything measurable at 38 runbooks.

### Zero-shot: what "no retrain needed" actually buys — MEASURED

The architecture's headline claim is that a runbook added after the build routes
without a retrain. It is true in the sense that matters operationally, and it is
much weaker than it sounds. Measured by holding three runbooks out of **both**
stages' training data and scoring only the benchmark cases that require them
(`training/eval_zero_shot.py`):

| | zero-shot | same recipe, trained |
|---|---|---|
| cases requiring a held-out runbook | **4/14** | 13/14 |
| the other 141 cases | 130 | 131 |

Per runbook, the pattern is the whole finding:

- `daintree.terminals.rename` — **4 of 7**. Distinctive vocabulary ("rename",
  "titles", "tabs") that no neighbour claims, so its document wins on content.
- `daintree.forge.manage-issue` — **0 of 4**. Loses every case to
  `start-work-on-issue` / `find-issue-work`.
- `daintree.runtime.debug-web-app` — **0 of 3**. Loses every case to
  `diagnose-and-fix`.

This is the asymmetry recorded at the top of this file, hit from the third
direction: **a new runbook's strongest words are stolen from its neighbours**, and
here the neighbours are trained while the newcomer is not, so the newcomer loses
every *contested* turn and wins only the unambiguous ones.

So state the property precisely, and do not let it drift in the docs:

- Before (Architecture B): a new runbook was **structurally unselectable** — no
  head row — *and* it moved `routing_revision`, so the selector degraded EVERY
  turn open until someone retrained. The catalog broke, not just the new runbook.
- Now: the catalog keeps working, and the new runbook is reachable and wins
  requests only it plausibly answers. It will still lose to established
  neighbours until it has boundary data of its own.

**A retrain is no longer what makes a runbook possible; it is what makes it
competitive.** That is a large operational improvement and not a licence to stop
retraining. `.claude/skills/retrain-selector` says the same thing.

### Cross-learning: what a shared head does and does not buy

With no per-runbook parameters, every training row teaches the SHARED reader —
"how does a request map onto a whenToUse fence" — rather than filling in one
runbook's row. So competence transfers across runbooks in a way it structurally
could not before. Two claims come out of that, and only one of them is measured.

**Proven.** A runbook held out of both stages still takes 4 of 14 of its own
benchmark cases (see the zero-shot section above). Under a per-runbook head that
number is **0 by construction** — there is no output to activate. Every one of
those 4 is transfer from other runbooks' data, so the mechanism is real and
demonstrated, not inferred.

**Not yet measured: that it COMPOUNDS.** The appealing version — each runbook
added makes every other runbook route better, so the catalog stabilises as it
grows — does not follow from the above and is not established here. The one
adjacent data point is mildly against a large effect: removing 411 rows (three
runbooks, ~5% of the corpus) moved the remaining 141 cases by 130 vs 131, i.e.
nothing outside the noise band.

The experiment that would settle it, when there is a reason to run it: train
stage 2 on data for N of the catalog's runbooks for N in {10, 20, 30, all}, and
each time score the SAME held-out runbooks. If cross-learning compounds, zero-shot
performance on the held-out set rises with N even though their own data is never
present. That is a learning curve over *breadth*, not volume, and it is the only
version of the question the benchmark can answer.

**And note where transfer is weakest, because it is exactly where the catalog is
heading.** The zero-shot breakdown is not uniform: `terminals.rename` scored 4/7
with no training, `manage-issue` 0/4 and `debug-web-app` 0/3. Distinctive
vocabulary transfers; **discrimination between two subtly different neighbours
does not**, because that distinction lives in the PAIR and not in either
document alone. A shared reader can learn "read the fence"; it cannot learn a
fence nobody wrote. As overlapping runbooks accumulate, boundary data — in both
directions — is the thing cross-learning does not give you for free.

### Runbook TEXT is now the fast routing lever

A corollary worth using. Editing a `whenToUse` used to move `routing_revision`
and require a retrain before it took effect, so text tuning was a ~20-minute
loop. Now the edited runbook is re-embedded on catalog load — **one** runbook, one
forward pass — and the change is live. Measured on the shipped artifact: adding
one clause to `review.review-change`'s whenToUse (that "tell me what you'd push
back on" is a request for FINDINGS even when framed as pre-push) took the
benchmark 158 -> 159, one case fixed and none broken, with 1 of 38 runbooks
re-embedded and nothing retrained.

`training/try_runbook_text.py` scores a proposed edit against the whole suite from
an in-memory overlay, writing nothing. Use it, and read both columns: a
locally-correct carve-out that fences on words two runbooks legitimately share is
a regression, and this file already records one that shipped and had to be
reverted. Tuning text against a suite this heavily inspected also fits it — a +1
inside a ±2 error bar is not evidence on its own, and the edit should be
defensible as a statement about the runbook before the number is consulted.

### Why brute-force cosine and not an ANN index

The shortlist's whole contract is `r@k = 1.000`: a candidate that never reaches
the head cannot be recovered downstream, so a shortlist miss caps the entire
selector. Approximate search sells exactly the property this stage cannot spend
— IVF scans only the `nprobe` nearest k-means buckets, so a runbook in an unprobed
bucket is invisible; HNSW's recall is a tunable below 1.

And the speed it buys is not needed. `M @ q` + `argpartition` over float32,
measured (see the latency breakdown above): 0.015 ms at 2,000 runbooks, 1.22 ms at
100,000. Where the crossovers actually sit:

| runbooks | tables | search | search as % of turn |
|---|---|---|---|
| 2,000 (the design target) | 9 MB | 0.02 ms | 0.2% |
| 10,000 | 46 MB | 0.12 ms | 0.9% |
| 100,000 | 461 MB | 1.22 ms | 8.2% |
| 875,000 | 4 GB | 10.7 ms | 44% |

- search reaches **10% of the turn** at ~111,000 runbooks
- search reaches parity with the ranking encoder at ~862,000 runbooks

At 2,000 runbooks IVF would be self-defeating anyway: with the usual
`nlist ~ sqrt(N)` it compares ~45 centroids and then scans ~45 vectors — about 90
comparisons against our 2,000, saving ~0.013 ms — while adding an index build, a
large binary dependency, and approximate recall.

**The axis that binds first is MEMORY, not latency.** The tables are 4.5 KB per
runbook (384-d retrieval + 768-d ranking, float32) and fill 1 GB at ~217,000
runbooks, while search does not become meaningful until ~111,000. So if this
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
2. **Runbook documents are longer than they look**: p50 262 tokens, max 368. A
   192-token cap — a reasonable-looking number borrowed from the query side —
   truncates exactly the `whenToUse` exclusion clauses that carry the routing
   signal. `doc_max_length` is 384 and the exporter asserts both stages agree.
3. **A vector table must be built by the same precision that queries it.**
   `vectors.npz` was built with int8 encoders and reused by an fp32 session, so
   documents and queries came from two different functions. It moved the
   benchmark by 2 cases between otherwise identical exports and read as run
   noise. `RunbookTable` now carries a `precision` stamp and a mismatch re-embeds.
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
   wins both, because what separates them is *which runbooks are loaded* — state,
   not words. Fixed by putting the active set into the input (`[loaded: …]`).
4. **Corpus balance shows up directly as category failures.** 272 follow-ups vs
   88 topic-shifts taught "loaded → keep", and the model ranked stale runbooks at
   0.98 confidence. Generating 135 more topic-shifts moved it 2/9 → 6/9.
5. **int8 changes decisions on the classifier** (which runbook ranks first).
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

- **int8 ranking encoder, fp32 retriever and head** (retriever fp32 since
  2026-09-28 — see that entry at the foot). The ranking encoder carries ~80% of
  the parameters; the head is 2.4M parameters (9.5MB fp32), so quantising it
  would trade numerical slack for memory nobody is short of. Quantisation must
  be PER-CHANNEL — see Architecture C trap 4. Measured whole-process RSS with
  both encoders loaded: ~410MB all-int8, 482MB with the fp32 retriever.
- **Cloud Run: --cpu=1 --memory=2Gi --concurrency=8.** 1Gi would run today at
  ~55% but leaves no headroom for the 100-200 runbook catalog target plus
  concurrent turn buffers; the second GiB costs ~$7/mo at min-instances=1.
- **Earn-your-place admission** (setwise.py NEWCOMER_FLOOR_WHEN_ACTIVE=0.65):
  with runbooks already loaded, a runbook NOT in the active set needs p>=0.65 to
  join; active runbooks and fresh-turn picks ride on the model's ranking alone.
  Encodes the asymmetric cost of adding mid-conversation (re-encodes the runbook
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
documentation. The selector ran clean: `ran=true`, `degraded=false`, zero runbooks,
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

Measured over 120 docs positives: **113/120 asked cold, 5/120 with a runbook
loaded.** Since `query_document` always prepends a 320-char history tail, the
practical reading is that documentation lookup worked on the FIRST message of a
conversation and almost nowhere else.

**Shipped mitigation** (`setwise.py`): the docs logit is re-read from a SECOND
COMPLETE PASS over the bare newest user message — its own retrieval, shortlist,
cosine/rank features, and an empty active set. Cost is +6.7 ms, skipped entirely
when the docs string and the ranking query agree.

A cheaper version reused the primary shortlist and swapped only the query
vector, and the measurement that justified it (113/120 either way) was worthless:
k is 32 against 38 runbooks, so any two shortlists must share ≥26 candidates and
retrieval is nearly a no-op. At 64 runbooks two top-32 sets can be disjoint, and
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
opening message — after an unrelated exchange, with a runbook loaded, and in the
second person. All three fail their DOCS EXPECTATION before the mitigation and
meet it after; the docs flag goes 11/19 → 14/19.

Be precise about what that does and does not say. `recall_pass_flags` scores
runbook ids only and ignores `expect.docs` entirely, so the routing headline
(158/172) is not evidence for this fix and never could be — the docs score is
the separate 14/19. Nor are the added cases free of routing content: `required:
[]` means the selector must come back empty, and
`docs-usage-question-after-unrelated-exchange` deliberately records a runbook
extra it should not pick.

When a benchmark is generated from the same assumptions as the training data,
agreement between them is not evidence.

---

# Context-bearing turns: a greeting erased the task (2026-08-20)

Reported from session `ses_3a3cd1e7`. Two turns in — "How are you?", then "How do
I create worktrees in this app?" — the server loaded **no runbook and retrieved no
documentation**, and the model answered from the tool schemas alone. Since the
only worktree-creation tool the CLI exposes is `worktree__createWithRecipe`,
whose description is entirely about recipes, the answer confidently explained
that worktrees are made from recipes. The correcting sentence ("omit `recipeId`
and it creates only the worktree") lives in `create-plain-worktree`'s body — the
runbook that did not load.

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

| history in front of "How do I create worktrees in this app?" | top runbook | cardinality |
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
   legitimately require runbooks — a docs turn and an operational turn are not
   exclusive, which is the entire premise of the pinned docs runbook arriving
   *alongside* another one. The first draft hardcoded `runbook_ids: []` in every
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
zero-answer to 17.5% and thinned the two-runbook class from 3.3% to 3.0%, and the
cardinality head followed the prior it was given: **docs 20/21 with multi-runbook
at 2/5**, against 4/5 before. Pass 2 raised the routing families to 600
single-runbook rows and added 95 two-runbook lead-in rows, landing the two-runbook
class at 3.8%, and took multi-runbook back to 4/5 for one docs case.

**Adding rows to fix one head moves the prior of another.** The two are trained
from one corpus, and nothing but the per-category table showed it — the headline
recall-pass moved by one case while a fifth of the multi-runbook category fell out.

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
round's point: `recall_pass_flags` scores runbook ids and ignores `expect.docs`
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
the augmentation: `find-issue-work` gained its pull-request vocabulary in a runbook
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
returned no runbooks for: it passed on both artifacts and scored the mitigation
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
time; two runbooks' `whenToUse` lines moved after stage 2 finished. Nothing gates
on it — the head has no per-runbook weights and `sync()` re-embeds the moved
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
3. `docs-question-mid-workflow` still misses its runbook: the mitigation strips
   context from OUTSIDE the message and cannot strip framing INSIDE one.
   `docs-negative-do-the-thing` remains.

---

# Catalog scale: what k costs, measured (2026-08-21)

Everything above was measured on a 38-runbook catalog against `k=32` — 84% of the
catalog on every ballot. At that ratio retrieval is barely a filter, so no number
in this log says what Stage 1 does when it has to be one. `bench scale` pads the
catalog with derived distractors (`shadow_catalog.py`) and re-measures oracle
recall — "did every required runbook reach the head", the only Stage 1 contract
that matters, because Stage 2 re-decides the order and nothing recovers an absent
candidate.

Seed 20260821, shipped int8 retriever. **154 of 177 cases count toward recall**:
19 require no runbook at all (chatter, no-task, most docs questions) and would be
vacuous successes, and 4 are background wakes that production never sends to the
model.

## Candidate misses, by catalog size and shortlist width

Counts, not rates. n is 154 and one case is 0.65 points, so a table of
four-decimal rates invites distinctions the sample cannot support.

| runbooks | k=16 | k=32 (ships) | k=48 | k=64 | k=96 |
|---:|---:|---:|---:|---:|---:|
| 39 | 0 | **0** | 0 | 0 | 0 |
| 100 | 0 | **0** | 0 | 0 | 0 |
| 200 | 1 | **0** | 0 | 0 | 0 |
| 500 | 4 | **2** | 0 | 0 | 0 |
| 1000 | 11 | **4** | 3 | 1 | 0 |

`k=32` takes its first miss at 500. `k=48` is clean at 500 and takes 3 at 1,000;
`k=96` is clean throughout.

**State that as a trigger, not a capacity limit.** This is one fixed synthetic
catalog and 154 target-bearing cases. Zero misses at 200 gives roughly a 98%
one-sided lower bound, which is not a demonstration of 99.5%. The supportable
claim is: *on this pool, k=32 produced no observed misses through 200, and the
first two appeared at 500.* Treat 200 as the point to re-measure, and re-measure
against the real catalog of the day rather than trusting this table.

## What reservation actually buys — a correction

The first version of this section claimed dense retrieval alone "loses the open
runbook on 41% of turns that have one" by 200 runbooks. **That was the wrong
denominator and the claim does not survive it.** It counted every visible active
id on every active case — including topic shifts, where dropping the old runbook
is the CORRECT answer, and optional foundations that active-set pairing restores
anyway.

Scored properly — actives that are also REQUIRED, i.e. the ones the turn should
keep:

| runbooks | dense alone | with reservation | every-active coverage, dense |
|---:|---:|---:|---:|
| 39 | 13/13 | 13/13 | 1.0000 |
| 100 | 13/13 | 13/13 | 0.8261 |
| 200 | 13/13 | 13/13 | 0.6087 |
| 500 | 13/13 | 13/13 | 0.5217 |
| 1000 | 13/13 | 13/13 | 0.4348 |

**Dense retrieval keeps every active-and-required runbook at every size measured.**
The `[loaded: …]` prefix carries the active ids into the query and it holds up:
on this corpus, reservation rescues nothing that was actually going to be lost.

So candidate reservation is **insurance, not a repair**. It is still worth having
— it costs nothing, it converts a failure mode that no downstream floor can fix
into an impossibility, and the coverage column shows retrieval genuinely does
start dropping actives as the catalog grows — but this benchmark has 13 cases
where an active runbook is also required, and none of them were at risk. Do not
cite it as a measured save. The corpus work that would make this question
answerable is more continuation rows with a workflow open, which is the same gap
the docs head has.

## Which categories break first

At `k=32`: everything holds through 200. At 500, `foundation-only` (5/6) and
`docs-classification` (7/8) each lose one. At 1,000, `routing-trap` loses two
(70/72). `daintree.foundation` is the thin case throughout — it is deliberately
the most generic document in the catalog, so it is what a crowd of near-neighbours
buries. That is a concrete argument for domain foundations before the catalog
gets there, and it is what the CI smoke's one-miss budget is sized around: at 200
runbooks the foundation sits at rank 26 of 200 under int8 and rank **31** under
fp32, the last slot `k=32` has.

## Distractor quality is the measurement instrument

The first pool was not one. Templates varied only the id, and the id is not
rendered into a runbook document — so 961 distractors produced **492 distinct
documents**, one repeated seven times. Identical vectors still occupy separate
shortlist slots, so "1,000 runbooks" meant ~530 routing surfaces crowding each
other by multiplicity, and every conclusion past 200 rested on it. Generation now
enforces distinct rendered documents and fails loudly when a template's variation
space is exhausted (`tests/unit/test_shadow_catalog.py`).

Two other instrument biases, both found by review rather than by the numbers
looking wrong:

* `_paraphrase` copied the source's summary and whenToUse verbatim. That is not a
  distractor, it is an ALIAS — an entry that genuinely answers the same request,
  scored as a miss when retrieved. It rewords now.
* Three of five shapes named their source by dotted id in an exclusion clause,
  so half the pool carried an exact token sequence that the target's own document
  does not contain (a runbook's document has no id in it) while the query does, via
  `ACTIVE_PREFIX`. That biased the dense numbers harsh. One exclusion in four
  names an id now, against the real catalog's 4.1 outgoing references per runbook.

## Cost

Embedding 1,000 runbook documents: **~250s**, once. Sizes are nested and the
classifier's digest-keyed vector memo outlives each table, so every smaller size
in a sweep is free (measured 0.00s). That figure is also the honest cold-start
worst case for a 1,000-runbook catalog with no packaged `vectors.npz`.

CI runs a 200-runbook, `k=32` smoke (~24s): a one-miss budget on candidate recall,
and an exact 1.0 on active coverage, which is a construction guarantee rather
than a measurement.

---

# Selector confidence cannot gate reasoning (2026-08-22)

**Question.** The serving model is non-thinking on every call. Should it reason on
turns where runbook routing looks WEAK — a runbook the classifier is unsure about
means the model has to work the steps out for itself — and stay non-thinking when a
runbook matches cleanly? The proposal was a threshold on `SelectionDecision.confidence`
(the first admitted runbook's probability): ~1.0 → no reasoning, below ~0.65 → reason.

**Answer: no. The signal does not exist.** Measured over the 177-case routing corpus
with the shipped artifact, on the 154 turns that require a runbook:

| population | n | min | p25 | median | max |
|---|---:|---:|---:|---:|---:|
| selection CORRECT | 142 | 0.82 | 1.00 | 1.00 | 1.00 |
| selection MISSED a required runbook | 12 | 0.00 | 0.99 | **1.00** | 1.00 |
| turn needs no runbook at all | 19 | 0.00 | 0.00 | 0.00 | 0.00 |

The head is confident when it is wrong. Eleven of the twelve misses scored ≥ 0.98 —
`review-the-branch`, `sweep-now-plus-wake-later` and `search-closed-prs-is-find-work`
all missed a required runbook at exactly 1.00. A 0.65 threshold turns reasoning on for
**1 of 154 turns** and catches **8%** of the misses. Every threshold from 0.50 to
0.80 selects the same single turn.

Two other candidate signals were measured and fail the same way:

* **Top1−top2 margin.** Correct p10 0.128 / median 0.997; missed p10 0.024 / median
  0.933. Overlapping, not separating.
* **The cardinality head.** Correct `{1: 138, 2: 4}`; missed `{0: 1, 1: 10, 2: 1}`.
  The same distribution.

The one cleanly separated population is "nothing was selected" — exactly 0.00,
20 turns. But it is not the population the idea wanted: 11 are docs questions and 6
are contentless greetings, both the cheapest turn shapes there are, and only 1 of
the 20 actually needed a runbook. After excluding docs and greetings it is 3 turns of
173, so there is nothing there to serve either.

**Why this was predictable, and the general lesson.** A sigmoid output is not a
probability until someone measures that it is. This head was trained to rank, and
ranking is all it does well: it puts the right runbook first far more often than its
score says anything about whether it did. Calibration is a separate property and it
was never trained for, so reading the score as a confidence is reading a number that
was never fitted to mean that.

**Do not build the gate on this artifact.** If the idea is wanted, it needs a head
whose scores are calibrated first — measure expected calibration error or a Brier
score on held-out cases, not on this corpus, which the head has effectively seen.
The cheaper alternative is to stop asking the classifier: a discrete
`clear | ambiguous | insufficient` output trained as its own target would at least
be fitted to the question being asked of it. `TODO.md` carries the trigger.

Reproduce: `benchmarks/runbook_selection/local_runner.py` drives the same artifact;
the distribution above comes from `SetwiseClassifier.select(...).scores` for the
first admitted id, scored against each case's `required` set.

---

## 2026-08-29 — the corpus becomes a build artifact, and hard negatives get a consumer

Two changes, one structural and one to stage 1's objective. **No shipped artifact
was retrained or re-exported**: the compiled corpus holds the same 9,800 rows it
did before, row for row, so the numbers at the top of this file still describe
the artifact in `runbooks/local/artifacts/`. The training change applies to the
next retrain.

### The corpus was its own input

`training/data/selection.jsonl` was both `assemble_data.py`'s output and its
`--base`. Three consequences, all structural rather than incidental:

- **Nothing could be removed by regenerating.** Only by hand-editing 2.8 MB of
  JSONL.
- **Provenance was one flat `source` string.** "Which run added this row" and
  "drop everything from the run with the bad prompt" were unanswerable.
- **Re-running a generator grew the corpus rather than reproducing it.**
  `augment_context.py` measured itself taking 9,903 rows to 10,405 on a second
  application, and shipped a guard against its own re-run whose documented
  recovery was `git show <commit>:…jsonl >`. Git was the undo stack for the
  training data.

Now three planes: `training/journals/` (raw, append-only, **committed** — the
generator is a nondeterministic paid model, so a discarded journal is a row that
can never be traced to a prompt again), `training/corpus/` (curated YAML, one
file per runbook, the source of truth), and `training/data/` (compiled). Placement
is mechanical: a row answering exactly one runbook lives in that runbook's file,
anything else in `shared/<source>.yaml`. `compile_corpus` is pure — same bytes on
a re-run — and `tests/unit/test_training_corpus.py` gates the committed artifact
against a fresh compile on every push. That gate runs in ORDINARY CI, not the
training environment: the compile path touches no torch and no weights, only the
catalog, the benchmark cases and the serving-side query renderer.

Migration was verified lossless: 9,800 rows out, 9,800 back, identical as a
multiset over query/history/active/expect/is_docs/source/empty_kind, with all
2,042 confusability entries preserved. Rows predating the split carry
`run: pre-split`, which is honest — most of those journals are genuinely gone.

### `hard_negatives` was never read by anything

The field was written on 2,042 rows. `train_retriever`'s module docstring said
it was used "as explicit negatives on top of the in-batch ones"; `targets()`
said it applied "-inf masking" for them. **No line of code ever read it.** It was
inert from the day it was written, and the two docstrings were the only evidence
it had an effect. It was also never mined — the assembler hardcoded the id of
the runbook whose generation block produced the row.

Two separate repairs:

1. **Naming.** It is now `confusable_with`: the runbooks a HUMAN judged a query
   could be mistaken for, usually straight out of a `whenToUse` carve-out. That
   is what the data always was, and it is stable across retrains.
2. **A consumer.** Against a FULL-CATALOG denominator — every runbook re-embedded
   each step — adding a confusable runbook "as a negative" is a no-op, because it
   was in the denominator already. The only thing a hard negative can do here is
   carry more WEIGHT, so `--hard-negative-weight` multiplies a selected
   negative's term in the softmax denominator (its logit gets `log w`).
   Positives never receive it. On top of the authored set, negatives are **mined
   every epoch** from the live encoder (ANCE-style), filtered by NV-Retriever's
   `TopK-PercPos` rule — keep a candidate only where
   `s(q, neg) <= (1 - margin) * s(q, best positive)` — because the top-scoring
   unlabelled document is very often a legitimate alternative answer, and
   training the encoder to push it away teaches it something false. Re-mining
   every epoch is affordable for the same reason the full denominator is: tens
   of documents, not millions. Held-out runbooks are excluded from mining as
   well as from training rows, or the zero-shot probe would be measuring a model
   that had been trained on the thing it is meant not to have seen.

### Measured: 3 seeds, 6 epochs, `w=4.0` against `w=1.0` (the mechanism disabled)

`w=1.0` is exactly the pre-change behaviour, since the field was inert.

| | r@1 off → on | r@3 off → on | r@8 off → on | r@16 off → on |
|---|---|---|---|---|
| seed 41 | 0.850 → 0.855 | 0.965 → 0.970 | 0.987 → 0.988 | 0.998 → 0.998 |
| seed 7 | 0.833 → 0.838 | 0.964 → 0.969 | 0.989 → 0.989 | 0.996 → 0.997 |
| seed 101 | 0.828 → 0.831 | 0.957 → 0.962 | 0.989 → 0.992 | 0.999 → 0.998 |
| **mean** | **0.8370 → 0.8413** | **0.9620 → 0.9670** | 0.9883 → 0.9897 | 0.9977 → 0.9977 |
| sign | **3/3 better** | **3/3 better** | 2/3 better, 1 level | 1 better, 1 level, **1 worse** |

**Read this as small but sign-consistent, not as a win worth quoting.** +0.4pp
r@1 and +0.5pp r@3 is about five rows of a ~990-row validation split — inside
what a single seed could produce by luck. What makes it worth keeping is that
r@3 moved by exactly +0.005 in all three seeds, and that the one regression
anywhere in the table is seed 101 at r@16 (0.999 → 0.998, a single row) against
consistent gains at the ranks that decide the top of the list.

**It does not fix anything that is currently broken.** `r@32` is 1.000 with the
mechanism off, and `r@32` is the metric that decides whether the shortlist loses
the answer — so this buys precision at the top of a list that already contains
the right runbook. The real argument for it is structural: a full-catalog
denominator dilutes as the catalog grows, and at 200-1,000 runbooks the
contrast against the few genuinely confusable ones is where the signal has to
come from. `bench scale` already puts the first candidate miss at 500 runbooks.

Kept at `w=4.0` because it is better where it matters and costs a single row
at one rank on one seed. `--hard-negative-weight 1.0` disables it. Note the LOSS values are not comparable between arms — an inflated
denominator raises the loss by construction (0.249 vs 0.152 at seed 41), and
that is arithmetic, not a worse fit.

Cost: one extra forward pass over the training rows per epoch, for mining.

### The zero-shot probe was leakier than its number implied

`--holdout-runbooks` is what produced the **4/14 vs 13/14** figure quoted at
the top of this file and in `.claude/skills/retrain-selector`. It dropped rows that
ANSWERED a held-out runbook and nothing else, so three paths still fed the
encoder the thing it was meant not to have seen:

- the loss DENOMINATOR — the catalog is re-embedded with gradients every
  step, so every held-out document was explicitly trained as a negative on
  every row;
- rows whose ACTIVE SET named it, which `build_query` renders into the
  `[loaded: …]` prefix (80 such rows for the three ids that were held out);
- and, once `confusable_with` gained a consumer, 274 authored negatives
  naming it — a regression this change would have introduced.

All three are closed. **The 4/14 number was measured under the leaky probe**
and is therefore, if anything, optimistic about a genuinely unseen runbook;
re-measure before quoting it as a floor. None of this affects an ordinary
run: `holdout` is empty unless the flag is passed.


# The retrain lottery, and what actually moved routing (2026-09-24/25)

Retraining for the 41st runbook (`agents.check-usage-limits`) scored **198/217** against
the shipped artifact's **203**, on a corpus that had changed by 63 rows (0.6%). This is
the investigation, the fixes, and the one change that measurably improved routing.

## The 198 was a draw, not a regression

Same recipe, same corpus, four seeds: **198 / 205 / 202 / 198** (dev, int8). A control
retrain on the shipped artifact's own corpus and seed reproduced 203 — but disagreed with
the shipped artifact on 6 cases (MPS training is not bit-reproducible). None of the five
cases the 198 lost went to the new runbook or to anything the new rows touched. The
single-run spread of this recipe is ~7 cases; "retrain and accept if it scores higher" was
a coin toss with a thumb on neither side.

Why 63 rows behaved like a new seed (found by Codex): the train/val split was a seeded
shuffle of the WHOLE corpus, so adding rows reassigned ~1,020 existing ones across the
split — ~12% of the training set replaced. `training.common.stable_split` now keys each
row's side on its content.

## Correctness fixes (kept regardless of their measured effect)

- **Truncated documents.** Under both tokenizers `watch-outcome` is 510 tokens and
  `fan-out-work`/`provision-local-environment` 398, against `doc_max_length` 384 — the cut
  fell exactly on the whenToUse exclusion fences. Both stages now train at 512, and
  `test_every_runbook_document_fits_the_shipped_document_cap` fails the next runbook edit
  that would cross it. `watch-outcome` sits 2 tokens under the new cap.
- **Batch-dependent serving vectors.** Dynamic int8 quantisation scales over the whole
  input tensor, so a document encoded in a batch differed 6-7% (relative L2) from the same
  document encoded alone, and which documents shared a batch depended on cache history.
  `sync` encodes one document per call; pinned by
  `test_a_runbook_vector_does_not_depend_on_what_it_was_synced_with`.
- **Split churn** (above).
- **Train/serve active-set skew.** Production pairs every specific with
  `daintree.foundation` and shows the model both; 61% of the corpus's stateful rows (and
  many eval cases) carried the specific alone — a state serving never produces.
  `canonical_active` renders them through the server's own merge; the benchmark runner
  can do the same (`DAINTREE_BENCH_CANONICAL_ACTIVE=1`). All numbers below are scored that
  way. Training on canonical states was measured separately (see the ablation table).

## Evaluation that could see a difference

The 217-case benchmark is a development set (inspected across many iterations), and at
~7 cases of seed spread it cannot resolve a 3-case gain from one run. Three suites were
added, written by agents that read ONLY the catalog digest and runbook files, each case
labelled blind by two further annotators (Opus + Sonnet) and kept only on a majority:

| suite | cases | unanimous | status |
|---|---|---|---|
| round 1 (easy) | 100 | 100 | development — its failures were read |
| round 2 (hard: fences, state, messy turns) | 189 | 189 | development — its failure SHAPES informed generator briefs |
| round 3 (personas, goal-phrased, long sessions) | 197 | 196 | **sealed**: scored once, after the decision |

Every arm is scored over several seeds and compared by mean (and paired by seed where
possible); a single run is never a result.

## What moved: verified targeted data, not training tricks

781 rows (`run: 2026-09-24-selector-hardening`) from eight generator agents aimed at the
measured weak spots — turns needing two runbooks, topic shifts that share vocabulary with
the loaded runbook, no-task turns while a runbook is loaded, fence traps in both
directions, social lead-ins, pasted output before the ask. Every row was relabelled blind
by a second model and kept only where the labels agreed (791/801); rows near-duplicating
any holdout case were removed.

int8 recall-pass, canonical evaluation, mean ± sd over seeds:

| arm | n | dev 217 | round 1 | round 2 | extras |
|---|---|---|---|---|---|
| baseline recipe, old corpus | 4 | 200.8 ± 3.4 | 93.5 | 166.0 | 31.8 |
| all training changes, OLD corpus | 3 | 201.7 ± 4.6 | 93.0 | 167.3 | 33.7 |
| all training changes + new data (FULL) | 5 | 202.6 ± 3.2 | 97.6 | 173.6 | 31.4 |
| FULL − EMA (**chosen**) | 6 | 202.8 ± 1.5 | 97.2 | 174.7 | 31.0 |
| FULL − document augmentation | 5 | 202.0 ± 1.2 | 96.2 | 174.2 | 31.8 |
| FULL − EMA + canonical-active training | 3 | 199.0 ± 1.0 | 98.3 | 177.3 | 33.7 |
| permutation sampling alone | 2 | 200.0 ± 1.4 | 94.0 | 166.5 | 34.0 |

- **The training-procedure changes alone bought nothing measurable** (row 2 ≈ row 1, same
  seed noise). Kept because they are fixes, not because they scored.
- **The data bought +4 on round 1 and +8 on round 2**, and cut dev seed spread from ±3.4 to
  ±1.5. Some of round 2's gain is adaptive (generator briefs echoed its failure shapes) —
  which is why round 3 was sealed.
- **EMA** changes nothing at the end of a OneCycle schedule (the learning rate is already
  near zero; EMA and live weights read within 0.001 on validation), matching Codex's
  prediction. Dropped.
- **Canonical-active training** trades ~4 dev cases for ~3 round-2 cases: net zero on the
  pre-declared score. Not shipped; the eval-side canonicalisation stays.
- **Admission floors are a weak lever** (every floor from 0.45-0.75 moves ≤2 cases per
  suite), and **runbook-text fences that name a neighbour's vocabulary backfire** with this
  encoder: adding "find past, closed or merged PRs" to `follow-up-on-issue`'s exclusions
  made the shipped artifact LOSE the case the fence was written for — the words attract
  regardless of the "do not".

## Ensemble distillation: built, measured, not shipped

`--teachers` scores every training row's ballot with several stage-2 models and trains
the student on a 50/50 mix of hard labels and the ensemble's averaged probabilities —
ensemble behaviour at one model's inference cost. Five `FULL − EMA` seeds as teachers,
three fresh student seeds: **474.0** on the decision score against 475.2 for the teachers'
own recipe. It does not beat plain training on this corpus.

The first build had a bug worth recording: the soft slot targets went through the
hard-label loss, which carries `pos_weight` ≈ 31. Against a soft target t that moves the
optimum to 31t / (31t + 1 − t) — a neighbour the teachers scored 0.1 is pushed to 0.78,
over the bonus floor. It showed as 21/217 turns loading three runbooks (6-8 otherwise),
extras 52 against 31 and strict 161 against 175-192, with recall untouched — so a
recall-only readout would have called it the best arm. Soft targets now use unweighted
BCE, summed over slots so the term is not ~30x smaller than the hard loss.

## The decision, and the sealed result

The rule was written down before round 3 was scored (`mean over seeds of dev + round 1 +
round 2`, highest wins; ship that recipe's seed-41 artifact, no seed selection; ship only
if round 3 gains ≥ 2 cases and dev loses ≤ 1). Decision scores: `FULL − EMA` 475.2,
canonical 474.7, distillation 474.0, FULL 473.8, no-doc-aug 472.4, baseline 461.3.

Round 3, scored once, int8, canonical active sets:

| | seeds | round 3 (197) |
|---|---|---|
| artifact shipped before this entry | — | 183 |
| baseline recipe, current corpus | 4 | 181 / 178 / 177 / 177 — mean 178.3 |
| `FULL − EMA` | 6 | 187 / 189 / 183 / 186 / 188 / 186 — mean **186.5** |

**+8.2 cases (+4.2 points) at the recipe level, with non-overlapping seed ranges; errors
fall from a mean of 18.8 to 10.5.** The shipped artifact is seed 41 (186); it is the exact
file set that was measured, copied rather than re-exported, because every export redraws
the int8 graph. On the benchmark it reads 200/217 int8 — inside the recipe's range, and
the draw the rule dealt; docs-classification is its weakest category (20/24).


# Agent-written queries: the corpus was answering the wrong caller (2026-09-28)

Since the service became the `search_runbooks` MCP, almost every query is written by the
CALLING AGENT — Claude Code or Codex describing "the outcome wanted" in a short imperative
("Move a running agent to a new worktree and have it implement a code change"). The corpus
was written for the retired sidebar assistant: of 10,850 rows, **322 (3%)** were bare,
impersonal task descriptions; the rest were user chat. This round measured that gap, filled
it, and swept every remaining model-side lever under the same hardware budget.

## A suite shaped like production: holdout round 4

320 single-message cases from four blind writers (Claude-style and Codex-style agent
queries, users' own words, and every `whenToUse` fence from both sides), each labelled blind
by gpt-6-sol and gpt-6-astra (319 unanimous with the writer). 43 near-duplicates of training
rows removed; the remaining 277 split at random into **round4a** (138, development) and
**round4b** (139, sealed until the decision). Both live in `benchmarks/runbook_selection/holdout/`.

The shipped artifact scored 123/138 on round4a — routing-trap 46/46 and single-runbook 60/60,
but **no-task 3/15**: an ordinary coding task written as an agent's task description
("Convert the callbacks in fs_utils.js to async/await", "Add type hints to…") selected
`spawn-visible-agent` at 0.97-0.99. The sidebar assistant could not edit files, so for it
"a code change" always meant "launch an agent", and the corpus taught exactly that.

## What moved: verified agent-style data

Six generator agents (catalog digest + runbook files only; never benchmarks, tests or the
holdouts) wrote ~65% agent-written / ~35% user-words rows per catalog neighbourhood, with
fences in BOTH directions for the pairs that measurably lose, plus foundation-alone reads,
nothing-applies coding tasks and two-runbook asks. Every row was relabelled blind by
gpt-6-sol and kept only where both agreed: **1,792 of 1,802** (run
`2026-09-28-agent-queries`). A second, smaller round aimed at the new model's remaining
development misses added **536 of 541** (run `2026-09-28-agent-queries-2`). Rows
near-duplicating any holdout case (difflib > 0.8) were dropped before ingest. Corpus
10,850 → 13,155.

Recall-pass summed over dev (286) + rounds 1-3 + round4a, int8 ranker, canonical active sets,
three seeds (41/7/17) each:

| arm | per seed | mean |
|---|---|---|
| shipped artifact (as served) | — | 846 |
| current recipe, old corpus | 847 / 842 / 842 | 843.7 |
| + agent-query data (round 1) | 861 / 859 / 856 | 858.7 |
| … with an fp32 retriever | 867 / 860 / 860 | 862.3 |
| + round 2 data, fp32 retriever (**chosen**) | 869 / 860 / 862 | **863.7** |
| + `--llrd 0.85` | 867 / 862 / — | 864.5 against 864.5 for the same two seeds without it |

**+20 over the recipe it replaces, with non-overlapping seed ranges.** Round4a (the
production-shaped suite) 123 → 134 on the shipped seed, no-task 3/15 → 12/15, and extras
27 → 8. The second data round is +1.4 at the recipe level — noise, and adaptive (its briefs
were written from development misses) — but it lowered extras and halved fp32/int8
decision drift, and the pre-declared rule chose it.

## The fp32 retriever

The int8 "lottery" was mostly the retriever. Re-exporting one checkpoint reproduces the same
int8 decisions exactly (`quantize_dynamic` is deterministic here), so the variation is a
property of each retrain's weights. Serving the retriever fp32 and the ranker int8, over
eight artifacts: 0, 0, 0, +1 on the four old-corpus ones; +6, +1, +4, +4 on the four new ones
(dev + rounds 1-3 + round4a). It never lost a case. Cost: **+51MB RSS (431 → 482MB) and
+0.5ms** (select p50 9.7 → 10.2ms, one thread). `SetwiseClassifier` now loads
`retriever/model.onnx` in both modes, stamps tables `int8-ranker` so a table built by the old
int8 retriever re-embeds instead of being reused, and the exporter no longer writes an int8
retriever.

## Measured and rejected

| lever | result (same suites) | verdict |
|---|---|---|
| gte-modernbert-base stage 2 (149M, +4ms) | 831 vs 847 paired | −16, under-selects |
| ettin-encoder-150m stage 2 | 831 vs 848 | −17 |
| snowflake-arctic-embed-m-v1.5 stage 2 (BERT-base) | 862 vs 869 paired | tie at best |
| e5-base-v2 stage 2 (BERT-base) | 863 vs 869 paired | tie at best |
| static QDQ int8 (MinMax / 99.99 percentile) | 817 / 820 vs 847 | −27 / −30 |
| uniform soup of 3 seeds (`--init-seed` shared) | 863 / 862 vs means 862.3 / 863.7 | = the seed mean, no gain |
| `BONUS_FLOOR` 0.55 → 0.45 | +2 / +2 / 0 for +8 / +3 / +9 extras | not worth a constant tuned on read suites |
| `--llrd 0.85` (layer-wise LR decay) | −2 / +2 paired on seeds 41 / 7 | neutral; the third seed was stopped when it could no longer change the choice |

Notes worth keeping:

- **bge-base's retrieval pre-training beats newer general encoders here.** ModernBERT-family
  models score higher on GLUE and lower on this task; both under-selected (fewer extras,
  more misses). The ONNX export of ModernBERT needs `attn_implementation="eager"` —
  `export_two_stage` now does that for non-BERT backbones.
- **Soups need a shared head init, and then they work** — unlike the random-init soup
  recorded above, `--init-seed` soups score at the seed mean. They remove the lottery at
  3x training cost; they do not beat it. `training/soup_setwise.py` refuses unaligned heads.
- **Static int8 calibrates activation ranges the dynamic path measures per input**, and
  512-token runbook documents blow those ranges for 40-token queries. Dynamic per-channel
  with `reduce_range` stays.

## The sealed result

Decision rule written before any arm was scored: mean over seeds 41/7/17 of int8 recall-pass
on dev + rounds 1-3 + round4a; ship the winner's seed-41 artifact only if round4b beats the
shipped artifact (as served) by ≥ 2 and the dev mean is no more than 2 below it.

Round 4b, scored once, int8, canonical active sets:

| | recall-pass | strict | extras | no-task |
|---|---|---|---|---|
| artifact shipped before this entry (as served, both encoders int8) | 130/139 | 111 | 28 | 6/15 |
| chosen recipe, seed 41 | **138/139** | 128 | 11 | 14/15 |

+8 against a required +2, and the chosen recipe's dev mean (268.0) is above the old
artifact's 266. The artifact that SHIPS is that recipe's seed 41 retrained once more after
the docs-label fix below (stage 2 only): 270/98/178/188/133 = 867 on the development suites
against 869 before the fix, 138/139 on round 4b (strict 126, extras 12), docs 24/26, shortlist
ablation 0/282, whole-process RSS 469MB. Round 4b is development data from here.

Cases this retrain was owed and now passes: `host-issue-agent-worktree` ("launch an agent in
a new worktree for issue N" → start-work-on-issue), the two rename regressions
`cancel-one-named-watcher-is-foundation` and `injection-in-tool-output` (their TODO entry is
closed), `tx-move-running-agent-to-new-worktree-agent-query` and
`tx-fact-contest-peer-vote-user-words`.

## A docs label this round broke, and fixed

The first install failed `bench local` on the docs floor (4/26 missed lookups, ceiling 3). The
cause was in this round's own data: one generator brief asked for "questions about the
Daintree PRODUCT itself" as nothing-applies contrasts, and 17 such rows ("What's the keyboard
shortcut in Daintree to split a pane?") landed as `is_docs: false` — the exact contradiction
recorded on 2026-08-20. Search never reads the docs head, but the label was wrong, so the rows
were relabelled `docs: true` and stage 2 retrained (stage 1 never reads the flag). **A brief
that asks for "not a runbook" must also say whether it is a documentation question.**

## Corrections after the shipped weights were trained

Review found four row-level defects after the shipped stage 2 had trained, fixed in the
corpus rather than by another retrain: two rows near-duplicating evaluation cases were
removed ("Launch an agent in a new worktree for issue 212 and have it implement the fix",
0.867 against `host-issue-agent-worktree`; "which worktrees do i have rn", 0.824 against
round 2's `hE-foundation-list-worktrees`), and two more tab-behaviour questions were set to
`docs: true`. The shipped weights saw the corpus BEFORE these four changes (13,155 rows; the
journals keep both removed rows), so `host-issue-agent-worktree` passing is partly a
near-duplicate and should not be quoted as a generalisation win.

Review also found that `--init-seed` re-seeded the MPS generator as well as the CPU one
(`torch.manual_seed` seeds every device; `set_rng_state` restores only CPU). So in every
`--init-seed` arm above, seeds 7 and 17 differed only in data order and CPU-side sampling,
not device-side dropout. The shipped artifact is seed 41 with `--init-seed 41` and is
unaffected; the trainer now seeds only the CPU generator for the head.

## Still open

- `vectors.npz` identity is (precision stamp, document digests), not the encoders that built
  it: an artifact directory holding a table from a DIFFERENT checkpoint at the same precision
  would be adopted. The exporter always rebuilds the table with the graphs, so this needs a
  hand-assembled artifact to bite; an embedding-function fingerprint in the table would close
  it.
- `edit-not-explore` (dev) expects "Add a retry with backoff to the webhook sender" to load
  `spawn-visible-agent`; round 4's writers and both annotators label plain code changes
  `[]`, since the runbook claims requests that ask for a visible agent. The answer depends on
  the caller — the sidebar assistant cannot edit, an agent pane can — and the query does not
  say which. Left as written; the new data leans `[]`, and the shipped artifact now misses it (p=0.013).

# Catalog 40 → 87, and the caller becomes the orchestration layer (2026-09-28)

The catalog grew from 40 selectable runbooks to 87 in one round, and the user set the
principle the new runbooks — and six old ones — were rebuilt around: **the caller is
Daintree's orchestration layer**. It reads Daintree state, runs Daintree operations and
verifies; the work itself (code, git, environment, writing) goes to a visible agent in the
right worktree, briefed with the runbook's know-how for that work (`docs/RUNBOOKS.md`,
`CLAUDE.md`, the foundation's first line). This settles the `edit-not-explore` conflict
recorded above: a plain code change routes to `daintree.edits.spawn-visible-agent`, not `[]`.

## How the runbooks were made

Six ideation agents (one per domain, each seeded by Ask Google) proposed 72 candidates; 47
survived dedupe; eight authoring agents wrote them with cases and traps against the pane
action schema; eight Codex reviews (four gpt-6-astra max, four gpt-6-sol high) checked
each against `pane-core.json` and the app source; fix agents applied the findings under the
principle. `file-issues-batch` was dropped (no Daintree role; batch filing moved into
`manage-issue`) and a coverage-gap pass added `decompose-work` and extended five runbooks
(memory → Daintree's `.daintree/assistant/` store; recipe authoring; `.worktreeinclude`;
merge trains; adopting a contest winner). Reverse fences were written into 33 established
runbooks.

## Data

| run | rows | how verified |
|---|---|---|
| `2026-09-28-catalog-expansion` | 2,321 compiled single-turn (positives, both-direction boundaries, pairs) | blind gpt-6-sol, 2,380/2,384 agreed |
| `…-stateful` | 705 compiled follow-ups / topic shifts | blind gpt-6-sol with history, 707/736 |
| `…-late`, `…-late-stateful` | 217 (decompose-work, gap-fixed territory) | 207/247 (adopt-the-winner rows dropped: the routing text does not claim them) |
| `…-refine` | ~350 (trial-found weak spots; docs-vs-action contrast) | 372/372 |
| blind relabel | 206 corpus rows + 26 benchmark/holdout cases | sol, then astra on disagreements; changed only where both agreed AND the new label was a new runbook or project work leaving `[]` |

The multi-turn rows were not optional: 2,300 single-turn rows alone pushed two corpus
shortcut guards over (docs label 2.19x likelier with history; cardinality TV 0.069 between
active and inactive rows). Generating follow-ups and topic shifts for the new runbooks fixed
both honestly. A generator brief that asks for `[]` rows must now also say project work is
not `[]` (the principle) — the audit moved about 80 old `near_miss` code-change rows off `[]`.

Round 5 (266 cases, 133 dev / 133 sealed) was written against the grown catalog by four
read-only writers and labelled blind by sol and astra: 270/270 unanimous.

## Round 1 retrain ("expand": stage 1 + three stage-2 seeds, settled recipe)

| | dev | r1 | r2 | r3 | r4a | r4b | r5a | total |
|---|---|---|---|---|---|---|---|---|
| shipped (40-runbook weights, new catalog) | 632/687 | 91 | 175 | 181 | 121 | 130 | 113 | 1443 |
| s41 | 659 | 99 | 175 | 189 | 132 | 134 | 130 | 1518 |
| s42 | 657 | 96 | 178 | 190 | 134 | 137 | 130 | 1522 |
| s43 | 656 | 97 | 174 | 185 | 130 | 135 | 131 | 1508 |

Stage 1: r@32 0.999, r@1 0.849 at 87 runbooks; candidate_miss 0 on every suite. s42 had the
best total but failed the docs gate (4/26 missed lookups > 0.12): the new imperative data
pulled "How do I create a worktree from a pull request?" onto `create-plain-worktree`. s41
passed (23/26). New-runbook cases (x-*): 395-397/401.

## Round 2 retrain ("refine": +350 rows aimed at trial-found weak spots and the docs boundary)

| | dev | r1 | r2 | r3 | r4a | r4b | r5a | total | docs |
|---|---|---|---|---|---|---|---|---|---|
| s41 | 661 | 97 | 174 | 184 | 131 | 137 | 133 | 1517 | 24/26 |
| s42 | 662 | 97 | 174 | 186 | 133 | 137 | 129 | 1518 | 25/26 |
| s43 | 655 | 98 | 175 | 187 | 131 | 136 | 130 | 1512 | 25/26 |

Mean 1515.7 against round 1's 1516.0. The rule written before this retrain replaced round 1
only on a mean at least as high, so **round 1's s41 ships**: the recipes tie, and a rule
chosen before the numbers is not reopened after them by a third of a case. The refine rows
stay in the corpus — they are verified, and they fixed the docs boundary in every seed
(24-25/26 against 22-23/26) — so the NEXT retrain inherits them. **The shipped weights were
trained on the corpus before them** (16,396 rows; the journal keeps them).

## Shipped, and the sealed check

Round 1 s41: bench local QUALITY GATE pass, 659/687 recall-pass, strict 602, extra 82,
docs 23/26, candidate_miss 0, p50 ~11 ms. Sealed round 5b, scored once:

| round 5b (133) | recall-pass | strict | extra |
|---|---|---|---|
| previous artifact (40-runbook weights) | 112 (84.2%) | 82 | 44 |
| **shipped** | **126 (94.7%)** | **115** | **17** |

**Leakage, found by review after scoring:** the dedupe screened queries but not history
lines, and four training rows' HISTORY near-duplicated three 5b cases (`r5-W1-026`,
`r5-W4-037`, `r5-W1-048`, difflib 0.84-0.88). The shipped model passes all three; without
them 5b is **123/130 (94.6%)**. 43 such rows (queries or history, any suite) were removed
from the corpus afterwards; the shipped weights saw them. `compile_corpus` only drops EXACT
collisions, so the next round must screen history too.

Real Haiku queries from the live trials, before → after: plain code changes ("Add input
validation to subtotal()…", "convert the utils folder to TypeScript") now select
`spawn-visible-agent` at 0.999; "give an agent 3 minutes…", "standup summary…", "which of my
worktrees can I get rid of", "wrap up for today…", "run the dev server in a terminal I can
watch", "use the plugin's TDD skill", "launch multiple agents to test a CLI…" all select
their runbook at ≥0.98. Still wrong: "spike two approaches … and recommend one" (nothing),
"red-team the coupon-codes branch" (create-plain-worktree), "switch agent model to Sonnet"
(spawn), "how does the cart total get calculated" (nothing) — each has refine rows waiting
for the next retrain.

## CI flipped a docs case: the refine round's s42 replaces round 1 s41

Round 1 s41 passed `bench local` on the Mac at docs 23/26 — exactly the ceiling (3 missed
lookups, 0.12). GitHub CI's Linux runner scored the same artifact 657/687 with docs 22/26 and
failed the gate: onnxruntime on x86 and on Apple silicon differ in the last bits, and a
model sitting ON a floor passes or fails by platform. An artifact must clear its floors with
a margin, not by the last case.

The refine seeds all clear the docs boundary with room (24-25/26). Refine s42 is the best of
them on every measure that was not already spent: 662/687 (strict 606, extra 77, docs 25/26)
against round 1 s41's 659 (strict 602, extra 82, docs 23/26), and 1518 over dev + rounds 1-4b
+ 5a against 1518. Round 5b, already opened, scores it 124/133 (strict 109, extra 23) against
126/133 — two cases, inside the ~7-case seed spread, and read after the choice rather than
used for it. It ships. Both it and round 1 were trained before the review removed the 43
leaking rows; a retrain on the cleaned corpus is the next step.

## What the live trials said (isolated Daintree, Claude Haiku orchestrating)

Five runners exercised 44 runbooks in ~90 trials (harness: an isolated Daintree build on its
own MCP port, a throwaway repo per run, transcripts found by the pane's `agentSessionId`).
The mechanics hold — Haiku picks the runbook, launches visible agents in their own
worktrees with notify+handback, ends its turn and resumes on the notice. What fails is the
orchestrator's own judgment:
- it reports the child's claims as its own verification (fixed where re-run by "before any
  report, verify yourself" steps; now also in the foundation);
- it falls back to Claude Code's native subagents (foundation: "never through a native
  subagent");
- it paraphrases briefs and drops rules even when told "verbatim";
- it writes the search query about the WORK ("convert to TypeScript") not the orchestration
  ("give an agent 3 minutes…"), so mechanism runbooks lose (tool description and client
  instruction now say keep how the user wants it done);
- destructive slips before fixes: committing on main unasked, `kill -9` on another
  checkout's server, checking out old commits in the main checkout.
Forge-heavy runbooks (PR queue, PR review threads, triage, Dependabot batches, release
notes) were NOT run: they need a real GitHub repository.

## Still open

- Five forge runbooks unverified live; `hunt-flaky-test`, `run-e2e-suite`, `postmortem-run`,
  `prune-stale-branches` (keep-list ignored) and several gated patterns carry body fixes no
  trial has confirmed.
- App: `terminal.readLastMessageOwned` returns `terminal-unknown` for panes the caller
  launched or was handed; `terminal.new` returns the same panel for identical calls within
  two minutes, undocumented; `terminal.list` shows an idle agent as `waiting`.
- Haiku sometimes loops on ToolSearch and never calls `search_runbooks` on long
  multi-agent prompts — a host behaviour, not routing.
- `fan-out-work` (510/512 tokens) and `competition` (505/512) have no room left in their
  routing text: the next fence there needs a trim or domain foundations (TODO).

# Round 6: catalog 87 → 105 (2026-09-28)

An outside review proposed 28 runbooks. 18 survived triage on one test: a distinct deliverable, a distinct failure to
guard against, distinct acceptance evidence, and a real Daintree orchestration role. The survivors are:
- **Coordinating the workload:** reprioritize-work, resolve-task-collisions, hand-off-orchestration,
  relieve-resource-contention.
- **Recovery:** undo-agent-changes, salvage-failed-attempt, recover-lost-work, resolve-semantic-conflict.
- **Review, security, prototype, docs:** respond-to-secret-exposure, audit-spec-coverage, productionize-prototype,
  sync-project-docs.
- **Release:** assess-readiness, deploy-and-verify, rollback-deployment, publish-package, test-distributable,
  prepare-feature-demo.

`migrate-worktree-database` gained restore-from-backup rather than a runbook of its own. The other proposals were
cut for these reasons:
- **Already covered:** a budget policy (`check-usage-limits` plus a fan-out cap), roster benchmarking
  (`competition`/`interview`), unwind-a-workflow, an environment matrix, and migration rehearsal.
- **No mechanism in Daintree:** cross-repository work (a connection is bound to one project), feature-flag
  rollout (no flag service or observation), and design verification (no screenshot action).
- **Not for Daintree users:** a maintainer tool for validating runbooks.

Process: the same as the 40 → 87 round.
- Five authors wrote the runbooks. Codex reviewed each batch (astra max on recovery, security and release; sol
  high on the rest) and fix agents applied the findings.
- Reverse fences went into 44 established runbooks. `fan-out-work`, `watch-outcome` and `review-change` now sit
  at 505-510 of their 512 routing tokens. Nearly every new body is within ~10 B of the 10240 B bundle ceiling,
  so the foundation cannot grow without trims.
- Found by reading the app source: Daintree refuses to hand a terminal over while the pane that launched it is
  still running, so a coordinator hand-off must exit the old coordinator before the user hands its children on.

Data:
- **Round-6 rows:** 1,046 single-turn rows were generated, and 1,041 of them agreed with a blind gpt-6-sol
  label. 11 of those were dropped as near-duplicates of a case, leaving 1,030.
- **Round-6 stateful rows:** 288 follow-ups and topic shifts were generated. 285 agreed with the blind label and
  29 near-duplicates were dropped, leaving 256. Screening now covers history lines against every case suite.
- **Relabel audit:** 1,050 existing cases and rows were candidates, flagged by the shipped selector ranking a new
  runbook top-3 or by vocabulary. Sol and astra agreed on 10 relabels. 9 corpus rows moved and no case changed.
  `tx-drive-release-checklist-in-browser-yourself` stays `[]`, as decided in the last round.
- **Corpus total:** 17,987 rows.
- **Holdout round 6:** 170 cases, 169 of them unanimous across writer, sol and astra. 5 near-duplicates were
  dropped, leaving 6a with 82 cases (dev) and 6b with 83 (sealed).

Routing before any retrain. The shipped refine-s42 weights with the 105-runbook catalog pass `bench local`:
- **Overall:** 804/834 recall-pass, strict 732.
- **Docs:** 29/30, with 1 missed lookup, well under the ceiling.
- **Candidate misses:** 0.
- **Holdouts (int8):** r1 97, r2 173, r3 185, r4a 133, r4b 137, r5a 127, r5b 123.
- **New runbooks:** the authors' y- cases rank their runbook first almost everywhere zero-shot. The exceptions are
  the casual phrasings of `prepare-feature-demo` and `respond-to-secret-exposure`.

The retrain has two stages. Stage 1 (the retriever) is trained: r@32 0.998, r@1 0.850 at 105 runbooks. Stage-2
seeds 41-43 were running at commit time, and the decision rule was written before any of them (margin on docs,
y- ≥ 90%, no holdout drop > 3, 6b scored once).

## Live trials (isolated Daintree, Haiku orchestrating)

The trials were stopped after 10 of the 18 runbooks: the runners and ~40 authoring agents had taken the user's
weekly Claude limit to 94%. Results:
- **Passed:** `resolve-task-collisions`, a live collision settled end to end. `deploy-and-verify` served the right
  revision and verified `/version`, the release symlink and a smoke test.
- **Partial:**
  - `assess-readiness` reported unknown areas as absent; the report now names every area.
  - `rollback-deployment` read git history as the serving release and could skip the authorization gate; both are
    fixed in the text. It stopped and asked, and production was untouched.
  - `respond-to-secret-exposure` kept every safety property, but child inventories printed the value once and
    called an unfinished search clean.
  - `salvage-failed-attempt` got the right outcome but skipped a user confirmation.
  - `prepare-feature-demo` had no preview started, or a hidden server reported as the demo.
- **Fail:** `test-distributable`. The orchestrator fixed the packaging bug it found and committed to main in all
  three runs, even after two body edits.
- **Not reached:** `reprioritize-work` routing. Haiku's query described the urgent fix, not the reallocation.
- **App, found by the demo runner:**
  - a dev-preview in a worktree that is not the active one does not start until it is shown (`worktree.setActive`);
  - the preview picks its own port;
  - no action reports the preview's URL.

## Shipped: round-6 seed 41

CI failed the first push of this round in `test_shipped_search_finds_the_obvious_runbook`. That test ran the
first search against the round-5 artifact, and the first search embedded the ~60 new or re-fenced runbooks
inside a 30 s timeout, which is 10 s on this Mac and more than 30 s on the Linux runner. The test now syncs the
catalog before timing a search, as the app's lifespan does. A freshly exported artifact also embeds every
runbook, so startup does no embedding at all.

Seed 41 is shipped without waiting for 42 and 43. Against the rule, the gate passes with margin and y- reaches
146/147. No suite drops against the round-5 weights on the same catalog; every row except r4a improved:

| | r1 | r2 | r3 | r4a | r4b | r5a | r5b | r6a | bench local | strict | extra | docs missed |
|---|---|---|---|---|---|---|---|---|---|---|---|---|
| round-5 weights | 97 | 173 | 185 | 133 | 137 | 127 | 123 | 74 | 804/834 | 732 | 97 | 1/30 |
| **round-6 s41** | **98** | **175** | **188** | 133 | **139** | **129** | **125** | **80** | **806/834** | **746** | **82** | 2/30 |

The x- cases score 395/401 (from 393), and candidate_miss is 0. Sealed round 6b, scored once: **81/83 (97.6%)**,
strict 71.

From here the cycle is add runbooks, run `training/retrain.sh` (one seed, gated by `bench local`, restores the
previous artifact on failure), commit, and release. The release no longer waits for GitHub CI: Cloud Build
smoke-tests every candidate before it takes traffic.

## Still open
- Trials of the other 8 runbooks, and reruns confirming the body fixes, are owed once the usage limit resets.
- "A checking runbook reports failures, it doesn't repair them" belongs in the foundation, where every
  delivered runbook would carry it. The foundation has no byte budget left, so adding the rule needs trims
  elsewhere first.

# Catalog 106 → 153: the hosts wave, measured zero-shot (2026-10-16)

Issue #144: measure recall, ownership confusion and bundle cost as the hosts family lands, before
anything about the model, `k` or the budgets changes. Nothing about them did: no floor, no `k`, no
artifact, no budget, no routing text. What changed is measurement (an ownership audit of 39 new
cases, and `bench compare` / `bench scale` fixes) and this record.

**Before** is `133bab34`, the last commit before `daintree.hosts.foundation` (`378f17cf`): 107
runbook files, 106 selectable, 850 cases, catalog `sha256:39f7e189…`. **After** is `af7b68d3` on
develop: 154 files, 153 selectable (`daintree.docs.answer-usage-question` stays hidden), 1,876 cases,
catalog `sha256:79bff154…`; then the same tree plus the audit, 1,915 cases. In between, 47 runbooks
were added, every one of them `daintree.hosts.*`. 24 established runbooks had their routing text
rewritten, with reverse fences against the family or a Remote Host clause, and 8 had bodies edited.

**The selector did not change.** The artifact tree is `a5c2edf4` at both commits: the round-6
seed-41 weights, trained at 105 runbooks. Every hosts runbook is routed zero-shot, embedded at
startup and never trained on. So this is the "reachable, not competitive" case CLAUDE.md describes,
measured across a whole family rather than one runbook.

**No benchmark case changed.** All 850 pre-wave cases are byte-identical at HEAD (id, messages,
expectations, category), so every move below is the catalog's doing.

## Totals

| | before | after | after + audit |
|---|---|---|---|
| runbook files / selectable | 107 / 106 | 154 / 153 | 154 / 153 |
| cases | 850 | 1,876 | 1,915 |
| recall-pass | 821 | 1,790 | 1,818 |
| strict | 755 | 1,537 | 1,558 |
| miss trials | 25 | 82 | 93 |
| extra trials | 89 | 321 | 336 |
| candidate_miss | 0 | 0 | 0 |
| docs correct | 28/30 | 29/31 | 29/31 |
| quality gate | pass | pass | pass |

Against the floors at after + audit: recall-pass 0.949 (floor 0.90), strict 0.814 (0.78), miss 0.049
(≤ 0.09), extra 0.175 (≤ 0.20), weakest category 4/6 and 6/9 (0.55). Extra is the nearest.

Split by where the case came from:
- **The 850 shared cases:** recall-pass 821 → 821 and strict 755 → 753. The wave fixed none and
  broke none.
- **The wave's own 1,026 cases:** recall-pass 969 and strict 784, which is 0.764. Taken alone that
  is under the 0.78 strict floor; the suite clears it only because the older cases carry it.

| category | recall-pass before → after + audit | strict before → after + audit |
|---|---|---|
| adversarial | 4/5 → 5/6 | 3/5 → 4/6 |
| background-wake | 4/5 → 4/6 | 4/5 → 4/6 |
| continuation | 9/9 → 22/25 | 8/9 → 14/25 |
| docs-classification | 22/24 → 22/24 | 20/24 → 20/24 |
| foundation-only | 5/7 → 6/9 | 5/7 → 5/9 |
| host-query | 41/42 → 43/44 | 37/42 → 39/44 |
| multi-runbook | 5/5 → 5/5 | 4/5 → 4/5 |
| no-task | 6/8 → 6/8 | 6/8 → 6/8 |
| routing-trap | 490/502 → 1,140/1,174 | 453/502 → 999/1,174 |
| single-runbook | 226/232 → 556/603 | 206/232 → 454/603 |
| topic-shift | 9/11 → 9/11 | 9/11 → 9/11 |

**`foundation-only` did not regress.** Its 5/7 → 6/9 is the same two shared misses on both sides
(`novel-read-terminal-output`, `cap_full`; `capability-home-config-commands`, `rank_miss`) plus two
new `daintree.hosts.foundation`-alone cases. `hosts-foundation-switch-on-messaging` passes;
`hosts-foundation-how-to-talk` gets nothing at all. The TODO trigger "sooner if `foundation-only`
regresses" has not fired.

## Every moved case

`bench compare` lists 25 shared cases whose loaded set moved. None changed recall-pass. Strict
fell on 7 and rose on 5. The three that list a lost runbook on the after side
(`explore-readonly-agent`, `keyword-bait-rename`, `tx-continue-agents-cut-off-by-sleep`) failed
identically before. The compare now prints both sides' losses, which is what shows that.

**A new hosts runbook rides in as an extra (12).** Untrained, it scores near 1.0 on shapes that
share its vocabulary:
- `hosts.supervise-swarm` (7 cases). It is the strongest attractor of the wave: "keep work already
  out … moving across turns" shares `orchestration.watch-outcome`'s wake-me vocabulary.
  - `open-dialogue-not-multi-agent`, `wake-me-on-merge-is-not-a-status-read` and
    `y-deploy-verify-trap-watch` lose strict.
  - `host-notice-then-verify` swaps `agents.steer-running-agent` (re-fenced) for it.
  - `sweep-now-plus-wake-later` swaps `orchestration.workspace-cleanup` for it.
  - `watch-grant-for-unattended-rollback` takes it with `hosts.park-swarm`, in place of
    `park-work` and `rollback-deployment`.
  - `tx-continue-agents-cut-off-by-sleep` takes it with `hosts.steer-swarm`. That case's
    `agents.recover-agent` miss moves from `rank_miss` to `cap_full`.
- `hosts.do-delegated-work` displaces `edits.spawn-visible-agent` in `host-issue-agent-worktree`.
- `hosts.clean-up-for-hub` joins `novel-resolve-one-queue-item` (loses strict).
- `hosts.shard-test-suite` displaces `context.feed-running-agent` in
  `tx-drive-release-checklist-in-browser-yourself`, a no-task case failing on both sides.
- `hosts.report-swarm-results` displaces `review.review-pr-queue` in `x-gap-land-two-in-order-agent`.
- `hosts.verify-for-hub` displaces `release.test-distributable` in `y-deploy-verify-agent`.

**A re-fenced runbook moves (6).** Its routing text was rewritten against the hosts family, so it
was re-embedded:
- `keyword-bait-rename` drops `diagnostics`. It still misses `foundation` on both sides.
- `memory-not-explore` swaps `status-and-resume` for `execute-durable-workflow`. Both were
  re-fenced, and the case is non-strict on both sides.
- `y-release-ready-trap-deploy` drops `release.assess-readiness` and gains strict.
- Three cases whose required runbook was re-fenced pick up a new extra and lose strict:
  - `x-bug-bash-trap-e2e`: `ci.run-e2e-suite`'s routing text grew from 1,005 to 1,604 characters
    for server provenance on a host, and the case picks up `ci.run-local-checks`.
  - `x-migration-sweep-user`: `orchestration.migration-sweep` was re-fenced; the case picks up
    `worktree.relocate-agents`.
  - `x-port-user-whats-on`: `runtime.free-busy-port` gained its Remote Host clause; the case picks
    up `workflow.follow-up-on-issue`.

**Admission-floor flicker (7).** No runbook text involved changed. In each case the runner-up sits
within 0.05 of the 0.55 `BONUS_FLOOR`. The set-wise head scores every candidate against the whole
shortlist, and the shortlist now holds hosts runbooks, so a score that close moves across the
floor. Measured with `bench search` at after:

| case | runner-up | score | effect |
|---|---|---|---|
| `x-gap-spike-keep-winner-casual` | `resolve-task-collisions` | 0.52 | dropped, strict gained |
| `x-trap-rebase-merge-pr-is-land` | `git.sync-with-base` | 0.50 | dropped, strict gained |
| `x-prune-user-delete-merged` | `workspace-cleanup` | 0.50 | dropped, strict gained |
| `y-deploy-verify-trap-rollback` | `investigate-outage` | 0.54 | dropped, strict gained |
| `x-decompose-prd-into-issues-agent` | `fan-out-work` | (not re-ranked) | dropped |
| `tx-ask-codex-second-order-risks` | `agents.ask-running-agent` | 0.55 | admitted |
| `explore-readonly-agent` | `agents.ask-and-relay` | 0.59 | admitted; still misses `spawn-visible-agent` on both sides |

## Ownership audit (`cases/253-ownership-traps.yaml`)

The issue cites a 2026-10-15 32-query audit. It is not in the repository, so it was replaced with
one that is:
- **How the 39 cases were written.** Each was drafted blind, before any was ranked, and is kept
  whether it passes or not.
- **How the owners were set.** Every owner comes from the routing text, with the deciding clause
  quoted in the case note.
- **Screening.**
  - Every string was screened with `SequenceMatcher` against 54,381 case and corpus strings
    (queries and history), and the maximum is now 0.71.
  - Seven drafts at 0.75 or above were reworded before they were ranked. One of them had reused
    an existing case's example SHA.
- **What was dropped.** Two probes whose owner the routing text genuinely splits were left out
  (findings 7 below).

| boundary | cases | recall-pass | strict | the misses |
|---|---|---|---|---|
| Remote Host vs this machine: usage, placement | 7 | 7 | 2 | none; the local usage twin rides along both ways, `place-work` pulls `swarm-fan-out`, `run-local-cohort`, `steer-swarm` |
| worktree cloud devbox vs Remote Host | 5 | 5 | 5 | — |
| dev stack across hosts | 5 | 2 | 1 | `coordinate-dev-services` lost to `hosts.foundation` alone; to `run-validation-matrix` + `ci.run-e2e-suite`; to the local `worktree.provision-local-environment` |
| one dev server here, a port clash | 4 | 3 | 3 | a remote vite port went to `hosts.reassign-host-work` + `recover-and-clean-up` (`free-busy-port`, `cap_full`) |
| the lane's side of an exchange | 8 | 4 | 3 | `verify-for-hub` lost to `shard-test-suite` + `report-swarm-results`; `review-for-hub` and one `demo-for-hub` lost to the envelope (`handle-hub-message` alone); one `demo-for-hub` lost to `connect-host` |
| the hub's side of the same work | 6 | 3 | 3 | the hub's one-host verify went to the LANE's `verify-for-hub`, not `delegate-to-host`; `recover-and-clean-up` lost to the local `workspace-cleanup` and to `swarm-fan-out` |
| this machine's own owners | 4 | 4 | 4 | — |
| **total** | **39** | **28** | **21** | 10 `rank_miss`, 1 `cap_full`, 0 `candidate_miss` |

Every miss had the right runbook on the shortlist, and every one of those runbooks is untrained.
No local owner lost to a hosts runbook here. Against the cited audit's two claims:
- "Receiver requests routed to unrelated procedures" reproduces: `connect-host` for a lane demo,
  and `shard-test-suite` for a lane verify.
- "Host requests routed to local owners" reproduces: `workspace-cleanup` and
  `provision-local-environment`.
- "Host requests routed to cloud-resource owners" does not: 5/5 strict in both directions.

Across the whole suite after the audit, 97 cases fail:
- 59 lose a hosts-owned runbook:
  - 21 to a local owner;
  - 23 to another hosts runbook;
  - 15 by picking nothing wrong, i.e. under-selection.
- 34 lose a local-owned runbook. 8 of those pull a hosts runbook in: both
  `fbp-hosts-*-same-number` / `-free-over-there-*`, both `undo-swarm-changes-lane-*`,
  `clean-up-for-hub-reverse-lane-read-only`, `migrate-db-hub-tunnel-check-before-reset`,
  `tx-continue-agents-cut-off-by-sleep` and `own-remote-vite-port-taken`.
- 4 are no-task or docs cases.

Stage losses: `rank_miss` 80, `cap_full` 10, `cardinality_short` 2, `background_reuse` 1,
`floor_reject` 0, `candidate_miss` 0.

## Shortlist scale (`bench scale`)

`k=32` misses, by catalog size, counted over the cases with a required runbook:

| | real | 200 | 500 | 1,000 |
|---|---|---|---|---|
| before (107 files, 820 cases) | 0 | 0 | 0 | 3 |
| after + audit (154 files, 1,885 cases) | 0 | 3 | 6 | 9 |

Wider shortlists after: `k=48` misses 0 / 3 / 6 at 200 / 500 / 1,000, `k=64` misses 0 / 1 / 5, and
`k=96` misses 0 / 0 / 4. The hosts family is 47 similar documents, and it crowds the cosine
shortlist for hosts-adjacent queries. This is the "own confusion structure" the TODO said a real
catalog would have.
- **At 200, all three misses are hosts-adjacent and sit just past the shortlist.**
  `supervise-swarm-host-message-to-hub` needs `hosts.supervise-swarm`, rank 33.
  `tx-continue-agents-cut-off-by-sleep` needs `agents.recover-agent`, rank 34.
  `clean-up-for-hub-reverse-lane-read-only` needs `worktree.audit-worktrees`, rank 37.
- `daintree.foundation` is no longer the deepest required runbook.
- `foundation-only` reaches the shortlist 9/9 at every size.
- **The dormant `scale.smoke()` would now fail.** Its budget is 1 miss at 200 runbooks. Nothing
  calls it.

`k` stays 32: 0 candidate misses at 153 selectable, and every loss above is downstream of the
shortlist. The ~200 trigger stands, with a thinner margin than the 2026-08-21 sweep showed (0
misses at 200 then, 3 now). `k=48` would clear 200.

`bench scale` with no `--sizes` had refused to run since the catalog passed 100. A size below the
real catalog is now measured at the real catalog's size, and the run says so.

## Bundle cost

The ceilings are unchanged and every runbook is under them. They bind with almost no room:
- **Foundations.** `daintree.foundation` is 5,384 B against its 5,400 B ceiling.
  `daintree.hosts.foundation` is 2,417 B.
- **Bundles** (a runbook plus everything `supporting_for` sends with it, against 10,240 B):
  - median 10,205 B;
  - largest 10,237 B (`runtime.measure-performance`, 3 B of headroom);
  - 98 of 153 at 10,000 B or more, and 117 at 9,500 B or more.
- **Every hosts leaf ships three bodies:** itself and both foundations, 7,801 B before its own
  text. A hosts procedure therefore has 2,439 B of room against 5,120 B elsewhere. The 46 hosts
  leaves have 7–211 B of headroom (median 15).

So a rule added to `daintree.hosts.foundation` costs every hosts leaf byte for byte, and
`daintree.foundation` can take 16 B. Shared hosts judgment has to be paid for by trimming leaves
first.

## Findings

1. **A retrain is owed, and the evidence is now measured.**
   - Every audit miss had an untrained hosts runbook on the shortlist, and lost on rank.
   - Of the 59 suite failures that lose a hosts-owned runbook, every runbook is on the
     shortlist and untrained.
   - The corpus already carries the rows: every hosts runbook has 61–132 compiled rows
     (`coordinate-dev-services` 66, `demo-for-hub` 74, `recover-and-clean-up` 132). The retrain
     trains on what is there; it is not an authoring job.
   - Follow the pipeline: `training/retrain.sh` over three or more seeds, a fresh sealed holdout
     round for the hosts family, then `calibrate_floors.py`.
   - Decide `k` at that retrain only if the catalog is headed past ~200. `k=48` is the measured
     minimum there.
2. **The extra rate is the floor nearest to binding.** It rose from 89 trials (0.105) to 336
   (0.175) for 47 runbooks, against a 0.20 ceiling. Another untrained wave of this size would
   reach it.
3. **`hosts.supervise-swarm` is the wave's attractor:** an extra on 7 of the 25 moved shared cases,
   all watch-outcome or conversation shapes. Write reverse rows where
   `orchestration.watch-outcome` wins against it. Do not fence its untrained `whenToUse` without a
   per-fence `training.try_runbook_text` measurement: a fence on an untrained runbook has raised
   it on the queries it fenced before.
4. **The lane's envelope takes the lane's work.** `handle-hub-message` alone took 2 of 8 lane
   requests, and `connect-host` a third. Rows are owed where the specific receiver
   (`review-for-hub`, `demo-for-hub`, `verify-for-hub`) wins with the envelope as
   `confusable_with`.
5. **The hub takes the lane's verifier.** "Have the linux box verify my commit" picked
   `verify-for-hub` over `delegate-to-host`. Rows are owed on the hub's phrasing.
6. **Cross-host dev stacks lose `coordinate-dev-services`** on 3 of 5 phrasings: to the hosts
   foundation alone, to the validation matrix with e2e, and to the local
   `provision-local-environment`.
7. **Two owners are split in the routing text, with no fence either way.** `bench lint` ranks the
   first pair the closest in the catalog with no id edge. The six most confusable pairs in the
   whole catalog are all inside `daintree.hosts`. Settle ownership in the text, measure it, then
   case it.
   - "is messaging even turned on there" fits both `hosts.diagnose-messaging` ("which gate is in
     the way") and `hosts.foundation` ("why one is not reachable"). Cosine 0.69.
   - "ready to take work" fits both `hosts.foundation` ("whether it is set up") and
     `hosts.start-swarm`.
8. **The usage twins ride together.** `agents.check-usage-limits` comes along on 2 of 3 remote
   usage asks, and `hosts.check-usage-limits` comes along on the local one. Both texts already
   fence each other, so this is training, not text.
9. **No hosts procedure can grow.** See Bundle cost.

## Proof layers reached

- **Layer 1 (selection):** `bench coverage`, `bench local`, `bench compare`, `bench scale` and
  `bench search`.
- **Layer 2 (contract):** the budget and hygiene tests in `tests/unit`, unchanged and passing.
- **Layers 3–5:** none. No trial ran, and the hub and lane surfaces are not stageable
  (`.claude/skills/runbook/references/trials.md`). Nothing here says a request is handled well;
  only which runbook search returns for it.

## Commands

```bash
# before, in a detached worktree (the artifact is LFS: git lfs pull there first)
git worktree add --detach ../wave-base 133bab34
(cd ../wave-base && PYTHONPATH=src:. "$OLDPWD/.venv/bin/python" \
  -m benchmarks.runbook_selection.bench local --out "$OLDPWD/before.json")
(cd ../wave-base && PYTHONPATH=src:. "$OLDPWD/.venv/bin/python" \
  -m benchmarks.runbook_selection.bench scale --sizes 107,200,500,1000)
# after
python -m benchmarks.runbook_selection.bench coverage
python -m benchmarks.runbook_selection.bench local --out after.json
python -m benchmarks.runbook_selection.bench compare before.json after.json
python -m benchmarks.runbook_selection.bench scale          # 0,100 → 154; then 200, 500, 1000
python -m benchmarks.runbook_selection.bench lint --top 12
python -m benchmarks.runbook_selection.bench search --max-results 6 "<a moved case's text>"
pytest tests/unit                                           # budgets and hygiene (layer 2)
git rev-parse 133bab34:src/daintree_assistant_server/runbooks/local/artifacts HEAD:src/daintree_assistant_server/runbooks/local/artifacts
```

The bundle numbers use the budget test's own sum: each selectable runbook's body bytes plus
`catalog.supporting_for([id])`.

## Still open
- **The retrain** in finding 1, with rows for findings 3–6 first.
- **Owners for finding 7**, decided in the routing text.
- **The audit file** is the yardstick for the retrain. Rerun the wave recipe in
  `benchmarks/runbook_selection/README.md` after it, and after each further wave.
