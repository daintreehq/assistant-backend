"""``bench local`` loss accounting: missing runbooks reconciled against stages.

Two units live in the summary and must stay apart. ``recall_miss_trials`` counts
trials with any miss and is the miss RATE's numerator; the stage histogram counts
missing RUNBOOKS. A trial that loses two required runbooks is one of the first and
two of the second, and a gate that compared the histogram with the trial count
went red on exactly that case (``global-continue-local-projects-and-remote-studios``)
with every loss correctly attributed.

These drive the real ``build_local_doc`` and its verdict, so a renamed summary key
fails here rather than agreeing with a hand-written copy forever.
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from typing import Any

from benchmarks.runbook_selection.cases import BenchCase
from benchmarks.runbook_selection.local_runner import StageLoss, build_local_doc
from benchmarks.runbook_selection.scoring import score_trial


def _case(case_id: str, required: list[str]) -> BenchCase:
    return BenchCase(
        id=case_id, category="routing-trap", source="t", messages=[], active=[],
        required=required, optional=[], turn_context=None, available_tools=[], note="",
    )


def _doc(
    picks: dict[str, tuple[list[str], list[str]]], losses: list[StageLoss]
) -> dict[str, Any]:
    """``picks`` maps a case id to (required, picked)."""
    cases = [_case(cid, required) for cid, (required, _) in picks.items()]
    results = [
        score_trial(
            case, 0, picks[case.id][1], degraded=False, latency_s=0.01, confidence=None,
            task_type=None, reason=None, prompt_tokens=None, completion_tokens=None,
        )
        for case in cases
    ]
    catalog: Any = SimpleNamespace(revision="r", selectable_ids=frozenset())
    return build_local_doc(
        cases=cases, whole_corpus=cases, results=results, losses=losses,
        spans={"total_ms": [1.0]}, picked=[picks[c.id][1] for c in cases],
        reserved_cases=0, artifact=Path("a"), catalog=catalog, candidates=32,
    )


def _accounting_failures(doc: dict[str, Any]) -> list[str]:
    return [
        f for f in doc["summary"]["quality_failures"]
        if "stage" in f or "reconcil" in f or "missing_runbooks" in f
    ]


def test_one_trial_losing_two_runbooks_to_different_stages_reconciles() -> None:
    doc = _doc(
        {"two-lost": (["a", "b"], []), "fine": (["c"], ["c"])},
        [
            StageLoss("two-lost", "a", "rank_miss", 0.1),
            StageLoss("two-lost", "b", "cap_full", 0.9),
        ],
    )
    summary = doc["summary"]
    assert summary["recall_miss_trials"] == 1
    assert summary["missing_runbooks"] == 2
    assert summary["stages"]["rank_miss"] == 1 and summary["stages"]["cap_full"] == 1
    assert summary["unreconciled_losses"] == []
    assert _accounting_failures(doc) == []


def test_background_reuse_losses_reconcile_like_any_other_stage() -> None:
    doc = _doc(
        {"wake": (["a", "b"], ["x"])},
        [
            StageLoss("wake", "a", "background_reuse", None),
            StageLoss("wake", "b", "background_reuse", None),
        ],
    )
    assert doc["summary"]["stages"]["background_reuse"] == 2
    assert _accounting_failures(doc) == []


def test_an_omitted_attribution_fails_the_gate() -> None:
    doc = _doc(
        {"two-lost": (["a", "b"], [])},
        [StageLoss("two-lost", "a", "rank_miss", 0.1)],
    )
    assert doc["summary"]["unreconciled_losses"] == [
        "two-lost/b: missed but attributed to no stage"
    ]
    failures = _accounting_failures(doc)
    assert any("stages sum to 1 but 2 required runbooks" in f for f in failures)
    assert any("two-lost/b" in f for f in failures)


def test_a_duplicated_attribution_fails_the_gate() -> None:
    doc = _doc(
        {"one-lost": (["a"], [])},
        [
            StageLoss("one-lost", "a", "rank_miss", 0.1),
            StageLoss("one-lost", "a", "floor_reject", 0.3),
        ],
    )
    assert doc["summary"]["unreconciled_losses"] == ["one-lost/a: attributed more than once"]
    assert any("stages sum to 2 but 1" in f for f in _accounting_failures(doc))


def test_an_omission_offset_by_a_duplicate_elsewhere_still_fails() -> None:
    """The totals agree; only the per-(case, runbook) match sees it."""
    doc = _doc(
        {"lost-one": (["a"], []), "lost-other": (["b"], [])},
        [
            StageLoss("lost-one", "a", "rank_miss", 0.1),
            StageLoss("lost-one", "a", "rank_miss", 0.1),
        ],
    )
    summary = doc["summary"]
    assert sum(summary["stages"].values()) == summary["missing_runbooks"] == 2
    assert summary["unreconciled_losses"] == [
        "lost-one/a: attributed more than once",
        "lost-other/b: missed but attributed to no stage",
    ]
    assert summary["passed"] is False


def test_a_loss_for_a_runbook_that_was_loaded_or_an_undeclared_stage_fails() -> None:
    doc = _doc(
        {"one-lost": (["a"], ["c"])},
        [
            StageLoss("one-lost", "a", "mystery", 0.1),
            StageLoss("one-lost", "c", "rank_miss", 0.1),
        ],
    )
    assert doc["summary"]["unreconciled_losses"] == [
        "one-lost/c: attributed but not missed",
        "one-lost/a: undeclared stage 'mystery'",
    ]


def test_a_candidate_miss_still_fails_however_the_accounting_lands() -> None:
    doc = _doc(
        {"two-lost": (["a", "b"], [])},
        [
            StageLoss("two-lost", "a", "candidate_miss", None),
            StageLoss("two-lost", "b", "rank_miss", 0.1),
        ],
    )
    summary = doc["summary"]
    assert summary["candidate_miss_runbooks"] == 1
    assert _accounting_failures(doc) == []
    assert any("candidate_miss_runbooks" in f for f in summary["quality_failures"])
