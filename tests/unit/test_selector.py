"""Skill selector tests."""

from __future__ import annotations

import json

import pytest
from fakes import FakeProvider, make_result, selection_result

from daintree_assistant_server.contracts.openai import ChatMessage
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


def test_parse_select_args_missing_skill_ids_is_malformed(catalog) -> None:
    # A valid-JSON object without a skill_ids array is malformed (triggers repair /
    # fail-open), NOT an authoritative empty selection that would clear active skills.
    assert parse_select_args(json.dumps({"confidence": 0.5, "task_type": "x"}), catalog, 3) is None
    assert parse_select_args(json.dumps({}), catalog, 3) is None
    assert parse_select_args(json.dumps({"skill_ids": None}), catalog, 3) is None
    # An explicit empty array IS a valid clear.
    parsed = parse_select_args(json.dumps({"skill_ids": []}), catalog, 3)
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
    assert "Return the JSON object now." in text


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
    fake_provider.complete_result = selection_result([target])
    decision = await selector.select(_msgs("do the task"), [])
    assert decision.ran is True
    assert decision.degraded is False
    assert decision.skill_ids == [target]


@pytest.mark.asyncio
async def test_selector_uses_json_mode_and_thinking(
    selector: Selector, fake_provider: FakeProvider, catalog, settings
) -> None:
    # The selector must NOT force a tool_choice (incompatible with thinking on DeepSeek):
    # it asks for json_object and runs thinking-on by default, with reasoning headroom
    # added to the output cap (max_tokens is shared by reasoning + the visible JSON).
    fake_provider.complete_result = selection_result([sorted(catalog.known_ids)[0]])
    await selector.select(_msgs("do the task"), [])
    req = fake_provider.requests[-1]
    assert req.response_format == {"type": "json_object"}
    assert req.tool_choice is None
    assert req.thinking_disabled is False
    assert req.reasoning_effort == "high"
    assert req.max_tokens == (
        settings.selector_max_output_tokens + settings.deepseek_thinking_headroom_tokens
    )


@pytest.mark.asyncio
async def test_selector_parses_content_ignoring_reasoning(
    selector: Selector, fake_provider: FakeProvider, catalog
) -> None:
    # In thinking mode DeepSeek returns the chain-of-thought in reasoning_content and the
    # answer JSON in content. The selector must parse content and ignore reasoning_content.
    target = sorted(catalog.known_ids)[0]
    fake_provider.complete_result = make_result(
        content=json.dumps({"skill_ids": [target], "confidence": 0.9, "task_type": "t"}),
        reasoning_content="Let me think... this is prose, not JSON, and must be ignored.",
    )
    decision = await selector.select(_msgs("do the task"), [])
    assert decision.degraded is False
    assert decision.skill_ids == [target]


@pytest.mark.asyncio
async def test_selector_repairs_then_succeeds(selector: Selector, fake_provider: FakeProvider, catalog) -> None:
    target = sorted(catalog.known_ids)[0]
    fake_provider.complete_result = [
        make_result(content="garbage"),
        selection_result([target]),
    ]
    decision = await selector.select(_msgs("task"), [])
    assert decision.skill_ids == [target]
    assert len(fake_provider.requests) == 2


@pytest.mark.asyncio
async def test_selector_degrades_after_repeated_malformed(selector: Selector, fake_provider: FakeProvider) -> None:
    fake_provider.complete_result = [
        make_result(content="bad"),
        make_result(content="worse"),
    ]
    decision = await selector.select(_msgs("task"), [])
    assert decision.degraded is True
    assert "skill_selector_failed_open" in decision.warnings


@pytest.mark.asyncio
async def test_selector_degrades_on_upstream_error(selector: Selector, fake_provider: FakeProvider) -> None:
    from daintree_assistant_server.errors import upstream_error

    fake_provider.complete_error = upstream_error("boom")
    decision = await selector.select(_msgs("task"), [])
    assert decision.degraded is True


@pytest.mark.asyncio
async def test_selector_retries_transient_then_succeeds(
    selector: Selector, fake_provider: FakeProvider, catalog
) -> None:
    from daintree_assistant_server.errors import upstream_timeout

    target = sorted(catalog.known_ids)[0]
    calls = [0]

    def complete(_req: object):
        calls[0] += 1
        if calls[0] < 2:
            raise upstream_timeout()
        return selection_result([target])

    fake_provider.complete_result = complete
    decision = await selector.select(_msgs("task"), [])
    assert decision.degraded is False
    assert decision.skill_ids == [target]
    assert calls[0] == 2  # retried the transient failure


@pytest.mark.asyncio
async def test_selector_exhausts_retries_then_fails_open(
    selector: Selector, fake_provider: FakeProvider
) -> None:
    from daintree_assistant_server.errors import upstream_timeout

    calls = [0]

    def complete(_req: object):
        calls[0] += 1
        raise upstream_timeout()  # transient every time

    fake_provider.complete_result = complete
    decision = await selector.select(_msgs("task"), [])
    assert decision.degraded is True
    assert "skill_selector_failed_open" in decision.warnings
    assert calls[0] == 4  # 1 try + 3 retries, then fail open
