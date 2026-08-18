# Local skill selection — experiment log

> ## ⭐ SHIPPED CONFIGURATION
> ```
> backbone   BAAI/bge-base-en-v1.5  (109M)
> head       37 skills + 1 docs + 4 cardinality
> corpus     training/data/selection.jsonl @ 7,773 rows (five new skills fully served)
> train      --epochs 10 --lr 5e-5 --batch-size 48 --balance-power 0.0 --seed 41
> serve      ONNX int8 (300 MB RSS), earn-your-place admission
> ```
> **Current benchmark: shipped int8 measures 157/166 recall-pass (94.6%),
> fp32 157/166; int8 p50 9.2 ms. Synthetic held-out exact-match is 0.844.**
> The suite grew to 166: `18-new-skill-surfaces.yaml` gives the five newest
> skills the recall and routing-trap cases they landed without. On the ORIGINAL
> 150 the same artifact measures 142 — i.e. the expansion cost nothing and the
> new skills route at 15/16 (fp32 16/16) on top.
>
> **The lesson of this round: a fence is only learned in the direction it is
> written.** The five new skills' first full-strength corpus (7,602 rows, ~55
> positives + ~45 boundary negatives each, generated per-skill) scored 16/16 on
> the new cases but dropped the original 150 from 141 to 138 — the new forge
> skills were displacing `prep-branch-for-review`, `find-issue-work` and
> `start-work-on-issue`. The cause is in the catalog, not the model: a new skill
> declares carve-outs against the older skills it neighbours, while those older
> skills predate it and name it nowhere, so every generated boundary row ran
> new→old and none ran old→new. 174 reverse-direction rows (established skill
> correct, new skill as the hard negative, catalog-derived) took the original 150
> to 142 and the total to 157. **Adding a skill means generating boundary data
> for its neighbours too, not just for itself.**
>
> The 3-seed logit ensemble reached 131 but costs 3x memory and latency for +1
> inside the noise band — not shipped. Weight-soup fails here because each
> seed's head init is random, so the averaged heads are not aligned.
>
> Rebuild:
> ```
> .venv-train/bin/python -m training.train_classifier \
>     --epochs 10 --lr 5e-5 --balance-power 0.0 --seed 41 --base-model BAAI/bge-base-en-v1.5
> .venv-train/bin/python -m training.export_classifier
> ```
> Alternates kept: `training/models/BEST_130` (round-3 corpus era),
> `training/models/r4_seed23`. Corpus snapshot of the 130 run:
> `training/data/.selection_best_130.jsonl`.

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

## Architecture B — multi-label classifier (current)

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

## Latency (single thread, matching a 1-vCPU container)

| approach | p50 | vs DeepSeek |
|---|---|---|
| DeepSeek selector (network) | 2,044 ms | 1x |
| cross-encoder pipeline, full docs | 309 ms | 7x |
| classifier + bi-encoder fusion | 3.1 ms | 661x |
| **classifier alone** | **2.1 ms** | **955x** | *(MiniLM-era row — the shipped bge-base measures ~9ms int8; see header)* |

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
   split is row-random over paraphrase families.

---

### Seed / precision variance (the honest error bar)

Round-4 corpus, same recipe: seeds 17/23/41 scored 129/127/130 (torch fp32);
int8 exports moved individual runs by ±2 more. Synthetic val varies only ±0.002
— the benchmark spread is 20-odd near-tie cases flipping, not model quality
moving. Report the family as **129 ± 2**; do not chase single-run peaks.

3-seed logit-mean ensemble: 131/150. Weight soup: 127 (heads unaligned across
seeds — random init per seed — so averaging them is meaningless).

## Serving-policy decisions (measured, settled)

- **int8 is the shipped precision for bge-base**: 300MB RSS vs 665MB fp32 —
  fp32 does not fit a 1GiB container beside the server. bge-base int8 PASSES the
  decision-parity check (MiniLM-L6 int8 did not).
- **Cloud Run: --cpu=1 --memory=2Gi --concurrency=8.** 1Gi would run today at
  ~55% but leaves no headroom for the 100-200 skill catalog target plus
  concurrent turn buffers; the second GiB costs ~$7/mo at min-instances=1.
- **Earn-your-place admission** (classifier.py NEWCOMER_FLOOR_WHEN_ACTIVE=0.65):
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
- **Much larger local backbone** (bge-base 109M, gte-modernbert-base 149M).
- **More diverse synthetic data.** *(Done — the corpus grew to 6,399 across
  five targeted rounds; see header.)*
