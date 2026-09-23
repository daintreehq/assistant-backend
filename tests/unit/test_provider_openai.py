"""OpenAI adapter: the translation layer, and the fields this host refuses.

``OpenAIProvider`` inherits error mapping, retries, redaction and the key probe
from ``OpenRouterProvider`` — the parent's suite covers those. What is NOT
inherited is everything on the wire: this adapter speaks the Responses API and
translates to and from the chat shape the rest of the server uses, so a
translation bug is invisible everywhere above it until a real turn fails.

The body rules are pinned against a host that 400s rather than ignores: every
field listed as absent below was measured to be rejected for this model, so a
regression here is a failed turn, not a wasted byte.
"""

from __future__ import annotations

import copy
import json
from typing import Any

import httpx
import pytest
from pydantic import SecretStr

from daintree_assistant_server.config import (
    EXECUTOR_MODEL,
    EXECUTOR_ORIGIN,
    EXECUTOR_REASONING_EFFORT,
    Settings,
)
from daintree_assistant_server.errors import DaintreeError
from daintree_assistant_server.providers.base import ProviderChatRequest
from daintree_assistant_server.providers.openai import OpenAIProvider

_TOOL = {
    "type": "function",
    "function": {
        "name": "fs__read",
        "description": "Read a file.",
        "parameters": {
            "type": "object",
            "properties": {"path": {"type": "string"}, "offset": {"type": "integer"}},
            "required": ["path"],
        },
    },
}

_MODELS_BODY = {"object": "list", "data": [{"id": "gpt-6-luna", "object": "model"}]}


def _provider(handler: Any = None) -> OpenAIProvider:
    transport = httpx.MockTransport(handler or _never_called)
    client = httpx.AsyncClient(transport=transport, timeout=httpx.Timeout(5.0))
    return OpenAIProvider(client, api_key=SecretStr("sk-test"), reasoning_effort="medium")


def _never_called(request: httpx.Request) -> httpx.Response:
    raise AssertionError("body construction must not touch the transport")


def _request(**overrides: Any) -> ProviderChatRequest:
    fields: dict[str, Any] = {
        "model": EXECUTOR_MODEL,
        "messages": [{"role": "user", "content": "hi"}],
    }
    fields.update(overrides)
    return ProviderChatRequest(**fields)


def _body(**overrides: Any) -> dict[str, Any]:
    return _provider()._build_body(_request(**overrides), stream=False)  # noqa: SLF001


# ---------------------------------------------------------------------------
# The body.
# ---------------------------------------------------------------------------


def test_fields_this_model_rejects_are_never_sent() -> None:
    body = _body(temperature=0.2, stop=["x"], max_tokens=300, thinking_disabled=True)
    for rejected in ("temperature", "stop", "max_tokens", "max_completion_tokens",
                     "reasoning_effort", "messages", "stream_options"):
        assert rejected not in body, rejected
    assert body["max_output_tokens"] == 300
    assert body["store"] is False


@pytest.mark.parametrize("disabled", [True, False])
@pytest.mark.parametrize("asked", [None, "low", "high"])
def test_one_reasoning_posture_whatever_the_request_asks(
    disabled: bool, asked: str | None
) -> None:
    """A caller-reachable second posture would be a mode by another name."""
    body = _body(thinking_disabled=disabled, reasoning_effort=asked)
    assert body["reasoning"] == {"effort": "medium"}


def test_the_shipped_posture_is_medium() -> None:
    assert EXECUTOR_REASONING_EFFORT == "medium"


def test_tools_are_flattened_and_sent_non_strict() -> None:
    """Strict is the Responses default and the CLI's optional fields break it."""
    body = _body(tools=[_TOOL], tool_choice="auto")
    assert body["tools"] == [
        {
            "type": "function",
            "name": "fs__read",
            "description": "Read a file.",
            "parameters": _TOOL["function"]["parameters"],
            "strict": False,
        }
    ]
    assert body["tool_choice"] == "auto"


def test_a_forced_tool_choice_is_translated() -> None:
    body = _body(
        tools=[_TOOL], tool_choice={"type": "function", "function": {"name": "fs__read"}}
    )
    assert body["tool_choice"] == {"type": "function", "name": "fs__read"}


def test_json_object_becomes_a_text_format() -> None:
    assert _body(response_format={"type": "json_object"})["text"] == {
        "format": {"type": "json_object"}
    }
    assert "text" not in _body()


def test_a_tiny_output_cap_is_raised_to_the_host_minimum() -> None:
    assert _body(max_tokens=1)["max_output_tokens"] == 16


def test_prompt_cache_key_fits_the_host_limit() -> None:
    assert _body(cache_session_id="daintree-task:x")["prompt_cache_key"] == "daintree-task:x"
    long_key = "k" * 78
    hashed = _body(cache_session_id=long_key)["prompt_cache_key"]
    assert len(hashed) == 64
    assert hashed == _body(cache_session_id=long_key)["prompt_cache_key"]
    assert "prompt_cache_key" not in _body()


def test_stream_flag() -> None:
    body = _provider()._build_body(_request(include_usage=True), stream=True)  # noqa: SLF001
    assert body["stream"] is True
    assert "stream_options" not in body


def test_a_tool_round_translates_to_paired_input_items() -> None:
    messages = [
        {"role": "system", "content": "base"},
        {
            "role": "user",
            "name": "daintree_compaction",
            "content": [
                {"type": "text", "text": "look"},
                {"type": "image_url", "image_url": {"url": "data:image/png;base64,AA=="}},
            ],
        },
        {
            "role": "assistant",
            "content": "Reading.",
            "reasoning_content": "private",
            "tool_calls": [
                {"id": "c1", "type": "function",
                 "function": {"name": "fs__read", "arguments": '{"path":"a"}'}},
                {"id": "c2", "type": "function",
                 "function": {"name": "fs__read", "arguments": ""}},
            ],
        },
        {"role": "tool", "tool_call_id": "c1", "content": "A"},
        {"role": "tool", "tool_call_id": "c2", "content": [{"type": "text", "text": "B"}]},
    ]
    original = copy.deepcopy(messages)
    items = _body(messages=messages)["input"]

    assert messages == original, "the caller's messages must not be mutated"
    assert items == [
        {"role": "system", "content": "base"},
        {
            "role": "user",
            "content": [
                {"type": "input_text", "text": "look"},
                {"type": "input_image", "image_url": "data:image/png;base64,AA=="},
            ],
        },
        {"role": "assistant", "content": "Reading."},
        {"type": "function_call", "call_id": "c1", "name": "fs__read",
         "arguments": '{"path":"a"}'},
        {"type": "function_call", "call_id": "c2", "name": "fs__read", "arguments": "{}"},
        {"type": "function_call_output", "call_id": "c1", "output": "A"},
        {"type": "function_call_output", "call_id": "c2",
         "output": [{"type": "input_text", "text": "B"}]},
    ]


# ---------------------------------------------------------------------------
# Non-streaming results.
# ---------------------------------------------------------------------------


def _response(output: list[dict[str, Any]], **extra: Any) -> dict[str, Any]:
    body: dict[str, Any] = {
        "id": "resp_1",
        "created_at": 1,
        "model": "gpt-6-luna",
        "status": "completed",
        "output": output,
        "usage": {
            "input_tokens": 100,
            "input_tokens_details": {"cached_tokens": 80},
            "output_tokens": 30,
            "output_tokens_details": {"reasoning_tokens": 12},
            "total_tokens": 130,
        },
    }
    body.update(extra)
    return body


async def _complete(payload: dict[str, Any]) -> Any:
    seen: dict[str, Any] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["url"] = str(request.url)
        seen["body"] = json.loads(request.content)
        return httpx.Response(200, json=payload)

    result = await _provider(handler).complete(_request(max_tokens=100))
    return result, seen


@pytest.mark.asyncio
async def test_a_text_reply_reads_as_a_chat_completion() -> None:
    result, seen = await _complete(
        _response(
            [
                {"type": "reasoning", "summary": []},
                {"type": "message", "role": "assistant",
                 "content": [{"type": "output_text", "text": "Hel"},
                             {"type": "output_text", "text": "lo"}]},
            ]
        )
    )
    assert seen["url"] == "https://api.openai.com/v1/responses"
    assert result.choices[0]["message"] == {"role": "assistant", "content": "Hello"}
    assert result.finish_reason == "stop"
    assert result.usage.prompt_tokens == 100
    assert result.usage.completion_tokens == 30
    assert result.usage.cached_tokens == 80
    assert result.usage.cost is None


@pytest.mark.asyncio
async def test_function_calls_read_as_tool_calls() -> None:
    result, _ = await _complete(
        _response(
            [{"type": "function_call", "id": "fc_1", "call_id": "call_1",
              "name": "fs__read", "arguments": '{"path":"a"}'}]
        )
    )
    assert result.finish_reason == "tool_calls"
    assert result.tool_calls == [
        {"id": "call_1", "type": "function",
         "function": {"name": "fs__read", "arguments": '{"path":"a"}'}}
    ]
    assert result.choices[0]["message"]["content"] is None


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("reason", "finish"),
    [("max_output_tokens", "length"), ("content_filter", "content_filter")],
)
async def test_an_incomplete_response_keeps_its_reason(reason: str, finish: str) -> None:
    result, _ = await _complete(
        _response([], status="incomplete", incomplete_details={"reason": reason})
    )
    assert result.finish_reason == finish


@pytest.mark.asyncio
async def test_a_forced_single_object_call_returns_its_arguments() -> None:
    payload = _response(
        [{"type": "function_call", "id": "fc_1", "call_id": "call_1",
          "name": "classify", "arguments": '{"label":"x"}'}]
    )
    seen: dict[str, Any] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["body"] = json.loads(request.content)
        return httpx.Response(200, json=payload)

    result = await _provider(handler).structured_tool_call(
        _request(), function_name="classify", function_schema={"type": "object"}
    )
    assert result.arguments_json == '{"label":"x"}'
    assert seen["body"]["tool_choice"] == {"type": "function", "name": "classify"}
    assert seen["body"]["reasoning"] == {"effort": "medium"}


# ---------------------------------------------------------------------------
# Streams.
# ---------------------------------------------------------------------------


def _sse(events: list[dict[str, Any]]) -> bytes:
    return "".join(f"event: {e['type']}\ndata: {json.dumps(e)}\n\n" for e in events).encode()


async def _stream(events: list[dict[str, Any]]) -> tuple[list[dict[str, Any]], Any]:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200, content=_sse(events), headers={"content-type": "text/event-stream"}
        )

    stream = await _provider(handler).open_stream(_request(stream=True))
    chunks = [chunk async for chunk in stream]
    return chunks, stream


_CREATED = {"type": "response.created", "response": {"id": "resp_1"}}
_COMPLETED = {
    "type": "response.completed",
    "response": {
        "status": "completed",
        "usage": {"input_tokens": 10, "output_tokens": 4, "total_tokens": 14,
                  "input_tokens_details": {"cached_tokens": 0},
                  "output_tokens_details": {"reasoning_tokens": 2}},
    },
}


@pytest.mark.asyncio
async def test_a_text_stream_yields_content_chunks_then_a_finish() -> None:
    chunks, stream = await _stream(
        [
            _CREATED,
            {"type": "response.in_progress"},
            {"type": "response.output_item.added", "item": {"type": "reasoning", "id": "rs"}},
            {"type": "response.output_text.delta", "delta": "Hel"},
            {"type": "response.output_text.delta", "delta": "lo"},
            {"type": "response.output_text.done", "text": "Hello"},
            _COMPLETED,
        ]
    )
    # Lifecycle events are consumed: the FIRST chunk is the first output, which
    # is what closes the failover window.
    assert chunks[0]["choices"][0]["delta"] == {"content": "Hel"}
    assert "".join(c["choices"][0]["delta"].get("content", "") for c in chunks) == "Hello"
    assert chunks[-1]["choices"][0]["finish_reason"] == "stop"
    assert chunks[-1]["usage"]["prompt_tokens"] == 10
    assert stream.usage.completion_tokens == 4
    assert all(c["id"] == "resp_1" for c in chunks)


@pytest.mark.asyncio
async def test_a_tool_call_stream_accumulates_like_chat_chunks() -> None:
    chunks, _ = await _stream(
        [
            _CREATED,
            {"type": "response.output_item.added",
             "item": {"type": "function_call", "id": "fc_1", "call_id": "call_1",
                      "name": "fs__read", "arguments": ""}},
            {"type": "response.function_call_arguments.delta", "item_id": "fc_1",
             "delta": '{"path":'},
            {"type": "response.output_item.added",
             "item": {"type": "function_call", "id": "fc_2", "call_id": "call_2",
                      "name": "fs__read", "arguments": ""}},
            {"type": "response.function_call_arguments.delta", "item_id": "fc_1",
             "delta": '"a"}'},
            {"type": "response.function_call_arguments.delta", "item_id": "fc_2",
             "delta": "{}"},
            _COMPLETED,
        ]
    )
    calls: dict[int, dict[str, str]] = {}
    for chunk in chunks:
        for tc in chunk["choices"][0]["delta"].get("tool_calls", []):
            slot = calls.setdefault(tc["index"], {"id": "", "name": "", "arguments": ""})
            slot["id"] = tc.get("id") or slot["id"]
            slot["name"] = tc.get("function", {}).get("name") or slot["name"]
            slot["arguments"] += tc.get("function", {}).get("arguments", "")
    assert calls == {
        0: {"id": "call_1", "name": "fs__read", "arguments": '{"path":"a"}'},
        1: {"id": "call_2", "name": "fs__read", "arguments": "{}"},
    }
    assert chunks[-1]["choices"][0]["finish_reason"] == "tool_calls"


@pytest.mark.asyncio
async def test_an_incomplete_stream_finishes_with_length() -> None:
    chunks, _ = await _stream(
        [
            _CREATED,
            {"type": "response.output_text.delta", "delta": "cut"},
            {"type": "response.incomplete",
             "response": {"status": "incomplete",
                          "incomplete_details": {"reason": "max_output_tokens"}}},
        ]
    )
    assert chunks[-1]["choices"][0]["finish_reason"] == "length"


@pytest.mark.asyncio
async def test_a_failed_response_maps_onto_the_taxonomy() -> None:
    with pytest.raises(DaintreeError) as exc:
        await _stream(
            [
                _CREATED,
                {"type": "response.failed",
                 "response": {"error": {"code": "rate_limit_exceeded",
                                        "message": "Rate limit reached"}}},
            ]
        )
    assert exc.value.code == "upstream_rate_limited"


@pytest.mark.asyncio
async def test_an_error_event_maps_context_length() -> None:
    with pytest.raises(DaintreeError) as exc:
        await _stream(
            [{"type": "error", "code": "context_length_exceeded",
              "message": "Your input exceeds the context window of this model."}]
        )
    assert exc.value.code == "context_window_exceeded"


@pytest.mark.asyncio
async def test_a_stream_that_ends_without_completing_is_a_protocol_error() -> None:
    with pytest.raises(DaintreeError) as exc:
        await _stream([_CREATED, {"type": "response.output_text.delta", "delta": "x"}])
    assert exc.value.code == "upstream_protocol_error"


# ---------------------------------------------------------------------------
# Key verification and wiring.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_a_valid_key_verifies_against_the_model_list() -> None:
    seen: dict[str, Any] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["url"] = str(request.url)
        seen["headers"] = {k.lower() for k in request.headers}
        return httpx.Response(200, json=_MODELS_BODY)

    record = await _provider(handler).verify_key(None)
    assert record["models"] == ["gpt-6-luna"]
    assert seen["url"] == "https://api.openai.com/v1/models"
    assert "http-referer" not in seen["headers"]
    assert "x-title" not in seen["headers"]


@pytest.mark.asyncio
async def test_a_rejected_key_is_a_verdict_not_an_unknown() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(401, json={"error": {"message": "invalid"}})

    with pytest.raises(DaintreeError) as exc:
        await _provider(handler).verify_key(None)
    assert exc.value.code == "invalid_api_key"


@pytest.mark.asyncio
async def test_a_200_that_is_not_a_model_list_is_an_error() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"data": {"label": "not a list"}})

    with pytest.raises(DaintreeError) as exc:
        await _provider(handler).verify_key(None)
    assert exc.value.code == "upstream_protocol_error"


def test_the_container_builds_this_adapter_at_this_origin_with_this_key() -> None:
    """Right class, right URL, right credential, right effort — together."""
    from daintree_assistant_server.main import _build_provider

    settings = Settings(STATE_SIGNING_KEY_CURRENT="x" * 32, OPENAI_API_KEY="sk-openai-test")
    provider = _build_provider(settings)
    assert isinstance(provider, OpenAIProvider)
    # EXACTLY the origin — `startswith` would accept a look-alike host.
    assert provider._base_url == f"https://{EXECUTOR_ORIGIN}/v1"  # noqa: SLF001
    assert provider._resolve_key(None).get_secret_value() == "sk-openai-test"  # noqa: SLF001
    assert provider._reasoning_effort == EXECUTOR_REASONING_EFFORT  # noqa: SLF001
    assert EXECUTOR_MODEL == "gpt-6-luna"


def test_odd_content_shapes_translate_without_crashing() -> None:
    items = _body(
        messages=[
            {"role": "user", "content": ["not-a-part", {"type": "text", "text": "a"}]},
            {"role": "assistant", "content": [{"type": "text", "text": "b"}, "junk"]},
            {"role": "assistant", "content": None,
             "tool_calls": [{"id": "c1", "type": "function",
                             "function": {"name": "f", "arguments": "{}"}}]},
            {"role": "tool", "tool_call_id": "c1", "content": None},
        ]
    )["input"]
    assert items == [
        {"role": "user", "content": [{"type": "input_text", "text": "a"}]},
        {"role": "assistant", "content": "b"},
        {"type": "function_call", "call_id": "c1", "name": "f", "arguments": "{}"},
        {"type": "function_call_output", "call_id": "c1", "output": ""},
    ]


def test_a_non_json_object_format_is_not_forwarded() -> None:
    assert "text" not in _body(response_format={"type": "text"})


@pytest.mark.asyncio
async def test_non_dict_output_items_are_skipped() -> None:
    result, _ = await _complete(
        _response(
            ["junk", {"type": "message", "content": ["junk", {"type": "output_text",
                                                               "text": "ok"}]}]
        )
    )
    assert result.choices[0]["message"]["content"] == "ok"


@pytest.mark.asyncio
async def test_empty_and_orphaned_deltas_emit_nothing() -> None:
    chunks, _ = await _stream(
        [
            _CREATED,
            {"type": "response.output_text.delta", "delta": ""},
            {"type": "response.function_call_arguments.delta", "item_id": "unknown",
             "delta": "{}"},
            {"type": "response.output_text.delta", "delta": "x"},
            _COMPLETED,
        ]
    )
    assert [c["choices"][0]["delta"] for c in chunks] == [{"content": "x"}, {}]


async def _stream_raw(body: bytes) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=body, headers={"content-type": "text/event-stream"})

    stream = await _provider(handler).open_stream(_request(stream=True))
    async for _ in stream:
        pass


@pytest.mark.asyncio
@pytest.mark.parametrize("payload", [b"data: {not json}\n\n", b"data: [1, 2]\n\n"])
async def test_a_malformed_event_is_a_protocol_error(payload: bytes) -> None:
    with pytest.raises(DaintreeError) as exc:
        await _stream_raw(payload)
    assert exc.value.code == "upstream_protocol_error"


@pytest.mark.asyncio
async def test_invalid_utf8_framing_is_a_protocol_error() -> None:
    with pytest.raises(DaintreeError) as exc:
        await _stream_raw(b"data: \xff\xfe\n\n")
    assert exc.value.code == "upstream_protocol_error"


class _BreakingStream(httpx.AsyncByteStream):
    def __init__(self, error: Exception) -> None:
        self._error = error

    async def __aiter__(self) -> Any:
        yield _sse([_CREATED, {"type": "response.output_text.delta", "delta": "x"}])
        raise self._error


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("error", "code"),
    [
        (httpx.ReadTimeout("slow"), "upstream_timeout"),
        (httpx.RemoteProtocolError("dropped"), "upstream_unavailable"),
    ],
)
async def test_a_mid_stream_transport_failure_maps_onto_the_taxonomy(
    error: Exception, code: str
) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, stream=_BreakingStream(error))

    stream = await _provider(handler).open_stream(_request(stream=True))
    first = await stream.__anext__()
    assert first["choices"][0]["delta"] == {"content": "x"}
    with pytest.raises(DaintreeError) as exc:
        await stream.__anext__()
    assert exc.value.code == code
