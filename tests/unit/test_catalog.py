"""Runbook catalog tests."""

from __future__ import annotations

import pytest

from daintree_assistant_server.runbooks.catalog import Catalog, default_runbooks_dir, load_catalog
from daintree_assistant_server.runbooks.loader import RunbookLoadError, parse_runbook
from daintree_assistant_server.runbooks.models import Runbook

RUNBOOK_A = """---
id: a.one
title: One
summary: first
whenToUse: when one
risk: read
---
body one
"""

RUNBOOK_B = """---
id: b.two
title: Two
summary: second
whenToUse: when two
risk: project
---
body two
"""


def _catalog() -> Catalog:
    return Catalog([parse_runbook(RUNBOOK_A, expected_id="a.one"), parse_runbook(RUNBOOK_B, expected_id="b.two")])


def _runbook(runbook_id: str, *, foundation: bool = False) -> Runbook:
    """A minimal purpose-built runbook — used to test id-namespace behaviour without
    pinning tests to real catalog ids (the real catalog evolves)."""
    lines = ["---", f"id: {runbook_id}", "title: T", "summary: s", "whenToUse: w"]
    if foundation:
        lines.append("foundation: true")
    lines += ["risk: read", "---", "body", ""]
    return parse_runbook("\n".join(lines), expected_id=runbook_id)


def test_real_catalog_loads_all_packaged_files() -> None:
    catalog = load_catalog()
    # Every packaged runbook file loads (no runbook silently dropped), whatever the
    # count — robust to the catalog growing as runbook learning writes new runbooks.
    packaged = list(default_runbooks_dir().glob("*.md"))
    assert packaged, "no packaged runbook files found"
    assert len(catalog) == len(packaged)
    assert catalog.revision.startswith("sha256:")


def test_revision_is_stable_across_loads() -> None:
    assert load_catalog().revision == load_catalog().revision


def test_get_and_contains() -> None:
    catalog = _catalog()
    assert "a.one" in catalog
    assert catalog.get("a.one") is not None
    assert catalog.get("missing") is None


def test_get_many_skips_unknown_preserves_order() -> None:
    catalog = _catalog()
    got = catalog.get_many(["b.two", "missing", "a.one"])
    assert [s.id for s in got] == ["b.two", "a.one"]


def test_filter_known() -> None:
    catalog = _catalog()
    assert catalog.filter_known(["a.one", "nope"]) == ["a.one"]


def test_selector_metadata_is_request_invariant_and_minimal() -> None:
    catalog = _catalog()
    by_id = {m["id"]: m for m in catalog.selector_metadata()}
    # Exactly the fields the selector prompt renders AND instructs on.
    assert set(by_id["a.one"]) == {"id", "summary", "when_to_use", "foundation"}
    assert by_id["a.one"]["foundation"] is False
    # Nothing per-request may appear in the catalog block: the active set is rendered
    # once after it, so these lines stay byte-stable for the content-prefix cache.
    assert "active" not in by_id["a.one"]


def test_foundation_flag_defaults_false_and_parses_true() -> None:
    plain = parse_runbook(RUNBOOK_A, expected_id="a.one")
    assert plain.meta.foundation is False
    marked = parse_runbook(
        RUNBOOK_A.replace("risk: read", "foundation: true\nrisk: read"),
        expected_id="a.one",
    )
    assert marked.meta.foundation is True
    assert marked.selector_view()["foundation"] is True


def test_real_catalog_every_runbook_resolves_to_a_foundation() -> None:
    # Structural, not id-hardcoded (the real catalog evolves): the point of the
    # namespace walk is that NO runbook loads without a base judgment layer, so every
    # runbook must resolve to some foundation above it.
    catalog = load_catalog()
    foundations = [s.id for s in catalog.runbooks if s.meta.foundation]
    assert foundations, "the real catalog has no foundation runbook"
    unpaired = [s.id for s in catalog.runbooks if catalog.domain_foundation(s.id) is None]
    assert unpaired == []


def test_domain_foundation_pairs_specific_with_its_domain_foundation() -> None:
    catalog = Catalog([_runbook("dom.area.base", foundation=True), _runbook("dom.area.thing")])
    # A specific resolves to its domain foundation...
    assert catalog.domain_foundation("dom.area.thing") == "dom.area.base"
    # ...and the foundation resolves to itself (the caller dedups).
    assert catalog.domain_foundation("dom.area.base") == "dom.area.base"


def test_domain_foundation_walk_is_nearest_first() -> None:
    catalog = Catalog(
        [
            _runbook("dom.base", foundation=True),  # domain key "dom"
            _runbook("dom.area.narrow", foundation=True),  # domain key "dom.area"
            _runbook("dom.area.thing"),
            _runbook("dom.other.thing"),
            _runbook("dom.deep.nested.thing"),
        ]
    )
    # The nearest ancestor wins: dom.area.* takes the dom.area foundation, not dom's.
    assert catalog.domain_foundation("dom.area.thing") == "dom.area.narrow"
    # A namespace with no foundation of its own walks up one level...
    assert catalog.domain_foundation("dom.other.thing") == "dom.base"
    # ...and keeps walking through several foundation-less levels.
    assert catalog.domain_foundation("dom.deep.nested.thing") == "dom.base"


def test_domain_foundation_none_when_no_ancestor_has_one() -> None:
    catalog = Catalog([_runbook("dom.area.base", foundation=True), _runbook("other.thing")])
    # The walk never crosses into an unrelated top-level namespace.
    assert catalog.domain_foundation("other.thing") is None


def test_two_foundations_in_one_domain_is_rejected() -> None:
    # There is no sensible tie-break. Picking the id-sorted first means the LOSER does
    # not even resolve to itself, so selecting it would admit BOTH foundations and burn
    # two slots of the active-set budget on one domain. Fail closed at load instead.
    with pytest.raises(RunbookLoadError, match="two foundation runbooks"):
        Catalog([_runbook("dom.zeta", foundation=True), _runbook("dom.alpha", foundation=True)])


def test_one_foundation_per_domain_across_distinct_domains_is_fine() -> None:
    catalog = Catalog(
        [
            _runbook("dom.alpha", foundation=True),
            _runbook("other.base", foundation=True),
            _runbook("dom.thing"),
            _runbook("other.thing"),
        ]
    )
    assert catalog.domain_foundation("dom.thing") == "dom.alpha"
    assert catalog.domain_foundation("other.thing") == "other.base"


def test_duplicate_id_rejected() -> None:
    runbook = parse_runbook(RUNBOOK_A, expected_id="a.one")
    with pytest.raises(RunbookLoadError, match="duplicate"):
        Catalog([runbook, runbook])


def test_revision_changes_with_content() -> None:
    base = _catalog().revision
    changed = Catalog(
        [
            parse_runbook(RUNBOOK_A.replace("body one", "body one, edited"), expected_id="a.one"),
            parse_runbook(RUNBOOK_B, expected_id="b.two"),
        ]
    ).revision
    assert base != changed
