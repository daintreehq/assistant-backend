"""Cerebras provider adapter tests using a mock HTTP transport."""

from __future__ import annotations

import asyncio
import json as _json
from typing import Any

import httpx
import pytest
import structlog.testing
from pydantic import SecretStr

from daintree_assistant_server.errors import DaintreeError
from daintree_assistant_server.providers.base import ProviderChatRequest
from daintree_assistant_server.providers.cerebras import (
    _ERROR_DETAIL_CAP,
    _REDACTED,
    CerebrasProvider,
    _extract_upstream_error,
)
from daintree_assistant_server.providers.retry import retry_upstream

_BASE = "https://api.cerebras.ai/v1"
_MODEL = "gemma-4-31b"
# The provider's constructor default (fallback) key, and a distinct per-call key.
# They must differ so a dropped per-call credential is detectable rather than
# accidentally matching the fallback. Both are long, distinctive sentinels so a
# test can assert the literal value is ABSENT without matching incidental prose.
_DEFAULT_KEY = "csk-default-credential"
_CALL_KEY = "csk-per-call-credential"

SSE_OK = (
    'data: {"id":"x","object":"chat.completion.chunk","created":1,"model":"gemma-4-31b",'
    '"choices":[{"index":0,"delta":{"role":"assistant","content":"Hel"}}]}\n\n'
    'data: {"id":"x","object":"chat.completion.chunk","created":1,"model":"gemma-4-31b",'
    '"choices":[{"index":0,"delta":{"content":"lo"}}]}\n\n'
    'data: {"id":"x","object":"chat.completion.chunk","created":1,"model":"gemma-4-31b",'
    '"choices":[{"index":0,"delta":{},"finish_reason":"stop"}],'
    '"usage":{"prompt_tokens":5,"completion_tokens":2,"total_tokens":7}}\n\n'
    "data: [DONE]\n\n"
)


def _provider(handler, **kwargs) -> CerebrasProvider:
    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    kwargs.setdefault("api_key", SecretStr(_DEFAULT_KEY))
    return CerebrasProvider(client, base_url=_BASE, **kwargs)


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
                "model": "gemma-4-31b",
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
async def test_reasoning_effort_is_a_bare_string() -> None:
    # Cerebras takes a flat `reasoning_effort`, NOT OpenRouter's `reasoning`
    # object — which it rejects with a 400. Only the skill-learning calls on
    # gpt-oss-120b ever set this; the serving model has no reasoning mode.
    captured: dict = {}
    provider = _provider(_capture(captured))
    await provider.complete(_req(thinking_disabled=False, reasoning_effort="high"))
    assert captured["body"]["reasoning_effort"] == "high"
    assert "reasoning" not in captured["body"]
    await provider.aclose()


@pytest.mark.asyncio
async def test_a_non_thinking_call_sends_nothing_at_all() -> None:
    """Omission IS the non-thinking posture, and it has to be.

    There is no "off" value on this wire — `reasoning_effort` takes
    low/medium/high and nothing else — so a non-thinking call cannot state its
    posture, only decline to state one. The explicit-disable the previous adapter
    sent has no expressible equivalent, and sending the old object is a 400.
    """
    captured: dict = {}
    provider = _provider(_capture(captured))
    await provider.complete(_req(thinking_disabled=True, reasoning_effort="high"))
    assert "reasoning_effort" not in captured["body"]
    assert "reasoning" not in captured["body"]
    await provider.aclose()


@pytest.mark.asyncio
async def test_thinking_enabled_with_no_tier_sends_no_field_either() -> None:
    # There is no bare "enable" to send: with no tier, the model's own default
    # applies and the field is simply absent.
    captured: dict = {}
    provider = _provider(_capture(captured))
    await provider.complete(_req(thinking_disabled=False, reasoning_effort=None))
    assert "reasoning_effort" not in captured["body"]
    assert "reasoning" not in captured["body"]
    await provider.aclose()


@pytest.mark.asyncio
async def test_max_tokens_is_sent_under_exactly_one_name() -> None:
    """The trap that breaks every request if it is ever got wrong.

    Cerebras accepts `max_tokens` OR `max_completion_tokens` alone, but 400s when
    both arrive. The normalized field here is called `max_tokens`, so passing it
    straight through *works* — right up until something also sets the other name,
    at which point every single call fails.
    """
    captured: dict = {}
    provider = _provider(_capture(captured))
    await provider.complete(_req(max_tokens=512))
    assert captured["body"]["max_completion_tokens"] == 512
    assert "max_tokens" not in captured["body"]
    await provider.aclose()


@pytest.mark.asyncio
@pytest.mark.parametrize("field", ["provider", "session_id", "reasoning", "usage"])
async def test_no_openrouter_only_field_is_ever_sent(field: str) -> None:
    """Each of these was on every request under the previous adapter.

    Each is now a hard `400 wrong_api_format`. They are checked here rather than
    discovered in production because the failure is total — not one degraded
    feature but every call — and nothing in a mocked test would otherwise notice
    a stray field.
    """
    captured: dict = {}
    provider = _provider(_capture(captured))
    await provider.complete(
        _req(
            cache_session_id="daintree-main-abc123",
            thinking_disabled=False,
            reasoning_effort="high",
            response_format={"type": "json_object"},
        )
    )
    assert field not in captured["body"]
    await provider.aclose()


@pytest.mark.asyncio
async def test_cache_session_key_rides_the_openai_standard_field() -> None:
    # `prompt_cache_key` is the OpenAI-standard spelling and the one Cerebras
    # accepts. The spelling is the whole risk: `session_id` — OpenRouter's name
    # for the same idea — is a hard 400, and this field is attached to every main
    # turn and every utility task, so getting it wrong breaks everything.
    captured: dict = {}
    provider = _provider(_capture(captured))
    await provider.complete(_req(cache_session_id="daintree-main-abc123"))
    assert captured["body"]["prompt_cache_key"] == "daintree-main-abc123"
    await provider.aclose()


@pytest.mark.asyncio
async def test_no_cache_session_key_sends_no_field() -> None:
    # Absent, not null. A caller with no session says nothing about prefixes
    # rather than claiming an empty one.
    captured: dict = {}
    provider = _provider(_capture(captured))
    await provider.complete(_req())
    assert "prompt_cache_key" not in captured["body"]
    await provider.aclose()


@pytest.mark.asyncio
async def test_cache_session_key_is_sent_on_the_streaming_path() -> None:
    # open_stream has its own branch through _build_body. A turn streams, so
    # covering only `complete` would cover the path production never takes.
    captured: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["body"] = _json.loads(request.content)
        return httpx.Response(200, headers={"content-type": "text/event-stream"}, text=SSE_OK)

    provider = _provider(handler)
    stream = await provider.open_stream(_req(stream=True, cache_session_id="daintree-main-xyz"))
    assert captured["body"]["prompt_cache_key"] == "daintree-main-xyz"
    await stream.aclose()
    await provider.aclose()


@pytest.mark.asyncio
async def test_structured_tool_call_forwards_the_cache_session_key() -> None:
    # Same trap as the api_key case below: this sub-request is rebuilt field by
    # field, and it carries the parent's ENTIRE prompt. Dropping the key here
    # would send the turn's largest call off the node holding its prefix.
    captured: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["body"] = _json.loads(request.content)
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
                    }
                ],
                "usage": {},
            },
        )

    provider = _provider(handler)
    await provider.structured_tool_call(
        _req(cache_session_id="daintree-main-abc123"),
        function_name="pick",
        function_schema={"type": "object", "properties": {"a": {"type": "integer"}}},
    )
    assert captured["body"]["prompt_cache_key"] == "daintree-main-abc123"
    await provider.aclose()


def test_provider_chat_request_repr_omits_the_cache_session_key() -> None:
    # A stable per-session correlator, hashed but still a correlator. It rides
    # `repr=False` beside api_key so a traceback or debug line cannot spread it.
    text = repr(_req(cache_session_id="daintree-main-abc123"))
    assert "daintree-main-abc123" not in text
    assert "cache_session_id" not in text


@pytest.mark.asyncio
async def test_outbound_reasoning_content_is_renamed() -> None:
    # The Daintree contract and the CLI speak reasoning_content; Cerebras speaks
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
    assert exc.value.status_code == 503
    assert exc.value.code == "upstream_unavailable"
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
    assert exc.value.code == "upstream_request_rejected"
    assert "rejected this server's request" in exc.value.message.lower()
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
    assert exc.value.code == "upstream_protocol_error"
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
    assert exc.value.code == "upstream_protocol_error"
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
    assert exc.value.code == "upstream_protocol_error"
    await provider.aclose()


@pytest.mark.asyncio
async def test_complete_malformed_choices_raises_upstream_error() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"id": "x", "created": 0, "model": "m", "choices": ["bad"]})

    provider = _provider(handler)
    with pytest.raises(DaintreeError) as exc:
        await provider.complete(_req())
    assert exc.value.code == "upstream_protocol_error"
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
    # back — that would spend this server's Cerebras account on a request whose
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
        "csk-trailing ",
        "csk-x\n",
        "sk or v1",
        "csk-del\x7f",  # h11 ACCEPTS DEL, so a blocklist of controls misses it
        "csk-nonlatin\U0001f600",  # would raise inside httpx's latin-1 encoder
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
    # A full-length `csk-` + 64 hex key must sail straight through.
    key = "csk-" + "0123456789abcdef" * 4
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
    provider = CerebrasProvider(client, api_key=SecretStr(_DEFAULT_KEY), base_url=_BASE)
    await asyncio.gather(
        provider.complete(_req(model="model-a", api_key=SecretStr("csk-caller-a"))),
        provider.complete(_req(model="model-b", api_key=SecretStr("csk-caller-b"))),
    )
    assert seen["model-a"] == "Bearer csk-caller-a"
    assert seen["model-b"] == "Bearer csk-caller-b"
    await provider.aclose()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("status", "code", "hint"),
    [
        (401, "provider_invalid_api_key", "replace or rotate"),
        (402, "provider_insufficient_credits", "credits"),
        (403, "provider_key_forbidden", "permissions"),
    ],
)
async def test_provider_account_failures_are_distinguishable(
    status: int, code: str, hint: str
) -> None:
    """Three different problems with three different fixes.

    These used to collapse into one 502 ``upstream_error``, which told a tester
    with an exhausted balance that their credentials were rejected — so they
    replaced a perfectly good key. The CLI branches on ``error.code``, so only
    distinct codes can produce distinct advice.
    """

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            status, json={"error": {"message": f"account problem for {_CALL_KEY}"}}
        )

    provider = _provider(handler)
    with pytest.raises(DaintreeError) as exc:
        await provider.complete(_req(api_key=SecretStr(_CALL_KEY)))
    # The status mirrors the upstream's own semantics rather than flattening to
    # 502: these are genuinely the caller's account, not a gateway fault.
    assert exc.value.status_code == status
    assert exc.value.code == code
    assert hint in exc.value.message.lower()
    # Never echo upstream account prose or the credential it quoted back.
    assert _CALL_KEY not in exc.value.message
    assert "account problem" not in exc.value.message
    # Deterministic: replaying reaches the same answer, slower, and on the
    # streaming path burns the CLI's retry budget after meta committed a 200.
    assert exc.value.retryable is False
    await provider.aclose()


@pytest.mark.asyncio
async def test_provider_invalid_key_is_distinct_from_our_own_door_401() -> None:
    """Both are 401 and both mean "fix your key" — but not the same fix.

    Ours means the header was missing or malformed at Daintree's door; this one
    means the key travelled fine and Cerebras refused it. A client that cannot
    tell them apart cannot write correct copy for either.
    """
    from daintree_assistant_server.errors import invalid_api_key, provider_invalid_api_key

    ours = invalid_api_key()
    theirs = provider_invalid_api_key()
    assert ours.status_code == theirs.status_code == 401
    assert ours.code != theirs.code


@pytest.mark.asyncio
async def test_no_default_and_no_per_call_key_fails_cleanly() -> None:
    # The server constructs its provider with NO default credential; a call that
    # arrives without a per-call key is a propagation bug and must fail loudly
    # BEFORE any HTTP request — never fall back to some ambient account.
    reached = {"http": False}

    def handler(request: httpx.Request) -> httpx.Response:  # pragma: no cover
        reached["http"] = True
        return httpx.Response(200, json={})

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    provider = CerebrasProvider(client, base_url=_BASE)  # api_key omitted → None
    with pytest.raises(DaintreeError) as exc:
        await provider.complete(_req())
    assert exc.value.code == "upstream_error"
    assert reached["http"] is False
    await provider.aclose()


@pytest.mark.asyncio
async def test_keyless_provider_still_honors_a_per_call_key() -> None:
    captured: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["auth"] = request.headers["Authorization"]
        return httpx.Response(
            200, json={"id": "x", "created": 0, "model": "m", "choices": [], "usage": {}}
        )

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    provider = CerebrasProvider(client, base_url=_BASE)  # no default credential
    await provider.complete(_req(api_key=SecretStr(_CALL_KEY)))
    assert captured["auth"] == f"Bearer {_CALL_KEY}"
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


def _escape_first(text: str) -> str:
    """``text`` with only its first character JSON-escaped — the minimal evasion."""
    return f"\\u{ord(text[0]):04x}" + text[1:]


@pytest.mark.parametrize(
    ("label", "body"),
    [
        # Every branch that has no single message to quote used to echo the RAW
        # (still-encoded) body, so an escaped key slipped past the exact match and
        # any reader who parsed the detail got it back.
        ("error is a list", '{{"error":["bad {esc}"]}}'),
        ("top level not an object", '["bad {esc}"]'),
        ("error dict with no message", '{{"error":{{"detail":"bad {esc}"}}}}'),
        ("not json at all", "{{plain text bad {esc}"),
    ],
)
def test_escaped_credential_is_not_recoverable_from_any_fallback_branch(
    label: str, body: str
) -> None:
    key = "csk-" + "0123456789abcdef" * 4
    raw = body.format(esc=_escape_first(key))
    assert key not in raw  # precondition: no literal match for a scrub to find
    detail = _extract_upstream_error(raw.encode(), secret=SecretStr(key))
    assert detail is not None
    assert key not in detail
    # And not recoverable by decoding the escapes either, which is the whole point.
    assert key not in detail.encode("latin-1", "backslashreplace").decode("unicode_escape")


def test_double_encoded_credential_drops_the_diagnostic_entirely() -> None:
    # Two encoding layers defeat any single-pass scrub. Rather than emit something
    # a reader could decode, the detail is withheld — losing a rare message beats
    # leaking a key.
    key = "csk-" + "0123456789abcdef" * 4
    inner = _json.dumps({"k": _escape_first(key)})
    raw = _json.dumps({"error": {"message": inner}})
    detail = _extract_upstream_error(raw.encode(), secret=SecretStr(key))
    assert detail is not None
    assert key not in detail
    assert key not in detail.encode("latin-1", "backslashreplace").decode("unicode_escape")


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
    provider = CerebrasProvider(
        client, api_key=SecretStr(_DEFAULT_KEY), base_url=_BASE, default_timeout=840.0
    )
    await provider.complete(_req(timeout=25.0))
    assert captured["timeout"] == {"connect": 10.0, "read": 25.0, "write": 30.0, "pool": 10.0}
    await provider.aclose()


@pytest.mark.asyncio
async def test_stream_transport_interruption_is_retryable() -> None:
    from daintree_assistant_server.providers.cerebras import CerebrasStream

    class _Ctx:
        async def __aexit__(self, *_a: object) -> bool:
            return False

    class _Resp:
        async def aiter_bytes(self):  # noqa: ANN202 - test stub
            raise httpx.ReadError("connection dropped")
            yield b""  # pragma: no cover - makes this an async generator

    stream = CerebrasStream(_Ctx(), _Resp(), _MODEL)
    with pytest.raises(DaintreeError) as exc:
        await stream.prefetch_first_event()
    assert exc.value.code == "upstream_unavailable"
    assert exc.value.retryable is True
    await stream.aclose()


@pytest.mark.asyncio
async def test_structured_tool_call() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        import json as _json

        body = _json.loads(request.content)
        assert body["tool_choice"]["function"]["name"] == "select_skills"
        # A forced tool call must go non-thinking.
        assert "reasoning_effort" not in body
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
    # Even if the caller enabled thinking, a forced tool call goes non-thinking.
    # A forced tool_choice and a chain-of-thought are an awkward pair on every
    # model this server has run against, and the caller here wants one object
    # back rather than reasoning about it.
    captured: dict = {}
    provider = _provider(_capture(captured))
    await provider.structured_tool_call(
        _req(thinking_disabled=False, reasoning_effort="high"),
        function_name="select_skills",
        function_schema={"type": "object"},
    )
    assert "reasoning_effort" not in captured["body"]
    assert "reasoning" not in captured["body"]
    await provider.aclose()


# --- key verification (sign-in probe) ---------------------------------------


@pytest.mark.asyncio
async def test_verify_key_maps_a_provider_401_to_invalid_api_key() -> None:
    """A 401/403 is a VERDICT on the key and must be distinguishable from a failure."""

    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(401, json={"error": {"message": "no"}})

    transport = httpx.MockTransport(handler)
    async with httpx.AsyncClient(transport=transport) as client:
        provider = CerebrasProvider(client, base_url="https://cerebras.test/v1")
        with pytest.raises(DaintreeError) as exc:
            await provider.verify_key(SecretStr("csk-bad"))

    assert exc.value.code == "invalid_api_key"


@pytest.mark.asyncio
async def test_verify_key_maps_other_failures_to_upstream_error() -> None:
    """A 500 means we could not get an answer — never report that as an invalid key."""

    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500, text="boom")

    transport = httpx.MockTransport(handler)
    async with httpx.AsyncClient(transport=transport) as client:
        provider = CerebrasProvider(client, base_url="https://cerebras.test/v1")
        with pytest.raises(DaintreeError) as exc:
            await provider.verify_key(SecretStr("csk-caller"))

    assert exc.value.code == "upstream_unavailable"


@pytest.mark.asyncio
async def test_verify_key_never_leaks_the_key_in_an_error() -> None:
    """The upstream ECHOES the key back; none of it may reach the client envelope.

    A body that never contained the secret would let a completely unsafe passthrough
    pass this test, so the failure body quotes the credential verbatim — which is what
    a real provider error does when it complains about the key it was given.
    """
    secret = "csk-supersecret"  # noqa: S105 - test credential, not a real key

    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500, text=f"upstream blew up on {secret}")

    transport = httpx.MockTransport(handler)
    async with httpx.AsyncClient(transport=transport) as client:
        provider = CerebrasProvider(client, base_url="https://cerebras.test/v1")
        with pytest.raises(DaintreeError) as exc:
            await provider.verify_key(SecretStr(secret))

    # Both the human message and the serialized wire envelope — the message alone
    # would miss a leak that rode a structured field out to the client.
    assert "supersecret" not in str(exc.value)
    assert "supersecret" not in exc.value.envelope().model_dump_json()


@pytest.mark.asyncio
async def test_verify_key_returns_the_reachable_models() -> None:
    """The probe answers more than "is this key valid?".

    Cerebras has no key-introspection endpoint, so the probe is a model listing —
    and that is a better answer than a bare verdict: it also proves the account
    can reach the model this server is pinned to, which a 200 alone would hide.
    """

    async def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path.endswith("/models")
        assert request.headers["Authorization"] == f"Bearer {_CALL_KEY}"
        return httpx.Response(
            200,
            json={
                "object": "list",
                "data": [
                    {"id": "gemma-4-31b", "object": "model"},
                    {"id": "gpt-oss-120b", "object": "model"},
                    {"not": "a model entry"},
                    "garbage",
                ],
            },
        )

    transport = httpx.MockTransport(handler)
    async with httpx.AsyncClient(transport=transport) as client:
        provider = CerebrasProvider(client, base_url=_BASE)
        record = await provider.verify_key(SecretStr(_CALL_KEY))

    # Malformed entries are dropped rather than coerced: this list is reported to
    # a caller and read back by the doctor, so a non-string id has no business in
    # it.
    assert record == {"models": ["gemma-4-31b", "gpt-oss-120b"]}


@pytest.mark.parametrize(
    "payload",
    [
        {},
        {"data": None},
        {"data": "nope"},
        {"data": {"id": "x"}},
        {"error": {"message": "temporarily unavailable"}},
        [{"data": []}],
        None,
    ],
    ids=["empty", "null-data", "string-data", "dict-data", "error-envelope", "list", "null"],
)
@pytest.mark.asyncio
async def test_verify_key_rejects_a_200_that_is_not_a_model_listing(payload: object) -> None:
    """A 200 whose body is not a listing answers nothing.

    Degrading it to an empty record would surface as `valid: true` — the exact
    false positive this probe exists to prevent — so it is an error, not a
    default.
    """

    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=payload)

    transport = httpx.MockTransport(handler)
    async with httpx.AsyncClient(transport=transport) as client:
        provider = CerebrasProvider(client, base_url=_BASE)
        with pytest.raises(DaintreeError) as exc:
            await provider.verify_key(SecretStr(_CALL_KEY))
    assert exc.value.code == "upstream_protocol_error"


@pytest.mark.asyncio
async def test_verify_key_rejects_a_redirect_instead_of_reading_it_as_valid() -> None:
    """EXACTLY 200, deliberately — not `< 400`.

    The client does not follow redirects, so a 3xx arrives here as-is, and a
    body-shaped 302 would otherwise read as a successful verification.
    """

    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(302, json={"object": "list", "data": []})

    transport = httpx.MockTransport(handler)
    async with httpx.AsyncClient(transport=transport) as client:
        provider = CerebrasProvider(client, base_url=_BASE)
        with pytest.raises(DaintreeError) as exc:
            await provider.verify_key(SecretStr(_CALL_KEY))
    assert exc.value.code != "invalid_api_key"


@pytest.mark.asyncio
async def test_verify_key_keeps_a_rate_limit_as_a_rate_limit() -> None:
    """429 is retryable and carries Retry-After; flattening it to 502 loses both."""

    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(429, headers={"Retry-After": "30"})

    transport = httpx.MockTransport(handler)
    async with httpx.AsyncClient(transport=transport) as client:
        provider = CerebrasProvider(client, base_url="https://cerebras.test/v1")
        with pytest.raises(DaintreeError) as exc:
            await provider.verify_key(SecretStr("csk-caller"))

    assert exc.value.code == "upstream_rate_limited"
    assert exc.value.headers == {"Retry-After": "30"}


@pytest.mark.asyncio
async def test_verify_key_maps_a_transport_timeout_to_upstream_timeout() -> None:
    async def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectTimeout("too slow")

    transport = httpx.MockTransport(handler)
    async with httpx.AsyncClient(transport=transport) as client:
        provider = CerebrasProvider(client, base_url="https://cerebras.test/v1")
        with pytest.raises(DaintreeError) as exc:
            await provider.verify_key(SecretStr("csk-caller"))

    assert exc.value.code == "upstream_timeout"


@pytest.mark.asyncio
async def test_verify_key_maps_a_transport_error_to_upstream_error() -> None:
    async def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("no route")

    transport = httpx.MockTransport(handler)
    async with httpx.AsyncClient(transport=transport) as client:
        provider = CerebrasProvider(client, base_url="https://cerebras.test/v1")
        with pytest.raises(DaintreeError) as exc:
            await provider.verify_key(SecretStr("csk-caller"))

    assert exc.value.code == "upstream_unavailable"


@pytest.mark.asyncio
async def test_verify_key_does_not_inherit_the_main_model_timeout() -> None:
    """The probe sits behind a sign-in spinner; the 14-minute model budget is not it.

    A bare float would also collapse connect/write/pool onto the same value — the trap
    ``_timeout`` documents — so assert the structure survived, not just the read bound.
    """
    captured: dict[str, Any] = {}

    async def handler(request: httpx.Request) -> httpx.Response:
        captured["timeout"] = request.extensions.get("timeout")
        return httpx.Response(200, json={"object": "list", "data": [{"id": "gemma-4-31b"}]})

    transport = httpx.MockTransport(handler)
    base = httpx.Timeout(connect=5.0, read=840.0, write=15.0, pool=5.0)
    async with httpx.AsyncClient(transport=transport, timeout=base) as client:
        provider = CerebrasProvider(
            client, base_url="https://cerebras.test/v1", default_timeout=840.0
        )
        await provider.verify_key(SecretStr("csk-caller"))

    assert captured["timeout"]["read"] == 10.0
    assert captured["timeout"]["connect"] == 5.0
    assert captured["timeout"]["write"] == 15.0
    assert captured["timeout"]["pool"] == 5.0
