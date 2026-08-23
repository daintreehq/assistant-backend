"""Stage 2 of the two-stage selector: the set-wise head over a retrieved shortlist.

Stage 1 (``train_retriever.py``) is frozen before this runs. It supplies, per
training row, the ``k`` nearest runbooks by cosine and their scores; this stage
learns to answer the real question over that shortlist — which of these, possibly
none, possibly two — plus the docs and cardinality decisions.

## What is different from the classifier this replaces

The shipped classifier owns a weight ROW per runbook. This head owns none: it
consumes a runbook's *embedding*, produced by the same encoder that embeds the
conversation. Two consequences, and they are the reason for the whole exercise:

1. **A runbook added after training can still be selected.** Embed its document
   with the shipped encoder, append the row to the table, and it is a candidate.
   No retrain to unblock it, no ``routing_revision`` staleness gate, no
   degrade-open window while a rebuild happens. It does NOT arrive on equal
   terms — measured against a both-stages holdout it takes 4 of 14 of its own
   benchmark cases against 13 of 14 trained, winning only requests no neighbour
   also claims. What a retrain buys is competitiveness, not possibility.
2. **Catalog size stops driving head size.** The head always sees ``k``
   candidates. At 39 runbooks and at 1,000 it is the same computation and the same
   difficulty; only stage 1's job gets harder, and cosine search over 1,000
   vectors is microseconds.

The cost is capacity: a text-derived runbook vector cannot memorise as much as a
freely-learned row (measured directly as B5 in ``EXPERIMENTS.md``, where merely
INITIALISING the old head from runbook embeddings hurt). The shared projection
``cand_proj`` is what buys some of it back — it is free to learn how to sharpen
text embeddings into discriminative ones, and being shared across runbooks it
applies to runbooks it has never seen.

## The ordering question

``--ordering`` selects how the shortlist's order is treated, so the choice is
measured rather than assumed:

- ``equivariant`` (default) — no positional term, retrieval evidence supplied as
  per-candidate FEATURES (cosine and normalised rank). Candidates are shuffled
  per batch, which is a mathematical NO-OP here and is meant as one: with no
  positional term the head cannot see order, so the shuffle is a standing guard
  that nothing has quietly introduced one. ``eval_two_stage --ablate`` is the
  same guard against the exported graph, where it must report 0 cases changed.
- ``positional`` — candidates in cosine order with a learned rank embedding. The
  head may use "rank 0 is usually right" as a prior, which helps when stage 1 is
  reliable and is exactly what breaks when it is not.
- ``blind`` — equivariant with the retrieval features zeroed. An ablation: how
  much of the answer is stage 1's ordering doing?
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

from daintree_assistant_server.runbooks.catalog import (  # noqa: E402
    default_runbooks_dir,
    load_catalog,
)
from daintree_assistant_server.runbooks.local.candidates import (  # noqa: E402
    assemble_candidates,
)
from daintree_assistant_server.runbooks.local.documents import (  # noqa: E402
    INDEX_DOC_VARIANT,
    routing_revision,
    runbook_document,
)
from training.common import build_query, corpus_rows, device  # noqa: E402
from training.setwise_model import CARD_CLASSES, SetwiseHead  # noqa: E402

DATA = ROOT / "training" / "data"
MODELS = ROOT / "training" / "models"

ORDERINGS = ("equivariant", "positional", "blind")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--epochs", type=int, default=10)
    ap.add_argument("--batch-size", type=int, default=32)
    ap.add_argument("--lr", type=float, default=5e-5)
    ap.add_argument("--head-lr", type=float, default=3e-4)
    ap.add_argument("--val-fraction", type=float, default=0.12)
    ap.add_argument("--seed", type=int, default=41)
    ap.add_argument("--max-length", type=int, default=256)
    ap.add_argument("--doc-max-length", type=int, default=384)
    ap.add_argument("--candidates", type=int, default=32)
    ap.add_argument("--base-model", default="BAAI/bge-base-en-v1.5")
    ap.add_argument("--retriever", default=str(MODELS / "retriever"))
    ap.add_argument("--ordering", choices=ORDERINGS, default="equivariant")
    ap.add_argument("--layers", type=int, default=2)
    ap.add_argument("--d-model", type=int, default=256)
    ap.add_argument("--balance-power", type=float, default=0.0)
    ap.add_argument(
        "--doc-refresh",
        type=int,
        default=20,
        help="re-embed the runbook documents every N steps, without gradients "
        "(0 = re-embed every step WITH gradients, ~6x slower). See below.",
    )
    ap.add_argument(
        "--holdout-runbooks",
        default="",
        help="comma-separated runbook ids to strip from TRAINING rows entirely — "
        "the zero-shot probe: can the head route a runbook it never trained on?",
    )
    ap.add_argument("--out", default=str(MODELS / "setwise"))
    args = ap.parse_args()

    import torch
    from torch.utils.data import WeightedRandomSampler
    from transformers import AutoModel, AutoTokenizer

    dev = device()
    catalog = load_catalog(default_runbooks_dir())
    labels = sorted(catalog.selectable_ids)
    index = {sid: i for i, sid in enumerate(labels)}
    docs = [
        runbook_document(catalog.get(sid).meta, variant=INDEX_DOC_VARIANT) for sid in labels
    ]
    n_runbooks = len(labels)
    k = min(args.candidates, n_runbooks)
    print(f"catalog: {n_runbooks} selectable runbooks, shortlist k={k}, device {dev}")

    random.seed(args.seed)
    torch.manual_seed(args.seed)
    np.random.seed(args.seed)

    rows = corpus_rows(DATA / "selection.jsonl")
    random.Random(args.seed).shuffle(rows)
    cut = int(len(rows) * (1 - args.val_fraction))
    train_rows, val_rows = rows[:cut], rows[cut:]

    holdout = {s.strip() for s in args.holdout_runbooks.split(",") if s.strip()}
    if holdout:
        unknown = holdout - set(labels)
        if unknown:
            raise SystemExit(f"--holdout-runbooks names unknown ids: {sorted(unknown)}")
        before = len(train_rows)
        train_rows = [
            r for r in train_rows if not (set(r["expect_ids"]) & holdout)
        ]
        print(
            f"zero-shot probe: held {len(holdout)} runbook(s) out of training — "
            f"{before - len(train_rows)} rows dropped, {len(train_rows)} remain"
        )

    # ---------------------------------------------------------------- stage 1
    # Frozen. Its only job here is to produce each row's shortlist ONCE; nothing
    # downstream can change it, so it is computed up front rather than in the
    # training loop.
    print(f"embedding corpus with frozen retriever ({args.retriever}) …")
    r_meta = json.loads((Path(args.retriever) / "meta.json").read_text())

    def stage1_shortlists() -> tuple[np.ndarray, ...]:
        """Every row's shortlist, computed once. Stage 1 is frozen.

        Scoped as a function so the retriever's weights are unreachable — and
        therefore collectable — the moment it returns. Stage 2's backbone is
        three times larger and about to go on the same accelerator.
        """
        r_tok = AutoTokenizer.from_pretrained(args.retriever)
        r_model = AutoModel.from_pretrained(args.retriever).to(dev).eval()

        def r_embed(texts: list[str], max_length: int) -> np.ndarray:
            out = []
            with torch.no_grad():
                for start in range(0, len(texts), 128):
                    enc = r_tok(
                        texts[start : start + 128],
                        padding=True,
                        truncation=True,
                        max_length=max_length,
                        return_tensors="pt",
                    ).to(dev)
                    hidden = r_model(**enc)[0]
                    m = enc["attention_mask"].unsqueeze(-1).to(hidden.dtype)
                    pooled = (hidden * m).sum(1) / m.sum(1).clamp(min=1e-9)
                    out.append(
                        torch.nn.functional.normalize(pooled, dim=-1)
                        .cpu()
                        .float()
                        .numpy()
                    )
            return np.concatenate(out)

        doc_vecs = r_embed(docs, r_meta["doc_max_length"])

        def shortlist(subset: list[dict]) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
            """Rows' candidate ballots, built by the SERVING assembler.

            Not a top-k slice. ``assemble_candidates`` is the shipped policy —
            it seats an active runbook the cosine shortlist missed — and if
            training slices instead, the head learns on a ballot the server
            never builds. Every row's active set is right here in the corpus, so
            there is no reason for the two to differ.

            ``rank_norm`` comes back per row as well, because a reserved
            candidate carries 1.0 rather than its slot index.
            """
            out_idx, out_cos, out_rank, out_sim, out_dense = [], [], [], [], []
            for start in range(0, len(subset), 128):
                batch = subset[start : start + 128]
                sim = r_embed(
                    [build_query(r) for r in batch], r_meta["max_length"]
                ).astype(np.float32) @ doc_vecs.T.astype(np.float32)
                idx = np.zeros((len(batch), k), dtype=np.int64)
                cos = np.zeros((len(batch), k), dtype=np.float32)
                rank = np.zeros((len(batch), k), dtype=np.float32)
                dense = np.zeros((len(batch), k), dtype=np.int64)
                for i, row in enumerate(batch):
                    chosen = assemble_candidates(
                        sim[i], labels, set(row.get("active_ids") or ()), k
                    )
                    idx[i] = chosen.indices
                    cos[i] = chosen.cosine
                    rank[i] = chosen.rank_norm
                    # The same row with reservation switched OFF. Kept only so
                    # the two recalls below can be reported apart; it never
                    # reaches the head.
                    dense[i] = assemble_candidates(sim[i], labels, (), k).indices
                out_idx.append(idx)
                out_cos.append(cos)
                out_rank.append(rank)
                out_sim.append(sim.astype(np.float32))
                out_dense.append(dense)
            return (
                np.concatenate(out_idx),
                np.concatenate(out_cos),
                np.concatenate(out_rank),
                np.concatenate(out_sim),
                np.concatenate(out_dense),
            )

        a = shortlist(train_rows)
        c = shortlist(val_rows)
        return (*a, *c)

    (
        tr_cand, tr_cos, tr_rank, tr_sim, tr_dense,
        va_cand, va_cos, va_rank, va_sim, va_dense,
    ) = stage1_shortlists()
    if dev == "mps":
        torch.mps.empty_cache()

    def recall_at_k(subset: list[dict], cand: np.ndarray) -> float:
        need = 0
        hit = 0
        for r, row_cand in zip(subset, cand, strict=True):
            want = {index[s] for s in r["expect_ids"] if s in index}
            if not want:
                continue
            need += 1
            hit += want <= set(row_cand.tolist())
        return hit / max(need, 1)

    def reserved_rate(cand: np.ndarray, dense: np.ndarray) -> float:
        """Share of rows where reservation actually seated something."""
        moved = [set(a.tolist()) != set(b.tolist()) for a, b in zip(cand, dense, strict=True)]
        return sum(moved) / max(len(moved), 1)

    # DENSE recall and BALLOT recall, apart. Reporting only the second would
    # count a positive that reservation rescued as a stage-1 hit, and hide the
    # retrieval degradation reservation exists to compensate for — which is
    # precisely the number that decides whether k is still big enough.
    print(
        f"stage-1 recall@{k} (dense only):  train {recall_at_k(train_rows, tr_dense):.4f}  "
        f"val {recall_at_k(val_rows, va_dense):.4f}"
    )
    print(
        f"stage-1 recall@{k} (ballot):      train {recall_at_k(train_rows, tr_cand):.4f}  "
        f"val {recall_at_k(val_rows, va_cand):.4f}"
    )
    print(
        f"rows with a reserved active:      train {reserved_rate(tr_cand, tr_dense):.4f}  "
        f"val {reserved_rate(va_cand, va_dense):.4f}"
    )

    # ---------------------------------------------------------------- stage 2
    tok = AutoTokenizer.from_pretrained(args.base_model)
    backbone = AutoModel.from_pretrained(args.base_model).to(dev)
    hidden_size = backbone.config.hidden_size
    head = SetwiseHead(
        hidden_size,
        d_model=args.d_model,
        layers=args.layers,
        rank_positions=k if args.ordering == "positional" else 0,
    ).to(dev)
    use_retrieval_feats = args.ordering != "blind"
    shuffle_candidates = args.ordering != "positional"
    print(
        f"ordering={args.ordering}  rank_emb={head.rank_emb is not None}  "
        f"retrieval_feats={use_retrieval_feats}  shuffle={shuffle_candidates}"
    )

    doc_enc = tok(
        docs, padding=True, truncation=True, max_length=args.doc_max_length,
        return_tensors="pt",
    ).to(dev)
    print(f"runbook documents: {doc_enc['input_ids'].shape[1]} tokens padded")

    def pool(model, ids, mask):  # type: ignore[no-untyped-def]
        hidden = model(input_ids=ids, attention_mask=mask)[0]
        m = mask.unsqueeze(-1).to(hidden.dtype)
        return (hidden * m).sum(1) / m.sum(1).clamp(min=1e-9)

    # ---- the runbook-side embedding cache -----------------------------------
    #
    # Runbook documents are long (p50 262 tokens against a query's ~64) and there
    # are ~38 of them in every batch's candidate union, so encoding them WITH
    # gradients on every step costs about six times what the queries do and
    # dominates training. They are also, unlike the queries, the same 38 strings
    # every step — what changes is only the encoder drifting under them.
    #
    # So they are embedded under no_grad and refreshed every ``doc_refresh``
    # steps. The encoder is then trained as a QUERY encoder, with the shared
    # ``cand_proj`` learning to read whatever it produces on the runbook side.
    # This is the asymmetric setup dense retrieval has used for years
    # (periodic index refresh), and it suits this architecture specifically: a
    # runbook-side representation that stays near the pretrained space is one a
    # brand-new runbook's document also lands in, which is the zero-shot property
    # the whole design is for. Set --doc-refresh 0 to restore full gradient flow.
    doc_cache: torch.Tensor | None = None

    def refresh_docs() -> None:
        nonlocal doc_cache
        if args.doc_refresh <= 0:
            return
        was_training = backbone.training
        backbone.eval()
        with torch.no_grad():
            doc_cache = pool(
                backbone, doc_enc["input_ids"], doc_enc["attention_mask"]
            ).detach()
        if was_training:
            backbone.train()

    def make_batch(
        subset: list[dict],
        cand: np.ndarray,
        cos: np.ndarray,
        rank: np.ndarray,
        sim: np.ndarray,
        *,
        train: bool,
    ) -> tuple[np.ndarray, ...]:
        """Assemble one batch's candidate slots, features and targets.

        The ballot arriving here is already the SERVING one — built by
        ``assemble_candidates``, actives seated. What this adds is a training-only
        device about LABELS: a positive missing from the ballot is swapped in over
        the weakest candidate, because a row whose answer is absent teaches the
        head only to say "none of these", which is a stage-1 failure being blamed
        on stage 2. At VAL time nothing is swapped — a retrieval miss must be
        visible as a miss, which is what makes the reported number honest.

        The two must not fight. A swap steps over any slot holding a positive
        (as it always has) AND any slot holding an active runbook, because evicting
        a reserved candidate to seat a positive would train the head on a ballot
        the server cannot produce.
        """
        b = len(subset)
        idx = cand.copy()
        cs = cos.copy()
        rk = rank.copy()
        y = np.zeros((b, k), dtype=np.float32)
        card = np.zeros(b, dtype=np.int64)
        docs_y = np.zeros(b, dtype=np.float32)
        for i, row in enumerate(subset):
            want = [index[s] for s in row["expect_ids"] if s in index]
            card[i] = min(len(want), CARD_CLASSES - 1)
            docs_y[i] = 1.0 if row.get("is_docs") else 0.0
            present = set(idx[i].tolist())
            if train:
                protected = set(want) | {
                    index[s] for s in (row.get("active_ids") or ()) if s in index
                }
                free = k - 1
                for w in want:
                    if w in present:
                        continue
                    while free >= 0 and int(idx[i, free]) in protected:
                        free -= 1
                    if free < 0:
                        break
                    idx[i, free] = w
                    # The swapped positive's TRUE cosine, read out of the full
                    # similarity row. This used to be `cos[i].min()` — the
                    # ballot's weakest score — which was a fair stand-in only
                    # while the ballot was a pure top-k, where an off-ballot
                    # runbook is below the minimum by definition. Reservation
                    # broke that: a deeply-ranked active seated on this ballot
                    # can BE the minimum, and a positive ranked just outside k
                    # would then be handed a cosine far below its own, teaching
                    # the head a feature value the server never sends.
                    cs[i, free] = float(sim[i, w])
                    rk[i, free] = 1.0
                    present.add(w)
                    free -= 1
            for j, sid_idx in enumerate(idx[i].tolist()):
                if sid_idx in want:
                    y[i, j] = 1.0
        return idx, cs, rk, y, card, docs_y

    def features(
        subset: list[dict],
        idx: np.ndarray,
        cs: np.ndarray,
        rank_norm: np.ndarray,
        *,
        rng: random.Random | None,
    ) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
        """Per-candidate scalars, plus the optional shuffle. See FEATURE_NAMES.

        ``rank_norm`` is passed in rather than re-derived from slot position:
        the serving assembler writes 1.0 for a reserved candidate, and
        recomputing ``arange(k)`` here would quietly overwrite that with the
        slot's own rank — training the head on retrieval evidence the server
        does not send.
        """
        b = len(subset)
        rank_norm = rank_norm.astype(np.float32, copy=True)
        active = np.zeros((b, k), dtype=np.float32)
        for i, row in enumerate(subset):
            actives = {index[s] for s in (row.get("active_ids") or []) if s in index}
            for j, sid_idx in enumerate(idx[i].tolist()):
                if sid_idx in actives:
                    active[i, j] = 1.0
        feats = np.stack([cs.astype(np.float32), active, rank_norm], axis=-1)
        if not use_retrieval_feats:
            feats[:, :, 0] = 0.0
            feats[:, :, 2] = 0.0
        perm = np.tile(np.arange(k), (b, 1))
        if shuffle_candidates and rng is not None:
            for i in range(b):
                p = list(range(k))
                rng.shuffle(p)
                perm[i] = p
        return feats, perm, active

    def run_batch(
        subset: list[dict],
        idx: np.ndarray,
        cs: np.ndarray,
        rk: np.ndarray,
        y: np.ndarray,
        rng: random.Random | None,
    ):  # type: ignore[no-untyped-def]
        feats, perm, _ = features(subset, idx, cs, rk, rng=rng)
        idx_p = np.take_along_axis(idx, perm, axis=1)
        y_p = np.take_along_axis(y, perm, axis=1)
        feats_p = np.take_along_axis(feats, perm[:, :, None], axis=1)

        q_enc = tok(
            [build_query(r) for r in subset],
            padding=True, truncation=True, max_length=args.max_length,
            return_tensors="pt",
        ).to(dev)
        q = pool(backbone, q_enc["input_ids"], q_enc["attention_mask"])
        if doc_cache is not None:
            s = doc_cache[torch.tensor(idx_p, device=dev)]
        else:
            # --doc-refresh 0: encode WITH gradients, but only the runbooks this
            # batch actually shortlisted. At 38 runbooks that is the whole catalog;
            # at 1,000 it is the handful in play, which is what keeps this path's
            # cost independent of catalog size.
            union = np.unique(idx_p)
            u_pos = {int(v): i for i, v in enumerate(union.tolist())}
            s_all = pool(
                backbone,
                doc_enc["input_ids"][union],
                doc_enc["attention_mask"][union],
            )
            gather = torch.tensor(
                [[u_pos[int(v)] for v in row] for row in idx_p], device=dev
            )
            s = s_all[gather]
        feats_t = torch.tensor(feats_p, device=dev)
        # Training always fills every slot, so the head's PADDING path is never
        # exercised here. It only engages when the catalog is smaller than k,
        # which this catalog is not and is not heading towards; if that ever
        # changes, train with ragged shortlists rather than trusting an
        # untrained mask.
        mask = torch.ones(len(subset), k, device=dev)
        runbook_logits, docs_logit, card_logits = head(q, s, feats_t, mask)
        return runbook_logits, docs_logit, card_logits, torch.tensor(y_p, device=dev), idx_p

    # A positive is ~1 slot in k, so unweighted BCE answers "never".
    pos_rate = max(float(np.mean([len(r["expect_ids"]) for r in train_rows])), 0.1)
    pos_weight = torch.tensor(
        [min((k - pos_rate) / pos_rate, 50.0)], device=dev, dtype=torch.float32
    )
    slot_loss = torch.nn.BCEWithLogitsLoss(pos_weight=pos_weight)
    # The docs head needs its own pos_weight, for the same reason the slot head
    # does and more so: only 2.4% of the corpus wants a documentation lookup, so
    # unweighted BCE answers "no" to everything and scores 97.6%. Measured: the
    # first build of this trainer left it unweighted and the docs FLAG dropped
    # from 12/16 to 9/16 on the benchmark while every other category held.
    docs_pos = max(sum(1 for r in train_rows if r.get("is_docs")), 1)
    docs_loss = torch.nn.BCEWithLogitsLoss(
        pos_weight=torch.tensor(
            [min((len(train_rows) - docs_pos) / docs_pos, 50.0)],
            device=dev,
            dtype=torch.float32,
        )
    )
    card_counts = np.bincount(
        [min(len([s for s in r["expect_ids"] if s in index]), CARD_CLASSES - 1)
         for r in train_rows],
        minlength=CARD_CLASSES,
    ).astype(np.float32).clip(min=1.0)
    card_weight = torch.tensor(
        len(train_rows) / (CARD_CLASSES * card_counts), device=dev, dtype=torch.float32
    )
    card_loss = torch.nn.CrossEntropyLoss(weight=card_weight)

    opt = torch.optim.AdamW(
        [
            {"params": backbone.parameters(), "lr": args.lr},
            {"params": head.parameters(), "lr": args.head_lr},
        ]
    )
    from collections import Counter

    sources = [r.get("source", "?") for r in train_rows]
    freq = Counter(sources)
    weights = torch.tensor(
        [1.0 / (freq[s] ** args.balance_power) for s in sources], dtype=torch.double
    )
    steps_per_epoch = (len(train_rows) + args.batch_size - 1) // args.batch_size
    sched = torch.optim.lr_scheduler.OneCycleLR(
        opt,
        max_lr=[args.lr, args.head_lr],
        total_steps=args.epochs * steps_per_epoch,
        pct_start=0.1,
    )
    rng = random.Random(args.seed)

    def evaluate() -> tuple[float, float, float]:
        backbone.eval()
        head.eval()
        # Evaluate against FRESH runbook vectors, never a cache that is up to
        # doc_refresh steps behind: serving embeds the catalog with the final
        # encoder, so a validation number computed against stale vectors is
        # measuring a model that will never run.
        refresh_docs()
        exact = 0
        card_ok = 0
        docs_ok = 0
        with torch.no_grad():
            for start in range(0, len(val_rows), 64):
                sl = slice(start, start + 64)
                subset = val_rows[sl]
                idx, cs, rk, y, card, docs_y = make_batch(
                    subset, va_cand[sl], va_cos[sl], va_rank[sl], va_sim[sl], train=False
                )
                logits, dlogit, clogits, y_t, idx_p = run_batch(
                    subset, idx, cs, rk, y, None
                )
                counts = clogits.argmax(1).cpu().numpy()
                probs = torch.sigmoid(logits).cpu().numpy()
                for i, row in enumerate(subset):
                    want = {index[s] for s in row["expect_ids"] if s in index}
                    order = np.argsort(-probs[i])[: int(counts[i])]
                    got = {int(idx_p[i][j]) for j in order}
                    exact += got == want
                card_ok += int((counts == card).sum())
                docs_ok += int(
                    ((torch.sigmoid(dlogit).cpu().numpy() >= 0.5) == (docs_y > 0.5)).sum()
                )
        n = len(val_rows)
        return exact / n, card_ok / n, docs_ok / n

    for epoch in range(1, args.epochs + 1):
        backbone.train()
        head.train()
        sampler = WeightedRandomSampler(
            weights, num_samples=len(train_rows), replacement=True
        )
        order = list(sampler)
        total = 0.0
        for step, start in enumerate(range(0, len(order), args.batch_size)):
            if args.doc_refresh > 0 and step % args.doc_refresh == 0:
                refresh_docs()
            picks = order[start : start + args.batch_size]
            subset = [train_rows[p] for p in picks]
            idx, cs, rk, y, card, docs_y = make_batch(
                subset, tr_cand[picks], tr_cos[picks], tr_rank[picks], tr_sim[picks],
                train=True,
            )
            opt.zero_grad()
            logits, dlogit, clogits, y_t, _ = run_batch(subset, idx, cs, rk, y, rng)
            loss = (
                slot_loss(logits, y_t)
                + card_loss(clogits, torch.tensor(card, device=dev))
                + docs_loss(dlogit, torch.tensor(docs_y, device=dev))
            )
            loss.backward()
            opt.step()
            sched.step()
            total += float(loss.detach())
        exact, card_acc, docs_acc = evaluate()
        print(
            f"  epoch {epoch}: loss {total / steps_per_epoch:.4f}  "
            f"val exact-match {exact:.4f}  card {card_acc:.4f}  docs {docs_acc:.4f}"
        )

    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    torch.save(
        {"backbone": backbone.state_dict(), "head": head.state_dict()}, out / "model.pt"
    )
    tok.save_pretrained(out)
    (out / "labels.json").write_text(json.dumps(labels, indent=2))
    (out / "meta.json").write_text(
        json.dumps(
            {
                "base_model": args.base_model,
                "hidden_size": hidden_size,
                "d_model": args.d_model,
                "layers": args.layers,
                "candidates": k,
                "ordering": args.ordering,
                "rank_positions": k if args.ordering == "positional" else 0,
                "cardinality_classes": CARD_CLASSES,
                "max_length": args.max_length,
                "doc_max_length": args.doc_max_length,
                "doc_variant": INDEX_DOC_VARIANT,
                "doc_refresh": args.doc_refresh,
                "retriever": str(args.retriever),
                # Recorded, NOT enforced. The head has no per-runbook weights, so a
                # catalog change does not invalidate it — this is provenance for
                # a human reading the artifact, not a serving gate. Dropping that
                # gate is the point of the architecture.
                "routing_revision_at_train": routing_revision(catalog),
                "holdout_runbooks": sorted(holdout),
            },
            indent=2,
        )
    )
    print(f"\nsaved set-wise model to {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
