"""Skill catalog tests."""

from __future__ import annotations

import pytest

from daintree_assistant_server.skills.catalog import Catalog, load_catalog
from daintree_assistant_server.skills.loader import SkillLoadError, parse_skill

SKILL_A = """---
id: a.one
title: One
version: 1.0.0
summary: first
whenToUse: when one
risk: read
---
body one
"""

SKILL_B = """---
id: b.two
title: Two
version: 2.0.0
summary: second
whenToUse: when two
risk: project
---
body two
"""


def _catalog() -> Catalog:
    return Catalog([parse_skill(SKILL_A, expected_id="a.one"), parse_skill(SKILL_B, expected_id="b.two")])


def test_real_catalog_loads_seven() -> None:
    catalog = load_catalog()
    assert len(catalog) == 7
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


def test_selector_metadata_active_flag_and_no_body() -> None:
    catalog = _catalog()
    meta = catalog.selector_metadata(["a.one"])
    by_id = {m["id"]: m for m in meta}
    assert by_id["a.one"]["active"] is True
    assert by_id["b.two"]["active"] is False
    assert "body" not in by_id["a.one"]
    assert "required_tools" not in by_id["a.one"]


def test_public_list_includes_required_tools_no_body() -> None:
    catalog = _catalog()
    entries = catalog.public_list()
    assert all("body" not in e for e in entries)
    assert all("required_tools" in e for e in entries)


def test_duplicate_id_rejected() -> None:
    skill = parse_skill(SKILL_A, expected_id="a.one")
    with pytest.raises(SkillLoadError, match="duplicate"):
        Catalog([skill, skill])


def test_revision_changes_with_version() -> None:
    base = _catalog().revision
    bumped = Catalog(
        [
            parse_skill(SKILL_A.replace("version: 1.0.0", "version: 1.0.1"), expected_id="a.one"),
            parse_skill(SKILL_B, expected_id="b.two"),
        ]
    ).revision
    assert base != bumped
