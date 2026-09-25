"""Two training-side properties the selector's retrain depends on.

``stable_split`` — the train/val split used to be a seeded shuffle of the whole
corpus, so adding 63 rows reassigned ~1,020 existing ones and a 0.6% data change
behaved like a new seed. A content-keyed split must leave every existing row where
it was.

``canonical_active`` — the server pairs every specific with its domain foundation
the turn it is selected, and the selector sees that whole set. A training row whose
active set lacks the foundation describes a state production does not produce, so
training renders active sets through the server's own merge.
"""

from __future__ import annotations

from daintree_assistant_server.runbooks.active_set import merge_active_runbooks
from daintree_assistant_server.runbooks.catalog import default_runbooks_dir, load_catalog
from training.common import canonical_active, stable_split

FOUNDATION = "daintree.foundation"


def _rows(n: int, prefix: str) -> list[dict]:
    return [
        {"query": f"{prefix} request {i}", "history": [], "active_ids": [], "expect_ids": []}
        for i in range(n)
    ]


def test_adding_rows_never_moves_an_existing_row_across_the_split() -> None:
    base = _rows(2_000, "old")
    train, val = stable_split(base, 0.12)
    grown_train, grown_val = stable_split(base + _rows(300, "new"), 0.12)
    key = lambda r: r["query"]  # noqa: E731
    assert {key(r) for r in train} <= {key(r) for r in grown_train}
    assert {key(r) for r in val} <= {key(r) for r in grown_val}
    # And it is a real split, near the requested fraction.
    assert 0.08 < len(val) / len(base) < 0.16


def test_the_split_ignores_row_order() -> None:
    base = _rows(500, "x")
    _, val_a = stable_split(base, 0.2)
    _, val_b = stable_split(list(reversed(base)), 0.2)
    assert {r["query"] for r in val_a} == {r["query"] for r in val_b}


def test_canonical_active_is_what_the_server_would_hold() -> None:
    catalog = load_catalog(default_runbooks_dir())
    specific = next(
        sid for sid in catalog.selectable_ids
        if sid != FOUNDATION and catalog.domain_foundation(sid) == FOUNDATION
    )
    assert canonical_active([specific], catalog) == [specific, FOUNDATION]
    assert canonical_active([FOUNDATION], catalog) == [FOUNDATION]
    assert canonical_active([], catalog) == []
    # Routed through the server's own merge, so the two cannot drift.
    assert (
        canonical_active([specific], catalog)
        == merge_active_runbooks([specific], [], catalog, 3).active
    )
