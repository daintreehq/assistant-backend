"""Skill catalog tests."""

from __future__ import annotations

import pytest

from daintree_assistant_server.skills.catalog import Catalog, default_skills_dir, load_catalog
from daintree_assistant_server.skills.loader import SkillLoadError, parse_skill
from daintree_assistant_server.skills.models import Skill

SKILL_A = """---
id: a.one
title: One
summary: first
whenToUse: when one
risk: read
---
body one
"""

SKILL_B = """---
id: b.two
title: Two
summary: second
whenToUse: when two
risk: project
---
body two
"""


def _catalog() -> Catalog:
    return Catalog([parse_skill(SKILL_A, expected_id="a.one"), parse_skill(SKILL_B, expected_id="b.two")])


def _skill(skill_id: str, *, foundation: bool = False) -> Skill:
    """A minimal purpose-built skill — used to test id-namespace behaviour without
    pinning tests to real catalog ids (the real catalog evolves)."""
    lines = ["---", f"id: {skill_id}", "title: T", "summary: s", "whenToUse: w"]
    if foundation:
        lines.append("foundation: true")
    lines += ["risk: read", "---", "body", ""]
    return parse_skill("\n".join(lines), expected_id=skill_id)


def test_real_catalog_loads_all_packaged_files() -> None:
    catalog = load_catalog()
    # Every packaged skill file loads (no skill silently dropped), whatever the
    # count — robust to the catalog growing as skill learning writes new skills.
    packaged = list(default_skills_dir().glob("*.md"))
    assert packaged, "no packaged skill files found"
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
    plain = parse_skill(SKILL_A, expected_id="a.one")
    assert plain.meta.foundation is False
    marked = parse_skill(
        SKILL_A.replace("risk: read", "foundation: true\nrisk: read"),
        expected_id="a.one",
    )
    assert marked.meta.foundation is True
    assert marked.selector_view()["foundation"] is True


def test_real_catalog_every_skill_resolves_to_a_foundation() -> None:
    # Structural, not id-hardcoded (the real catalog evolves): the point of the
    # namespace walk is that NO skill loads without a base judgment layer, so every
    # skill must resolve to some foundation above it.
    catalog = load_catalog()
    foundations = [s.id for s in catalog.skills if s.meta.foundation]
    assert foundations, "the real catalog has no foundation skill"
    unpaired = [s.id for s in catalog.skills if catalog.domain_foundation(s.id) is None]
    assert unpaired == []


def test_domain_foundation_pairs_specific_with_its_domain_foundation() -> None:
    catalog = Catalog([_skill("dom.area.base", foundation=True), _skill("dom.area.thing")])
    # A specific resolves to its domain foundation...
    assert catalog.domain_foundation("dom.area.thing") == "dom.area.base"
    # ...and the foundation resolves to itself (the caller dedups).
    assert catalog.domain_foundation("dom.area.base") == "dom.area.base"


def test_domain_foundation_walk_is_nearest_first() -> None:
    catalog = Catalog(
        [
            _skill("dom.base", foundation=True),  # domain key "dom"
            _skill("dom.area.narrow", foundation=True),  # domain key "dom.area"
            _skill("dom.area.thing"),
            _skill("dom.other.thing"),
            _skill("dom.deep.nested.thing"),
        ]
    )
    # The nearest ancestor wins: dom.area.* takes the dom.area foundation, not dom's.
    assert catalog.domain_foundation("dom.area.thing") == "dom.area.narrow"
    # A namespace with no foundation of its own walks up one level...
    assert catalog.domain_foundation("dom.other.thing") == "dom.base"
    # ...and keeps walking through several foundation-less levels.
    assert catalog.domain_foundation("dom.deep.nested.thing") == "dom.base"


def test_domain_foundation_none_when_no_ancestor_has_one() -> None:
    catalog = Catalog([_skill("dom.area.base", foundation=True), _skill("other.thing")])
    # The walk never crosses into an unrelated top-level namespace.
    assert catalog.domain_foundation("other.thing") is None


def test_two_foundations_in_one_domain_is_rejected() -> None:
    # There is no sensible tie-break. Picking the id-sorted first means the LOSER does
    # not even resolve to itself, so selecting it would admit BOTH foundations and burn
    # two slots of the active-set budget on one domain. Fail closed at load instead.
    with pytest.raises(SkillLoadError, match="two foundation skills"):
        Catalog([_skill("dom.zeta", foundation=True), _skill("dom.alpha", foundation=True)])


def test_one_foundation_per_domain_across_distinct_domains_is_fine() -> None:
    catalog = Catalog(
        [
            _skill("dom.alpha", foundation=True),
            _skill("other.base", foundation=True),
            _skill("dom.thing"),
            _skill("other.thing"),
        ]
    )
    assert catalog.domain_foundation("dom.thing") == "dom.alpha"
    assert catalog.domain_foundation("other.thing") == "other.base"


def test_public_list_includes_required_tools_no_body() -> None:
    catalog = _catalog()
    entries = catalog.public_list()
    assert all("body" not in e for e in entries)
    assert all("required_tools" in e for e in entries)
    # foundation is a selector-only concern; it must not leak to the public wire.
    assert all("foundation" not in e for e in entries)
    # priority is gone from the schema entirely — it must not reappear on the wire.
    assert all("priority" not in e for e in entries)


def test_duplicate_id_rejected() -> None:
    skill = parse_skill(SKILL_A, expected_id="a.one")
    with pytest.raises(SkillLoadError, match="duplicate"):
        Catalog([skill, skill])


def test_revision_changes_with_content() -> None:
    base = _catalog().revision
    changed = Catalog(
        [
            parse_skill(SKILL_A.replace("body one", "body one, edited"), expected_id="a.one"),
            parse_skill(SKILL_B, expected_id="b.two"),
        ]
    ).revision
    assert base != changed
