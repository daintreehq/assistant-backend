"""Which catalog sizes ``bench scale`` measures as the real catalog grows.

The default sweep was written at 38 runbooks and asked for 100; once the catalog
passed 100 the default refused to run at all. A size the catalog has outgrown now
measures the real catalog, and says so.
"""

from __future__ import annotations

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
