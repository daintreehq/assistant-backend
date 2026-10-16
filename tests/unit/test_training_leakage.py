from __future__ import annotations

import json

from training.audit_leakage import find_overlaps
from training.corpus_source import Example


def _example(query: str, history: tuple[str, ...] = ()) -> Example:
    return Example(
        query=query, history=history, expect=(), source="no_task",
        origin="shared/probe.yaml", run="probe",
    )


def test_screening_finds_query_and_history_without_disclosing_evaluation_text() -> None:
    examples = [
        _example("PROTECTED query!"),
        _example("Unrelated latest turn", ("assistant: Protected earlier turn.",)),
    ]
    overlaps = find_overlaps(examples, {"protected query", "protected earlier turn"})
    assert [(r["example_index"], r["field"], r["kind"]) for r in overlaps] == [
        (0, "query", "exact"), (1, "history[0]", "exact"),
    ]
    assert "protected" not in json.dumps(overlaps)


def test_near_screening_includes_history_and_does_not_flag_unrelated_prose() -> None:
    examples = [
        _example("Please rename the terminal tab now"),
        _example("A completely different question", (
            "user: Please rename the terminal tab now",
        )),
        _example("What is a distributed database?"),
    ]
    protected = {"please rename the terminal tab today"}
    assert find_overlaps(examples, protected) == []
    overlaps = find_overlaps(examples, protected, near=True)
    assert [(r["example_index"], r["field"]) for r in overlaps] == [
        (0, "query"), (1, "history[0]"),
    ]
    assert all(r["kind"] == "near" and r["similarity"] > 0.8 for r in overlaps)
