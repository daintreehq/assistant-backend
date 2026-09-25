"""The text helpers the routing benchmark and training corpus render with."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from daintree_assistant_server.contracts.openai import ChatMessage
from daintree_assistant_server.runbooks.catalog import load_catalog
from daintree_assistant_server.runbooks.local.documents import (
    last_user_text,
    routing_revision,
    runbook_document,
)


def test_last_user_text_is_the_newest_user_message_whitespace_collapsed() -> None:
    messages = [
        ChatMessage(role="user", content="first"),
        ChatMessage(role="assistant", content="reply"),
        ChatMessage(role="user", content="  rename   my\nterminals "),
        ChatMessage(role="tool", content="output", tool_call_id="c1"),
    ]
    assert last_user_text(messages) == "rename my terminals"


def test_last_user_text_reads_every_content_part_shape() -> None:
    parts = ["plain", {"type": "text", "text": "dict"}, SimpleNamespace(text="typed"), 7]
    message = SimpleNamespace(role="user", content=parts)
    assert last_user_text([message]) == "plain dict typed"


def test_last_user_text_is_none_without_a_user_message() -> None:
    assert last_user_text([ChatMessage(role="assistant", content="x")]) is None
    assert last_user_text([ChatMessage(role="user", content="   ")]) is None
    assert last_user_text([SimpleNamespace(role="user", content=None)]) is None


@pytest.mark.parametrize("variant", ["full", "short", "trigger", "when", "summary_when"])
def test_every_document_variant_renders(variant: str) -> None:
    meta = SimpleNamespace(
        title="T", summary="S.", when_to_use="Use for A. Do not use for B."
    )
    assert runbook_document(meta, variant=variant)


def test_routing_revision_moves_only_with_routing_text() -> None:
    catalog = load_catalog()
    revision = routing_revision(catalog)
    assert revision.startswith("routing:")
    assert routing_revision(catalog) == revision
