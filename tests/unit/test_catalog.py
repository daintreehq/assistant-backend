"""Skill catalog tests."""

from __future__ import annotations

import pytest

from daintree_assistant_server.skills.catalog import Catalog, default_skills_dir, load_catalog
from daintree_assistant_server.skills.loader import SkillLoadError, parse_skill

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


def test_selector_metadata_active_flag_and_no_body() -> None:
    catalog = _catalog()
    meta = catalog.selector_metadata(["a.one"])
    by_id = {m["id"]: m for m in meta}
    assert by_id["a.one"]["active"] is True
    assert by_id["b.two"]["active"] is False
    assert "body" not in by_id["a.one"]
    # The selector now sees risk + required_tools (to avoid over-selecting risky skills).
    assert "risk" in by_id["a.one"]
    assert "required_tools" in by_id["a.one"]


def test_public_list_includes_required_tools_no_body() -> None:
    catalog = _catalog()
    entries = catalog.public_list()
    assert all("body" not in e for e in entries)
    assert all("required_tools" in e for e in entries)


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
