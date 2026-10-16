"""Which catalog sizes ``bench scale`` measures as the real catalog grows.

The default sweep was written at 38 runbooks and asked for 100; once the catalog
passed 100 the default refused to run at all. A size the catalog has outgrown now
measures the real catalog, and says so.
"""

from __future__ import annotations

import pytest

from benchmarks.runbook_selection import scale
from benchmarks.runbook_selection.scale import DEFAULT_SIZES, effective_sizes


def test_the_default_sweep_runs_past_its_first_size() -> None:
    sizes, raised = effective_sizes(DEFAULT_SIZES, 154)
    assert sizes == (154, 200, 500, 1000)
    assert raised == [100]


def test_zero_is_the_real_catalog_and_is_not_reported_as_raised() -> None:
    assert effective_sizes((0,), 154) == ((154,), [])


def test_a_catalog_past_every_size_still_measures_itself() -> None:
    assert effective_sizes(DEFAULT_SIZES, 1200) == ((1200,), [100, 200, 500, 1000])


def test_sizes_at_or_above_the_catalog_pass_through_sorted_and_deduplicated() -> None:
    assert effective_sizes((500, 154, 200, 500), 154) == ((154, 200, 500), [])


def test_smoke_reports_the_size_it_measured_once_the_catalog_outgrows_it(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Past 200 real runbooks the smoke row is the real catalog, and must say so."""

    class _Classifier:
        candidates = 32

        def __init__(self, *_: object) -> None:
            pass

    row = {
        "size": 201,
        "k": 32,
        "all_required": 0.7,
        "all_required_hits": 7,
        "all_required_n": 10,
        "active_coverage": 1.0,
        "retained": 1.0,
        "dense_retained": 1.0,
        "reserved_cases": 0,
        "ballot_share": 0.16,
    }
    monkeypatch.setattr(scale, "SetwiseClassifier", _Classifier)
    monkeypatch.setattr(scale, "sweep", lambda **_: {"rows": [row]})
    summary = scale.smoke()["summary"]
    assert summary["size"] == 201
    assert "at 201 runbooks" in summary["quality_failures"][0]
