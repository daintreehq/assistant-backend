"""The Cerebras body, which is the half of this adapter that is ours to get wrong.

The live wire is unverified — `tests/live/` does not exist and CI bars paid calls —
so what Cerebras accepts is pinned by nothing. What IS pinnable offline is the
body this adapter builds, and every assertion below is about a difference from the
parent adapter that would be silent if it regressed.
"""

from __future__ import annotations

from typing import Any

import httpx
import pytest
from pydantic import SecretStr

from daintree_assistant_server.providers.base import ProviderChatRequest
from daintree_assistant_server.providers.cerebras import CerebrasProvider
from daintree_assistant_server.providers.openrouter import OpenRouterProvider


def _ok(request: httpx.Request) -> httpx.Response:
    return httpx.Response(200)


def _provider(**kwargs: Any) -> CerebrasProvider:
    # Never entered: every test here builds a body, it does not send one.
    client = httpx.AsyncClient(transport=httpx.MockTransport(_ok))
    return CerebrasProvider(client, api_key=SecretStr("k"), **kwargs)


def _request(**overrides: Any) -> ProviderChatRequest:
    fields: dict[str, Any] = {
        "model": "gemma-4-31b",
        "messages": [{"role": "user", "content": "hi"}],
        "max_tokens": 160,
        "temperature": 0.2,
    }
    fields.update(overrides)
    return ProviderChatRequest(**fields)


def test_the_token_cap_is_sent_under_cerebras_spelling_and_only_that_one() -> None:
    """One spelling per adapter. Sending both is the failure this prevents, and a
    body carrying `max_tokens` here would be silently ignored or rejected."""
    body = _provider()._build_body(_request(), stream=False)

    assert body["max_completion_tokens"] == 160
    assert "max_tokens" not in body


def test_the_parent_still_uses_the_openai_spelling() -> None:
    """The inverse, asserted together so the pair cannot drift apart.

    If someone 'unifies' these later, exactly one of these two tests fails —
    which is the point.
    """
    client = httpx.AsyncClient(transport=httpx.MockTransport(_ok))
    body = OpenRouterProvider(client, api_key=SecretStr("k"))._build_body(
        _request(), stream=False
    )

    assert body["max_tokens"] == 160
    assert "max_completion_tokens" not in body


def test_no_reasoning_object_is_ever_sent() -> None:
    """The parent MUST send `{"enabled": false}` — DeepSeek reasons by default, so
    omission there means "think". Here omission IS the posture: the model does not
    reason, and Together's spelling is not a parameter Cerebras advertises."""
    body = _provider()._build_body(_request(thinking_disabled=True), stream=False)

    assert "reasoning" not in body
    assert "reasoning_effort" not in body


def test_an_explicitly_requested_effort_is_forwarded() -> None:
    """The one case where the field is real: a caller asked for it by name.

    Never synthesised from `thinking_disabled`, which would put an unverified
    parameter on every fast call to buy a disable the model does not need.
    """
    body = _provider()._build_body(
        _request(thinking_disabled=False, reasoning_effort="low"), stream=False
    )
    assert body["reasoning_effort"] == "low"


def test_the_prompt_cache_key_is_withheld_unless_the_account_has_it() -> None:
    """Account-gated, and an unadvertised parameter is a needless 400 on a path
    whose entire value is being fast."""
    body = _provider()._build_body(_request(cache_session_id="fast-v1-abc"), stream=False)
    assert "prompt_cache_key" not in body


def test_the_prompt_cache_key_is_sent_when_explicitly_enabled() -> None:
    body = _provider(send_prompt_cache_key=True)._build_body(
        _request(cache_session_id="fast-v1-abc"), stream=False
    )
    assert body["prompt_cache_key"] == "fast-v1-abc"


def test_no_together_or_openrouter_only_field_reaches_cerebras() -> None:
    """A sibling's parameter on this wire is a 400 at best and silent behaviour
    change at worst."""
    body = _provider(send_prompt_cache_key=True)._build_body(
        _request(cache_session_id="s", thinking_disabled=True), stream=True
    )

    for forbidden in ("provider", "reasoning", "transforms", "route", "session_id"):
        assert forbidden not in body, f"{forbidden} must never be sent to Cerebras"


def test_tools_and_tool_choice_pass_through_when_present() -> None:
    """The fast stage sends none, but the adapter is not the place to enforce
    that — a policy baked into a transport is a policy nobody can find."""
    body = _provider()._build_body(
        _request(tools=[{"type": "function", "function": {"name": "x"}}], tool_choice="auto"),
        stream=False,
    )
    assert body["tools"]
    assert body["tool_choice"] == "auto"


def test_streaming_asks_for_usage_the_same_way_the_parent_does() -> None:
    body = _provider()._build_body(_request(include_usage=True), stream=True)
    assert body["stream"] is True
    assert body["stream_options"] == {"include_usage": True}


def test_the_key_probe_reads_the_openai_shaped_model_list() -> None:
    """Cerebras answers `{"object": "list", "data": [...]}`, not OpenRouter's
    `{"data": {...}}` mapping. The parent's decoder would raise on a perfectly
    good key — reporting "we could not tell" about a credential that had just
    proved itself."""
    record = _provider()._decode_key_record(
        {"object": "list", "data": [{"id": "gemma-4-31b"}, {"id": "gpt-oss-120b"}]}
    )
    assert record["models"] == ["gemma-4-31b", "gpt-oss-120b"]


@pytest.mark.parametrize("payload", [[], {"data": {}}, {"nope": 1}, "text"])
def test_an_unreadable_model_listing_is_a_protocol_error(payload: Any) -> None:
    from daintree_assistant_server.errors import DaintreeError

    with pytest.raises(DaintreeError):
        _provider()._decode_key_record(payload)


def test_the_adapter_points_at_cerebras_and_sends_no_attribution_headers() -> None:
    """Attribution headers exist for OpenRouter's public rankings; Cerebras runs
    none, so they would be bytes on every request buying nothing."""
    assert CerebrasProvider.default_base_url == "https://api.cerebras.ai/v1"
    assert CerebrasProvider.attribution_headers == {}


def test_message_level_reasoning_is_stripped_too() -> None:
    """The parent rewrites `reasoning_content` into `reasoning` on every assistant
    row, so dropping only the top-level field leaves a sibling's parameter on the
    wire. Today's fast prompt never sends one — but an adapter that is correct
    only because of what one caller happens to send is not correct.
    """
    body = _provider()._build_body(
        _request(
            messages=[
                {"role": "user", "content": "hi"},
                {
                    "role": "assistant",
                    "content": "sure",
                    "reasoning_content": "internal chain of thought",
                },
            ]
        ),
        stream=False,
    )

    assert "reasoning" not in body
    for message in body["messages"]:
        assert "reasoning" not in message, f"a sibling's field reached Cerebras: {message}"
        assert "reasoning_content" not in message
