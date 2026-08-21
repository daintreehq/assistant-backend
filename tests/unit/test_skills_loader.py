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


def test_tags_key_rejected() -> None:
    # `tags` was removed from the schema for the same reason as `priority`: once
    # selector_view() narrowed to {id, summary, when_to_use, foundation} nothing read
    # it. extra="forbid" must now reject it, so a stale skill file fails loudly at
    # load (a /readyz failure) instead of silently carrying dead metadata forward.
    bad = VALID.replace("risk: read", "tags:\n  - alpha\nrisk: read")
    with pytest.raises(SkillLoadError):
        parse_skill(bad, expected_id="test.example")


def test_unknown_frontmatter_key_rejected() -> None:
    bad = VALID.replace("risk: read", "risk: read\nsecret_field: oops")
    with pytest.raises(SkillLoadError):
        parse_skill(bad, expected_id="test.example")


def test_priority_key_rejected() -> None:
    # `priority` was removed from the schema (nothing read it). extra="forbid" must
    # now reject it, so a stale skill file fails loudly instead of silently carrying
    # dead metadata forward.
    bad = VALID.replace("risk: read", "priority: 50\nrisk: read")
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


def test_a_yaml_scalar_that_pyyaml_cannot_construct_is_a_skill_error() -> None:
    """PyYAML does not route every failure through ``yaml.YAMLError``.

    An unquoted impossible date is constructed, not parsed, so it surfaces as a
    bare ``ValueError``. Left uncaught it escapes this module entirely and an
    ordinary typo reads as a server fault rather than "your frontmatter is
    wrong" — which is the whole promise of loading strictly.
    """
    text = VALID.replace("title: Example", "title: 2025-02-29")
    with pytest.raises(SkillLoadError, match="invalid YAML frontmatter"):
        parse_skill(text)


def test_a_recursion_error_from_the_parser_is_a_skill_error(monkeypatch) -> None:
    """The other way PyYAML escapes its own error type.

    Deeply nested frontmatter exhausts the parser's recursion before it can
    raise anything of its own, and a RecursionError reaching the reload endpoint
    is a 500 for what is still just a bad file. Forced rather than nested by
    hand: the depth that actually trips CPython is a moving target, and a test
    that only fails on some builds is not a test.
    """
    def boom(_text: str) -> object:
        raise RecursionError("maximum recursion depth exceeded")

    monkeypatch.setattr("daintree_assistant_server.skills.loader.yaml.safe_load", boom)
    with pytest.raises(SkillLoadError, match="invalid YAML frontmatter"):
        parse_skill(VALID)
