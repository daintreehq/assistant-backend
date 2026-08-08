"""OpenRouter provider adapter tests using a mock HTTP transport."""

from __future__ import annotations

import httpx
import pytest

from daintree_assistant_server.errors import DaintreeError
from daintree_assistant_server.providers.base import ProviderChatRequest
from daintree_assistant_server.providers.openrouter import OpenRouterProvider

_BASE = "https://openrouter.ai/api/v1"
_MODEL = "deepseek/deepseek-v4-flash-0731"

SSE_OK = (
    'data: {"id":"x","object":"chat.completion.chunk","created":1,"model":"deepseek/deepseek-v4-flash-0731",'
    '"choices":[{"index":0,"delta":{"role":"assistant","content":"Hel"}}]}\n\n'
    'data: {"id":"x","object":"chat.completion.chunk","created":1,"model":"deepseek/deepseek-v4-flash-0731",'
    '"choices":[{"index":0,"delta":{"content":"lo"}}]}\n\n'
    'data: {"id":"x","object":"chat.completion.chunk","created":1,"model":"deepseek/deepseek-v4-flash-0731",'
    '"choices":[{"index":0,"delta":{},"finish_reason":"stop"}],'
    '"usage":{"prompt_tokens":5,"completion_tokens":2,"total_tokens":7}}\n\n'
    "data: [DONE]\n\n"
)


def _provider(handler, **kwargs) -> OpenRouterProvider:
    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    return OpenRouterProvider(client, api_key="key", base_url=_BASE, **kwargs)


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
                "model": "deepseek/deepseek-v4-flash-0731",
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
async def test_reasoning_disabled_sent_by_default() -> None:
    # thinking_disabled=True (the default) sends an EXPLICIT disable directive — a
    # reasoning model reasons when the field is absent, so this is what keeps
    # non-thinking calls (the latency-critical selector above all) actually
    # non-thinking, and what keeps any client-forced tool_choice from 400ing.
    captured: dict = {}
    provider = _provider(_capture(captured))
    await provider.complete(_req())
    assert captured["body"]["reasoning"] == {"enabled": False}
    await provider.aclose()


@pytest.mark.asyncio
async def test_reasoning_not_sent_when_flag_off() -> None:
    # send_reasoning=False means the model has no reasoning mode at all: send
    # neither the directive nor the effort.
    captured: dict = {}
    provider = _provider(_capture(captured), send_reasoning=False)
    await provider.complete(_req())
    assert "reasoning" not in captured["body"]
    await provider.aclose()


@pytest.mark.asyncio
async def test_reasoning_enabled_sends_effort() -> None:
    # OpenRouter's unified object carries the tier; `effort` and `max_tokens` are
    # mutually exclusive there, so a tiered request must not also emit a bare
    # enable or a flat reasoning_effort field.
    captured: dict = {}
    provider = _provider(_capture(captured))
    await provider.complete(_req(thinking_disabled=False, reasoning_effort="max"))
    assert captured["body"]["reasoning"] == {"effort": "max"}
    assert "reasoning_effort" not in captured["body"]
    await provider.aclose()


@pytest.mark.asyncio
async def test_reasoning_enabled_omits_effort_when_unset() -> None:
    captured: dict = {}
    provider = _provider(_capture(captured))
    await provider.complete(_req(thinking_disabled=False))
    assert captured["body"]["reasoning"] == {"enabled": True}
    await provider.aclose()


@pytest.mark.asyncio
async def test_disabled_reasoning_never_sends_effort() -> None:
    # reasoning_effort is meaningless without thinking; it must not leak into a
    # non-thinking request even if a caller set it.
    captured: dict = {}
    provider = _provider(_capture(captured))
    await provider.complete(_req(thinking_disabled=True, reasoning_effort="high"))
    assert captured["body"]["reasoning"] == {"enabled": False}
    await provider.aclose()


@pytest.mark.asyncio
async def test_reasoning_params_omitted_when_flag_off() -> None:
    # send_reasoning=False: even a thinking-enabled request sends neither field.
    captured: dict = {}
    provider = _provider(_capture(captured), send_reasoning=False)
    await provider.complete(_req(thinking_disabled=False, reasoning_effort="max"))
    assert "reasoning" not in captured["body"]
    assert "reasoning_effort" not in captured["body"]
    await provider.aclose()


# ---- endpoint-selection policy --------------------------------------------


@pytest.mark.asyncio
async def test_provider_routing_policy_sent_on_every_request() -> None:
    # OpenRouter picks WHICH endpoint answers, so this block is not decoration: it
    # is the throughput-first / no-data-collection product decision. A request that
    # silently omitted it would route on price to any endpoint, including ones that
    # train on prompts.
    captured: dict = {}
    provider = _provider(_capture(captured))
    await provider.complete(_req())
    assert captured["body"]["provider"] == {
        "sort": "throughput",
        "data_collection": "deny",
        "require_parameters": True,
        "allow_fallbacks": True,
    }
    await provider.aclose()


@pytest.mark.asyncio
async def test_provider_routing_policy_sent_on_streams_too() -> None:
    captured: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        import json as _json

        captured["body"] = _json.loads(request.content)
        return httpx.Response(200, headers={"content-type": "text/event-stream"}, text=SSE_OK)

    provider = _provider(handler)
    stream = await provider.open_stream(_req(stream=True))
    assert captured["body"]["provider"]["sort"] == "throughput"
    assert captured["body"]["provider"]["data_collection"] == "deny"
    await stream.aclose()
    await provider.aclose()


@pytest.mark.asyncio
async def test_attribution_headers_sent() -> None:
    seen: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["referer"] = request.headers.get("http-referer")
        seen["title"] = request.headers.get("x-title")
        return httpx.Response(
            200, json={"id": "x", "created": 0, "model": "m", "choices": [], "usage": {}}
        )

    provider = _provider(handler)
    await provider.complete(_req())
    assert seen["referer"] and seen["title"]
    await provider.aclose()


@pytest.mark.asyncio
async def test_no_prompt_cache_key_is_sent() -> None:
    # No endpoint for this model advertises prompt_cache_key, and with
    # require_parameters on, sending an unadvertised parameter can empty the
    # eligible pool — i.e. a hard routing failure, not a lost optimisation.
    captured: dict = {}
    provider = _provider(_capture(captured))
    await provider.complete(_req())
    assert "prompt_cache_key" not in captured["body"]
    await provider.aclose()


# ---- reasoning field rename (OpenRouter `reasoning` <-> wire reasoning_content)


@pytest.mark.asyncio
async def test_outbound_reasoning_content_is_renamed() -> None:
    # The Daintree contract and the CLI speak reasoning_content; OpenRouter speaks
    # `reasoning`. An unrecognised field would be dropped silently, losing the
    # replayed chain-of-thought rather than erroring, so assert the rename.
    captured: dict = {}
    provider = _provider(_capture(captured))
    await provider.complete(
        _req(
            messages=[
                {"role": "user", "content": "hi"},
                {
                    "role": "assistant",
                    "content": "",
                    "reasoning_content": "step one",
                    "tool_calls": [{"id": "c", "type": "function", "function": {"name": "f"}}],
                },
            ]
        )
    )
    assistant = captured["body"]["messages"][1]
    assert assistant["reasoning"] == "step one"
    assert "reasoning_content" not in assistant
    # Untouched messages are passed through unchanged.
    assert captured["body"]["messages"][0] == {"role": "user", "content": "hi"}
    await provider.aclose()


@pytest.mark.asyncio
async def test_inbound_reasoning_is_renamed_on_complete() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "id": "x",
                "created": 0,
                "model": "m",
                "choices": [
                    {
                        "index": 0,
                        "message": {
                            "role": "assistant",
                            "content": "answer",
                            "reasoning": "because",
                        },
                        "finish_reason": "stop",
                    }
                ],
                "usage": {},
            },
        )

    provider = _provider(handler)
    result = await provider.complete(_req())
    assert result.message["reasoning_content"] == "because"
    assert "reasoning" not in result.message
    await provider.aclose()


@pytest.mark.asyncio
async def test_inbound_reasoning_is_renamed_on_stream() -> None:
    sse = (
        'data: {"id":"x","choices":[{"index":0,"delta":{"reasoning":"thinking..."}}]}\n\n'
        "data: [DONE]\n\n"
    )

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, headers={"content-type": "text/event-stream"}, text=sse)

    provider = _provider(handler)
    stream = await provider.open_stream(_req(stream=True))
    first = await stream.prefetch_first_event()
    assert first["choices"][0]["delta"]["reasoning_content"] == "thinking..."
    assert "reasoning" not in first["choices"][0]["delta"]
    await stream.aclose()
    await provider.aclose()


@pytest.mark.asyncio
async def test_non_string_inbound_reasoning_is_dropped_not_renamed() -> None:
    # The shape validators guarantee reasoning_content is a string when present and
    # this rename runs outside their reach, so a structured/encrypted value must be
    # dropped rather than smuggled through under the wire name.
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "id": "x",
                "created": 0,
                "model": "m",
                "choices": [
                    {"index": 0, "message": {"role": "assistant", "reasoning": {"blob": 1}}}
                ],
                "usage": {},
            },
        )

    provider = _provider(handler)
    result = await provider.complete(_req())
    assert "reasoning" not in result.message
    assert "reasoning_content" not in result.message
    await provider.aclose()


# ---- 404 routing failure ---------------------------------------------------


@pytest.mark.asyncio
async def test_404_reads_as_a_routing_failure_not_a_bad_request() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(404, json={"error": {"message": "No allowed providers"}})

    provider = _provider(handler)
    with pytest.raises(DaintreeError) as exc:
        await provider.complete(_req())
    assert exc.value.status_code == 502
    assert exc.value.retryable is False
    assert "provider-routing policy" in exc.value.message
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


@pytest.mark.parametrize(
    "payload",
    [
        "[1, 2, 3]",  # list root
        '"just a string"',  # scalar root
        '{"choices": {"0": {}}}',  # choices not a list
        '{"choices": ["not an object"]}',  # choice entry not a dict
        '{"choices": [{"index": 0, "delta": "text"}]}',  # delta not a dict
        '{"choices": [{"delta": {"content": 5}}]}',  # content not a string
        '{"choices": [{"delta": {"reasoning_content": {}}}]}',  # reasoning not a string
        '{"choices": [{"delta": {"tool_calls": "x"}}]}',  # tool_calls not a list
        '{"choices": [{"delta": {"tool_calls": ["x"]}}]}',  # tool call not a dict
        '{"choices": [{"delta": {"tool_calls": [{"function": "f"}]}}]}',  # function not a dict
        '{"error": "boom"}',  # error not an object
        '{"usage": "lots"}',  # usage not an object
    ],
)
@pytest.mark.asyncio
async def test_stream_malformed_chunk_shape_raises_upstream_error(payload: str) -> None:
    # Malformed-but-valid JSON must surface as a DaintreeError from the provider
    # read path, never as an AttributeError in the orchestrator.
    sse = f"data: {payload}\n\ndata: [DONE]\n\n"

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, headers={"content-type": "text/event-stream"}, text=sse)

    provider = _provider(handler)
    stream = await provider.open_stream(_req(stream=True))
    with pytest.raises(DaintreeError) as exc:
        await stream.prefetch_first_event()
    assert exc.value.code == "upstream_error"
    assert exc.value.status_code == 502
    await stream.aclose()
    await provider.aclose()


@pytest.mark.asyncio
async def test_stream_usage_only_and_empty_choices_chunks_pass_validation() -> None:
    # Shape validation must not reject legitimate degenerate chunks: an empty
    # choices list and a usage-only chunk both occur on real streams.
    sse = (
        'data: {"id":"x","choices":[],"usage":{"total_tokens":7}}\n\n'
        "data: [DONE]\n\n"
    )

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, headers={"content-type": "text/event-stream"}, text=sse)

    provider = _provider(handler)
    stream = await provider.open_stream(_req(stream=True))
    first = await stream.prefetch_first_event()
    assert first == {"id": "x", "choices": [], "usage": {"total_tokens": 7}}
    assert await stream.prefetch_first_event() is None
    assert stream.usage is not None and stream.usage.total_tokens == 7
    await stream.aclose()
    await provider.aclose()


@pytest.mark.asyncio
async def test_complete_non_object_response_raises_upstream_error() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=[1, 2, 3])

    provider = _provider(handler)
    with pytest.raises(DaintreeError) as exc:
        await provider.complete(_req())
    assert exc.value.code == "upstream_error"
    await provider.aclose()


@pytest.mark.asyncio
async def test_complete_malformed_choices_raises_upstream_error() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"id": "x", "created": 0, "model": "m", "choices": ["bad"]})

    provider = _provider(handler)
    with pytest.raises(DaintreeError) as exc:
        await provider.complete(_req())
    assert exc.value.code == "upstream_error"
    await provider.aclose()


# ---- request-body dumping (dev-only diagnostic) ---------------------------


@pytest.mark.asyncio
async def test_dump_dir_writes_request_bodies(tmp_path) -> None:
    import json as _json

    dump_dir = tmp_path / "dumps"
    provider = _provider(_capture({}), dump_dir=str(dump_dir))
    await provider.complete(_req())
    files = list(dump_dir.glob("*.json"))
    assert len(files) == 1
    body = _json.loads(files[0].read_text(encoding="utf-8"))
    assert body["model"] == _MODEL
    await provider.aclose()


@pytest.mark.asyncio
async def test_no_dump_without_dump_dir(tmp_path, monkeypatch: pytest.MonkeyPatch) -> None:
    # The provider must not read the environment: even with the legacy env var
    # set, nothing is dumped unless dump_dir is wired in explicitly (Settings).
    monkeypatch.setenv("DAINTREE_DUMP_UPSTREAM_DIR", str(tmp_path / "env-dumps"))
    provider = _provider(_capture({}))
    await provider.complete(_req())
    assert not (tmp_path / "env-dumps").exists()
    await provider.aclose()


# ---- structured timeouts ---------------------------------------------------


@pytest.mark.asyncio
async def test_per_request_timeout_overrides_read_only() -> None:
    # The per-call budget must land on READ alone; connect/write/pool keep the
    # client's structured components instead of being collapsed to the big value.
    captured: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["timeout"] = request.extensions.get("timeout")
        return httpx.Response(
            200, json={"id": "x", "created": 0, "model": "m", "choices": [], "usage": {}}
        )

    client = httpx.AsyncClient(
        transport=httpx.MockTransport(handler),
        timeout=httpx.Timeout(connect=10.0, read=840.0, write=30.0, pool=10.0),
    )
    provider = OpenRouterProvider(client, api_key="key", base_url=_BASE, default_timeout=840.0)
    await provider.complete(_req(timeout=25.0))
    assert captured["timeout"] == {"connect": 10.0, "read": 25.0, "write": 30.0, "pool": 10.0}
    await provider.aclose()


@pytest.mark.asyncio
async def test_stream_transport_interruption_is_retryable() -> None:
    from daintree_assistant_server.providers.openrouter import OpenRouterStream

    class _Ctx:
        async def __aexit__(self, *_a: object) -> bool:
            return False

    class _Resp:
        async def aiter_bytes(self):  # noqa: ANN202 - test stub
            raise httpx.ReadError("connection dropped")
            yield b""  # pragma: no cover - makes this an async generator

    stream = OpenRouterStream(_Ctx(), _Resp(), _MODEL)
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
        assert body["reasoning"] == {"enabled": False}
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
    # — otherwise DeepSeek 400s the forced tool_choice ("Thinking mode does not
    # support this tool_choice"), and which OpenRouter endpoint enforces that is
    # not ours to bet on.
    captured: dict = {}
    provider = _provider(_capture(captured))
    await provider.structured_tool_call(
        _req(thinking_disabled=False, reasoning_effort="max"),
        function_name="select_skills",
        function_schema={"type": "object"},
    )
    assert captured["body"]["reasoning"] == {"enabled": False}
    assert "reasoning_effort" not in captured["body"]
    await provider.aclose()
