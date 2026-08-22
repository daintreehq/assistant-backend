"""OpenRouter provider adapter tests using a mock HTTP transport.

The point of this file is the DIVERGENCE from the Cerebras adapter. Both speak
an OpenAI-shaped wire, which is exactly what makes the four places they differ
easy to get wrong and invisible when wrong: a `max_completion_tokens` here, an
omitted `reasoning` object, a flat-first error read, or a resurrected `provider`
routing block would all still produce a working-looking request. So most of what
follows asserts what is NOT sent, or which of two spellings won.
"""

from __future__ import annotations

import json as _json
from typing import Any

import httpx
import pytest
from pydantic import SecretStr

from daintree_assistant_server.errors import DaintreeError
from daintree_assistant_server.providers.base import ProviderChatRequest
from daintree_assistant_server.providers.openrouter import (
    _CREDENTIAL_ECHOED,
    _ERROR_DETAIL_CAP,
    _REDACTED,
    OpenRouterProvider,
    _extract_upstream_error,
)

_BASE = "https://openrouter.ai/api/v1"
_MODEL = "deepseek/deepseek-v4-flash"
# The provider's constructor default (fallback) key, and a distinct per-call key.
# They must differ so a dropped per-call credential is detectable rather than
# accidentally matching the fallback.
_DEFAULT_KEY = "sk-or-v1-default-credential"
_CALL_KEY = "sk-or-v1-per-call-credential"

SSE_OK = (
    'data: {"id":"x","object":"chat.completion.chunk","created":1,'
    '"model":"deepseek/deepseek-v4-flash","provider":"DeepSeek",'
    '"choices":[{"index":0,"delta":{"role":"assistant","content":"Hel"}}]}\n\n'
    'data: {"id":"x","object":"chat.completion.chunk","created":1,'
    '"model":"deepseek/deepseek-v4-flash",'
    '"choices":[{"index":0,"delta":{"content":"lo"}}]}\n\n'
    'data: {"id":"x","object":"chat.completion.chunk","created":1,'
    '"model":"deepseek/deepseek-v4-flash",'
    '"choices":[{"index":0,"delta":{},"finish_reason":"stop"}],'
    '"usage":{"prompt_tokens":5,"completion_tokens":2,"total_tokens":7,"cost":0.0004}}\n\n'
    "data: [DONE]\n\n"
)


def _provider(handler: Any, **kwargs: Any) -> OpenRouterProvider:
    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    kwargs.setdefault("api_key", SecretStr(_DEFAULT_KEY))
    return OpenRouterProvider(client, base_url=_BASE, **kwargs)


def _capture(captured: dict[str, Any]) -> Any:
    def handler(request: httpx.Request) -> httpx.Response:
        captured["url"] = str(request.url)
        captured["headers"] = dict(request.headers)
        captured["body"] = _json.loads(request.content)
        return httpx.Response(
            200, json={"id": "x", "created": 0, "model": _MODEL, "choices": [], "usage": {}}
        )

    return handler


def _req(**kwargs: Any) -> ProviderChatRequest:
    base: dict[str, Any] = {"model": _MODEL, "messages": [{"role": "user", "content": "hi"}]}
    base.update(kwargs)
    return ProviderChatRequest(**base)


# ---- wire basics --------------------------------------------------------


@pytest.mark.asyncio
async def test_url_and_auth() -> None:
    captured: dict[str, Any] = {}
    provider = _provider(_capture(captured))
    await provider.complete(_req())
    assert captured["url"] == f"{_BASE}/chat/completions"
    assert captured["headers"]["authorization"] == f"Bearer {_DEFAULT_KEY}"
    assert captured["body"]["model"] == _MODEL
    await provider.aclose()


@pytest.mark.asyncio
async def test_attribution_headers_are_sent() -> None:
    captured: dict[str, Any] = {}
    provider = _provider(_capture(captured))
    await provider.complete(_req())
    assert captured["headers"]["http-referer"]
    assert captured["headers"]["x-title"]
    await provider.aclose()


@pytest.mark.asyncio
async def test_a_per_call_key_overrides_the_default() -> None:
    captured: dict[str, Any] = {}
    provider = _provider(_capture(captured))
    await provider.complete(_req(api_key=SecretStr(_CALL_KEY)))
    assert captured["headers"]["authorization"] == f"Bearer {_CALL_KEY}"
    await provider.aclose()


@pytest.mark.asyncio
async def test_complete_reads_the_reported_cost() -> None:
    """The one thing this provider reports that Cerebras never does.

    `ProviderUsage.cost` is `None` on every Cerebras call, so nothing upstream of
    the boundary had ever seen a real number arrive. `from_wire` already parsed
    it; this pins that the adapter actually passes the usage block through.
    """

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "id": "abc",
                "created": 10,
                "model": _MODEL,
                "provider": "DeepSeek",
                "choices": [
                    {
                        "index": 0,
                        "message": {"role": "assistant", "content": "hi"},
                        "finish_reason": "stop",
                    }
                ],
                "usage": {
                    "prompt_tokens": 3,
                    "completion_tokens": 1,
                    "total_tokens": 4,
                    "cost": 0.00012,
                },
            },
        )

    provider = _provider(handler)
    result = await provider.complete(_req())
    assert result.text == "hi"
    assert result.usage.cost == 0.00012
    assert result.usage.cost_complete is True
    # Observability only, but this is the provider that can actually report it.
    assert result.endpoint == "DeepSeek"
    await provider.aclose()


# ---- the four divergences from the Cerebras adapter ---------------------


@pytest.mark.asyncio
async def test_max_tokens_uses_the_openai_standard_spelling() -> None:
    """`max_tokens`, never `max_completion_tokens`.

    The inverse of the Cerebras adapter. Sending both is what actually breaks, so
    the assertion that matters is the absence of the other name.
    """
    captured: dict[str, Any] = {}
    provider = _provider(_capture(captured))
    await provider.complete(_req(max_tokens=64))
    assert captured["body"]["max_tokens"] == 64
    assert "max_completion_tokens" not in captured["body"]
    await provider.aclose()


@pytest.mark.asyncio
async def test_a_non_thinking_call_disables_reasoning_explicitly() -> None:
    """The highest-value assertion in this file.

    DeepSeek V4 Flash reasons by DEFAULT when `reasoning` is absent. Omitting the
    field — which is precisely the correct behaviour in the Cerebras adapter —
    would leave every compaction call silently thinking, buying tokens and
    latency on the one task chosen for being cheap. Nothing else would notice:
    the call still succeeds and still returns valid JSON.
    """
    captured: dict[str, Any] = {}
    provider = _provider(_capture(captured))
    await provider.complete(_req(thinking_disabled=True))
    assert captured["body"]["reasoning"] == {"enabled": False}
    # And never Cerebras's bare-string spelling, which OpenRouter does not take.
    assert "reasoning_effort" not in captured["body"]
    await provider.aclose()


@pytest.mark.asyncio
async def test_a_thinking_call_sends_its_effort_tier() -> None:
    captured: dict[str, Any] = {}
    provider = _provider(_capture(captured))
    await provider.complete(_req(thinking_disabled=False, reasoning_effort="high"))
    assert captured["body"]["reasoning"] == {"effort": "high"}
    await provider.aclose()


@pytest.mark.asyncio
async def test_a_thinking_call_with_no_tier_sends_a_bare_enable() -> None:
    captured: dict[str, Any] = {}
    provider = _provider(_capture(captured))
    await provider.complete(_req(thinking_disabled=False))
    assert captured["body"]["reasoning"] == {"enabled": True}
    await provider.aclose()


@pytest.mark.parametrize(
    "field",
    [
        # The routing apparatus, deliberately deleted in the move to Cerebras.
        # Its return would be invisible in a passing test suite, so it is asserted
        # against by name.
        "provider",
        "routing",
        "order",
        "fallbacks",
        # Cache affinity: neither vendor's spelling is sent by this adapter.
        "session_id",
        "prompt_cache_key",
    ],
)
@pytest.mark.asyncio
async def test_no_routing_or_affinity_field_is_ever_sent(field: str) -> None:
    captured: dict[str, Any] = {}
    provider = _provider(_capture(captured))
    # cache_session_id is ACCEPTED (the request type is provider-neutral) and
    # must simply not reach the wire.
    await provider.complete(_req(cache_session_id="daintree-task-context_compact"))
    assert field not in captured["body"]
    await provider.aclose()


def test_a_nested_error_body_is_read_first() -> None:
    """OpenRouter wraps the reason; Cerebras puts it at the top level.

    Both shapes are read by both adapters — only the PRIORITY differs — so the
    body has to carry BOTH for the assertion to mean anything. With only a
    nested message present, flat-first code would fall through to it and pass.
    """
    body = _json.dumps(
        {"error": {"message": "the nested reason"}, "message": "the flat reason"}
    )
    assert _extract_upstream_error(body) == "the nested reason"


def test_a_flat_error_body_still_works() -> None:
    # An intermediary (proxy, mock, gateway) may still send the flat shape.
    assert _extract_upstream_error(_json.dumps({"message": "flat reason"})) == "flat reason"


# ---- credential safety --------------------------------------------------


def test_an_echoed_credential_is_scrubbed() -> None:
    body = _json.dumps({"error": {"message": f"bad key {_DEFAULT_KEY}"}})
    detail = _extract_upstream_error(body, secret=SecretStr(_DEFAULT_KEY))
    assert detail is not None
    assert _DEFAULT_KEY not in detail
    assert _REDACTED in detail


def test_a_doubly_encoded_credential_drops_the_whole_detail() -> None:
    """The backstop: one decode pass hides the key from an exact match.

    Asserts the SENTINEL, not merely the absence of the plaintext. Absence is
    satisfied by the escaped form on its own, so a version of this test that
    only checked `_DEFAULT_KEY not in detail` would still pass with
    `_still_reveals` deleted — pinning nothing.
    """
    escaped = "".join(f"\\u{ord(c):04x}" for c in _DEFAULT_KEY)
    body = _json.dumps({"error": {"message": f"bad key {escaped}"}})
    detail = _extract_upstream_error(body, secret=SecretStr(_DEFAULT_KEY))
    assert detail == _CREDENTIAL_ECHOED
    assert _DEFAULT_KEY not in detail


def test_the_detail_is_capped() -> None:
    body = _json.dumps({"error": {"message": "x" * 5000}})
    detail = _extract_upstream_error(body)
    assert detail is not None
    assert len(detail) <= _ERROR_DETAIL_CAP


@pytest.mark.asyncio
async def test_a_malformed_credential_is_rejected_before_httpx_sees_it() -> None:
    """h11 embeds the offending header value in its exception, which httpcore
    logs at DEBUG — so a bad key must be refused here, not handed downstream."""
    provider = _provider(_capture({}), api_key=SecretStr("has a space"))
    with pytest.raises(DaintreeError):
        await provider.complete(_req())
    await provider.aclose()


# ---- error taxonomy -----------------------------------------------------


@pytest.mark.parametrize(
    ("status", "code"),
    [
        (401, "provider_invalid_api_key"),
        (402, "provider_insufficient_credits"),
        (403, "provider_key_forbidden"),
        (429, "upstream_rate_limited"),
    ],
)
@pytest.mark.asyncio
async def test_account_failures_map_to_distinct_codes(status: int, code: str) -> None:
    """Distinct codes, because the CLI branches on them to give distinct advice.
    A spent balance told to check its credentials is the bug this prevents."""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(status, json={"error": {"message": "nope"}})

    provider = _provider(handler)
    with pytest.raises(DaintreeError) as exc:
        await provider.complete(_req())
    assert exc.value.code == code
    await provider.aclose()


@pytest.mark.asyncio
async def test_a_5xx_is_retryable_and_an_account_failure_is_not() -> None:
    def five_hundred(request: httpx.Request) -> httpx.Response:
        return httpx.Response(503, json={"error": {"message": "down"}})

    provider = _provider(five_hundred)
    with pytest.raises(DaintreeError) as exc:
        await provider.complete(_req())
    assert exc.value.retryable is True
    await provider.aclose()

    def unauthorized(request: httpx.Request) -> httpx.Response:
        return httpx.Response(401, json={"error": {"message": "bad key"}})

    provider = _provider(unauthorized)
    with pytest.raises(DaintreeError) as exc:
        await provider.complete(_req())
    assert exc.value.retryable is False
    await provider.aclose()


@pytest.mark.asyncio
async def test_a_4xx_carries_the_upstream_reason_through() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            400, json={"error": {"message": "temperature must be between 0 and 2"}}
        )

    provider = _provider(handler)
    with pytest.raises(DaintreeError) as exc:
        await provider.complete(_req())
    assert "temperature must be between 0 and 2" in exc.value.message
    await provider.aclose()


# ---- streaming ----------------------------------------------------------


@pytest.mark.asyncio
async def test_stream_yields_chunks_and_names_its_endpoint() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert _json.loads(request.content)["stream"] is True
        return httpx.Response(
            200, text=SSE_OK, headers={"content-type": "text/event-stream"}
        )

    provider = _provider(handler)
    stream = await provider.open_stream(_req(stream=True))
    chunks = [await stream.prefetch_first_event()]
    async for chunk in stream:
        chunks.append(chunk)
    assert stream.endpoint == "DeepSeek"
    assert stream.usage is not None
    assert stream.usage.cost == 0.0004
    await stream.aclose()
    await provider.aclose()


@pytest.mark.asyncio
async def test_a_truncated_stream_is_an_error_not_a_clean_finish() -> None:
    truncated = (
        'data: {"id":"x","object":"chat.completion.chunk","created":1,"model":"m",'
        '"choices":[{"index":0,"delta":{"content":"partial"}}]}\n\n'
    )

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200, text=truncated, headers={"content-type": "text/event-stream"}
        )

    provider = _provider(handler)
    stream = await provider.open_stream(_req(stream=True))
    await stream.prefetch_first_event()
    with pytest.raises(DaintreeError):
        async for _ in stream:
            pass
    await stream.aclose()
    await provider.aclose()


# ---- structured tool call ----------------------------------------------


@pytest.mark.asyncio
async def test_structured_tool_call_returns_the_arguments() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        body = _json.loads(request.content)
        assert body["tool_choice"] == {"type": "function", "function": {"name": "pick"}}
        return httpx.Response(
            200,
            json={
                "id": "x",
                "created": 0,
                "model": _MODEL,
                "choices": [
                    {
                        "index": 0,
                        "message": {
                            "role": "assistant",
                            "content": None,
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
        _req(), function_name="pick", function_schema={"type": "object"}
    )
    assert result.name == "pick"
    assert result.arguments_json == '{"a":1}'
    await provider.aclose()


@pytest.mark.asyncio
async def test_structured_tool_call_with_no_call_returns_empty_arguments() -> None:
    """Empty args rather than a crash, so the caller's repair path can run."""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "id": "x",
                "created": 0,
                "model": _MODEL,
                "choices": [
                    {
                        "index": 0,
                        "message": {"role": "assistant", "content": "sorry"},
                        "finish_reason": "stop",
                    }
                ],
                "usage": {},
            },
        )

    provider = _provider(handler)
    result = await provider.structured_tool_call(
        _req(), function_name="pick", function_schema={"type": "object"}
    )
    assert result.arguments_json == ""
    await provider.aclose()


# ---- verify_key ---------------------------------------------------------


@pytest.mark.asyncio
async def test_verify_key_probes_the_key_endpoint() -> None:
    """`GET /key`, not `/models`.

    OpenRouter serves its model catalogue UNAUTHENTICATED, so a listing would
    come back 200 for a key that cannot spend anything — the exact false positive
    a verification endpoint exists to prevent.
    """
    seen: dict[str, Any] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["url"] = str(request.url)
        return httpx.Response(200, json={"data": {"label": "dev", "usage": 1.5}})

    provider = _provider(handler)
    record = await provider.verify_key()
    assert seen["url"] == f"{_BASE}/key"
    assert record["label"] == "dev"
    await provider.aclose()


@pytest.mark.asyncio
async def test_verify_key_rejects_a_bad_credential() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(401, json={"error": {"message": "no"}})

    provider = _provider(handler)
    with pytest.raises(DaintreeError) as exc:
        await provider.verify_key()
    assert exc.value.status_code == 401
    await provider.aclose()


@pytest.mark.asyncio
async def test_verify_key_never_reports_an_unknown_as_invalid() -> None:
    """A non-200 that is not a rejection means we could not get an answer.

    Reporting that as "invalid" would tell a user with a perfectly good key to
    go and replace it.
    """

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500, json={"error": {"message": "boom"}})

    provider = _provider(handler)
    with pytest.raises(DaintreeError) as exc:
        await provider.verify_key()
    # The exact code, not merely "not 401": a mapping that turned this into a
    # 400 or a 402 would also satisfy the weaker assertion while telling the
    # user something false about their key.
    assert exc.value.code == "upstream_unavailable"
    await provider.aclose()


@pytest.mark.asyncio
async def test_verify_key_rejects_a_200_that_is_not_a_key_record() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"data": ["not", "a", "record"]})

    provider = _provider(handler)
    with pytest.raises(DaintreeError):
        await provider.verify_key()
    await provider.aclose()


# ---- in-band stream errors ---------------------------------------------
#
# Most turns stream, so this is the taxonomy path that matters most. An in-band
# SSE error carries no HTTP status, and the trap it exists to avoid is collapsing
# every one of them into a single retryable code — which makes the CLI replay a
# spent balance and an invalid key forever.


@pytest.mark.parametrize(
    ("err", "code"),
    [
        # A numeric code is a far better signal than prose, so it reuses the HTTP
        # mapping wholesale rather than inventing a second, weaker taxonomy.
        ({"code": 402, "message": "no funds"}, "provider_insufficient_credits"),
        ({"code": "429", "message": "slow down"}, "upstream_rate_limited"),
        # No usable status: fall back to prose, narrowly.
        ({"message": "Insufficient credit to run this model"}, "provider_insufficient_credits"),
        ({"message": "No auth credentials found"}, "provider_invalid_api_key"),
        ({"message": "rate limit exceeded"}, "upstream_rate_limited"),
        # Nothing matched: a generic upstream error, not a guess.
        ({"message": "something else entirely"}, "upstream_error"),
    ],
)
def test_in_band_stream_errors_map_to_the_same_taxonomy(err: dict[str, Any], code: str) -> None:
    from daintree_assistant_server.providers.openrouter import map_stream_error

    assert map_stream_error(err).code == code


@pytest.mark.parametrize(
    "code",
    [
        # `bool` is an `int` subclass — `{"code": true}` must not become status 1.
        True,
        # Out of the HTTP range, and a non-numeric string: neither is a status.
        999,
        "not-a-status",
        "99",
    ],
)
def test_a_non_status_code_falls_through_to_prose(code: Any) -> None:
    """A bogus `code` must not be read as an HTTP status.

    Reading `true` as status 1, or `999` as a real status, would send the error
    down the wrong taxonomy branch and produce confident nonsense.
    """
    from daintree_assistant_server.providers.openrouter import _stream_error_status

    assert _stream_error_status({"code": code}) is None


@pytest.mark.asyncio
async def test_an_in_band_error_chunk_raises_mid_stream() -> None:
    body = (
        'data: {"id":"x","object":"chat.completion.chunk","created":1,"model":"m",'
        '"choices":[{"index":0,"delta":{"content":"hi"}}]}\n\n'
        'data: {"error":{"code":402,"message":"out of credit"}}\n\n'
    )

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text=body, headers={"content-type": "text/event-stream"})

    provider = _provider(handler)
    stream = await provider.open_stream(_req(stream=True))
    await stream.prefetch_first_event()
    with pytest.raises(DaintreeError) as exc:
        async for _ in stream:
            pass
    assert exc.value.code == "provider_insufficient_credits"
    await stream.aclose()
    await provider.aclose()


@pytest.mark.asyncio
async def test_a_malformed_sse_payload_is_a_protocol_error() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200, text="data: {not json at all}\n\n", headers={"content-type": "text/event-stream"}
        )

    provider = _provider(handler)
    stream = await provider.open_stream(_req(stream=True))
    with pytest.raises(DaintreeError) as exc:
        await stream.prefetch_first_event()
    assert exc.value.code == "upstream_protocol_error"
    await stream.aclose()
    await provider.aclose()


@pytest.mark.asyncio
async def test_a_structurally_invalid_chunk_is_a_protocol_error() -> None:
    """Valid JSON, unusable shape. Consumers index straight into
    choices[0]["delta"], so this must be a 502 and never an AttributeError 500."""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            text='data: {"choices":"not-a-list"}\n\n',
            headers={"content-type": "text/event-stream"},
        )

    provider = _provider(handler)
    stream = await provider.open_stream(_req(stream=True))
    with pytest.raises(DaintreeError) as exc:
        await stream.prefetch_first_event()
    assert exc.value.code == "upstream_protocol_error"
    await stream.aclose()
    await provider.aclose()


# ---- transport failures -------------------------------------------------


@pytest.mark.parametrize("phase", ["complete", "stream"])
@pytest.mark.asyncio
async def test_a_timeout_maps_to_upstream_timeout(phase: str) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("too slow", request=request)

    provider = _provider(handler)
    with pytest.raises(DaintreeError) as exc:
        if phase == "complete":
            await provider.complete(_req())
        else:
            await provider.open_stream(_req(stream=True))
    assert exc.value.code == "upstream_timeout"
    await provider.aclose()


@pytest.mark.parametrize("phase", ["complete", "stream"])
@pytest.mark.asyncio
async def test_a_transport_error_is_retryable(phase: str) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("refused", request=request)

    provider = _provider(handler)
    with pytest.raises(DaintreeError) as exc:
        if phase == "complete":
            await provider.complete(_req())
        else:
            await provider.open_stream(_req(stream=True))
    assert exc.value.retryable is True
    await provider.aclose()


@pytest.mark.asyncio
async def test_a_stream_that_fails_to_open_maps_its_status_and_keeps_the_reason() -> None:
    """The error body is drained before the context is torn down — otherwise the
    whole diagnostic is lost and the caller sees a bare status."""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(400, json={"error": {"message": "bad temperature"}})

    provider = _provider(handler)
    with pytest.raises(DaintreeError) as exc:
        await provider.open_stream(_req(stream=True))
    assert "bad temperature" in exc.value.message
    await provider.aclose()


@pytest.mark.asyncio
async def test_a_stream_open_401_is_not_retryable() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(401, json={"error": {"message": "nope"}})

    provider = _provider(handler)
    with pytest.raises(DaintreeError) as exc:
        await provider.open_stream(_req(stream=True))
    assert exc.value.code == "provider_invalid_api_key"
    assert exc.value.retryable is False
    await provider.aclose()


@pytest.mark.asyncio
async def test_verify_key_maps_transport_failures_without_blaming_the_key() -> None:
    def timeout_handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("slow", request=request)

    provider = _provider(timeout_handler)
    with pytest.raises(DaintreeError) as exc:
        await provider.verify_key()
    assert exc.value.code == "upstream_timeout"
    await provider.aclose()

    def connect_handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("refused", request=request)

    provider = _provider(connect_handler)
    with pytest.raises(DaintreeError) as exc:
        await provider.verify_key()
    assert exc.value.code == "upstream_unavailable"
    await provider.aclose()


@pytest.mark.asyncio
async def test_verify_key_rejects_an_unreadable_body() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text="not json", headers={"content-type": "application/json"})

    provider = _provider(handler)
    with pytest.raises(DaintreeError):
        await provider.verify_key()
    await provider.aclose()


# ---- response shape guards ----------------------------------------------


@pytest.mark.asyncio
async def test_a_non_json_completion_is_a_protocol_error() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text="<html>oops</html>")

    provider = _provider(handler)
    with pytest.raises(DaintreeError) as exc:
        await provider.complete(_req())
    assert exc.value.code == "upstream_protocol_error"
    await provider.aclose()


@pytest.mark.asyncio
async def test_a_structurally_invalid_completion_is_a_protocol_error() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"id": "x", "created": 0, "choices": "nope"})

    provider = _provider(handler)
    with pytest.raises(DaintreeError) as exc:
        await provider.complete(_req())
    assert exc.value.code == "upstream_protocol_error"
    await provider.aclose()


# ---- reasoning_content rename ------------------------------------------
#
# The wire calls chain-of-thought `reasoning`; the Daintree contract and the CLI
# call it `reasoning_content`. The rename is confined to this adapter, so it has
# to happen in BOTH directions or context is silently dropped.


@pytest.mark.asyncio
async def test_outbound_reasoning_content_is_renamed() -> None:
    captured: dict[str, Any] = {}
    provider = _provider(_capture(captured))
    await provider.complete(
        _req(
            messages=[
                {"role": "user", "content": "hi"},
                {"role": "assistant", "content": "a", "reasoning_content": "because"},
            ]
        )
    )
    sent = captured["body"]["messages"][1]
    assert sent["reasoning"] == "because"
    assert "reasoning_content" not in sent
    await provider.aclose()


@pytest.mark.asyncio
async def test_inbound_reasoning_is_renamed_back() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "id": "x",
                "created": 0,
                "model": _MODEL,
                "choices": [
                    {
                        "index": 0,
                        "message": {
                            "role": "assistant",
                            "content": "a",
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
async def test_an_untouched_message_is_not_rebuilt() -> None:
    """Copy-on-write: the byte-stable prefix the prompt cache depends on must be
    identical, not merely equal, for messages carrying no chain-of-thought."""
    from daintree_assistant_server.providers.openrouter import _rewrite_messages

    original = [{"role": "user", "content": "hi"}]
    assert _rewrite_messages(original)[0] is original[0]


# ---- dev-only body dumping ---------------------------------------------


@pytest.mark.asyncio
async def test_the_request_body_can_be_dumped_for_cache_forensics(tmp_path: Any) -> None:
    """Dev-only diagnostic, gated out of production by readiness. It is THE tool
    for finding the byte where two 'identical' requests diverge."""
    captured: dict[str, Any] = {}
    provider = _provider(_capture(captured), dump_dir=str(tmp_path))
    await provider.complete(_req())
    dumps = list(tmp_path.iterdir())
    assert len(dumps) == 1
    dumped = _json.loads(dumps[0].read_text())
    assert dumped["model"] == _MODEL
    # The disable rides along, so a dump is a faithful record of the wire body.
    assert dumped["reasoning"] == {"enabled": False}
    await provider.aclose()


def test_the_credential_is_scrubbed_before_the_detail_is_capped() -> None:
    """Order matters: capping first can slice a key and leave a usable prefix.

    The key is placed so that the cap boundary falls in the middle of it. If the
    cap were applied before the scrub, a recognisable head of the credential
    would survive in the returned text.
    """
    # Place the key so that exactly `surviving` of its characters would fall
    # inside the cap. The assertion then looks for precisely that fragment: under
    # correct behaviour the whole key is replaced before any slicing, so it is
    # absent; under cap-first it is present verbatim. Asserting a LONGER fragment
    # would pass either way, which is the bug this arithmetic exists to avoid.
    surviving = len(_DEFAULT_KEY) // 2
    prefix = "x" * (_ERROR_DETAIL_CAP - surviving)
    body = _json.dumps({"error": {"message": f"{prefix}{_DEFAULT_KEY}"}})

    # Guard the guard: if cap-first would leave nothing of the key, the test
    # below proves nothing.
    assert (prefix + _DEFAULT_KEY)[:_ERROR_DETAIL_CAP].endswith(_DEFAULT_KEY[:surviving])

    detail = _extract_upstream_error(body, secret=SecretStr(_DEFAULT_KEY))
    assert detail is not None
    assert len(detail) <= _ERROR_DETAIL_CAP
    assert _DEFAULT_KEY[:surviving] not in detail
    assert _REDACTED in detail


def test_a_unicode_escaped_credential_is_caught_by_scrubbing_after_extraction() -> None:
    """JSON can encode an ordinary key as \\u0073\\u006b…

    No literal match on the undecoded bytes would find it, but `json.loads`
    faithfully restores the plaintext — which is why the scrub runs on EXTRACTED
    text rather than on the raw body.
    """
    escaped = "".join(f"\\u{ord(c):04x}" for c in _DEFAULT_KEY)
    body = '{"error": {"message": "rejected ' + escaped + '"}}'
    detail = _extract_upstream_error(body, secret=SecretStr(_DEFAULT_KEY))
    assert detail is not None
    assert _DEFAULT_KEY not in detail
    assert _REDACTED in detail


@pytest.mark.asyncio
async def test_a_404_is_a_rejected_request_not_a_routing_dead_end() -> None:
    """Guards against reviving the deleted routing taxonomy.

    The old adapter mapped 404 to `upstream_no_compliant_provider` — "the policy
    filters emptied the pool". Nothing sends a routing policy now, so a 404 on a
    fixed, correct path can only mean the model id is wrong: our bug, reported
    with the upstream's own reason attached.
    """

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(404, json={"error": {"message": "Model does not exist"}})

    provider = _provider(handler)
    with pytest.raises(DaintreeError) as exc:
        await provider.complete(_req())
    assert exc.value.code == "upstream_request_rejected"
    assert "Model does not exist" in exc.value.message
    await provider.aclose()


@pytest.mark.asyncio
async def test_verify_key_rejects_a_redirect_rather_than_following_it() -> None:
    """EXACTLY 200 or we have no answer.

    The client does not follow redirects, so a 3xx arrives here as-is. A
    body-shaped 302 would read as a successful verification under a `>= 400`
    check — reporting a key as usable on the strength of a redirect.
    """

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(302, json={"data": {"label": "nope"}})

    provider = _provider(handler)
    with pytest.raises(DaintreeError):
        await provider.verify_key()
    await provider.aclose()


@pytest.mark.parametrize("include_usage", [True, False])
@pytest.mark.asyncio
async def test_stream_options_ride_the_include_usage_flag(include_usage: bool) -> None:
    """A stream reports usage only when asked; cost accounting depends on it."""
    captured: dict[str, Any] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["body"] = _json.loads(request.content)
        return httpx.Response(200, text=SSE_OK, headers={"content-type": "text/event-stream"})

    provider = _provider(handler)
    stream = await provider.open_stream(_req(stream=True, include_usage=include_usage))
    if include_usage:
        assert captured["body"]["stream_options"] == {"include_usage": True}
    else:
        assert "stream_options" not in captured["body"]
    await stream.aclose()
    await provider.aclose()


# ---------------------------------------------------------------------------
# `ProviderChatResult` accessors on a degenerate body.
# ---------------------------------------------------------------------------


def test_a_response_with_no_choices_reads_as_empty_rather_than_raising() -> None:
    """An upstream 200 carrying no `choices` must not take the turn down.

    It happens: a truncated body, a provider returning `{"choices": []}` on an
    internal filter, a proxy rewriting the envelope. Every consumer of `.text`
    and `.message` is on the response path, so an IndexError here converts a
    recoverable empty answer into a 500 the caller cannot act on.

    Covered incidentally by the retired Cerebras adapter's suite until that
    adapter was deleted; `tools/coverage_floors.py` caught the gap, which is the
    entire reason `providers/base.py` carries a per-module floor.
    """
    from daintree_assistant_server.providers.base import ProviderChatResult, ProviderUsage

    def _result(choices: list[dict[str, object]]) -> ProviderChatResult:
        return ProviderChatResult(
            id="r", created=0, model="m", usage=ProviderUsage(), choices=choices
        )

    empty = _result([])
    assert empty.message == {}
    assert empty.text == ""
    assert empty.tool_calls == []

    # A choice that is present but carries a null message is the same shape.
    null_message = _result([{"message": None}])
    assert null_message.message == {}
    assert null_message.text == ""
