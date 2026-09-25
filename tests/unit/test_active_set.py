"""Foundation-paired admission — the set a replayed conversation carries forward."""

from __future__ import annotations

from daintree_assistant_server.runbooks.active_set import merge_active_runbooks
from daintree_assistant_server.runbooks.catalog import Catalog
from daintree_assistant_server.runbooks.models import Runbook, RunbookMeta


def _runbook(rid: str, *, foundation: bool = False) -> Runbook:
    meta = RunbookMeta(
        id=rid, title=rid, summary="s", whenToUse="w", foundation=foundation
    )
    return Runbook(meta=meta, body="Body.\n\nReport back: done.")


CATALOG = Catalog(
    [
        _runbook("d.foundation", foundation=True),
        _runbook("d.one"),
        _runbook("d.two"),
        _runbook("d.three"),
    ]
)


def test_a_specific_is_admitted_with_its_foundation() -> None:
    result = merge_active_runbooks(["d.one"], [], CATALOG, 3)
    assert result.active == ["d.one", "d.foundation"]
    assert result.newly_loaded == ["d.one", "d.foundation"]
    assert result.dropped_over_cap == []


def test_new_ids_land_before_the_prior_set_and_the_foundation_is_paid_once() -> None:
    result = merge_active_runbooks(["d.two"], ["d.one", "d.foundation"], CATALOG, 3)
    assert result.active == ["d.two", "d.foundation", "d.one"]
    assert result.newly_loaded == ["d.two"]


def test_a_unit_that_does_not_fit_is_skipped_whole() -> None:
    # d.one + its foundation fill a cap of 2; d.two would need no foundation
    # slot but there is no room left at all.
    result = merge_active_runbooks(["d.one", "d.two"], [], CATALOG, 2)
    assert result.active == ["d.one", "d.foundation"]
    assert result.dropped_over_cap == ["d.two"]
    # At a cap of 1 even the first unit (specific + foundation) cannot fit.
    assert merge_active_runbooks(["d.one"], [], CATALOG, 1).active == []


def test_unknown_and_duplicate_ids_are_ignored() -> None:
    result = merge_active_runbooks(["nope", "d.one", "d.one"], [], CATALOG, 3)
    assert result.active == ["d.one", "d.foundation"]
    assert result.dropped_over_cap == []
