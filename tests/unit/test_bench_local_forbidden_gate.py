"""``bench local`` fails a run that loads a runbook a case forbids, whatever its rates.

A forbidden load already fails that case's recall, and recall is gated as a rate,
so one named wrong-workflow boundary fits under every floor. The document is built
by the runner's own ``build_local_doc`` rather than written out here, so a renamed
summary key cannot leave the gate and its test agreeing with each other.
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from benchmarks.runbook_selection import quality
from benchmarks.runbook_selection.cases import BenchCase, load_cases
from benchmarks.runbook_selection.local_runner import build_local_doc
from benchmarks.runbook_selection.quality import runbook_selection_local_verdict
from benchmarks.runbook_selection.scoring import score_trial
from daintree_assistant_server.runbooks.catalog import load_catalog


def _case(cid: str, *, forbid: tuple[str, ...] = (), docs: bool | None = None) -> BenchCase:
    return BenchCase(
        id=cid,
        category="routing-trap",
        source="test",
        messages=[],
        active=[],
        required=["daintree.a"],
        optional=["daintree.neighbour"],
        turn_context=None,
        available_tools=[],
        note="",
        expect_docs=docs,
        forbid=forbid,
    )


def _doc(loads: dict[str, list[str]], forbid: dict[str, tuple[str, ...]]) -> dict[str, Any]:
    """Twenty cases that each load their required runbook, plus what ``loads`` adds."""
    cases = [
        _case(f"c{i}", forbid=forbid.get(f"c{i}", ()), docs=True if i == 0 else None)
        for i in range(20)
    ]
    picked = [["daintree.a", *loads.get(c.id, [])] for c in cases]
    results = [
        score_trial(
            case, 0, p, degraded=False, latency_s=0.001, confidence=None, task_type=None,
            reason=None, prompt_tokens=None, completion_tokens=None, docs_query="q",
        )
        for case, p in zip(cases, picked, strict=True)
    ]
    catalog: Any = SimpleNamespace(revision="test", selectable_ids=["daintree.a"])
    return build_local_doc(
        cases=cases, whole_corpus=cases, results=results, losses=[],
        spans={"total_ms": [1.0]}, picked=picked, reserved_cases=0,
        artifact=Path("artifact"), catalog=catalog, candidates=32,
    )


def test_the_baseline_run_passes() -> None:
    """Without this the forbidden case below could fail for any reason at all."""
    verdict = runbook_selection_local_verdict(_doc({}, {}))
    assert verdict.passed, verdict.failures


def test_one_forbidden_load_fails_a_run_whose_rates_all_pass() -> None:
    doc = _doc({"c3": ["daintree.wrong"]}, {"c3": ("daintree.wrong",)})
    assert doc["summary"]["recall_pass_cases"] == 19  # 0.95, above its floor
    verdict = runbook_selection_local_verdict(doc)
    assert verdict.failures == (
        "forbidden_cases=1, 1 not in KNOWN_FORBIDDEN_LOADS, expected 0",
        "forbidden load: c3 selected daintree.wrong",
    )


def test_a_known_forbidden_load_passes_and_any_other_still_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The known list exempts that case loading that runbook, and nothing wider."""
    monkeypatch.setattr(quality, "KNOWN_FORBIDDEN_LOADS", {"c3": frozenset({"daintree.wrong"})})
    forbid = {"c3": ("daintree.wrong", "daintree.worse"), "c4": ("daintree.wrong",)}
    assert runbook_selection_local_verdict(_doc({"c3": ["daintree.wrong"]}, forbid)).passed

    other_runbook = _doc({"c3": ["daintree.worse"]}, forbid)
    assert runbook_selection_local_verdict(other_runbook).failures == (
        "forbidden_cases=1, 1 not in KNOWN_FORBIDDEN_LOADS, expected 0",
        "forbidden load: c3 selected daintree.worse",
    )

    other_case = _doc({"c3": ["daintree.wrong"], "c4": ["daintree.wrong"]}, forbid)
    assert runbook_selection_local_verdict(other_case).failures == (
        "forbidden_cases=2, 1 not in KNOWN_FORBIDDEN_LOADS, expected 0",
        "forbidden load: c4 selected daintree.wrong",
    )


def test_a_known_entry_whose_case_passes_fails_until_removed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The list only shrinks: a case won back must leave it, or it exempts a regression."""
    monkeypatch.setattr(
        quality,
        "KNOWN_FORBIDDEN_LOADS",
        {"c3": frozenset({"daintree.wrong"}), "not-in-this-run": frozenset({"daintree.x"})},
    )
    verdict = runbook_selection_local_verdict(_doc({}, {"c3": ("daintree.wrong",)}))
    assert verdict.failures == (
        "c3 no longer loads a forbidden runbook: remove it from KNOWN_FORBIDDEN_LOADS",
    )


def test_every_known_forbidden_load_names_a_real_case_and_its_forbidden_runbook() -> None:
    """An entry for a renamed case, or a runbook the case does not forbid, exempts nothing."""
    cases = {c.id: c for c in load_cases(load_catalog())}
    for cid, runbooks in quality.KNOWN_FORBIDDEN_LOADS.items():
        assert cid in cases, f"{cid} is not a benchmark case"
        assert runbooks <= set(cases[cid].forbid), f"{cid} does not forbid {sorted(runbooks)}"


def test_an_allowed_or_unforbidden_extra_keeps_its_current_contract() -> None:
    """An optional neighbour, and an extra the case does not forbid, are not exclusions."""
    doc = _doc({"c3": ["daintree.neighbour"], "c4": ["daintree.other"]}, {})
    assert runbook_selection_local_verdict(doc).passed


def test_a_missing_count_fails_rather_than_reading_as_zero() -> None:
    doc = _doc({}, {})
    del doc["summary"]["forbidden_cases"]
    failures = runbook_selection_local_verdict(doc).failures
    assert any(f.startswith("forbidden_cases is missing") for f in failures)


def test_a_missing_or_misaligned_per_case_list_fails() -> None:
    doc = _doc({}, {})
    del doc["forbidden"]
    assert "forbidden or case_ids is missing" in runbook_selection_local_verdict(doc).failures[0]

    doc = _doc({}, {})
    doc["forbidden"] = doc["forbidden"][:-1]
    assert "19 entries for 20 cases" in runbook_selection_local_verdict(doc).failures[0]


def test_a_count_that_disagrees_with_the_cases_fails() -> None:
    doc = _doc({"c3": ["daintree.wrong"]}, {"c3": ("daintree.wrong",)})
    doc["summary"]["forbidden_cases"] = 0
    failures = runbook_selection_local_verdict(doc).failures
    assert "forbidden_cases=0 but 1 cases list a forbidden load" in failures[0]
