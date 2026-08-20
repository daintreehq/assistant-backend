"""Stage 1 of the two-stage selector: the retrieval bi-encoder.

## What it is for

The shipped multi-label classifier answers "which of these 39 skills" with one
row per skill in a fixed head. That is optimal for 39 and structurally wrong for
200: every skill needs its own training mass, a skill added after the build gets
no row at all, and the head cannot say anything about a skill it never saw.

This encoder replaces the "which skills are even in play" half of that question
with a cosine search. Skills are embedded ONCE at build time; a turn embeds only
the conversation and takes the nearest ``k``. The head that follows (stage 2,
``train_setwise.py``) then only ever reasons over ``k`` candidates, so its job is
the same difficulty at 39 skills and at 1,000.

Nothing here is a reranker in the sense the abandoned Architecture A used the
word (``EXPERIMENTS.md``): there is no cross-encoder, no per-(query, skill)
forward pass. This stage produces an ordering and a shortlist, and stage 2 reads
cached vectors, not text.

## The objective

InfoNCE over cosine similarity, with the corpus's mined ``hard_negatives`` as
explicit negatives on top of the in-batch ones. Multi-positive rows (a turn that
genuinely needs two skills) are handled by summing the positive mass rather than
picking one arbitrarily, so a two-skill turn is not trained to rank one of its
own answers as a negative.

Rows with no expected skill (the "no task" shapes) are dropped HERE and only
here: there is no positive to contrast against, and the question they answer —
"this turn needs nothing" — belongs to stage 2's cardinality head, which sees
them in full.
"""

from __future__ import annotations

import argparse
import json
import random
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "src"))

from daintree_assistant_server.skills.catalog import (  # noqa: E402
    default_skills_dir,
    load_catalog,
)
from daintree_assistant_server.skills.local.documents import (  # noqa: E402
    INDEX_DOC_VARIANT,
    skill_document,
)
from training.common import build_query, corpus_rows, device  # noqa: E402

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
    ap.add_argument("--doc-max-length", type=int, default=384)
    ap.add_argument("--temperature", type=float, default=0.05)
    # all-MiniLM-L6-v2, chosen by measurement (training/sweep_retriever.py).
    # It matches bge-small-en-v1.5 exactly on both metrics that matter here
    # (r@32 1.000, r@8 0.990) at 68% of the parameters and HALF the serving
    # latency (2.23ms vs 4.46ms int8, single thread). bert-tiny is 9x cheaper
    # again but misses r@32 = 1.000, and a shortlist that drops the answer caps
    # the whole selector below where it already is.
    ap.add_argument("--base-model", default="sentence-transformers/all-MiniLM-L6-v2")
    ap.add_argument(
        "--holdout-skills",
        default="",
        help="comma-separated skill ids to strip from TRAINING rows entirely. "
        "Paired with train_setwise's flag of the same name, this makes a skill "
        "invisible to BOTH stages — the honest zero-shot probe, since a skill "
        "held out of only stage 2 was still shortlisted by an encoder that knew it.",
    )
    ap.add_argument("--out", default=str(MODELS / "retriever"))
    args = ap.parse_args()

    import torch
    from transformers import AutoModel, AutoTokenizer

    catalog = load_catalog(default_skills_dir())
    labels = sorted(catalog.selectable_ids)
    index = {sid: i for i, sid in enumerate(labels)}
    docs = [
        skill_document(catalog.get(sid).meta, variant=INDEX_DOC_VARIANT) for sid in labels
    ]
    print(f"catalog: {len(labels)} selectable skills")

    rows = corpus_rows(DATA / "selection.jsonl")
    labelled = [r for r in rows if [s for s in r["expect_ids"] if s in index]]
    print(f"corpus: {len(rows)} rows, {len(labelled)} with a selectable positive")

    random.seed(args.seed)
    torch.manual_seed(args.seed)
    random.Random(args.seed).shuffle(labelled)
    cut = int(len(labelled) * (1 - args.val_fraction))
    train_rows, val_rows = labelled[:cut], labelled[cut:]

    holdout = {sid.strip() for sid in args.holdout_skills.split(",") if sid.strip()}
    if holdout:
        unknown = holdout - set(labels)
        if unknown:
            raise SystemExit(f"--holdout-skills names unknown ids: {sorted(unknown)}")
        before = len(train_rows)
        train_rows = [r for r in train_rows if not (set(r["expect_ids"]) & holdout)]
        print(
            f"zero-shot probe: held {len(holdout)} skill(s) out of training — "
            f"{before - len(train_rows)} rows dropped, {len(train_rows)} remain"
        )

    dev = device()
    print(f"training device: {dev}")
    tok = AutoTokenizer.from_pretrained(args.base_model)
    model = AutoModel.from_pretrained(args.base_model).to(dev)

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
    # the catalog is small and it keeps the skill vectors exactly in step with
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
        """Multi-hot positives, plus -inf masking for mined hard negatives.

        Returned as a (batch, n_skills) float matrix of 1.0 / 0.0. Hard
        negatives need no special treatment in the loss — every non-positive
        skill is already a negative, and the corpus's mined ones simply carry
        more weight by being semantically close.
        """
        y = torch.zeros(len(subset), len(labels), device=dev)
        for i, row in enumerate(subset):
            for sid in row["expect_ids"]:
                if sid in index:
                    y[i, index[sid]] = 1.0
        return y

    for epoch in range(1, args.epochs + 1):
        model.train()
        random.shuffle(train_rows)
        total = 0.0
        batches = 0
        for start in range(0, len(train_rows), args.batch_size):
            batch = train_rows[start : start + args.batch_size]
            q_ids, q_mask = encode([build_query(r) for r in batch], args.max_length)
            opt.zero_grad()
            q = embed(q_ids, q_mask)
            d = embed(doc_ids, doc_mask)
            sim = q @ d.T / args.temperature
            y = targets(batch)
            # Multi-positive InfoNCE: -log( sum_pos exp(s) / sum_all exp(s) ).
            # Summing the positive mass rather than looping one-positive-per-row
            # means a two-skill turn is never asked to rank its own second answer
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
        print(f"  epoch {epoch}: loss {total / max(batches, 1):.4f}  {recalls}")

    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    model.save_pretrained(out)
    tok.save_pretrained(out)
    (out / "meta.json").write_text(
        json.dumps(
            {
                "base_model": args.base_model,
                "max_length": args.max_length,
                "doc_max_length": args.doc_max_length,
                "hidden_size": model.config.hidden_size,
                "doc_variant": INDEX_DOC_VARIANT,
                "holdout_skills": sorted(holdout),
            },
            indent=2,
        )
    )
    print(f"\nsaved retriever to {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
