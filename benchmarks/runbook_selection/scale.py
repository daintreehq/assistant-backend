"""Does the shortlist still contain the answer at 200, 500, 1,000 runbooks?

## The question, and why it is not the end-to-end score

Stage 1's contract is not to rank correctly — Stage 2 re-decides the order. Its
contract is **not to lose the answer**. So the metric is oracle recall: for every
required runbook, did it appear anywhere in the assembled ballot? Everything
downstream is repairable; this is not.

That number is uninformative today. `k=32` against 38 selectable runbooks puts 84%
of the catalog on every ballot, so recall is 1.000 by arithmetic rather than by
retrieval working. This module pads the catalog with realistic distractors
(`shadow_catalog.py`) and re-measures, which is the only way to learn what `k`
should be before there are 500 real runbooks to learn it from.

## What it reports, and why each slice is separate

    all_required     every required runbook on the ballot — the product contract
    active           the runbooks already loaded reached the head
    dense_active     ...and would have WITHOUT reservation
    by category      continuation / topic-shift / multi-runbook etc.

`active` versus `dense_active` is the point of the whole sweep. The first is
1.000 by construction — `candidates.py` seats an active runbook retrieval missed —
and the second is what retrieval alone achieves. The gap between them at each
size IS the number that says how much work reservation is doing, and it is the
number that was invisible before this existed.

## Cost

Embedding dominates: ~1,000 runbook documents at build time. The sizes are NESTED
(each is a prefix of the next) and the classifier's vector memo is keyed by
document digest and survives table replacement, so running the LARGEST first
makes every smaller size free. One sweep is a few minutes; it is an on-demand
harness, not a CI gate. `--sizes` trims it for a quick check.
"""

from __future__ import annotations

import statistics
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import numpy as np

from daintree_assistant_server.runbooks.catalog import default_runbooks_dir, load_catalog
from daintree_assistant_server.runbooks.local.candidates import assemble_candidates
from daintree_assistant_server.runbooks.local.documents import ACTIVE_PREFIX, query_document
from daintree_assistant_server.runbooks.local.setwise import SetwiseClassifier, _run_encoder

from .cases import BenchCase, load_cases
from .local_runner import ARTIFACT
from .shadow_catalog import SEED, Distractor, generate_distractors, shadow_catalog

#: Catalog sizes to sweep. ``0``, and any size the live catalog has already outgrown,
#: become the live catalog's own size at run time, so "today" is always the
#: baseline row and the default keeps running as the catalog grows past 100.
DEFAULT_SIZES = (0, 100, 200, 500, 1000)

#: Shortlist widths. 32 is what ships; the rest bracket it. Chosen to answer
#: "how much wider would it have to be", not to be swept exhaustively — the head
#: is exported at a fixed `k`, so moving it costs a retrain and a re-export.
DEFAULT_KS = (16, 32, 48, 64, 96)


@dataclass(frozen=True)
class RecallRow:
    """Oracle recall for one (catalog size, k) pair.

    Every rate carries its counts, because n is 177 cases and one case is 0.0056
    — a table of four-decimal rates invites distinctions the sample cannot
    support.
    """

    size: int
    k: int
    #: Cases where EVERY required runbook reached the ballot, over cases that HAVE
    #: a required runbook. 19 of the 177 require none (chatter, no-task, most docs
    #: questions); including them made 19 vacuous successes and inflated every
    #: rate here and in the per-category table.
    all_required: float
    all_required_hits: int
    all_required_n: int
    #: Actives that are ALSO required — the ones retaining is the right answer —
    #: reached the ballot. This is the workflow-continuity number.
    retained: float
    retained_hits: int
    retained_n: int
    #: ...and would have on dense retrieval alone, without reservation.
    dense_retained: float
    dense_retained_hits: int
    #: EVERY visible active reached the ballot, right answer or not. 1.0 by
    #: construction; a coverage invariant rather than a quality measure.
    active_coverage: float
    #: The same, dense-only. Reported because it is the pre-reservation
    #: behaviour, but it counts topic shifts — where losing the old runbook is
    #: CORRECT — so it overstates harm and must not be read as an eviction rate.
    dense_active_coverage: float
    active_n: int
    #: How many cases needed a reservation at all.
    reserved_cases: int
    #: Share of the catalog that reached the head.
    ballot_share: float
    by_category: dict[str, list[int]]


def _encode_queries(
    clf: SetwiseClassifier, cases: list[BenchCase], selectable: frozenset[str]
) -> list[tuple[np.ndarray, list[str], list[str]]]:
    """One retriever embedding per case, plus its visible actives and answer.

    Hoisted out of the k loop because the query vector does not depend on k, and
    NOT out of the size loop: the ACTIVE_PREFIX is built from the visible active
    set, which is projected against the catalog under test.
    """
    out = []
    for case in cases:
        visible = [sid for sid in case.active if sid in selectable]
        query = query_document(case.messages, turn_context=case.turn_context)
        text = ACTIVE_PREFIX.format(", ".join(visible)) + query if visible else query
        vec = _run_encoder(clf._retriever, clf._retriever_tok, [text], normalize=True)[0]
        out.append((vec, visible, [sid for sid in case.required if sid in selectable]))
    return out


def _is_background_wake(case: BenchCase, selectable: frozenset[str]) -> bool:
    """The STRUCTURAL rule in ``LocalSelector.select``, reproduced exactly.

    A turn with an active set whose newest eligible message is not the user's has
    no new instruction to classify, so production never sends it to the model at
    all. Scoring it here would measure retrieval on evidence the selector never
    sees. Note this is not the same as the ``background-wake`` benchmark
    CATEGORY: four of that category's five cases satisfy the predicate, and the
    fifth is an ordinary user request.
    """
    eligible = [m for m in case.messages if m.role in ("user", "assistant", "tool")]
    active = [sid for sid in case.active if sid in selectable]
    return bool(active) and bool(eligible) and eligible[-1].role != "user"


def effective_sizes(sizes: tuple[int, ...], floor: int) -> tuple[tuple[int, ...], list[int]]:
    """The sizes a sweep measures, and the requested ones it had to raise.

    A synthetic catalog cannot be smaller than the real one it pads, so a size
    below ``floor`` measures the real catalog instead. Raising it rather than
    refusing it is what keeps the DEFAULT sweep runnable: it was written at 38
    runbooks, and the 100 in it was refused outright once the catalog passed 100.
    The raised sizes are returned so the run can say so; an explicit ``--sizes 100``
    that silently came back as a 154-runbook row would be a mislabelled number.
    """
    raised = sorted({s for s in sizes if 0 < s < floor})
    return tuple(sorted({max(s, floor) for s in sizes})), raised


def sweep(
    *,
    sizes: tuple[int, ...] = DEFAULT_SIZES,
    ks: tuple[int, ...] = DEFAULT_KS,
    artifact: Path | None = None,
    seed: int = SEED,
    log: Any = print,
) -> dict[str, Any]:
    """Oracle recall across catalog sizes and shortlist widths."""
    if not sizes:
        raise ValueError("sweep needs at least one catalog size")
    if any(k <= 0 for k in ks):
        raise ValueError(f"shortlist widths must be positive, got {ks}")

    real = load_catalog(default_runbooks_dir())
    floor = len(real.runbooks)
    sizes, raised = effective_sizes(sizes, floor)
    if raised:
        log(f"  sizes {raised} are below the real catalog: measured at {floor} runbooks")

    largest = max(sizes)
    pool: list[Distractor] = generate_distractors(largest - floor, real, seed=seed)

    clf = SetwiseClassifier(artifact or ARTIFACT)
    all_cases = load_cases(real)
    # The wake cases are dropped rather than scored: production bypasses the
    # model on them, so retrieval never runs and a hit or a miss here is about
    # nothing. Counted, so the drop is visible in the report.
    scored = [c for c in all_cases if not _is_background_wake(c, real.selectable_ids)]

    rows: list[RecallRow] = []
    embed_seconds: dict[int, float] = {}
    # Largest FIRST: the vector memo is keyed by document digest and outlives
    # each table, so every smaller (nested) size is then served from it.
    for size in sorted(sizes, reverse=True):
        catalog = shadow_catalog(size, real, pool)
        started = time.perf_counter()
        table = clf.sync(catalog)
        embed_seconds[size] = round(time.perf_counter() - started, 2)
        log(f"  {size:5d} runbooks  table built in {embed_seconds[size]:6.2f}s")

        selectable = catalog.selectable_ids
        encoded = _encode_queries(clf, scored, selectable)

        for k in ks:
            hits = need = 0
            retained_hits = dense_retained_hits = retained_need = 0
            covered = dense_covered = active_n = 0
            reserved = 0
            by_cat: dict[str, list[int]] = {}
            for case, (vec, visible, required) in zip(scored, encoded, strict=True):
                sims = table.retrieval @ vec
                ballot = assemble_candidates(sims, table.ids, set(visible), k)
                ids = {table.ids[int(i)] for i in ballot.indices}
                dense = {
                    table.ids[int(i)]
                    for i in assemble_candidates(sims, table.ids, (), k).indices
                }
                if required:
                    need += 1
                    ok = set(required) <= ids
                    hits += ok
                    cell = by_cat.setdefault(case.category, [0, 0])
                    cell[0] += ok
                    cell[1] += 1
                if visible:
                    active_n += 1
                    covered += set(visible) <= ids
                    dense_covered += set(visible) <= dense
                    # The actives this turn SHOULD keep. On a topic shift that is
                    # empty, and the case contributes nothing — which is the
                    # point: losing the old runbook there is the right answer.
                    keep = set(visible) & set(required)
                    if keep:
                        retained_need += 1
                        retained_hits += keep <= ids
                        dense_retained_hits += keep <= dense
                reserved += bool(ballot.reserved.any())

            def rate(num: int, den: int) -> float:
                return round(num / den, 4) if den else 1.0

            rows.append(
                RecallRow(
                    size=size, k=k,
                    all_required=rate(hits, need),
                    all_required_hits=hits, all_required_n=need,
                    retained=rate(retained_hits, retained_need),
                    retained_hits=retained_hits, retained_n=retained_need,
                    dense_retained=rate(dense_retained_hits, retained_need),
                    dense_retained_hits=dense_retained_hits,
                    active_coverage=rate(covered, active_n),
                    dense_active_coverage=rate(dense_covered, active_n),
                    active_n=active_n,
                    reserved_cases=reserved,
                    ballot_share=round(min(k, len(selectable)) / len(selectable), 4),
                    by_category=by_cat,
                )
            )

    rows.sort(key=lambda r: (r.size, r.k))
    return {
        "suite": "runbook_selection_scale",
        "meta": {
            "seed": seed,
            "real_runbooks": floor,
            "cases_total": len(all_cases),
            "cases_scored": len(scored),
            "cases_with_a_required_runbook": sum(1 for c in scored if c.required),
            "background_wakes_skipped": len(all_cases) - len(scored),
            "sizes": list(sizes),
            "ks": list(ks),
            "shipped_k": clf.candidates,
            "catalog_revision": real.revision,
            "embed_seconds": embed_seconds,
        },
        "rows": [r.__dict__ for r in rows],
    }


def print_sweep(doc: dict[str, Any]) -> None:
    meta = doc["meta"]
    rows = [RecallRow(**r) for r in doc["rows"]]
    shipped = meta["shipped_k"]

    print(
        f"\noracle recall — seed {meta['seed']}, {meta['real_runbooks']} real runbooks "
        f"padded with derived distractors"
    )
    print(
        f"  {meta['cases_scored']} of {meta['cases_total']} cases scored "
        f"({meta['background_wakes_skipped']} background wakes never reach the "
        f"model in production); {meta['cases_with_a_required_runbook']} have a "
        f"required runbook and count toward recall"
    )

    print("\n  size     k   ballot%      all-required        retained   dense-retained")
    for row in rows:
        mark = " *" if row.k == shipped else "  "
        print(
            f"  {row.size:5d} {row.k:5d}{mark}  {row.ballot_share * 100:5.1f}%   "
            f"{row.all_required:.4f} ({row.all_required_hits}/{row.all_required_n})   "
            f"{row.retained:.4f} ({row.retained_hits}/{row.retained_n})   "
            f"{row.dense_retained:.4f} ({row.dense_retained_hits}/{row.retained_n})"
        )
    print(f"\n  * = the shipped k ({shipped}). Moving it costs a retrain and re-export.")
    print(
        "  retained = actives the turn SHOULD keep (active AND required) that "
        "reached the\n  ballot. A topic-shift case contributes nothing to it, "
        "because losing the old\n  runbook there is the right answer."
    )

    print("\nwhat reservation buys, at the shipped k:")
    for row in rows:
        if row.k != shipped:
            continue
        print(
            f"  {row.size:5d} runbooks   retained {row.dense_retained:.4f} -> "
            f"{row.retained:.4f}   |   every-active coverage "
            f"{row.dense_active_coverage:.4f} -> {row.active_coverage:.4f} "
            f"(n={row.active_n})   {row.reserved_cases} cases seated"
        )
    print(
        "  coverage counts EVERY visible active, including ones a topic shift "
        "should drop,\n  so it overstates harm; retained is the workflow-continuity "
        "number."
    )

    print("\nby category at the shipped k (hits/n, cases with a required runbook):")
    cats = sorted({c for row in rows for c in row.by_category})
    print(f"  {'category':22} " + "  ".join(f"{s:>9d}" for s in meta["sizes"]))
    for cat in cats:
        cells = []
        for size in meta["sizes"]:
            match = [r for r in rows if r.size == size and r.k == shipped]
            cell = match[0].by_category.get(cat) if match else None
            cells.append("        —" if not cell else f"{cell[0]:5d}/{cell[1]:<3d}")
        print(f"  {cat:22} " + "  ".join(cells))

    print("\nmisses at each k, by size (counts, because one case is 0.6 points):")
    for size in meta["sizes"]:
        cells = []
        for k in meta["ks"]:
            match = [r for r in rows if r.size == size and r.k == k]
            cells.append(
                f"k={k}:{match[0].all_required_n - match[0].all_required_hits}"
                if match else f"k={k}:—"
            )
        print(f"  {size:5d} runbooks   " + "   ".join(cells))

    print(f"\nembedding cost: {meta['embed_seconds']}")


#: Catalog size the CI smoke runs at.
#:
#: 200 rather than 1,000: the full sweep spends ~220s embedding a thousand runbook
#: documents, which is not a per-push cost, while 200 costs ~20s and is where the
#: measured numbers say the interesting things start.
SMOKE_SIZE = 200

#: The two floors are DIFFERENT KINDS OF CLAIM and are gated differently.
#:
#: ``active_coverage`` is a construction guarantee: ``candidates.py`` seats every
#: selectable active runbook whatever the cosines say, so it holds for any ranking,
#: any kernel, any architecture. Exact, and a value below it is a bug in the
#: assembler rather than a bad day for the model.
SMOKE_ACTIVE_FLOOR = 1.0
#:
#: ``all_required`` is a MEASUREMENT, and the margin behind it is thin enough to
#: matter. At 200 runbooks the deepest required runbook is ``daintree.foundation`` at
#: rank 26 of 200 under int8 — five positions inside ``k=32`` — and at rank 31
#: under fp32, which is the last slot there is. This repository has a documented
#: history of int8 decisions differing between arm64 and x86-64 on the SAME
#: artifact, so an exact 1.0 would be a gate tuned to one machine's kernel.
#:
#: Expressed as a MISS BUDGET rather than a rate, because the rate's denominator
#: is 158 target-bearing cases and a rate floor silently changes meaning when the
#: corpus grows. One miss allowed: enough to survive a rank moving a few
#: positions on a different runner, and still red for a real regression — the
#: same measurement is 2 misses at 500 runbooks and 3 at 1,000.
SMOKE_MISS_BUDGET = 1


def smoke(*, artifact: Path | None = None) -> dict[str, Any]:
    """One size, one k — the scale check cheap enough to run on every push."""
    doc = sweep(
        sizes=(SMOKE_SIZE,),
        ks=(SetwiseClassifier(artifact or ARTIFACT).candidates,),
        artifact=artifact,
        log=lambda *_: None,
    )
    row = doc["rows"][0]
    misses = row["all_required_n"] - row["all_required_hits"]
    failures = []
    if misses > SMOKE_MISS_BUDGET:
        failures.append(
            f"{misses} required runbooks did not reach the head at {SMOKE_SIZE} "
            f"runbooks (budget {SMOKE_MISS_BUDGET} of {row['all_required_n']} "
            f"cases) — a candidate miss is the one failure no downstream floor "
            f"can repair"
        )
    if row["active_coverage"] < SMOKE_ACTIVE_FLOOR:
        failures.append(
            f"active_coverage={row['active_coverage']} at {SMOKE_SIZE} runbooks, "
            f"expected {SMOKE_ACTIVE_FLOOR} — candidate reservation is meant to "
            f"make this exact by construction, so this is an assembler bug"
        )
    doc["summary"] = {
        "size": SMOKE_SIZE,
        "k": row["k"],
        "all_required": row["all_required"],
        "candidate_misses": misses,
        "miss_budget": SMOKE_MISS_BUDGET,
        "active_coverage": row["active_coverage"],
        "retained": row["retained"],
        "dense_retained": row["dense_retained"],
        "reserved_cases": row["reserved_cases"],
        "ballot_share": row["ballot_share"],
        "passed": not failures,
        "quality_failures": failures,
    }
    return doc


def recommend_k(doc: dict[str, Any], *, size: int, max_misses: int = 0) -> int | None:
    """Smallest swept ``k`` with at most ``max_misses`` at ``size``, or ``None``.

    A MISS BUDGET rather than a rate floor. The old signature took ``0.995``,
    which over 158 target-bearing cases is arithmetically "zero misses" while
    reading like a tolerance — and it compared against an already-rounded rate.
    An integer says what it means and does not change meaning when the corpus
    grows.

    A recommendation, not a decision: the head's ``k`` is baked into the exported
    graph, so changing it is a retrain plus a floor recalibration. What this
    answers is whether the current width has margin at a given catalog size,
    which is the question that should precede that spend rather than follow it.
    """
    clearing = [
        r["k"]
        for r in doc["rows"]
        if r["size"] == size and (r["all_required_n"] - r["all_required_hits"]) <= max_misses
    ]
    return min(clearing) if clearing else None


def latency_note(doc: dict[str, Any]) -> str:
    sizes = doc["meta"]["embed_seconds"]
    if not sizes:
        return ""
    return (
        "cold-start worst case: re-embedding the whole catalog costs "
        + ", ".join(f"{s} runbooks = {t}s" for s, t in sorted(sizes.items()))
        + f" (mean {statistics.mean(sizes.values()):.1f}s)"
    )
