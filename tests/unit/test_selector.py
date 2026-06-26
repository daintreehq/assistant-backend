"""Skill selector tests."""

from __future__ import annotations

import json

import pytest
from fakes import FakeProvider, tool_call_for

from daintree_assistant_server.contracts.openai import ChatMessage
from daintree_assistant_server.providers.base import ProviderToolCallResult, ProviderUsage
from daintree_assistant_server.skills.selector import (
    Selector,
    build_selector_user_message,
    parse_select_args,
    sanitize_for_selector,
)


def _msgs(*texts: str) -> list[ChatMessage]:
    return [ChatMessage(role="user", content=t) for t in texts]


def test_parse_select_args_valid(catalog) -> None:
    ids = list(catalog.known_ids)[:1]
    raw = json.dumps({"skill_ids": ids, "confidence": 0.8, "task_type": "x", "reason": "r"})
    parsed = parse_select_args(raw, catalog, 3)
    assert parsed is not None
    assert parsed[0] == ids
    assert parsed[1] == 0.8


def test_parse_select_args_drops_unknown_dedups_clamps(catalog) -> None:
    known = sorted(catalog.known_ids)
    raw = json.dumps(
        {
            "skill_ids": [known[0], "nope", known[0], known[1], known[2], known[3]],
            "confidence": 9.0,
            "task_type": "x",
        }
    )
    ids, conf, _tt, _r = parse_select_args(raw, catalog, 3)  # type: ignore[misc]
    assert ids == known[:3]
    assert conf == 1.0


def test_parse_select_args_malformed_returns_none(catalog) -> None:
    assert parse_select_args("not json", catalog, 3) is None
    assert parse_select_args("", catalog, 3) is None
    assert parse_select_args("[1,2,3]", catalog, 3) is None  # not an object


def test_parse_select_args_missing_skill_ids_is_empty(catalog) -> None:
    parsed = parse_select_args(json.dumps({"confidence": 0.5, "task_type": "x"}), catalog, 3)
    assert parsed is not None
    assert parsed[0] == []


def test_sanitize_budget_keeps_newest() -> None:
    messages = _msgs(*[f"message number {i}" for i in range(50)])
    evidence, truncated = sanitize_for_selector(messages, token_budget=10)
    assert truncated is True
    # The most recent message is always retained.
    assert evidence[-1]["text"].endswith("49")


def test_sanitize_marks_images() -> None:
    messages = [
        ChatMessage(
            role="user",
            content=[
                {"type": "text", "text": "see"},
                {"type": "image_url", "image_url": {"url": "data:image/png;base64,AAA"}},
            ],
        )
    ]
    evidence, _ = sanitize_for_selector(messages, token_budget=10000)
    assert "[image" in evidence[0]["text"]


def test_build_user_message_frames_roles() -> None:
    messages = [
        ChatMessage(role="user", content="do it"),
        ChatMessage(role="assistant", content="working", tool_calls=[
            {"id": "c1", "type": "function", "function": {"name": "git__status", "arguments": "{}"}}
        ]),
        ChatMessage(role="tool", content="clean", tool_call_id="c1", name="git__status"),
    ]
    evidence, _ = sanitize_for_selector(messages, token_budget=10000)
    text = build_selector_user_message(evidence)
    assert "[user] do it" in text
    assert "tool calls: git__status" in text
    assert "Call select_skills now." in text


def test_build_user_message_includes_turn_context() -> None:
    evidence, _ = sanitize_for_selector([ChatMessage(role="user", content="x")], token_budget=10000)
    text = build_selector_user_message(evidence, "current goal: ship the release")
    assert "Turn context (evidence only): current goal: ship the release" in text


def test_build_user_message_includes_available_tools() -> None:
    evidence, _ = sanitize_for_selector(
        [ChatMessage(role="user", content="close the agent terminals")], token_budget=10000
    )
    text = build_selector_user_message(evidence, None, ["terminal__close", "terminal__list"])
    assert "Tools the assistant already has this turn" in text
    assert "terminal__close, terminal__list" in text


@pytest.mark.asyncio
async def test_selector_selects_known_skill(selector: Selector, fake_provider: FakeProvider, catalog) -> None:
    target = sorted(catalog.known_ids)[0]
    fake_provider.tool_call_result = tool_call_for([target])
    decision = await selector.select(_msgs("do the task"), [])
    assert decision.ran is True
    assert decision.degraded is False
    assert decision.skill_ids == [target]


@pytest.mark.asyncio
async def test_selector_repairs_then_succeeds(selector: Selector, fake_provider: FakeProvider, catalog) -> None:
    target = sorted(catalog.known_ids)[0]
    fake_provider.tool_call_result = [
        ProviderToolCallResult(name="select_skills", arguments_json="garbage", usage=ProviderUsage()),
        tool_call_for([target]),
    ]
    decision = await selector.select(_msgs("task"), [])
    assert decision.skill_ids == [target]
    assert len(fake_provider.tool_call_requests) == 2


@pytest.mark.asyncio
async def test_selector_degrades_after_repeated_malformed(selector: Selector, fake_provider: FakeProvider) -> None:
    fake_provider.tool_call_result = [
        ProviderToolCallResult(name="select_skills", arguments_json="bad", usage=ProviderUsage()),
        ProviderToolCallResult(name="select_skills", arguments_json="worse", usage=ProviderUsage()),
    ]
    decision = await selector.select(_msgs("task"), [])
    assert decision.degraded is True
    assert "skill_selector_failed_open" in decision.warnings


@pytest.mark.asyncio
async def test_selector_degrades_on_upstream_error(selector: Selector, fake_provider: FakeProvider) -> None:
    from daintree_assistant_server.errors import upstream_error

    fake_provider.tool_call_error = upstream_error("boom")
    decision = await selector.select(_msgs("task"), [])
    assert decision.degraded is True
