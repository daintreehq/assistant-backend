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
import re
import sys
from pathlib import Path
from typing import TYPE_CHECKING

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
    split_when_to_use,
)
from training.common import (  # noqa: E402
    build_query,
    canonical_active,
    corpus_rows,
    device,
    stable_split,
)
from training.setwise_model import CARD_CLASSES, SetwiseHead  # noqa: E402

DATA = ROOT / "training" / "data"
MODELS = ROOT / "training" / "models"

ORDERINGS = ("equivariant", "positional", "blind")

if TYPE_CHECKING:
    import torch


def _encoder_param_groups(
    backbone: torch.nn.Module, lr: float, decay: float
) -> list[dict[str, object]]:
    """The encoder's optimiser groups: one group at ``lr``, or one per layer.

    With ``decay < 1`` the top transformer layer trains at ``lr`` and each layer
    below it at a further factor of ``decay``, embeddings lowest. Layers are
    found by the ``layer.<i>.`` / ``layers.<i>.`` segment BERT and ModernBERT
    both use; anything after the last layer (a final norm) trains at ``lr``.
    """
    params = list(backbone.named_parameters())
    if decay >= 1.0:
        return [{"params": [p for _, p in params], "lr": lr}]
    pattern = re.compile(r"(?:^|\.)layers?\.(\d+)\.")
    depth = {n: int(m.group(1)) for n, _ in params if (m := pattern.search(n))}
    if not depth:
        raise SystemExit("--llrd: no `layer.<i>.` / `layers.<i>.` parameters in this backbone")
    top = max(depth.values())
    by_scale: dict[float, list[torch.nn.Parameter]] = {}
    for name, p in params:
        if name in depth:
            scale = decay ** (top - depth[name])
        elif "embed" in name:
            scale = decay ** (top + 1)
        else:
            scale = 1.0
        by_scale.setdefault(scale, []).append(p)
    return [{"params": ps, "lr": lr * s} for s, ps in sorted(by_scale.items())]


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--epochs", type=int, default=10)
    ap.add_argument("--batch-size", type=int, default=32)
    ap.add_argument("--lr", type=float, default=5e-5)
    ap.add_argument("--head-lr", type=float, default=3e-4)
    ap.add_argument("--val-fraction", type=float, default=0.12)
    ap.add_argument("--seed", type=int, default=41)
    ap.add_argument("--max-length", type=int, default=256)
    ap.add_argument("--doc-max-length", type=int, default=512)
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
        help="`seed` shuffles the corpus by --seed, so adding rows reassigns ~all "
        "of them; `hash` keys each row's side on its content (stable_split).",
    )
    ap.add_argument(
        "--card-weight",
        choices=("inverse", "sqrt"),
        default="sqrt",
        help="cardinality class weights: full inverse frequency (the 3+ class "
        "weighs ~243) or its square root",
    )
    ap.add_argument(
        "--boundary-loss",
        type=float,
        default=0.1,
        help="weight of a margin loss ranking each positive above the row's "
        "authored `confusable_with` candidates on the ballot (0 = off)",
    )
    ap.add_argument(
        "--sampling",
        choices=("replacement", "permutation"),
        default="permutation",
        help="how an epoch draws rows. `replacement` is WeightedRandomSampler with "
        "replacement, so a uniform epoch sees ~63%% of distinct rows; `permutation` "
        "visits every row exactly once (requires --balance-power 0).",
    )
    ap.add_argument(
        "--ema",
        type=float,
        default=0.0,
        help="decay of an exponential moving average over backbone+head weights, "
        "evaluated and SAVED in place of the last step's (0 = off).",
    )
    ap.add_argument(
        "--doc-aug",
        type=int,
        default=3,
        help="clause-dropout variants of every runbook document, re-embedded at "
        "each refresh; each training slot draws one (0 = canonical only).",
    )
    ap.add_argument(
        "--doc-aug-drop",
        type=float,
        default=0.3,
        help="per-sentence drop probability for --doc-aug variants",
    )
    ap.add_argument(
        "--cand-noise",
        type=float,
        default=0.0,
        help="training-only Gaussian jitter on candidate vectors, relative to each "
        "vector's norm (0 = off)",
    )
    ap.add_argument(
        "--teachers",
        default="",
        help="comma-separated stage-2 model dirs whose averaged predictions are "
        "distilled into this run (same stage 1, encoder and doc cap)",
    )
    ap.add_argument("--distill-weight", type=float, default=0.5)
    ap.add_argument(
        "--init-seed",
        type=int,
        default=None,
        help="seed the head's initialisation separately from --seed, so several "
        "seeds share one starting point and their weights can be averaged "
        "(training/soup_setwise.py). Unset = the head draws from --seed.",
    )
    ap.add_argument(
        "--llrd",
        type=float,
        default=1.0,
        help="layer-wise LR decay for the encoder: layer l of L trains at "
        "lr * llrd**(L - l), embeddings at lr * llrd**(L + 1) (1.0 = off)",
    )
    ap.add_argument("--out", default=str(MODELS / "setwise"))
    args = ap.parse_args()
    if not 0.0 < args.llrd <= 1.0:
        raise SystemExit("--llrd must be in (0, 1]; 1.0 is off")
    if args.sampling == "permutation" and args.balance_power != 0.0:
        raise SystemExit("--sampling permutation draws uniformly; use --balance-power 0")
    if args.doc_aug and args.doc_refresh <= 0:
        # Augmentation lives on the cached-document path; the gradient path
        # encodes the canonical text. Defaulted on, so switch it off here rather
        # than make every --doc-refresh 0 run also pass --doc-aug 0.
        print("--doc-refresh 0: document augmentation disabled (cached path only)")
        args.doc_aug = 0

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

    rows = corpus_rows(Path(args.data))
    if args.canonical_active:
        rows = [
            {**r, "active_ids": canonical_active(r.get("active_ids") or [], catalog)}
            for r in rows
        ]
    if args.split == "hash":
        train_rows, val_rows = stable_split(rows, args.val_fraction)
    else:
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
    # CPU generator only: the head is built on CPU and moved after, and
    # `torch.manual_seed` would also re-seed CUDA/MPS, which `set_rng_state`
    # does not restore — every seed's device-side dropout would then follow
    # --init-seed instead of --seed.
    rng_state = torch.get_rng_state()
    if args.init_seed is not None:
        torch.default_generator.manual_seed(args.init_seed)
    head = SetwiseHead(
        hidden_size,
        d_model=args.d_model,
        layers=args.layers,
        rank_positions=k if args.ordering == "positional" else 0,
    ).to(dev)
    if args.init_seed is not None:
        # Everything after the head (dropout, sampling) still follows --seed.
        torch.set_rng_state(rng_state)
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

    # ---- document-side augmentation (--doc-aug) ------------------------------
    #
    # The head only ever sees ~40 fixed runbook vectors, so nothing stops it
    # recognising each vector by IDENTITY — a per-runbook parameter by another
    # name, and exactly what the architecture exists not to have. Clause-dropout
    # variants give the same runbook a different vector on different steps, so
    # what survives training is reading the content. Sentences naming another
    # runbook (the "do not use for X" fences) are dropped at a third of the rate:
    # they are the densest routing signal a document carries.
    aug_rng = random.Random(args.seed + 1)

    def _sentences(text: str) -> list[str]:
        import re

        return [s for s in re.split(r"(?<=[.;!?])\s+", text.strip()) if s]

    def _drop(text: str) -> str:
        sents = _sentences(text)
        if len(sents) <= 1:
            return text
        kept = [
            s for s in sents
            if aug_rng.random()
            >= (args.doc_aug_drop / 3 if "daintree." in s else args.doc_aug_drop)
        ]
        return " ".join(kept or [aug_rng.choice(sents)])

    def augmented_docs() -> list[str]:
        """Clause dropout over the POSITIVE description only.

        The exclusion tail ("do not use for X, use Y") is kept verbatim: dropping
        a fence while keeping the row's label would teach the head that a request
        the fence routes elsewhere belongs here — contradictory supervision.
        """
        out = []
        for sid in labels:
            meta = catalog.get(sid).meta
            title = (meta.title or "").strip()
            summary = _drop((meta.summary or "").strip())
            head_txt, tail_txt = split_when_to_use((meta.when_to_use or "").strip())
            when = f"{_drop(head_txt)} {tail_txt}".strip()
            out.append(f"{title}. {summary} {when}".strip())
        return out

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
    # (variants, runbooks, hidden) — row 0 is always the canonical document.
    doc_variants: torch.Tensor | None = None

    def refresh_docs(*, with_variants: bool = False) -> None:
        nonlocal doc_cache, doc_variants
        if args.doc_refresh <= 0:
            return
        was_training = backbone.training
        backbone.eval()
        with torch.no_grad():
            doc_cache = pool(
                backbone, doc_enc["input_ids"], doc_enc["attention_mask"]
            ).detach()
            if with_variants and args.doc_aug > 0:
                stack = [doc_cache]
                for _ in range(args.doc_aug):
                    enc = tok(
                        augmented_docs(), padding=True, truncation=True,
                        max_length=args.doc_max_length, return_tensors="pt",
                    ).to(dev)
                    stack.append(
                        pool(backbone, enc["input_ids"], enc["attention_mask"]).detach()
                    )
                doc_variants = torch.stack(stack)
            else:
                doc_variants = None
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
        if doc_variants is not None and rng is not None:
            # Training with --doc-aug: each slot draws its own variant.
            which = torch.randint(
                0, doc_variants.shape[0], idx_p.shape, device=dev
            )
            s = doc_variants[which, torch.tensor(idx_p, device=dev)]
        elif doc_cache is not None:
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
        if args.cand_noise > 0 and rng is not None:
            scale = s.norm(dim=-1, keepdim=True) / (s.shape[-1] ** 0.5)
            s = s + args.cand_noise * scale * torch.randn_like(s)
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
    card_w = len(train_rows) / (CARD_CLASSES * card_counts)
    if args.card_weight == "sqrt":
        card_w = np.sqrt(card_w)
    card_weight = torch.tensor(card_w, device=dev, dtype=torch.float32)
    card_loss = torch.nn.CrossEntropyLoss(weight=card_weight)

    groups = _encoder_param_groups(backbone, args.lr, args.llrd)
    groups.append({"params": list(head.parameters()), "lr": args.head_lr})
    opt = torch.optim.AdamW(groups)
    from collections import Counter

    sources = [r.get("source", "?") for r in train_rows]
    freq = Counter(sources)
    weights = torch.tensor(
        [1.0 / (freq[s] ** args.balance_power) for s in sources], dtype=torch.double
    )
    steps_per_epoch = (len(train_rows) + args.batch_size - 1) // args.batch_size
    sched = torch.optim.lr_scheduler.OneCycleLR(
        opt,
        max_lr=[g["lr"] for g in groups],
        total_steps=args.epochs * steps_per_epoch,
        pct_start=0.1,
    )
    rng = random.Random(args.seed)

    # ---- weight averaging (--ema) --------------------------------------------
    #
    # Training ends at a near-zero loss, and the last step's weights are one
    # point on a trajectory that ~20 near-tie benchmark cases are sensitive to.
    # An average along the SAME trajectory is a flatter point (unlike averaging
    # across seeds, which EXPERIMENTS.md measured as meaningless: independently
    # initialised heads are not aligned). Bias-corrected warm-up so the first
    # steps do not anchor the average to the pretrained weights.
    ema_params: list[torch.Tensor] = []
    live_params: list[torch.Tensor] = []
    if args.ema > 0:
        live_params = list(backbone.parameters()) + list(head.parameters())
        ema_params = [p.detach().clone() for p in live_params]
    ema_steps = 0

    def ema_update() -> None:
        nonlocal ema_steps
        ema_steps += 1
        d = min(args.ema, (1 + ema_steps) / (10 + ema_steps))
        with torch.no_grad():
            for e, p in zip(ema_params, live_params, strict=True):
                e.lerp_(p.detach(), 1.0 - d)

    def swap_ema() -> None:
        """Exchange live and averaged weights in place (call twice to restore)."""
        with torch.no_grad():
            for e, p in zip(ema_params, live_params, strict=True):
                tmp = p.detach().clone()
                p.copy_(e)
                e.copy_(tmp)

    def boundary_margin(subset, logits, y_t, idx_p):  # type: ignore[no-untyped-def]
        """Rank each positive above the row's AUTHORED confusables, margin 1.

        BCE gives a hard negative one slot's share of an averaged loss, the same
        as any easy one. The corpus already records which neighbour a query could
        be mistaken for (`confusable_with`, mostly lifted from whenToUse fences);
        stage 1 reads it, stage 2 never did. Averaged per row over (positive,
        confusable) pairs, then over the rows that have any.
        """
        conf = np.zeros(idx_p.shape, dtype=np.float32)
        for i, row in enumerate(subset):
            names = {index[s] for s in (row.get("confusable_with") or []) if s in index}
            names -= {index[s] for s in row["expect_ids"] if s in index}
            if not names:
                continue
            for j, sid_idx in enumerate(idx_p[i].tolist()):
                if sid_idx in names:
                    conf[i, j] = 1.0
        conf_t = torch.tensor(conf, device=dev)
        pairs = y_t[:, :, None] * conf_t[:, None, :]  # (b, pos, neg)
        n = pairs.sum(dim=(1, 2))
        if float(n.sum()) == 0.0:
            return logits.sum() * 0.0
        diff = logits[:, None, :] - logits[:, :, None]  # l_neg - l_pos
        per_row = (torch.nn.functional.softplus(1.0 + diff) * pairs).sum(dim=(1, 2))
        has = n > 0
        return (per_row[has] / n[has]).mean()

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

    # ---- ensemble distillation (--teachers) ----------------------------------
    #
    # A few stage-2 models trained from different seeds disagree on the same
    # ~20 near-tie turns that move a single model's benchmark score by several
    # cases between seeds. Their AVERAGE is steadier, but serving N encoders
    # costs N times the latency. So the ensemble's averaged probabilities are
    # computed once over every training row's ballot and the student learns
    # from them alongside the hard labels — ensemble behaviour, one model's
    # inference. Teachers must share this run's stage 1 and document cap: they
    # score the student's ballots, features included.
    soft_slot: list[dict[int, float]] = []
    soft_card = np.zeros((0, CARD_CLASSES), dtype=np.float32)
    soft_docs = np.zeros(0, dtype=np.float32)
    teacher_dirs = [t for t in args.teachers.split(",") if t.strip()]
    if teacher_dirs:
        n_rows = len(train_rows)
        acc_slot: list[dict[int, float]] = [{} for _ in range(n_rows)]
        acc_card = np.zeros((n_rows, CARD_CLASSES), dtype=np.float64)
        acc_docs = np.zeros(n_rows, dtype=np.float64)
        for tdir in teacher_dirs:
            t_meta = json.loads((Path(tdir) / "meta.json").read_text())
            if (
                t_meta["doc_max_length"] != args.doc_max_length
                or t_meta["base_model"] != args.base_model
                or t_meta.get("ordering", "equivariant") != args.ordering
            ):
                raise SystemExit(
                    f"teacher {tdir} does not match this run's encoder, doc cap or ordering"
                )
            state = torch.load(Path(tdir) / "model.pt", map_location="cpu")
            t_backbone = AutoModel.from_pretrained(args.base_model)
            t_backbone.load_state_dict(state["backbone"])
            t_backbone.to(dev).eval()
            t_head = SetwiseHead(
                hidden_size, d_model=t_meta["d_model"], layers=t_meta["layers"],
                rank_positions=t_meta.get("rank_positions", 0),
            )
            t_head.load_state_dict(state["head"])
            t_head.to(dev).eval()
            with torch.no_grad():
                t_docs = pool(t_backbone, doc_enc["input_ids"], doc_enc["attention_mask"])
                for start in range(0, n_rows, 64):
                    sl = slice(start, start + 64)
                    subset = train_rows[sl]
                    idx, cs, rk, _y, _card, _docs = make_batch(
                        subset, tr_cand[sl], tr_cos[sl], tr_rank[sl], tr_sim[sl], train=True
                    )
                    feats, _perm, _ = features(subset, idx, cs, rk, rng=None)
                    q_enc = tok(
                        [build_query(r) for r in subset], padding=True, truncation=True,
                        max_length=args.max_length, return_tensors="pt",
                    ).to(dev)
                    q = pool(t_backbone, q_enc["input_ids"], q_enc["attention_mask"])
                    s = t_docs[torch.tensor(idx, device=dev)]
                    mask = torch.ones(len(subset), k, device=dev)
                    lg, dl, cl = t_head(q, s, torch.tensor(feats, device=dev), mask)
                    p = torch.sigmoid(lg).cpu().numpy()
                    acc_card[sl] += torch.softmax(cl, -1).cpu().numpy()
                    acc_docs[sl] += torch.sigmoid(dl).cpu().numpy()
                    for i in range(len(subset)):
                        row_acc = acc_slot[start + i]
                        for j, sid_idx in enumerate(idx[i].tolist()):
                            row_acc[sid_idx] = row_acc.get(sid_idx, 0.0) + float(p[i, j])
            del t_backbone, t_head, t_docs, state
            if dev == "mps":
                torch.mps.empty_cache()
            print(f"teacher scored: {tdir}")
        m = float(len(teacher_dirs))
        soft_slot = [{key: v / m for key, v in d.items()} for d in acc_slot]
        soft_card = (acc_card / m).astype(np.float32)
        soft_docs = (acc_docs / m).astype(np.float32)

    def distill_loss(picks, logits, dlogit, clogits, idx_p):  # type: ignore[no-untyped-def]
        target = np.zeros(idx_p.shape, dtype=np.float32)
        for i, pk in enumerate(picks):
            d = soft_slot[pk]
            for j, sid_idx in enumerate(idx_p[i].tolist()):
                target[i, j] = d.get(sid_idx, 0.0)
        t_slot = torch.tensor(target, device=dev)
        t_card = torch.tensor(soft_card[picks], device=dev)
        t_docs = torch.tensor(soft_docs[picks], device=dev)
        card_kl = -(t_card * torch.log_softmax(clogits, -1)).sum(-1).mean()
        # UNWEIGHTED BCE against the teachers' probabilities. The hard-label
        # losses carry pos_weight (~31 for slots), and against a SOFT target t
        # that moves the optimum to 31t / (31t + 1 - t): a neighbour the teachers
        # gave 0.1 is pushed to 0.78, over the bonus floor. Measured on the first
        # build: 21 of 217 turns loaded three runbooks (6-8 without distillation)
        # and strict passes fell from 175-192 to 161. The teachers' probabilities
        # were already shaped by pos_weight; matching them needs no second dose.
        bce = torch.nn.functional.binary_cross_entropy_with_logits
        # Summed over slots rather than averaged: the hard slot loss spends its
        # weight on the one positive in ~32 (pos_weight), so a per-slot MEAN of
        # an unweighted term would be ~30x smaller and the mix would barely
        # distil at all.
        return bce(logits, t_slot) * logits.shape[1] + card_kl + bce(dlogit, t_docs)

    for epoch in range(1, args.epochs + 1):
        backbone.train()
        head.train()
        if args.sampling == "permutation":
            order = np.random.permutation(len(train_rows)).tolist()
        else:
            sampler = WeightedRandomSampler(
                weights, num_samples=len(train_rows), replacement=True
            )
            order = list(sampler)
        total = 0.0
        for step, start in enumerate(range(0, len(order), args.batch_size)):
            if args.doc_refresh > 0 and step % args.doc_refresh == 0:
                refresh_docs(with_variants=True)
            picks = order[start : start + args.batch_size]
            subset = [train_rows[p] for p in picks]
            idx, cs, rk, y, card, docs_y = make_batch(
                subset, tr_cand[picks], tr_cos[picks], tr_rank[picks], tr_sim[picks],
                train=True,
            )
            opt.zero_grad()
            logits, dlogit, clogits, y_t, idx_p = run_batch(subset, idx, cs, rk, y, rng)
            loss = (
                slot_loss(logits, y_t)
                + card_loss(clogits, torch.tensor(card, device=dev))
                + docs_loss(dlogit, torch.tensor(docs_y, device=dev))
            )
            if args.boundary_loss > 0:
                loss = loss + args.boundary_loss * boundary_margin(subset, logits, y_t, idx_p)
            if teacher_dirs:
                lam = args.distill_weight
                loss = (1 - lam) * loss + lam * distill_loss(picks, logits, dlogit, clogits, idx_p)
            loss.backward()
            opt.step()
            sched.step()
            if ema_params:
                ema_update()
            total += float(loss.detach())
        exact, card_acc, docs_acc = evaluate()
        line = (
            f"  epoch {epoch}: loss {total / steps_per_epoch:.4f}  "
            f"val exact-match {exact:.4f}  card {card_acc:.4f}  docs {docs_acc:.4f}"
        )
        if ema_params:
            swap_ema()
            e_exact, e_card, e_docs = evaluate()
            swap_ema()
            line += f"  | ema {e_exact:.4f}  card {e_card:.4f}  docs {e_docs:.4f}"
        print(line)

    if ema_params:
        # What gets saved — and therefore exported and served — is the average.
        swap_ema()

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
                "sampling": args.sampling,
                "ema": args.ema,
                "doc_aug": args.doc_aug,
                "cand_noise": args.cand_noise,
                "seed": args.seed,
                "init_seed": args.init_seed,
                "llrd": args.llrd,
                "epochs": args.epochs,
            },
            indent=2,
        )
    )
    print(f"\nsaved set-wise model to {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
