"""Stage 1 of the two-stage selector: the retrieval bi-encoder.

## What it is for

The shipped multi-label classifier answers "which of these 39 runbooks" with one
row per runbook in a fixed head. That is optimal for 39 and structurally wrong for
200: every runbook needs its own training mass, a runbook added after the build gets
no row at all, and the head cannot say anything about a runbook it never saw.

This encoder replaces the "which runbooks are even in play" half of that question
with a cosine search. Runbooks are embedded ONCE at build time; a turn embeds only
the conversation and takes the nearest ``k``. The head that follows (stage 2,
``train_setwise.py``) then only ever reasons over ``k`` candidates, so its job is
the same difficulty at 39 runbooks and at 1,000.

Nothing here is a reranker in the sense the abandoned Architecture A used the
word (``EXPERIMENTS.md``): there is no cross-encoder, no per-(query, runbook)
forward pass. This stage produces an ordering and a shortlist, and stage 2 reads
cached vectors, not text.

## The objective

InfoNCE over cosine similarity against the WHOLE catalog — every runbook is
re-embedded each step, so the denominator is every document rather than a
sampled batch. Multi-positive rows (a turn that genuinely needs two runbooks)
are handled by summing the positive mass rather than picking one arbitrarily, so
a two-runbook turn is not trained to rank one of its own answers as a negative.

## Hard negatives, and what they can possibly mean here

A full-catalog denominator already contrasts against every runbook, so "add the
confusable one as a negative" is a no-op: it was in the denominator anyway. This
is not a hypothetical — the corpus carried a ``hard_negatives`` field on 2,042
rows, this module's docstring said it was used "as explicit negatives on top of
the in-batch ones", ``targets()`` said it applied "-inf masking" for them, and
NO LINE OF CODE EVER READ THE FIELD. It was inert from the day it was written,
and the two docstrings describing its effect were the only evidence it had one.

So the only thing a hard negative can do against this denominator is carry more
WEIGHT than an ordinary one, which is what ``--hard-negative-weight`` does: a
selected negative's term in the softmax denominator is multiplied by ``w``,
i.e. its logit is raised by ``log w``. Positives are untouched.

Which negatives get that weight comes from two places, and the split is
deliberate:

- **Authored** (``confusable_with`` in the curated corpus). A human's judgement,
  usually transcribed from a runbook's own "do not use for X" clause. It is
  stable across retrains and it is what the catalog says about itself.
- **Mined** (this module, every epoch). ANCE-style: re-embed the catalog with
  the CURRENT weights, take each row's top scorers that are not its answer. This
  is what the encoder gets wrong today, which no authored list can know.

Mining is filtered the way NV-Retriever's ``TopK-PercPos`` filters it — a
candidate is kept only where ``s(q, neg) <= (1 - margin) * s(q, best positive)``
— because the highest-scoring non-labelled document is very often a legitimate
alternative answer nobody labelled, and training the encoder to push it away is
teaching it something false. Re-mining every epoch is affordable for exactly the
reason the full denominator is: the catalog is tens of documents, not millions,
so the usual argument for mining once offline does not apply.

Rows with no expected runbook (the "no task" shapes) are dropped HERE and only
here: there is no positive to contrast against, and the question they answer —
"this turn needs nothing" — belongs to stage 2's cardinality head, which sees
them in full.
"""

from __future__ import annotations

import argparse
import json
import math
import random
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "src"))

from daintree_assistant_server.runbooks.catalog import (  # noqa: E402
    default_runbooks_dir,
    load_catalog,
)
from daintree_assistant_server.runbooks.local.documents import (  # noqa: E402
    INDEX_DOC_VARIANT,
    runbook_document,
)
from training.common import (  # noqa: E402
    build_query,
    canonical_active,
    corpus_snapshot,
    device,
    stable_split,
)

DATA = ROOT / "training" / "data"
MODELS = ROOT / "training" / "models"


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--epochs", type=int, default=6)
    ap.add_argument("--batch-size", type=int, default=48)
    ap.add_argument("--lr", type=float, default=3e-5)
    ap.add_argument("--val-fraction", type=float, default=0.12)
    ap.add_argument("--seed", type=int, default=41)
    ap.add_argument("--max-length", type=int, default=256)
    ap.add_argument("--doc-max-length", type=int, default=512)
    ap.add_argument("--temperature", type=float, default=0.05)
    ap.add_argument(
        "--hard-negatives",
        type=int,
        default=4,
        help="mined negatives per row, on top of the authored `confusable_with`. "
        "The literature settles around 2-8 alongside a full/in-batch denominator; "
        "past ~10 the extra ones stop being hard and start distorting the geometry.",
    )
    ap.add_argument(
        "--hard-negative-weight",
        type=float,
        default=4.0,
        help="softmax-denominator weight for a hard negative (1.0 disables the whole "
        "mechanism and returns this to a plain full-catalog InfoNCE).",
    )
    ap.add_argument(
        "--hard-negative-margin",
        type=float,
        default=0.05,
        help="NV-Retriever TopK-PercPos filter: a mined candidate is kept only if it "
        "scores at or below (1 - margin) x the row's best positive. Guards against "
        "mining an unlabelled true answer, which is the dominant failure of top-k mining.",
    )
    # all-MiniLM-L6-v2, chosen by measurement (training/sweep_retriever.py).
    # It matches bge-small-en-v1.5 exactly on both metrics that matter here
    # (r@32 1.000, r@8 0.990) at 68% of the parameters and HALF the serving
    # latency (2.23ms vs 4.46ms int8, single thread). bert-tiny is 9x cheaper
    # again but misses r@32 = 1.000, and a shortlist that drops the answer caps
    # the whole selector below where it already is.
    ap.add_argument("--base-model", default="sentence-transformers/all-MiniLM-L6-v2")
    ap.add_argument(
        "--holdout-runbooks",
        default="",
        help="comma-separated runbook ids to strip from TRAINING rows entirely. "
        "Paired with train_setwise's flag of the same name, this makes a runbook "
        "invisible to BOTH stages — the honest zero-shot probe, since a runbook "
        "held out of only stage 2 was still shortlisted by an encoder that knew it.",
    )
    ap.add_argument("--data", default=str(DATA / "selection.jsonl"))
    ap.add_argument(
        "--canonical-active",
        action="store_true",
        help="render every row's active set as the server would hold it "
        "(specifics paired with their domain foundation) — see canonical_active",
    )
    ap.add_argument(
        "--split",
        choices=("seed", "hash"),
        default="hash",
        help="`hash` keys each row's side of the split on its content (stable_split)",
    )
    ap.add_argument(
        "--grad-checkpoint",
        action="store_true",
        help="recompute activations in the backward pass. The whole catalog is re-embedded "
        "with gradients every step, so peak memory grows with the catalog (it passed ~42 GiB "
        "at 155 runbooks and failed on 32-36 GB machines). Same maths, same recipe; trades "
        "~30%% more step time for a fraction of the memory.",
    )
    ap.add_argument("--out", default=str(MODELS / "retriever"))
    args = ap.parse_args()

    # Validate before anything expensive. Each of these fails SILENTLY rather
    # than loudly: `--hard-negative-weight 0` gives log(0) handled as 0.0, which
    # is w=1 with the mining forward pass still paid; `--hard-negative-margin 2`
    # makes `(1-margin)` negative, and against a negative best-positive score the
    # ceiling lands at +inf, so every document becomes an "eligible" hard
    # negative including the answer. A run that trains to completion on a
    # meaningless objective is worse than one that refuses to start.
    if not math.isfinite(args.hard_negative_weight) or args.hard_negative_weight < 1.0:
        raise SystemExit("--hard-negative-weight must be a finite value >= 1.0 (1.0 disables)")
    if not math.isfinite(args.hard_negative_margin) or not 0.0 <= args.hard_negative_margin < 1.0:
        raise SystemExit("--hard-negative-margin must be in [0.0, 1.0)")
    if args.hard_negatives < 0:
        raise SystemExit("--hard-negatives must be >= 0")
    if not math.isfinite(args.temperature) or args.temperature <= 0:
        raise SystemExit("--temperature must be a finite value > 0")

    import torch
    from transformers import AutoModel, AutoTokenizer

    catalog = load_catalog(default_runbooks_dir())
    labels = sorted(catalog.selectable_ids)
    index = {sid: i for i, sid in enumerate(labels)}
    docs = [
        runbook_document(catalog.get(sid).meta, variant=INDEX_DOC_VARIANT) for sid in labels
    ]
    print(f"catalog: {len(labels)} selectable runbooks")

    rows, training_corpus = corpus_snapshot(Path(args.data))
    if args.canonical_active:
        rows = [
            {**r, "active_ids": canonical_active(r.get("active_ids") or [], catalog)}
            for r in rows
        ]
    labelled = [r for r in rows if [s for s in r["expect_ids"] if s in index]]
    print(f"corpus: {len(rows)} rows, {len(labelled)} with a selectable positive")

    random.seed(args.seed)
    torch.manual_seed(args.seed)
    if args.split == "hash":
        train_rows, val_rows = stable_split(labelled, args.val_fraction)
        random.Random(args.seed).shuffle(train_rows)
    else:
        random.Random(args.seed).shuffle(labelled)
        cut = int(len(labelled) * (1 - args.val_fraction))
        train_rows, val_rows = labelled[:cut], labelled[cut:]

    holdout = {sid.strip() for sid in args.holdout_runbooks.split(",") if sid.strip()}
    if holdout:
        unknown = holdout - set(labels)
        if unknown:
            raise SystemExit(f"--holdout-runbooks names unknown ids: {sorted(unknown)}")
        before = len(train_rows)
        # Three exclusions, not one. Dropping rows that ANSWER a held-out runbook
        # is the obvious half and was all this used to do — but the probe claims
        # the encoder has never seen the runbook, and two other paths fed it:
        #
        #   * a row whose ACTIVE SET names it. `build_query` renders that into the
        #     `[loaded: …]` prefix, so the held-out id's text reached the encoder
        #     as query input on rows that passed the expect-only filter (80 such
        #     rows for the three ids the 4/14 figure was measured with).
        #   * the loss DENOMINATOR. The catalog is re-embedded with gradients
        #     every step, so every held-out document was explicitly trained as a
        #     negative on every row. `mask_columns` below removes it.
        #
        # None of this affects an ordinary run: `holdout` is empty by default.
        train_rows = [
            r
            for r in train_rows
            if not (set(r["expect_ids"]) & holdout) and not (set(r["active_ids"] or ()) & holdout)
        ]
        print(
            f"zero-shot probe: held {len(holdout)} runbook(s) out of training — "
            f"{before - len(train_rows)} rows dropped, {len(train_rows)} remain; "
            f"held-out documents are masked out of the loss denominator"
        )

    dev = device()
    print(f"training device: {dev}")
    tok = AutoTokenizer.from_pretrained(args.base_model)
    model = AutoModel.from_pretrained(args.base_model).to(dev)
    if args.grad_checkpoint:
        model.gradient_checkpointing_enable(gradient_checkpointing_kwargs={"use_reentrant": False})

    def encode(texts: list[str], max_length: int) -> tuple[torch.Tensor, torch.Tensor]:
        enc = tok(
            texts,
            padding=True,
            truncation=True,
            max_length=max_length,
            return_tensors="pt",
        )
        return enc["input_ids"].to(dev), enc["attention_mask"].to(dev)

    def embed(ids: torch.Tensor, mask: torch.Tensor) -> torch.Tensor:
        hidden = model(input_ids=ids, attention_mask=mask)[0]
        m = mask.unsqueeze(-1).to(hidden.dtype)
        pooled = (hidden * m).sum(1) / m.sum(1).clamp(min=1e-9)
        return torch.nn.functional.normalize(pooled, dim=-1)

    # The whole catalog is re-embedded every step. That is affordable because
    # the catalog is small and it keeps the runbook vectors exactly in step with
    # the encoder being trained — the alternative (a periodically refreshed
    # cache) trains the query side against stale documents, which is the same
    # class of silent train/serve drift ``documents.py`` exists to prevent.
    doc_ids, doc_mask = encode(docs, args.doc_max_length)

    opt = torch.optim.AdamW(model.parameters(), lr=args.lr)
    steps = args.epochs * ((len(train_rows) + args.batch_size - 1) // args.batch_size)
    sched = torch.optim.lr_scheduler.OneCycleLR(
        opt, max_lr=args.lr, total_steps=steps, pct_start=0.1
    )

    def targets(subset: list[dict]) -> torch.Tensor:
        """Multi-hot positives as a (batch, n_runbooks) matrix of 1.0 / 0.0."""
        y = torch.zeros(len(subset), len(labels), device=dev)
        for i, row in enumerate(subset):
            for sid in row["expect_ids"]:
                if sid in index:
                    y[i, index[sid]] = 1.0
        return y

    # Every way a held-out runbook could still reach the encoder, closed in one
    # place. Being a mined negative, an AUTHORED negative, or an ordinary
    # denominator term are all training signal about the document, and the probe
    # is a claim that there is none.
    holdout_cols = [index[sid] for sid in holdout]
    minable = torch.ones(len(labels), device=dev)
    for col in holdout_cols:
        minable[col] = 0.0

    def mask_columns(sim: torch.Tensor) -> torch.Tensor:
        """Remove held-out documents from a score matrix, before any loss reads it."""
        if not holdout_cols:
            return sim
        return sim.index_fill(
            1,
            torch.tensor(holdout_cols, device=sim.device),
            torch.finfo(sim.dtype).min,
        )

    def mine(subset: list[dict]) -> list[list[int]]:
        """Per-row hard negatives: authored ∪ mined-from-the-current-encoder."""
        authored = [
            [
                index[c]
                for c in row.get("confusable_with", [])
                # A held-out runbook is skipped here as well as in mining. An
                # authored negative bypasses `minable` entirely, so without this
                # the corpus's own confusability notes would hand the probe 274
                # explicit, UPWEIGHTED negatives naming the runbooks it is
                # supposed to know nothing about.
                if c in index and c not in holdout
            ]
            for row in subset
        ]
        if args.hard_negatives <= 0 or args.hard_negative_weight == 1.0:
            return authored

        model.eval()
        mined: list[list[int]] = []
        with torch.no_grad():
            d = embed(doc_ids, doc_mask)
            for start in range(0, len(subset), 256):
                chunk = subset[start : start + 256]
                q_ids, q_mask = encode([build_query(r) for r in chunk], args.max_length)
                # Raw cosine, NOT the temperature-scaled logit: the margin rule is
                # defined as a ratio of similarities, and dividing both sides by
                # the same temperature would leave it comparing scaled scores to
                # an unscaled threshold.
                sim = embed(q_ids, q_mask) @ d.T
                y = targets(chunk)
                best_pos = sim.masked_fill(y == 0, float("-inf")).max(dim=1).values
                # NV-Retriever's TopK-PercPos is written `(1 - margin) * s_pos`,
                # which assumes a NON-NEGATIVE positive score. Cosine is signed:
                # at s_pos = -0.20 that formula puts the ceiling at -0.19, ABOVE
                # the positive, so the filter inverts and admits documents
                # outscoring the answer. Subtracting a margin proportional to the
                # magnitude is identical wherever s_pos >= 0 and still moves the
                # ceiling downwards when it is not.
                ceiling = best_pos - args.hard_negative_margin * best_pos.abs()
                eligible = (y == 0) & (minable > 0) & (sim <= ceiling.unsqueeze(1))
                scored = sim.masked_fill(~eligible, float("-inf"))
                k = min(args.hard_negatives, len(labels))
                top = scored.topk(k, dim=1)
                for i in range(len(chunk)):
                    ranked = zip(
                        top.indices[i].tolist(), top.values[i].tolist(), strict=True
                    )
                    mined.append([int(j) for j, s in ranked if s > float("-inf")])
        model.train()
        # A row with no positive in the index has no ceiling to measure against;
        # `best_pos` is -inf there and nothing is eligible, which is correct.
        return [sorted(set(a) | set(m)) for a, m in zip(authored, mined, strict=True)]

    log_weight = math.log(args.hard_negative_weight) if args.hard_negative_weight > 0 else 0.0

    for epoch in range(1, args.epochs + 1):
        model.train()
        random.shuffle(train_rows)
        # Re-mined every epoch, against the weights as they now are. The whole
        # point of ANCE-style mining is that the negatives get harder as the
        # model gets better; mining once up front pins them to a checkpoint the
        # model leaves behind after the first epoch.
        negatives = mine(train_rows)
        mined_total = sum(len(n) for n in negatives)
        total = 0.0
        batches = 0
        for start in range(0, len(train_rows), args.batch_size):
            batch = train_rows[start : start + args.batch_size]
            batch_negs = negatives[start : start + args.batch_size]
            q_ids, q_mask = encode([build_query(r) for r in batch], args.max_length)
            opt.zero_grad()
            q = embed(q_ids, q_mask)
            d = embed(doc_ids, doc_mask)
            sim = mask_columns(q @ d.T / args.temperature)
            y = targets(batch)
            # Upweight the hard negatives in the DENOMINATOR only. Adding log(w)
            # to a logit multiplies its exp term by w, so this is a weighted
            # softmax and nothing more exotic; positives never receive it, since
            # `mine` excludes them.
            if log_weight:
                bump = torch.zeros_like(sim)
                for i, negs in enumerate(batch_negs):
                    for j in negs:
                        bump[i, j] = log_weight
                sim = sim + bump
            # Multi-positive InfoNCE: -log( sum_pos exp(s) / sum_all exp(s) ).
            # Summing the positive mass rather than looping one-positive-per-row
            # means a two-runbook turn is never asked to rank its own second answer
            # as a negative, which is what a naive cross-entropy would do.
            logsumexp_all = torch.logsumexp(sim, dim=1)
            neg_inf = torch.finfo(sim.dtype).min
            logsumexp_pos = torch.logsumexp(sim.masked_fill(y == 0, neg_inf), dim=1)
            loss = (logsumexp_all - logsumexp_pos).mean()
            loss.backward()
            opt.step()
            sched.step()
            total += float(loss.detach())
            batches += 1

        model.eval()
        with torch.no_grad():
            d = embed(doc_ids, doc_mask)
            hits = {1: 0, 3: 0, 8: 0, 16: 0, 32: 0}
            for start in range(0, len(val_rows), 128):
                batch = val_rows[start : start + 128]
                q_ids, q_mask = encode([build_query(r) for r in batch], args.max_length)
                sim = embed(q_ids, q_mask) @ d.T
                order = torch.argsort(-sim, dim=1).cpu()
                for i, row in enumerate(batch):
                    want = {index[s] for s in row["expect_ids"] if s in index}
                    ranked = order[i].tolist()
                    for k in hits:
                        if want <= set(ranked[:k]):
                            hits[k] += 1
        recalls = " ".join(f"r@{k} {v / len(val_rows):.3f}" for k, v in hits.items())
        per_row = mined_total / max(len(train_rows), 1)
        print(
            f"  epoch {epoch}: loss {total / max(batches, 1):.4f}  {recalls}  "
            f"hard-neg/row {per_row:.2f}"
        )

    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    model.save_pretrained(out)
    tok.save_pretrained(out)
    (out / "meta.json").write_text(
        json.dumps(
            {
                "base_model": args.base_model,
                "training_corpus": training_corpus,
                "max_length": args.max_length,
                "doc_max_length": args.doc_max_length,
                "hidden_size": model.config.hidden_size,
                "doc_variant": INDEX_DOC_VARIANT,
                "holdout_runbooks": sorted(holdout),
                "hard_negatives": args.hard_negatives,
                "hard_negative_weight": args.hard_negative_weight,
                "hard_negative_margin": args.hard_negative_margin,
            },
            indent=2,
        )
    )
    print(f"\nsaved retriever to {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
