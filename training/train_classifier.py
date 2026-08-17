"""Multi-label classifier over the catalog — one forward pass, no reranking.

## Why this exists

The two-stage retrieve-then-rerank design was measured and the second stage does
not earn its place. On the identical shortlist, over the 136 benchmark cases that
require at least one skill:

    bi-encoder ordering   recall@1 = 0.779
    cross-encoder ordering recall@1 = 0.699   (net -11 cases at rank 1)

The cross-encoder pushes the correct answer OFF rank 1 in 20 cases and pulls it
on in 9. And its "empty answer" sentinels turned out inert — gating on them
scored 111/150 at every threshold from 0.3 to 0.7.

Three separate measurements pointing the same way: the residual errors are not
ranking errors, they are CARDINALITY errors — return zero skills, or return two.
Those are properties of the QUERY, not of a (query, document) pair, so a
cross-encoder is structurally the wrong instrument. A pairwise scorer is being
asked a question that has no second argument.

So: score all 32 skills at once with a multi-label head. One sigmoid per skill,
trained with binary cross-entropy over the whole label vector, which makes the
model's job exactly the task's job — "which of these, possibly none, possibly
two". Empty falls out when every logit is low; multi falls out when two are high.
No shortlist, no sentinel documents, no admission heuristic.

## What it costs

Zero-shot generalisation to skills that were not in training. The catalog is
fixed per deploy and retraining is a build step (~2 minutes on MPS), so this is
affordable — but it is a real change: a skill added at runtime by skill learning
gets no head until the next build. Serving guards this with the routing_revision
provenance stamp: a stale head degrades open instead of serving blind.
"""

from __future__ import annotations

import argparse
import json
import random
import sys
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "src"))

from daintree_assistant_server.skills.catalog import (  # noqa: E402
    default_skills_dir,
    load_catalog,
)
from training.common import render_training_query  # noqa: E402

DATA = ROOT / "training" / "data"
MODELS = ROOT / "training" / "models"

#: Appended to the query so the classifier can see conversation STATE.
#:
#: Same reasoning as the reranker's ``[currently loaded]`` marker: continuation
#: and topic-shift turns are near-identical in wording and differ only in which
#: skills are open. Here it goes in the query, because there is no per-candidate
#: document to attach it to.
ACTIVE_PREFIX = "[loaded: {}] "


def _device() -> str:
    try:
        import torch

        if torch.backends.mps.is_available():
            return "mps"
    except Exception:  # noqa: BLE001
        pass
    return "cpu"


def build_query(row: dict) -> str:
    query = render_training_query(row["query"], row.get("history"))
    active = row.get("active_ids") or []
    if active:
        return ACTIVE_PREFIX.format(", ".join(active)) + query
    return query


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--epochs", type=int, default=8)
    ap.add_argument("--batch-size", type=int, default=32)
    ap.add_argument("--lr", type=float, default=3e-5)
    ap.add_argument("--val-fraction", type=float, default=0.12)
    ap.add_argument("--seed", type=int, default=17)
    ap.add_argument("--max-length", type=int, default=256)
    ap.add_argument("--base-model", default="BAAI/bge-base-en-v1.5")
    # 0.0 = no balancing (natural frequencies), 1.0 = full inverse frequency.
    ap.add_argument("--balance-power", type=float, default=0.5)
    ap.add_argument("--out", default=str(MODELS / "classifier"))
    args = ap.parse_args()

    import torch
    from torch.utils.data import DataLoader, TensorDataset
    from transformers import AutoModel, AutoTokenizer

    catalog = load_catalog(default_skills_dir())
    labels = sorted(catalog.selectable_ids)
    # One extra output for the documentation decision — an independent binary
    # question about the same query, so it rides the same encoder rather than
    # paying for a second forward pass.
    # Layout: [32 skill logits | 1 docs logit | 4 cardinality logits].
    #
    # The cardinality head is separate because a single per-skill threshold
    # conflates two different questions — "is this skill relevant" and "how many
    # skills does this turn need" — and the benchmark errors are dominated by the
    # second. A threshold sweep can trade misses against extras but can never
    # answer "this turn needs exactly zero" and "this turn needs exactly two" at
    # the same operating point, which is precisely what the no-task and
    # multi-skill categories require.
    CARD_CLASSES = 4
    n_out = len(labels) + 1 + CARD_CLASSES
    docs_idx = len(labels)
    card_slice = slice(len(labels) + 1, len(labels) + 1 + CARD_CLASSES)
    index = {sid: i for i, sid in enumerate(labels)}
    print(f"labels: {len(labels)} skills + 1 docs head = {n_out} outputs")

    rows = [
        json.loads(line)
        for line in (DATA / "selection.jsonl").read_text().splitlines()
        if line.strip()
    ]
    # Seed EVERY stochastic source, not just the row shuffle — head init,
    # dropout and the weighted sampler all draw from torch's RNG, and an
    # unseeded one meant "--seed 41" did not reproduce seed 41.
    random.seed(args.seed)
    torch.manual_seed(args.seed)
    random.Random(args.seed).shuffle(rows)
    cut = int(len(rows) * (1 - args.val_fraction))

    dev = _device()
    print(f"training device: {dev}")
    tok = AutoTokenizer.from_pretrained(args.base_model)
    backbone = AutoModel.from_pretrained(args.base_model)

    class Model(torch.nn.Module):
        def __init__(self) -> None:
            super().__init__()
            self.backbone = backbone
            self.drop = torch.nn.Dropout(0.1)
            self.head = torch.nn.Linear(backbone.config.hidden_size, n_out)

        def forward(self, ids: torch.Tensor, mask: torch.Tensor) -> torch.Tensor:
            hidden = self.backbone(input_ids=ids, attention_mask=mask)[0]
            m = mask.unsqueeze(-1).to(hidden.dtype)
            pooled = (hidden * m).sum(1) / m.sum(1).clamp(min=1e-9)
            return self.head(self.drop(pooled))

    model = Model().to(dev)

    def encode(subset: list[dict]) -> tuple[torch.Tensor, ...]:
        texts = [build_query(r) for r in subset]
        enc = tok(texts, padding=True, truncation=True, max_length=args.max_length,
                  return_tensors="pt")
        y = torch.zeros(len(subset), n_out)
        card = torch.zeros(len(subset), dtype=torch.long)
        for i, r in enumerate(subset):
            known = [sid for sid in r["expect_ids"] if sid in index]
            for sid in known:
                y[i, index[sid]] = 1.0
            if r.get("is_docs"):
                y[i, docs_idx] = 1.0
            card[i] = min(len(known), CARD_CLASSES - 1)
        return enc["input_ids"], enc["attention_mask"], y, card

    tr_ids, tr_mask, tr_y, tr_card = encode(rows[:cut])
    va_ids, va_mask, va_y, va_card = encode(rows[cut:])
    print("cardinality distribution:",
          {int(k): int(v) for k, v in zip(*torch.unique(tr_card, return_counts=True), strict=True)})
    # Sample by SOURCE, not uniformly.
    #
    # The corpus is unbalanced in a way that showed up directly as benchmark
    # failures: 272 follow-up examples (where keeping the loaded skill is right)
    # against 88 topic shifts (where dropping it is right). The model learned the
    # majority rule — on topic-shift benchmark cases it ranked the still-loaded
    # skill first at 0.98 confidence. Same story for no-task, which was almost
    # entirely bare acknowledgements with no conversation history, so "empty
    # while work is loaded" was never taught.
    #
    # Weighting by inverse source frequency makes each SHAPE of turn contribute
    # about equally per epoch, without discarding any data.
    from collections import Counter

    from torch.utils.data import WeightedRandomSampler

    sources = [r.get("source", "?") for r in rows[:cut]]
    freq = Counter(sources)
    # SQRT-inverse frequency, not full inverse.
    #
    # Full inverse balancing was measured and overcorrects: it lifted topic-shift
    # but dropped routing-trap from 52/58 to 46/58, because the boundary-negative
    # examples that teach routing are the largest source and full balancing
    # starves them. sqrt keeps the rare shapes well represented while leaving the
    # majority shape a majority — the usual compromise, and here the two error
    # modes are visible enough in the per-category table to confirm it.
    weights = torch.tensor(
        [1.0 / (freq[s] ** args.balance_power) for s in sources], dtype=torch.double
    )
    sampler = WeightedRandomSampler(weights, num_samples=len(sources), replacement=True)
    print("source mix:", dict(freq.most_common()))
    loader = DataLoader(TensorDataset(tr_ids, tr_mask, tr_y, tr_card),
                        batch_size=args.batch_size, sampler=sampler)

    # pos_weight per label: with 32 labels and ~1 positive per example, every
    # column is ~97% negative, and unweighted BCE answers that by predicting
    # zero everywhere. Computed per label rather than as one global constant
    # because the catalog is not uniformly represented (46 to 159 examples).
    pos = tr_y[:, : len(labels) + 1].sum(0).clamp(min=1.0)
    pos_weight = ((len(tr_y) - pos) / pos).clamp(max=50.0).to(dev)
    lossf = torch.nn.BCEWithLogitsLoss(pos_weight=pos_weight)
    # Class weights for cardinality: "two skills" is 153 of 2888 examples and
    # "three" only 13, so an unweighted head would simply never predict them.
    card_counts = torch.bincount(tr_card, minlength=CARD_CLASSES).float().clamp(min=1.0)
    card_weight = (len(tr_card) / (CARD_CLASSES * card_counts)).to(dev)
    cardf = torch.nn.CrossEntropyLoss(weight=card_weight)
    opt = torch.optim.AdamW(model.parameters(), lr=args.lr)
    sched = torch.optim.lr_scheduler.OneCycleLR(
        opt, max_lr=args.lr, total_steps=args.epochs * len(loader), pct_start=0.1
    )

    for epoch in range(1, args.epochs + 1):
        model.train()
        total = 0.0
        for ids, mask, y, card in loader:
            ids, mask, y, card = ids.to(dev), mask.to(dev), y.to(dev), card.to(dev)
            opt.zero_grad()
            logits = model(ids, mask)
            loss = lossf(logits[:, : len(labels) + 1], y[:, : len(labels) + 1])
            loss = loss + cardf(logits[:, card_slice], card)
            loss.backward()
            opt.step()
            sched.step()
            total += float(loss.detach())
        model.eval()
        with torch.no_grad():
            logits = model(va_ids.to(dev), va_mask.to(dev)).cpu()
        probs = torch.sigmoid(logits[:, : len(labels) + 1]).numpy()
        card_pred = logits[:, card_slice].argmax(1)
        card_acc = float((card_pred == va_card).float().mean())
        exact = _exact_match_card(probs[:, :len(labels)], va_y[:, :len(labels)].numpy(),
                                  card_pred.numpy())
        print(f"  epoch {epoch}: train_loss {total / len(loader):.4f}  "
              f"card_acc {card_acc:.4f}  val exact-match {exact:.4f}")

    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    torch.save(model.state_dict(), out / "model.pt")
    (out / "labels.json").write_text(json.dumps(labels, indent=2))
    from daintree_assistant_server.skills.local.documents import routing_revision

    (out / "meta.json").write_text(json.dumps(
        {"labels": len(labels), "docs_index": docs_idx,
         "cardinality_classes": CARD_CLASSES, "n_out": n_out,
         "base_model": args.base_model,
         # Stamped at TRAIN time from the catalog the labels were built against.
         # The export copies this verbatim — recomputing it there would bless
         # stale weights with whatever catalog happens to be live at export.
         "routing_revision": routing_revision(catalog),
         "max_length": args.max_length}, indent=2))
    tok.save_pretrained(out)
    print(f"\nsaved classifier to {out}")

    print("\nheld-out synthetic (cardinality head decides how many):")
    final_exact = _exact_match_card(
        probs[:, : len(labels)], va_y[:, : len(labels)].numpy(), card_pred.numpy()
    )
    print(f"  exact-match {final_exact:.4f}")
    print("\nheld-out synthetic, threshold rule for comparison:")
    print(f"  {'thresh':>7} {'exact':>8} {'empty-acc':>10}")
    y_np = va_y[:, :len(labels)].numpy()
    for t in (0.2, 0.3, 0.4, 0.5, 0.6, 0.7):
        exact = _exact_match(probs[:, :len(labels)], y_np, t)
        want_empty = y_np.sum(1) == 0
        got_empty = (probs[:, :len(labels)] >= t).sum(1) == 0
        empty_acc = float((want_empty == got_empty).mean())
        print(f"  {t:>7.1f} {exact:>8.4f} {empty_acc:>10.4f}")
    return 0


def _exact_match_card(probs: np.ndarray, y: np.ndarray, card: np.ndarray) -> float:
    """Exact-match when a predicted COUNT decides how many to take."""
    hits = 0
    for p, t, n in zip(probs, y, card, strict=True):
        picked = {int(i) for i in np.argsort(-p)[: int(n)]}
        hits += picked == {int(i) for i in np.nonzero(t)[0]}
    return hits / len(y)


def _exact_match(probs: np.ndarray, y: np.ndarray, threshold: float, cap: int = 3) -> float:
    """Exact-match rate under the serving rule: every label over threshold, capped."""
    hits = 0
    for p, t in zip(probs, y, strict=True):
        order = np.argsort(-p)
        picked = {int(i) for i in order[:cap] if p[i] >= threshold}
        want = {int(i) for i in np.nonzero(t)[0]}
        hits += picked == want
    return hits / len(y)


if __name__ == "__main__":
    raise SystemExit(main())
