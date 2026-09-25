"""``bench compare`` over two ``bench local`` runs: aligned by case id, every move shown.

The point of the per-case diff is the trades a total hides, so each is pinned: a
fix and a break that net to zero, a loaded set that moved with no pass count
changing, and a case that exists on one side only.
"""

from __future__ import annotations

from typing import Any

import pytest

from benchmarks.runbook_selection.local_runner import print_local_compare


def _run(
    cases: dict[str, list[str]],
    losses: list[tuple[str, str, str]],
    failed: tuple[str, ...] = (),
) -> dict[str, Any]:
    lost = {case for case, _, _ in losses} | set(failed)
    return {
        "meta": {"benchmark": "runbook_selection_local"},
        "summary": {
            "recall_pass_cases": sum(1 for c in cases if c not in lost),
            "cases_passed": 0,
            "cases_total": len(cases),
            "candidate_miss_trials": sum(1 for _, _, s in losses if s == "candidate_miss"),
        },
        "case_ids": list(cases),
        "recall_pass": [c not in lost for c in cases],
        "picked": list(cases.values()),
        "losses": [{"case": c, "runbook": r, "stage": s, "p": 0.1} for c, r, s in losses],
    }


def test_trades_that_net_to_zero_are_both_listed(capsys: pytest.CaptureFixture[str]) -> None:
    a = _run(
        {"fix-me": ["x"], "break-me": ["y"], "swap": ["y", "z"], "same": ["y", "z"],
         "quiet": []},
        [("fix-me", "y", "rank_miss")],
    )
    b = _run(
        {"break-me": ["x"], "fix-me": ["y"], "swap": ["q"], "same": ["z", "y"],
         "quiet": ["y"], "tx-new": []},
        [("break-me", "y", "rank_miss"), ("tx-new", "y", "candidate_miss")],
        failed=("quiet",),  # a no-task case that loaded something: no loss recorded
    )
    print_local_compare(a, b)
    out = capsys.readouterr().out
    assert "recall fixed in B (1): fix-me" in out
    assert "recall broken in B (2): break-me, quiet" in out
    assert "swap: ['y', 'z'] -> ['q']" in out
    assert "same:" not in out  # the same runbooks in another order is no change
    assert "only in B (1):" in out and "tx-new: []   lost: y (candidate_miss)" in out


def test_a_run_without_case_ids_is_refused_rather_than_aligned_by_position() -> None:
    old = _run({"a": ["x"]}, [])
    del old["case_ids"]
    with pytest.raises(ValueError, match="case ids"):
        print_local_compare(old, _run({"a": ["x"]}, []))
