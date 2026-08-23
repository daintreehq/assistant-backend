"""Runbook loader and validation tests."""

from __future__ import annotations

import pytest

from daintree_assistant_server.runbooks.loader import (
    RunbookLoadError,
    load_runbook_file,
    parse_runbook,
)

VALID = """---
id: test.example
title: Example
summary: A test runbook.
whenToUse: Use when testing.
risk: read
requiredTools:
  - git.status
  - agentTask.spawnForEdits
---

# Procedure

Do the thing.
"""


def test_parse_valid_runbook() -> None:
    runbook = parse_runbook(VALID, expected_id="test.example")
    assert runbook.id == "test.example"
    assert runbook.title == "Example"
    assert runbook.when_to_use == "Use when testing."
    assert runbook.risk == "read"
    assert runbook.body.startswith("# Procedure")
    assert runbook.content_hash.startswith("sha256:")


def test_filename_mismatch_rejected() -> None:
    with pytest.raises(RunbookLoadError, match="does not match filename"):
        parse_runbook(VALID, expected_id="wrong.id")


def test_missing_opening_delimiter() -> None:
    with pytest.raises(RunbookLoadError, match="opening frontmatter"):
        parse_runbook("id: x\n---\nbody")


def test_missing_closing_delimiter() -> None:
    with pytest.raises(RunbookLoadError, match="closing frontmatter"):
        parse_runbook("---\nid: x\nbody without close")


def test_empty_body_rejected() -> None:
    text = "---\nid: test.example\ntitle: T\nsummary: s\nwhenToUse: w\n---\n"
    with pytest.raises(RunbookLoadError, match="body is empty"):
        parse_runbook(text)


def test_invalid_risk_rejected() -> None:
    bad = VALID.replace("risk: read", "risk: nuclear")
    with pytest.raises(RunbookLoadError):
        parse_runbook(bad, expected_id="test.example")


def test_tags_key_rejected() -> None:
    # `tags` was removed from the schema for the same reason as `priority`: once
    # selector_view() narrowed to {id, summary, when_to_use, foundation} nothing read
    # it. extra="forbid" must now reject it, so a stale runbook file fails loudly at
    # load (a /readyz failure) instead of silently carrying dead metadata forward.
    bad = VALID.replace("risk: read", "tags:\n  - alpha\nrisk: read")
    with pytest.raises(RunbookLoadError):
        parse_runbook(bad, expected_id="test.example")


def test_unknown_frontmatter_key_rejected() -> None:
    bad = VALID.replace("risk: read", "risk: read\nsecret_field: oops")
    with pytest.raises(RunbookLoadError):
        parse_runbook(bad, expected_id="test.example")


def test_priority_key_rejected() -> None:
    # `priority` was removed from the schema (nothing read it). extra="forbid" must
    # now reject it, so a stale runbook file fails loudly instead of silently carrying
    # dead metadata forward.
    bad = VALID.replace("risk: read", "priority: 50\nrisk: read")
    with pytest.raises(RunbookLoadError):
        parse_runbook(bad, expected_id="test.example")


def test_content_hash_is_deterministic() -> None:
    a = parse_runbook(VALID, expected_id="test.example")
    b = parse_runbook(VALID, expected_id="test.example")
    assert a.content_hash == b.content_hash


def test_content_hash_changes_with_body() -> None:
    a = parse_runbook(VALID, expected_id="test.example")
    b = parse_runbook(VALID + "\nMore text.", expected_id="test.example")
    assert a.content_hash != b.content_hash


def test_crlf_normalized_before_hashing() -> None:
    lf = parse_runbook(VALID, expected_id="test.example")
    crlf = parse_runbook(VALID.replace("\n", "\r\n"), expected_id="test.example")
    assert lf.content_hash == crlf.content_hash
    assert "\r" not in crlf.body


def test_nul_bytes_rejected(tmp_path) -> None:
    path = tmp_path / "test.example.md"
    path.write_bytes(b"---\nid: test.example\n---\nbody\x00")
    with pytest.raises(RunbookLoadError, match="NUL"):
        load_runbook_file(path)


def test_load_runbook_file_uses_filename_as_id(tmp_path) -> None:
    path = tmp_path / "test.example.md"
    path.write_text(VALID, encoding="utf-8")
    runbook = load_runbook_file(path)
    assert runbook.id == "test.example"


def test_a_yaml_scalar_that_pyyaml_cannot_construct_is_a_runbook_error() -> None:
    """PyYAML does not route every failure through ``yaml.YAMLError``.

    An unquoted impossible date is constructed, not parsed, so it surfaces as a
    bare ``ValueError``. Left uncaught it escapes this module entirely and an
    ordinary typo reads as a server fault rather than "your frontmatter is
    wrong" — which is the whole promise of loading strictly.
    """
    text = VALID.replace("title: Example", "title: 2025-02-29")
    with pytest.raises(RunbookLoadError, match="invalid YAML frontmatter"):
        parse_runbook(text)


def test_a_recursion_error_from_the_parser_is_a_runbook_error(monkeypatch) -> None:
    """The other way PyYAML escapes its own error type.

    Deeply nested frontmatter exhausts the parser's recursion before it can
    raise anything of its own, and a RecursionError reaching the reload endpoint
    is a 500 for what is still just a bad file. Forced rather than nested by
    hand: the depth that actually trips CPython is a moving target, and a test
    that only fails on some builds is not a test.
    """
    def boom(_text: str) -> object:
        raise RecursionError("maximum recursion depth exceeded")

    monkeypatch.setattr("daintree_assistant_server.runbooks.loader.yaml.safe_load", boom)
    with pytest.raises(RunbookLoadError, match="invalid YAML frontmatter"):
        parse_runbook(VALID)
