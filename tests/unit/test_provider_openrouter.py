"""OpenRouter provider adapter tests using a mock HTTP transport."""

from __future__ import annotations

import asyncio

import httpx
import pytest
import structlog.testing
from pydantic import SecretStr

from daintree_assistant_server.errors import DaintreeError
from daintree_assistant_server.providers.base import ProviderChatRequest
from daintree_assistant_server.providers.openrouter import (
    _ERROR_DETAIL_CAP,
    _REDACTED,
    OpenRouterProvider,
    _extract_upstream_error,
)
from daintree_assistant_server.providers.retry import retry_upstream

_BASE = "https://openrouter.ai/api/v1"
_MODEL = "deepseek/deepseek-v4-flash-0731"
# The provider's constructor default (fallback) key, and a distinct per-call key.
# They must differ so a dropped per-call credential is detectable rather than
# accidentally matching the fallback. Both are long, distinctive sentinels so a
# test can assert the literal value is ABSENT without matching incidental prose.
_DEFAULT_KEY = "sk-or-v1-default-credential"
_CALL_KEY = "sk-or-v1-per-call-credential"

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
    kwargs.setdefault("api_key", SecretStr(_DEFAULT_KEY))
    return OpenRouterProvider(client, base_url=_BASE, **kwargs)


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
        assert request.headers["authorization"] == f"Bearer {_DEFAULT_KEY}"
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
async def test_structured_calls_route_under_the_tail_bounded_policy() -> None:
    # `require_parameters` excludes the fastest endpoint from every response_format
    # call (it does not implement the parameter), so a structured call always lands
    # in a slower pool whose throughput leaders have >10s latency tails. The bound
    # deprioritizes those. Without it a selector call — on the turn's first-byte
    # path — was measured at 10.75s.
    captured: dict = {}
    provider = _provider(_capture(captured))
    await provider.complete(_req(response_format={"type": "json_object"}))
    routing = captured["body"]["provider"]
    assert routing["preferred_max_latency"] == {"p90": 2.0, "p99": 5.0}
    # The privacy floor and the rest of the policy still apply unchanged.
    assert routing["data_collection"] == "deny"
    assert routing["sort"] == "throughput"
    assert routing["require_parameters"] is True
    assert routing["allow_fallbacks"] is True
    await provider.aclose()


@pytest.mark.asyncio
async def test_plain_chat_is_not_tail_bounded() -> None:
    # A chat turn reaches the fastest endpoint directly and is dominated by
    # generation, not first-byte: bounding it there would only shrink the pool.
    captured: dict = {}
    provider = _provider(_capture(captured))
    await provider.complete(_req())
    assert "preferred_max_latency" not in captured["body"]["provider"]
    await provider.aclose()


@pytest.mark.asyncio
async def test_serving_endpoint_is_captured_for_logging() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "id": "x",
                "created": 0,
                "model": "m",
                "choices": [],
                "usage": {},
                "provider": "CoreWeave",
            },
        )

    provider = _provider(handler)
    result = await provider.complete(_req())
    assert result.endpoint == "CoreWeave"
    await provider.aclose()


@pytest.mark.asyncio
async def test_missing_serving_endpoint_is_none_not_an_error() -> None:
    provider = _provider(_capture({}))
    result = await provider.complete(_req())
    assert result.endpoint is None
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
    await provider.complete(_req(api_key=SecretStr(_CALL_KEY)))
    files = list(dump_dir.glob("*.json"))
    assert len(files) == 1
    text = files[0].read_text(encoding="utf-8")
    body = _json.loads(text)
    assert body["model"] == _MODEL
    # The credential is caller material and the dump writes full request bodies to
    # disk, so it must not appear there — neither as a body field nor anywhere in
    # the raw text. `_build_body` allowlists wire fields by hand; this test is what
    # stops a future switch to asdict()/vars() from silently leaking the key.
    assert "api_key" not in body
    assert '"api_key"' not in text  # not nested anywhere either, under any parent
    assert _CALL_KEY not in text
    assert _DEFAULT_KEY not in text
    await provider.aclose()


@pytest.mark.asyncio
async def test_stream_dump_also_excludes_the_credential(tmp_path) -> None:
    # open_stream dumps through the same _build_body, but via its own branch (it
    # adds stream/stream_options), so cover it rather than assume symmetry.
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, headers={"content-type": "text/event-stream"}, text=SSE_OK)

    dump_dir = tmp_path / "stream-dumps"
    provider = _provider(handler, dump_dir=str(dump_dir))
    stream = await provider.open_stream(_req(stream=True, api_key=SecretStr(_CALL_KEY)))
    text = next(iter(dump_dir.glob("*.json"))).read_text(encoding="utf-8")
    assert '"stream": true' in text.lower()  # the stream branch really ran
    assert '"api_key"' not in text
    assert _CALL_KEY not in text
    await stream.aclose()
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


# ---- per-request credential ------------------------------------------------


def _auth_capture(seen: dict):
    def handler(request: httpx.Request) -> httpx.Response:
        seen["auth"] = request.headers["authorization"]
        return httpx.Response(
            200, json={"id": "x", "created": 0, "model": "m", "choices": [], "usage": {}}
        )

    return handler


@pytest.mark.asyncio
async def test_per_request_api_key_overrides_default() -> None:
    # The credential travels with the call: one shared provider serves the selector,
    # orchestrator, task runner and learner, so the key cannot live on the instance.
    seen: dict = {}
    provider = _provider(_auth_capture(seen))
    await provider.complete(_req(api_key=SecretStr(_CALL_KEY)))
    assert seen["auth"] == f"Bearer {_CALL_KEY}"
    await provider.aclose()


@pytest.mark.asyncio
async def test_api_key_none_uses_default() -> None:
    # None means "use the provider's configured default" — the environment key.
    # This is what every consumer does today, and why nothing observable changed.
    seen: dict = {}
    provider = _provider(_auth_capture(seen))
    await provider.complete(_req(api_key=None))
    assert seen["auth"] == f"Bearer {_DEFAULT_KEY}"
    await provider.aclose()


@pytest.mark.asyncio
async def test_empty_per_request_api_key_does_not_fall_back_to_the_default() -> None:
    # An explicitly supplied empty credential is a CALLER error. It must NOT fall
    # back — that would spend this server's OpenRouter account on a request whose
    # owner failed to authenticate — and it must fail before any request goes out.
    seen: dict = {}
    provider = _provider(_auth_capture(seen))
    with pytest.raises(DaintreeError) as exc:
        await provider.complete(_req(api_key=SecretStr("")))
    assert exc.value.retryable is False  # a bad credential is permanent, not transient
    assert not seen  # no request was sent at all — it did not silently use the default
    assert _DEFAULT_KEY not in exc.value.message
    await provider.aclose()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "bad",
    [
        "",
        " ",
        "sk-or-v1-trailing ",
        "sk-or-v1-x\n",
        "sk or v1",
        "sk-or-v1-del\x7f",  # h11 ACCEPTS DEL, so a blocklist of controls misses it
        "sk-or-v1-nonlatin\U0001f600",  # would raise inside httpx's latin-1 encoder
    ],
)
async def test_header_unsafe_credentials_are_rejected_without_echoing_the_value(
    bad: str,
) -> None:
    # h11 rejects an empty or whitespace-padded header value, and its
    # LocalProtocolError embeds the WHOLE value ("Illegal header value b'Bearer
    # sk-…'"), which httpcore logs at DEBUG. So a malformed credential must be
    # caught here, before httpx sees it, and the error must carry none of it.
    seen: dict = {}
    provider = _provider(_auth_capture(seen))
    with pytest.raises(DaintreeError) as exc:
        await provider.complete(_req(api_key=SecretStr(bad)))
    assert not seen  # never reached the transport
    # The distinctive stem is what must not appear; every case above shares it or
    # is pure whitespace, so this is a real assertion rather than a tautology.
    assert "sk-or-v1" not in exc.value.message
    await provider.aclose()


@pytest.mark.asyncio
async def test_a_realistic_credential_is_not_falsely_rejected() -> None:
    # The guard's real risk is over-rejection: a false positive breaks live traffic.
    # A full-length `sk-or-v1-` + 64 hex key must sail straight through.
    key = "sk-or-v1-" + "0123456789abcdef" * 4
    seen: dict = {}
    provider = _provider(_auth_capture(seen))
    await provider.complete(_req(api_key=SecretStr(key)))
    assert seen["auth"] == f"Bearer {key}"
    await provider.aclose()


@pytest.mark.asyncio
async def test_stream_uses_per_request_api_key() -> None:
    # open_stream builds its own headers; a forgotten argument there would send the
    # server's key on every streaming turn while the non-streaming path looked fine.
    seen: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["auth"] = request.headers["authorization"]
        return httpx.Response(200, headers={"content-type": "text/event-stream"}, text=SSE_OK)

    provider = _provider(handler)
    stream = await provider.open_stream(_req(stream=True, api_key=SecretStr(_CALL_KEY)))
    assert seen["auth"] == f"Bearer {_CALL_KEY}"
    await stream.aclose()
    await provider.aclose()


@pytest.mark.asyncio
async def test_structured_tool_call_forwards_per_request_api_key() -> None:
    # structured_tool_call rebuilds an internal sub-request field by field; a
    # dropped api_key there would silently use the default on every forced
    # tool-call path. The sentinels differ so the omission is detectable.
    seen: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["auth"] = request.headers["authorization"]
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
                            "tool_calls": [
                                {
                                    "id": "c1",
                                    "type": "function",
                                    "function": {"name": "pick", "arguments": '{"a":1}'},
                                }
                            ],
                        },
                        "finish_reason": "tool_calls",
                    }
                ],
                "usage": {},
            },
        )

    provider = _provider(handler)
    result = await provider.structured_tool_call(
        _req(api_key=SecretStr(_CALL_KEY)),
        function_name="pick",
        function_schema={"type": "object", "properties": {"a": {"type": "integer"}}},
    )
    assert result.name == "pick"
    assert seen["auth"] == f"Bearer {_CALL_KEY}"
    await provider.aclose()


def test_provider_chat_request_repr_omits_api_key() -> None:
    # A plain dataclass reprs every field by default, and a request object can end
    # up in a traceback or a debug line. repr=False keeps the credential out.
    request = _req(api_key=SecretStr(_CALL_KEY))
    text = repr(request)
    assert _CALL_KEY not in text
    assert "api_key" not in text


@pytest.mark.asyncio
async def test_complete_redacts_api_key_echoed_by_upstream() -> None:
    # The one path by which a caller's key could escape: the upstream reflects it
    # in an error body, and that text goes BOTH to the structured log and into the
    # client error envelope. Redaction runs on the DECODED diagnostic and before
    # the length cap, so neither a JSON escape nor a truncation can smuggle it out.
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            400, json={"error": {"message": f"invalid key {_CALL_KEY} for this account"}}
        )

    provider = _provider(handler)
    with structlog.testing.capture_logs() as logs, pytest.raises(DaintreeError) as exc:
        await provider.complete(_req(api_key=SecretStr(_CALL_KEY)))
    assert _CALL_KEY not in exc.value.message
    assert _REDACTED in exc.value.message
    # The reason itself must survive redaction — this stays a usable diagnostic.
    assert "for this account" in exc.value.message
    # Assert the log POSITIVELY, so an empty capture cannot pass this vacuously.
    events = [e for e in logs if e.get("event") == "upstream_http_error"]
    assert len(events) == 1
    assert _CALL_KEY not in str(events[0])
    assert _REDACTED in str(events[0]["detail"])
    await provider.aclose()


@pytest.mark.asyncio
async def test_stream_redacts_api_key_echoed_by_upstream() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(400, text=f"rejected credential {_CALL_KEY} here")

    provider = _provider(handler)
    with structlog.testing.capture_logs() as logs, pytest.raises(DaintreeError) as exc:
        await provider.open_stream(_req(stream=True, api_key=SecretStr(_CALL_KEY)))
    assert _CALL_KEY not in exc.value.message
    # Redacted, not merely discarded — the surrounding reason must still be there.
    assert _REDACTED in exc.value.message
    assert "rejected credential" in exc.value.message
    events = [e for e in logs if e.get("event") == "upstream_http_error"]
    assert len(events) == 1
    assert events[0]["phase"] == "stream_open"
    assert _CALL_KEY not in str(events[0])
    await provider.aclose()


@pytest.mark.asyncio
async def test_default_api_key_is_also_redacted_from_upstream_errors() -> None:
    # observability.py's rule is "no bearer keys" — not "no CALLER bearer keys".
    # Redacting the EFFECTIVE key covers the fallback case for free.
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(400, text=f"bad key {_DEFAULT_KEY} here")

    provider = _provider(handler)
    with structlog.testing.capture_logs() as logs, pytest.raises(DaintreeError) as exc:
        await provider.complete(_req())
    assert _DEFAULT_KEY not in exc.value.message
    assert _REDACTED in exc.value.message
    events = [e for e in logs if e.get("event") == "upstream_http_error"]
    assert len(events) == 1
    assert _DEFAULT_KEY not in str(events[0])
    await provider.aclose()


@pytest.mark.asyncio
async def test_retried_call_reuses_the_per_request_credential() -> None:
    # retry_upstream wraps an opaque closure, so today the same request object —
    # and its key — is reused. This locks that in: a future refactor that rebuilt
    # the request per attempt could silently fall back to the server default on
    # attempt two, which no other test would notice.
    seen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request.headers["authorization"])
        if len(seen) == 1:
            return httpx.Response(503, json={"error": {"message": "transient"}})
        return httpx.Response(
            200, json={"id": "x", "created": 0, "model": "m", "choices": [], "usage": {}}
        )

    provider = _provider(handler)
    request = _req(api_key=SecretStr(_CALL_KEY))
    await retry_upstream(
        lambda: provider.complete(request), retries=1, backoff_base_seconds=0.0
    )
    assert seen == [f"Bearer {_CALL_KEY}"] * 2
    await provider.aclose()


@pytest.mark.asyncio
async def test_concurrent_calls_do_not_cross_credentials() -> None:
    # The issue requires the httpx client and its pool stay SHARED, with only the
    # header varying. A refactor that mutated client.headers instead would bleed
    # one caller's key into another's in-flight request.
    import json as _json

    started = asyncio.Event()
    seen: dict[str, str] = {}

    async def handler(request: httpx.Request) -> httpx.Response:
        model = _json.loads(request.content)["model"]
        seen[model] = request.headers["authorization"]
        # Hold the first call open until the second is in flight, so the two
        # genuinely overlap on the shared client rather than serializing.
        if model == "model-a":
            started.set()
            await asyncio.sleep(0.05)
        else:
            await started.wait()
        return httpx.Response(
            200, json={"id": "x", "created": 0, "model": "m", "choices": [], "usage": {}}
        )

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    provider = OpenRouterProvider(client, api_key=SecretStr(_DEFAULT_KEY), base_url=_BASE)
    await asyncio.gather(
        provider.complete(_req(model="model-a", api_key=SecretStr("sk-or-v1-caller-a"))),
        provider.complete(_req(model="model-b", api_key=SecretStr("sk-or-v1-caller-b"))),
    )
    assert seen["model-a"] == "Bearer sk-or-v1-caller-a"
    assert seen["model-b"] == "Bearer sk-or-v1-caller-b"
    await provider.aclose()


def test_empty_secret_does_not_mangle_the_upstream_reason() -> None:
    # str.replace("") splices the placeholder between every character. A request
    # can no longer carry an empty credential this far (_resolve_key rejects it),
    # so this guards the helper directly — it stays defensive for any future caller.
    body = b'{"error": {"message": "temperature too high"}}'
    assert _extract_upstream_error(body, secret=SecretStr("")) == "temperature too high"


def test_json_escaped_credential_is_still_redacted() -> None:
    # JSON may encode an ordinary key as \\u0073\\u006b…: the undecoded body then
    # contains no literal match, but json.loads faithfully restores the plaintext.
    # Redacting the DECODED diagnostic is what closes that bypass.
    escaped = "".join(f"\\u{ord(c):04x}" for c in _CALL_KEY)
    body = f'{{"error": {{"message": "invalid {escaped} for account"}}}}'.encode()
    # Precondition: the bypass premise holds — no literal key in the raw bytes.
    assert _CALL_KEY not in body.decode()
    detail = _extract_upstream_error(body, secret=SecretStr(_CALL_KEY))
    assert detail is not None
    assert _CALL_KEY not in detail
    assert _REDACTED in detail
    assert "for account" in detail  # the diagnostic still survives


def test_credential_is_redacted_before_the_length_cap() -> None:
    # Capping first would slice the key and leave a usable prefix behind, so the
    # order is load-bearing. Position the key to STRADDLE the cap boundary: it must
    # start before the cap (so a prefix would survive a cap-first implementation)
    # and end after it (so the trailing bytes get cut).
    start = _ERROR_DETAIL_CAP - len(_CALL_KEY) // 2
    prefix = "upstream said "
    raw = prefix + "x" * (start - len(prefix)) + _CALL_KEY + " rejected"
    # Precondition: the key genuinely spans the boundary in the UNREDACTED text.
    assert start < _ERROR_DETAIL_CAP < start + len(_CALL_KEY)

    detail = _extract_upstream_error(raw.encode(), secret=SecretStr(_CALL_KEY))
    assert detail is not None
    assert _REDACTED in detail
    assert len(detail) <= _ERROR_DETAIL_CAP
    # No usable prefix of the key survives. A cap-first implementation would leave
    # _CALL_KEY[:14] here, which this catches.
    assert _CALL_KEY[:12] not in detail


def test_all_occurrences_of_the_credential_are_redacted() -> None:
    body = f'{{"error": {{"message": "{_CALL_KEY} and again {_CALL_KEY}"}}}}'.encode()
    detail = _extract_upstream_error(body, secret=SecretStr(_CALL_KEY))
    assert detail is not None
    assert _CALL_KEY not in detail


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
    provider = OpenRouterProvider(
        client, api_key=SecretStr(_DEFAULT_KEY), base_url=_BASE, default_timeout=840.0
    )
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
