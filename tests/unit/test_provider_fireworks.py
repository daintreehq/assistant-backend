"""Fireworks provider adapter tests using a mock HTTP transport."""

from __future__ import annotations

import httpx
import pytest

from daintree_assistant_server.errors import DaintreeError
from daintree_assistant_server.providers.base import ProviderChatRequest
from daintree_assistant_server.providers.fireworks import FireworksProvider

_BASE = "https://api.fireworks.ai/inference/v1"
_MODEL = "accounts/fireworks/models/deepseek-v4-flash"

SSE_OK = (
    'data: {"id":"x","object":"chat.completion.chunk","created":1,"model":"m",'
    '"choices":[{"index":0,"delta":{"role":"assistant","content":"Hel"}}]}\n\n'
    'data: {"id":"x","object":"chat.completion.chunk","created":1,"model":"m",'
    '"choices":[{"index":0,"delta":{"content":"lo"}}]}\n\n'
    'data: {"id":"x","object":"chat.completion.chunk","created":1,"model":"m",'
    '"choices":[{"index":0,"delta":{},"finish_reason":"stop"}],'
    '"usage":{"prompt_tokens":5,"completion_tokens":2,"total_tokens":7}}\n\n'
    "data: [DONE]\n\n"
)


def _provider(handler, **kwargs) -> FireworksProvider:
    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    return FireworksProvider(client, api_key="key", base_url=_BASE, **kwargs)


def _capture(captured: dict):
    import json as _json

    def handler(request: httpx.Request) -> httpx.Response:
        captured["url"] = str(request.url)
        captured["body"] = _json.loads(request.content)
        return httpx.Response(
            200, json={"id": "x", "created": 0, "model": "m", "choices": [], "usage": {}}
        )

    return handler


def _req(**kwargs: object) -> ProviderChatRequest:
    base: dict[str, object] = {"model": _MODEL, "messages": [{"role": "user", "content": "hi"}]}
    base.update(kwargs)
    return ProviderChatRequest(**base)  # type: ignore[arg-type]


@pytest.mark.asyncio
async def test_url_and_auth() -> None:
    captured: dict = {}
    provider = _provider(_capture(captured))
    await provider.complete(_req())
    assert captured["url"] == f"{_BASE}/chat/completions"
    assert captured["body"]["model"] == _MODEL
    await provider.aclose()


@pytest.mark.asyncio
async def test_complete_success() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.headers["authorization"] == "Bearer key"
        return httpx.Response(
            200,
            json={
                "id": "abc",
                "created": 10,
                "model": "m",
                "choices": [
                    {"index": 0, "message": {"role": "assistant", "content": "hi"}, "finish_reason": "stop"}
                ],
                "usage": {"prompt_tokens": 3, "completion_tokens": 1, "total_tokens": 4},
            },
        )

    provider = _provider(handler)
    result = await provider.complete(_req())
    assert result.text == "hi"
    assert result.finish_reason == "stop"
    assert result.usage.total_tokens == 4
    await provider.aclose()


@pytest.mark.asyncio
async def test_non_thinking_sends_reasoning_none() -> None:
    # thinking_disabled=True (the default) maps to reasoning_effort "none" — this is
    # what keeps the selector's forced tool_choice working and turns DeepSeek's
    # default-on reasoning off.
    captured: dict = {}
    provider = _provider(_capture(captured))
    await provider.complete(_req())
    assert captured["body"]["reasoning_effort"] == "none"
    await provider.aclose()


@pytest.mark.asyncio
async def test_thinking_enabled_sends_effort() -> None:
    captured: dict = {}
    provider = _provider(_capture(captured))
    await provider.complete(_req(thinking_disabled=False, reasoning_effort="max"))
    assert captured["body"]["reasoning_effort"] == "max"
    await provider.aclose()


@pytest.mark.asyncio
async def test_thinking_enabled_defaults_effort_high() -> None:
    captured: dict = {}
    provider = _provider(_capture(captured))
    await provider.complete(_req(thinking_disabled=False))
    assert captured["body"]["reasoning_effort"] == "high"
    await provider.aclose()


@pytest.mark.asyncio
async def test_reasoning_effort_always_sent() -> None:
    # There is no opt-out: reasoning_effort is always present so a non-thinking call
    # can never fall through to DeepSeek's default-on reasoning.
    captured: dict = {}
    provider = _provider(_capture(captured))
    await provider.complete(_req())
    assert "reasoning_effort" in captured["body"]
    await provider.aclose()


@pytest.mark.asyncio
async def test_prompt_cache_key_sent_by_default_when_not_json_mode() -> None:
    captured: dict = {}
    provider = _provider(_capture(captured))
    await provider.complete(_req(prompt_cache_key="daintree-main"))
    assert captured["body"]["prompt_cache_key"] == "daintree-main"
    await provider.aclose()


@pytest.mark.asyncio
async def test_prompt_cache_key_not_sent_when_flag_off() -> None:
    captured: dict = {}
    provider = _provider(_capture(captured), send_prompt_cache_key=False)
    await provider.complete(_req(prompt_cache_key="daintree-main"))
    assert "prompt_cache_key" not in captured["body"]
    await provider.aclose()


@pytest.mark.asyncio
async def test_prompt_cache_key_suppressed_in_json_mode() -> None:
    captured: dict = {}
    provider = _provider(_capture(captured), send_prompt_cache_key=True)
    await provider.complete(
        _req(prompt_cache_key="daintree-main", response_format={"type": "json_object"})
    )
    assert "prompt_cache_key" not in captured["body"]
    await provider.aclose()


@pytest.mark.asyncio
async def test_json_response_format_passed_through() -> None:
    captured: dict = {}
    provider = _provider(_capture(captured))
    await provider.complete(_req(response_format={"type": "json_object"}))
    assert captured["body"]["response_format"] == {"type": "json_object"}
    await provider.aclose()


@pytest.mark.asyncio
async def test_429_maps_with_retry_after() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(429, headers={"retry-after": "7"}, json={"error": {"message": "slow"}})

    provider = _provider(handler)
    with pytest.raises(DaintreeError) as exc:
        await provider.complete(_req())
    assert exc.value.status_code == 429
    assert exc.value.code == "upstream_rate_limited"
    assert exc.value.headers["Retry-After"] == "7"
    assert exc.value.retryable is True
    await provider.aclose()


@pytest.mark.asyncio
async def test_500_maps_retryable() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500, json={"error": {"message": "boom"}})

    provider = _provider(handler)
    with pytest.raises(DaintreeError) as exc:
        await provider.complete(_req())
    assert exc.value.status_code == 502
    assert exc.value.retryable is True
    await provider.aclose()


@pytest.mark.asyncio
async def test_400_surfaces_upstream_reason_and_is_not_retryable() -> None:
    # A 4xx means the upstream rejected OUR request; its message is the whole
    # diagnostic and must reach the caller (not be flattened to "status 400").
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            400,
            json={"error": {"message": "'items' must be a schema object, got list."}},
        )

    provider = _provider(handler)
    with pytest.raises(DaintreeError) as exc:
        await provider.complete(_req())
    assert exc.value.status_code == 502
    assert exc.value.retryable is False  # a malformed request must never be retried
    assert "items' must be a schema object" in exc.value.message
    await provider.aclose()


@pytest.mark.asyncio
async def test_stream_400_surfaces_upstream_reason() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(400, json={"error": {"message": "bad tool schema at x."}})

    provider = _provider(handler)
    with pytest.raises(DaintreeError) as exc:
        await provider.open_stream(_req(stream=True))
    assert exc.value.status_code == 502
    assert exc.value.retryable is False
    assert "bad tool schema at x." in exc.value.message
    await provider.aclose()


@pytest.mark.asyncio
async def test_400_non_json_body_falls_back_to_raw_text() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(400, text="upstream said no (not json)")

    provider = _provider(handler)
    with pytest.raises(DaintreeError) as exc:
        await provider.complete(_req())
    assert "upstream said no (not json)" in exc.value.message
    await provider.aclose()


@pytest.mark.asyncio
async def test_400_empty_body_still_yields_a_sensible_message() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(400, content=b"")

    provider = _provider(handler)
    with pytest.raises(DaintreeError) as exc:
        await provider.complete(_req())
    # No detail available, but the message must not be empty or read "None".
    assert "rejected the request" in exc.value.message.lower()
    assert "none" not in exc.value.message.lower()
    await provider.aclose()


@pytest.mark.asyncio
async def test_timeout_maps() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectTimeout("timed out")

    provider = _provider(handler)
    with pytest.raises(DaintreeError) as exc:
        await provider.complete(_req())
    assert exc.value.code == "upstream_timeout"
    await provider.aclose()


@pytest.mark.asyncio
async def test_stream_success_and_usage() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.headers["accept"] == "text/event-stream"
        return httpx.Response(200, headers={"content-type": "text/event-stream"}, text=SSE_OK)

    provider = _provider(handler)
    stream = await provider.open_stream(_req(stream=True, include_usage=True))
    first = await stream.prefetch_first_event()
    assert first["choices"][0]["delta"]["content"] == "Hel"
    rest = [c async for c in stream]
    assert len(rest) == 2
    assert stream.usage.total_tokens == 7
    await stream.aclose()
    await provider.aclose()


@pytest.mark.asyncio
async def test_stream_error_status_before_headers() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(429, headers={"retry-after": "3"}, json={"error": {"message": "x"}})

    provider = _provider(handler)
    with pytest.raises(DaintreeError) as exc:
        await provider.open_stream(_req(stream=True))
    assert exc.value.status_code == 429
    await provider.aclose()


@pytest.mark.asyncio
async def test_stream_inline_error_chunk_raises() -> None:
    sse = 'data: {"error":{"message":"rate limit exceeded","code":"429"}}\n\ndata: [DONE]\n\n'

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, headers={"content-type": "text/event-stream"}, text=sse)

    provider = _provider(handler)
    stream = await provider.open_stream(_req(stream=True))
    with pytest.raises(DaintreeError) as exc:
        await stream.prefetch_first_event()
    assert exc.value.code == "upstream_rate_limited"
    await stream.aclose()
    await provider.aclose()


@pytest.mark.asyncio
async def test_stream_eof_before_done_raises() -> None:
    sse = (
        'data: {"id":"x","object":"chat.completion.chunk","created":1,"model":"m",'
        '"choices":[{"index":0,"delta":{"content":"partial"}}]}\n\n'
    )

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, headers={"content-type": "text/event-stream"}, text=sse)

    provider = _provider(handler)
    stream = await provider.open_stream(_req(stream=True))
    first = await stream.prefetch_first_event()
    assert first["choices"][0]["delta"]["content"] == "partial"
    with pytest.raises(DaintreeError) as exc:
        await stream.__anext__()
    assert exc.value.code == "upstream_error"
    await stream.aclose()
    await provider.aclose()


@pytest.mark.asyncio
async def test_stream_transport_interruption_is_retryable() -> None:
    from daintree_assistant_server.providers.fireworks import FireworksStream

    class _Ctx:
        async def __aexit__(self, *_a: object) -> bool:
            return False

    class _Resp:
        async def aiter_lines(self):  # noqa: ANN202 - test stub
            raise httpx.ReadError("connection dropped")
            yield ""  # pragma: no cover - makes this an async generator

    stream = FireworksStream(_Ctx(), _Resp(), _MODEL)
    with pytest.raises(DaintreeError) as exc:
        await stream.prefetch_first_event()
    assert exc.value.code == "upstream_error"
    assert exc.value.retryable is True
    await stream.aclose()


@pytest.mark.asyncio
async def test_structured_tool_call() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        import json as _json

        body = _json.loads(request.content)
        assert body["tool_choice"]["function"]["name"] == "select_skills"
        # A forced tool call must go non-thinking.
        assert body["reasoning_effort"] == "none"
        return httpx.Response(
            200,
            json={
                "id": "x", "created": 0, "model": "m",
                "choices": [
                    {
                        "index": 0,
                        "message": {
                            "role": "assistant",
                            "tool_calls": [
                                {"id": "c", "type": "function",
                                 "function": {"name": "select_skills", "arguments": "{\"skill_ids\":[]}"}}
                            ],
                        },
                        "finish_reason": "tool_calls",
                    }
                ],
                "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2},
            },
        )

    provider = _provider(handler)
    result = await provider.structured_tool_call(
        _req(), function_name="select_skills", function_schema={"type": "object"}
    )
    assert result.name == "select_skills"
    assert result.arguments_json == '{"skill_ids":[]}'
    await provider.aclose()


@pytest.mark.asyncio
async def test_structured_tool_call_forces_non_thinking() -> None:
    # Even if the caller enabled thinking, a forced tool call MUST go non-thinking
    # (reasoning_effort "none") — otherwise Fireworks 400s the forced tool_choice.
    captured: dict = {}
    provider = _provider(_capture(captured))
    await provider.structured_tool_call(
        _req(thinking_disabled=False, reasoning_effort="max"),
        function_name="select_skills",
        function_schema={"type": "object"},
    )
    assert captured["body"]["reasoning_effort"] == "none"
    await provider.aclose()
