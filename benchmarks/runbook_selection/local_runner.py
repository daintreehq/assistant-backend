"""The selector that actually ships, scored offline — and told WHERE it lost.

This module drives ``SetwiseClassifier`` from the shipped ONNX artifact under
the classifier's full decision procedure: the visible-active projection, the
structural background-wake rule, the earn-your-place admission floors, and the
bare-question docs pass. It costs nothing and needs no key.

## Stage decomposition, which is the actual new thing

A single end-to-end miss rate is the metric this suite most needs to stop
reporting alone. Four unrelated failures collapse into it, and they want four
different fixes:

    candidate_miss      never on the ballot          -> stage 1: k, evidence, retrieval
    rank_miss           scored, and scored too low    -> stage 2: the head, training data
    floor_reject        out-scored the cut, refused   -> calibration
    cardinality_short   the count head said fewer     -> the count head
    cap_full            the answer was already full   -> max_active_runbooks

At 38 runbooks against ``k=32`` the first of those is nearly unreachable, which is
exactly why it is worth counting now: the number that must stay at zero as the
catalog grows is the one nobody is currently able to see. ``Selection.ranked``
carries the whole ballot for this, so the difference between "absent" and "scored
0.02" is readable rather than inferred.

Timings are decomposed for the same reason (``Selection.timings``): sync,
retriever encode, dense search, ranker encode, head, docs pass. One aggregate
cannot separate a tokenizer regression from a catalog re-embed.

## The headline metric is RECALL-PASS, not the strict set

The paid suite grades a case as passed only when the answer is exactly right:
nothing missing AND nothing extra. That is the correct bar for the prompt
selector, which was asked for an exact set. It is the wrong bar here, and reusing
it would be measuring this selector against another one's contract.

The local selector deliberately buys recall with precision. Its ``BONUS_FLOOR``
admits a further runbook above 0.45 beyond whatever the cardinality head asked for,
on the reasoning that a turn which MISSES its runbook answers without it — the
worst outcome available — while a spare runbook costs some prompt bytes. So
extras are a designed behaviour, not a defect, and ``training/eval_two_stage.py``,
``EXPERIMENTS.md`` and ``CLAUDE.md`` all already report recall-pass as the product
metric. This suite reports both, gates on recall-pass, and keeps the strict count
beside it so a precision collapse is still visible.
"""

from __future__ import annotations

import os
import statistics
import time
from collections import Counter
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from daintree_assistant_server.runbooks.catalog import Catalog, default_runbooks_dir, load_catalog
from daintree_assistant_server.runbooks.local import setwise as S
from daintree_assistant_server.runbooks.local.documents import last_user_text, query_document
from daintree_assistant_server.runbooks.local.setwise import SetwiseClassifier

from .cases import BenchCase, load_cases
from .quality import attach_verdict
from .scoring import percentile, score_trial, summarize

ARTIFACT = (
    Path(__file__).resolve().parents[2]
    / "src" / "daintree_assistant_server" / "runbooks" / "local" / "artifacts" / "selector"
)

#: Cap on active runbooks, mirroring ``Settings.max_active_runbooks``. Read from the
#: default rather than the environment: a benchmark whose admission cap moved
#: with someone's ``.env`` would report a different number on every machine.
MAX_ACTIVE = 3

STAGES = (
    "candidate_miss",
    "rank_miss",
    "floor_reject",
    "cardinality_short",
    "cap_full",
    "background_reuse",
)

#: How an admission rejection maps onto the stage that should be worked on.
#:
#: Mostly one to one, with one deliberate exception. ``REJECTED_CARDINALITY``
#: is the EXECUTED cause whenever the count head's answer was already met — but
#: it is only the useful DIAGNOSIS when the runbook would have ridden along at a
#: higher count, i.e. when it clears the bonus floor. A runbook the head scored at
#: 0.001 on a turn the count head called empty is not being held back by the
#: count; both are agreeing that it does not belong. See ``_attribute``.
_STAGE_BY_REASON = {
    S.REJECTED_NEWCOMER_FLOOR: "floor_reject",
    S.REJECTED_CAP: "cap_full",
    S.REJECTED_BONUS_FLOOR: "rank_miss",
}


@dataclass(frozen=True)
class StageLoss:
    """One required runbook the turn did not load, and the stage that lost it."""

    case_id: str
    runbook_id: str
    stage: str
    #: The head's probability, or ``None`` when the runbook never reached the head.
    probability: float | None


def _attribute(
    case: BenchCase,
    missing: str,
    *,
    ranked: dict[str, float],
    rejected: dict[str, str],
) -> StageLoss:
    """Which stage lost ``missing``.

    ``rejected`` comes from ``admit_with_reasons`` — the routine that actually
    made the decision — rather than from a reconstruction of it here. An earlier
    version did reconstruct it and got two cases backwards, both in the direction
    that points at the wrong subsystem: with the count head answering zero,
    ``admit`` never evaluates a floor at all, and a candidate the bonus scan
    stopped short of at the cap was blamed on a floor it never reached.
    """
    probability = ranked.get(missing)
    if probability is None:
        # Absent, not ranked low. No floor and no better head recovers this.
        return StageLoss(case.id, missing, "candidate_miss", None)

    reason = rejected.get(missing)
    stage = _STAGE_BY_REASON.get(reason or "")
    if stage is None:
        # REJECTED_CARDINALITY, or a runbook that was somehow neither picked nor
        # rejected. Attributed to the count head only when the count is what
        # stood in its way; see ``_STAGE_BY_REASON``.
        stage = (
            "cardinality_short"
            if probability >= SetwiseClassifier.BONUS_FLOOR
            else "rank_miss"
        )
    return StageLoss(case.id, missing, stage, probability)


def run_local(
    *,
    artifact: Path | None = None,
    catalog: Catalog | None = None,
    cases: list[BenchCase] | None = None,
    quantized: bool = True,
    id_filter: str | None = None,
) -> dict[str, Any]:
    """Score the shipped artifact over the corpus. No API calls, no key."""
    artifact = artifact or ARTIFACT
    catalog = catalog if catalog is not None else load_catalog(default_runbooks_dir())
    # The UNFILTERED corpus is the expectation, whatever this run executes.
    whole = load_cases(catalog)
    cases = cases if cases is not None else load_cases(catalog, id_filter=id_filter)

    clf = SetwiseClassifier(artifact, quantized=quantized)
    clf.sync(catalog)
    selectable = catalog.selectable_ids

    results: list[Any] = []
    picked_all: list[list[str]] = []
    losses: list[StageLoss] = []
    spans: dict[str, list[float]] = {}
    reserved_cases = 0

    canonical = os.environ.get("DAINTREE_BENCH_CANONICAL_ACTIVE") == "1"
    for case in cases:
        if canonical and case.active:
            from dataclasses import replace as _replace

            from daintree_assistant_server.runbooks.active_set import merge_active_runbooks

            paired = merge_active_runbooks(list(case.active), [], catalog, MAX_ACTIVE)
            case = _replace(case, active=paired.active)
        eligible = [m for m in case.messages if m.role in ("user", "assistant", "tool")]
        started = time.perf_counter()
        # The background-wake rule is STRUCTURAL and lives in ``LocalSelector``,
        # not in the classifier — a turn with no new user instruction never
        # reaches the model at all. Reproduced rather than skipped, because a
        # suite that let those cases run would be scoring the classifier on
        # evidence production never sends it.
        if case.active and eligible and eligible[-1].role != "user":
            picked, docs_q, sel = list(case.active), None, None
        else:
            visible = [sid for sid in case.active if sid in selectable]
            sel = clf.select(
                query_document(case.messages, turn_context=case.turn_context),
                visible,
                # The catalog goes in on EVERY call, as it does in production
                # (`LocalSelector.select`). Syncing once up front and omitting it
                # here would leave `sync_ms` out of the timed path entirely — and
                # the per-turn digest comparison is precisely the span that stops
                # being free when a catalog edit or a profile alternation makes it
                # re-embed.
                catalog=catalog,
                max_active=MAX_ACTIVE,
                docs_query=last_user_text(case.messages),
                exclude=case.exclude,
            )
            picked = [sid for sid in sel.runbook_ids if sid in selectable]
            docs_q = "q" if sel.docs_wanted else None
        latency_s = time.perf_counter() - started
        picked_all.append(list(picked))

        missing = sorted(set(case.required) - set(picked))
        if sel is None:
            # A background wake never reaches the model: there is no new user
            # instruction, so the prior active set is reused verbatim. A required
            # runbook missing from such a case is a disagreement between the case
            # and that STRUCTURAL rule, not a model error — and dropping it here
            # (which an earlier version did) left the stage histogram silently
            # short of the miss count it is supposed to explain.
            losses.extend(
                StageLoss(case.id, sid, "background_reuse", None) for sid in missing
            )
        else:
            for name, value in sel.timings.items():
                spans.setdefault(name, []).append(value)
            reserved_cases += bool(sel.reserved_ids)
            ranked = dict(sel.ranked)
            losses.extend(
                _attribute(case, sid, ranked=ranked, rejected=sel.rejected)
                for sid in missing
            )

        results.append(
            score_trial(
                case, 0, picked,
                degraded=False, latency_s=latency_s, confidence=None,
                task_type=None, reason=None, prompt_tokens=None, completion_tokens=None,
                docs_query=docs_q,
            )
        )

    return build_local_doc(
        cases=cases,
        whole_corpus=whole,
        results=results,
        losses=losses,
        spans=spans,
        picked=picked_all,
        reserved_cases=reserved_cases,
        artifact=artifact,
        catalog=catalog,
        candidates=clf.candidates,
        quantized=quantized,
        id_filter=id_filter,
    )


def build_local_doc(
    *,
    cases: list[BenchCase],
    whole_corpus: list[BenchCase],
    results: list[Any],
    losses: list[StageLoss],
    spans: dict[str, list[float]],
    picked: list[list[str]],
    reserved_cases: int,
    artifact: Path,
    catalog: Catalog,
    candidates: int,
    quantized: bool = True,
    id_filter: str | None = None,
) -> dict[str, Any]:
    """Assemble the result document, expectations and verdict.

    Split out of :func:`run_local` so the gate tests can drive this arithmetic
    directly. That is not tidiness: the expectations below (``cases_expected``
    and friends) are what the verdict checks the run against, and a hand-written
    copy of them in a test would agree with a renamed key forever. The paid
    runner's ``build_results_doc`` exists for the same reason.

    ``whole_corpus`` is the UNFILTERED case list. Deriving the expectation from
    ``cases`` instead would let ``--filter one-case`` declare its own subset to
    be the corpus and pass for having covered it completely.
    """
    summary = summarize(cases, results)

    # Recall-pass: every required runbook present, and — only when the case
    # required nothing — no extras either. The product metric (see the module
    # docstring), computed here rather than in the shared ``summarize`` because
    # the paid suite grades a different contract and should keep doing so.
    by_category: dict[str, list[bool]] = {}
    recall_pass = 0
    recall_flags: list[bool] = []
    # Per case, what it loaded that it must NOT have (``BenchCase.forbid``: the wrong
    # end of the hub/lane pair, or named in ``expect.forbid``). Failing recall, not
    # merely counted as an extra: a forbidden runbook is delivered in full to a
    # caller that cannot follow it.
    forbidden_loads: list[list[str]] = []
    for case, got_ids in zip(cases, picked, strict=True):
        got = set(got_ids)
        wrong = sorted(got & set(case.forbid))
        forbidden_loads.append(wrong)
        ok = (
            not (set(case.required) - got)
            and (bool(case.required) or not (got - case.allowed))
            and not wrong
        )
        recall_pass += ok
        recall_flags.append(ok)
        by_category.setdefault(case.category, []).append(ok)
    summary["recall_pass_cases"] = recall_pass
    summary["forbidden_cases"] = sum(1 for w in forbidden_loads if w)
    summary["recall_pass_categories"] = {
        name: {"cases": len(flags), "passed": sum(flags)}
        for name, flags in sorted(by_category.items())
    }

    by_stage = Counter(loss.stage for loss in losses)
    summary["stages"] = {name: by_stage.get(name, 0) for name in STAGES}
    # The number this whole suite exists to make visible as the catalog grows.
    # It must be zero at 38 runbooks and it is the first thing to move at 500.
    summary["candidate_miss_trials"] = by_stage.get("candidate_miss", 0)
    summary["reserved_cases"] = reserved_cases
    summary["spans_ms"] = {
        name: {
            "p50": round(percentile(sorted(values), 50), 3),
            "p90": round(percentile(sorted(values), 90), 3),
            "max": round(max(values), 3),
            "mean": round(statistics.mean(values), 3),
        }
        for name, values in sorted(spans.items())
        if values
    }

    doc = {
        "suite": "runbook_selection_local",
        "meta": {
            "benchmark": "runbook_selection_local",
            "artifact": str(artifact),
            "precision": "int8" if quantized else "fp32",
            "catalog_revision": catalog.revision,
            "selectable_runbooks": len(catalog.selectable_ids),
            "candidates": candidates,
            "max_active": MAX_ACTIVE,
            "filter": id_filter,
            "cases_expected": len(whole_corpus),
            # One trial a case, deliberately: the local selector is
            # deterministic, so a second trial is a second copy of the first.
            "trials_expected": len(whole_corpus),
            "docs_trials_expected": sum(
                1 for c in whole_corpus if c.expect_docs is not None
            ),
            "categories_expected": sorted({c.category for c in whole_corpus}),
        },
        "summary": summary,
        # What each case actually loaded, in case order. Carried so the fp32/int8
        # decision diff in ``training/eval_two_stage.py`` can be taken over two
        # runs of this function rather than a second copy of the scoring loop.
        "picked": [list(p) for p in picked],
        # The case each `picked` entry belongs to. `compare` aligns two runs on
        # it: position alone breaks the moment a case file gains a case.
        "case_ids": [case.id for case in cases],
        # Per case, by the same rule as `recall_pass_cases`. Not derivable from
        # `losses`: a no-task case that loads anything fails recall and loses
        # no required runbook.
        "recall_pass": recall_flags,
        # What each case loaded that it was forbidden to, aligned to `case_ids`.
        "forbidden": forbidden_loads,
        "losses": [
            {
                "case": loss.case_id,
                "runbook": loss.runbook_id,
                "stage": loss.stage,
                "p": None if loss.probability is None else round(loss.probability, 4),
            }
            for loss in losses
        ],
    }
    return attach_verdict(doc, "runbook_selection_local")


def print_local(doc: dict[str, Any]) -> None:
    summary = doc["summary"]
    meta = doc["meta"]
    print(
        f"artifact {meta['precision']}  k={meta['candidates']}  "
        f"{meta['selectable_runbooks']} selectable runbooks  {meta['catalog_revision']}"
    )
    print(
        f"recall-pass {summary['recall_pass_cases']}/{summary['cases_total']}   "
        f"strict {summary['cases_passed']}/{summary['cases_total']}   "
        f"miss {summary['recall_miss_trials']}   extra {summary['precision_extra_trials']}   "
        f"docs {summary['docs']['correct_trials']}/{summary['docs']['scored_trials']}"
    )
    stages = summary["stages"]
    print("\nwhere required runbooks were lost:")
    for name in STAGES:
        print(f"  {name:20} {stages[name]:3d}")
    print(f"  {'reserved cases':20} {summary['reserved_cases']:3d}")

    print("\nper-stage latency (ms):")
    for name, block in summary["spans_ms"].items():
        print(
            f"  {name:16} p50 {block['p50']:7.2f}  p90 {block['p90']:7.2f}  "
            f"max {block['max']:7.2f}"
        )

    wrong = [
        (cid, ids) for cid, ids in zip(doc["case_ids"], doc.get("forbidden") or [], strict=False)
        if ids
    ]
    if wrong:
        print(
            f"\nforbidden loads ({len(wrong)}): wrong role or wrong owner, delivered in full "
            "to a caller that must not follow it"
        )
        for cid, ids in wrong:
            print(f"  {cid:42} {', '.join(ids)}")

    if doc["losses"]:
        print("\nlosses:")
        for loss in doc["losses"]:
            p = "  —  " if loss["p"] is None else f"{loss['p']:.3f}"
            print(f"  {loss['case']:42} {loss['stage']:18} p={p}  {loss['runbook']}")


def print_categories(doc: dict[str, Any]) -> None:
    strict = doc["summary"]["categories"]
    print("\nby category (recall-pass / strict):")
    for name, block in sorted(doc["summary"]["recall_pass_categories"].items()):
        tight = strict.get(name, {})
        print(
            f"  {name:22} {block['passed']:3d}/{block['cases']:3d}"
            f"   {tight.get('passed', 0):3d}/{tight.get('cases', 0):3d}"
        )


def _by_case(doc: dict[str, Any]) -> dict[str, tuple[list[str], list[str], bool]]:
    """case id -> (what it loaded, what it lost and where, whether it passed recall)."""
    ids, flags = doc.get("case_ids"), doc.get("recall_pass")
    if not isinstance(ids, list) or not isinstance(flags, list):
        raise ValueError("a run saved before bench local recorded case ids; re-run it")
    lost: dict[str, list[str]] = {}
    for loss in doc.get("losses") or []:
        lost.setdefault(loss["case"], []).append(f"{loss['runbook']} ({loss['stage']})")
    return {
        cid: (list(p), lost.get(cid, []), bool(ok))
        for cid, p, ok in zip(ids, doc.get("picked") or [], flags, strict=True)
    }


def print_local_compare(a: dict[str, Any], b: dict[str, Any]) -> None:
    """Two ``bench local`` runs, case by case.

    The totals can hide a trade: an edit that fixes one case and breaks another
    nets to zero, and a case that swaps an extra for a wrong pick moves no pass
    count at all. So every case whose loaded set moved is listed, with what each
    side lost.
    """
    ca, cb = _by_case(a), _by_case(b)
    sa, sb = a["summary"], b["summary"]
    print("\n== bench local: A vs B ==")
    for label, key in (("recall-pass", "recall_pass_cases"), ("strict", "cases_passed")):
        print(
            f"{label:14} A {sa[key]}/{sa['cases_total']}   B {sb[key]}/{sb['cases_total']}"
        )
    print(
        f"{'candidate_miss':14} A {sa['candidate_miss_trials']}   "
        f"B {sb['candidate_miss_trials']}"
    )
    common = sorted(set(ca) & set(cb))
    fixed = [c for c in common if cb[c][2] and not ca[c][2]]
    broke = [c for c in common if ca[c][2] and not cb[c][2]]
    print(f"recall fixed in B ({len(fixed)}): {', '.join(fixed) or '—'}")
    print(f"recall broken in B ({len(broke)}): {', '.join(broke) or '—'}")
    moved = [c for c in common if set(ca[c][0]) != set(cb[c][0])]
    print(f"loaded set changed ({len(moved)}):")
    for cid in moved:
        lost = f"   B lost: {', '.join(cb[cid][1])}" if cb[cid][1] else ""
        print(f"  {cid}: {ca[cid][0]} -> {cb[cid][0]}{lost}")
    sides = (("only in A", set(ca) - set(cb), ca), ("only in B", set(cb) - set(ca), cb))
    for label, only, side in sides:
        if only:
            print(f"{label} ({len(only)}):")
            for cid in sorted(only):
                lost = f"   lost: {', '.join(side[cid][1])}" if side[cid][1] else ""
                print(f"  {cid}: {side[cid][0]}{lost}")
