"""Skill loader and validation tests."""

from __future__ import annotations

import pytest

from daintree_assistant_server.skills.loader import (
    SkillLoadError,
    load_skill_file,
    parse_skill,
)

VALID = """---
id: test.example
title: Example
summary: A test skill.
whenToUse: Use when testing.
tags:
  - alpha
  - beta
priority: 50
risk: read
requiredTools:
  - git.status
  - agentTask.spawnForEdits
---

# Procedure

Do the thing.
"""


def test_parse_valid_skill() -> None:
    skill = parse_skill(VALID, expected_id="test.example")
    assert skill.id == "test.example"
    assert skill.title == "Example"
    assert skill.when_to_use == "Use when testing."
    assert skill.meta.tags == ["alpha", "beta"]
    assert skill.risk == "read"
    assert skill.body.startswith("# Procedure")
    assert skill.content_hash.startswith("sha256:")


def test_filename_mismatch_rejected() -> None:
    with pytest.raises(SkillLoadError, match="does not match filename"):
        parse_skill(VALID, expected_id="wrong.id")


def test_missing_opening_delimiter() -> None:
    with pytest.raises(SkillLoadError, match="opening frontmatter"):
        parse_skill("id: x\n---\nbody")


def test_missing_closing_delimiter() -> None:
    with pytest.raises(SkillLoadError, match="closing frontmatter"):
        parse_skill("---\nid: x\nbody without close")


def test_empty_body_rejected() -> None:
    text = "---\nid: test.example\ntitle: T\nsummary: s\nwhenToUse: w\n---\n"
    with pytest.raises(SkillLoadError, match="body is empty"):
        parse_skill(text)


def test_invalid_risk_rejected() -> None:
    bad = VALID.replace("risk: read", "risk: nuclear")
    with pytest.raises(SkillLoadError):
        parse_skill(bad, expected_id="test.example")


def test_duplicate_tags_rejected() -> None:
    bad = VALID.replace("  - beta", "  - alpha")
    with pytest.raises(SkillLoadError):
        parse_skill(bad, expected_id="test.example")


def test_unknown_frontmatter_key_rejected() -> None:
    bad = VALID.replace("priority: 50", "priority: 50\nsecret_field: oops")
    with pytest.raises(SkillLoadError):
        parse_skill(bad, expected_id="test.example")


def test_content_hash_is_deterministic() -> None:
    a = parse_skill(VALID, expected_id="test.example")
    b = parse_skill(VALID, expected_id="test.example")
    assert a.content_hash == b.content_hash


def test_content_hash_changes_with_body() -> None:
    a = parse_skill(VALID, expected_id="test.example")
    b = parse_skill(VALID + "\nMore text.", expected_id="test.example")
    assert a.content_hash != b.content_hash


def test_crlf_normalized_before_hashing() -> None:
    lf = parse_skill(VALID, expected_id="test.example")
    crlf = parse_skill(VALID.replace("\n", "\r\n"), expected_id="test.example")
    assert lf.content_hash == crlf.content_hash
    assert "\r" not in crlf.body


def test_nul_bytes_rejected(tmp_path) -> None:
    path = tmp_path / "test.example.md"
    path.write_bytes(b"---\nid: test.example\n---\nbody\x00")
    with pytest.raises(SkillLoadError, match="NUL"):
        load_skill_file(path)


def test_load_skill_file_uses_filename_as_id(tmp_path) -> None:
    path = tmp_path / "test.example.md"
    path.write_text(VALID, encoding="utf-8")
    skill = load_skill_file(path)
    assert skill.id == "test.example"
