"""OpenAI provider adapter tests (GPT-5.5), using a mock HTTP transport.

The adapter runs on the official ``openai`` SDK, so the mock transport is injected
via the SDK's ``http_client`` hook. These lock down the GPT-5.5-specific request
translation (``max_completion_tokens``, dropped non-default temperature, flat
``reasoning_effort``) and the SDK-error → :class:`DaintreeError` mapping.
"""

from __future__ import annotations

import json as _json

import httpx
import pytest

from daintree_assistant_server.errors import DaintreeError
from daintree_assistant_server.providers.base import ProviderChatRequest
from daintree_assistant_server.providers.openai import OpenAIProvider

_BASE = "https://api.openai.com/v1"
_MODEL = "gpt-5.5"

_OK_BODY = {
    "id": "chatcmpl-1",
    "object": "chat.completion",
    "created": 10,
    "model": _MODEL,
    "choices": [
        {
            "index": 0,
            "message": {"role": "assistant", "content": "hi"},
            "finish_reason": "stop",
            "logprobs": None,
        }
    ],
    "usage": {"prompt_tokens": 3, "completion_tokens": 1, "total_tokens": 4},
}


def _provider(handler: object, **kwargs: object) -> OpenAIProvider:
    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))  # type: ignore[arg-type]
    return OpenAIProvider(api_key="key", base_url=_BASE, http_client=client, **kwargs)  # type: ignore[arg-type]


def _capture(captured: dict) -> object:
    def handler(request: httpx.Request) -> httpx.Response:
        captured["url"] = str(request.url)
        captured["auth"] = request.headers.get("authorization")
        captured["body"] = _json.loads(request.content)
        return httpx.Response(200, json=_OK_BODY)

    return handler


def _req(**kwargs: object) -> ProviderChatRequest:
    base: dict[str, object] = {
        "model": _MODEL,
        "messages": [{"role": "user", "content": "hi, reply in JSON"}],
    }
    base.update(kwargs)
    return ProviderChatRequest(**base)  # type: ignore[arg-type]


@pytest.mark.asyncio
async def test_url_auth_and_model() -> None:
    captured: dict = {}
    provider = _provider(_capture(captured))
    await provider.complete(_req())
    assert captured["url"] == f"{_BASE}/chat/completions"
    assert captured["auth"] == "Bearer key"
    assert captured["body"]["model"] == _MODEL
    await provider.aclose()


@pytest.mark.asyncio
async def test_build_translates_tokens_temperature_and_reasoning() -> None:
    # The skill learner sends max_tokens=24000, temperature=0.2, reasoning_effort="high"
    # (thinking on). GPT-5.5 requires max_completion_tokens, rejects non-default
    # temperature, and takes a flat reasoning_effort — the adapter must translate.
    captured: dict = {}
    provider = _provider(_capture(captured))
    await provider.complete(
        _req(
            max_tokens=24000,
            temperature=0.2,
            thinking_disabled=False,
            reasoning_effort="high",
            response_format={"type": "json_object"},
        )
    )
    body = captured["body"]
    assert body["max_completion_tokens"] == 24000
    assert "max_tokens" not in body
    assert "temperature" not in body  # 0.2 dropped (only the default is allowed)
    assert body["reasoning_effort"] == "high"
    assert body["response_format"] == {"type": "json_object"}
    await provider.aclose()


@pytest.mark.asyncio
async def test_default_temperature_is_forwarded() -> None:
    captured: dict = {}
    provider = _provider(_capture(captured))
    await provider.complete(_req(temperature=1.0))
    assert captured["body"]["temperature"] == 1.0
    await provider.aclose()


@pytest.mark.asyncio
async def test_reasoning_omitted_when_thinking_disabled() -> None:
    captured: dict = {}
    provider = _provider(_capture(captured))
    await provider.complete(_req(thinking_disabled=True, reasoning_effort="high"))
    assert "reasoning_effort" not in captured["body"]
    await provider.aclose()


@pytest.mark.asyncio
async def test_complete_maps_result() -> None:
    provider = _provider(lambda _request: httpx.Response(200, json=_OK_BODY))
    result = await provider.complete(_req())
    assert result.text == "hi"
    assert result.finish_reason == "stop"
    assert result.usage.total_tokens == 4
    await provider.aclose()


@pytest.mark.asyncio
async def test_4xx_is_non_retryable_with_detail() -> None:
    detail = "Unsupported parameter: 'max_tokens' is not supported with this model."

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(400, json={"error": {"message": detail}})

    provider = _provider(handler)
    with pytest.raises(DaintreeError) as excinfo:
        await provider.complete(_req())
    assert excinfo.value.retryable is False
    assert detail in excinfo.value.message
    await provider.aclose()


@pytest.mark.asyncio
async def test_5xx_is_retryable() -> None:
    provider = _provider(
        lambda _request: httpx.Response(500, json={"error": {"message": "boom"}})
    )
    with pytest.raises(DaintreeError) as excinfo:
        await provider.complete(_req())
    assert excinfo.value.retryable is True
    await provider.aclose()


@pytest.mark.asyncio
async def test_429_is_rate_limited() -> None:
    provider = _provider(
        lambda _request: httpx.Response(429, json={"error": {"message": "slow down"}})
    )
    with pytest.raises(DaintreeError) as excinfo:
        await provider.complete(_req())
    assert excinfo.value.status_code == 429
    assert excinfo.value.retryable is True
    await provider.aclose()


@pytest.mark.asyncio
async def test_connection_error_is_retryable() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("no route", request=request)

    provider = _provider(handler)
    with pytest.raises(DaintreeError) as excinfo:
        await provider.complete(_req())
    assert excinfo.value.retryable is True
    await provider.aclose()


@pytest.mark.asyncio
async def test_stream_and_tool_call_unsupported() -> None:
    provider = _provider(_capture({}))
    with pytest.raises(NotImplementedError):
        await provider.open_stream(_req())
    with pytest.raises(NotImplementedError):
        await provider.structured_tool_call(_req(), function_name="f", function_schema={})
    await provider.aclose()
