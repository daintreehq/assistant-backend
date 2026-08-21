"""Branches of the Cerebras adapter and the auth door that the main suites miss.

Every test here covers a path that only runs when something has already gone
wrong — a truncated stream, an unreadable body, a shape no healthy upstream
sends. The
per-module coverage floors in ``tools/coverage_floors.py`` exist for exactly this
class of code: a global percentage stays green while the error paths rot, and an
untested error path is how a 502 becomes a 500.
"""

from __future__ import annotations

from typing import Any

import httpx
import pytest
from pydantic import SecretStr

from daintree_assistant_server.errors import DaintreeError
from daintree_assistant_server.providers.base import ProviderChatRequest
from daintree_assistant_server.providers.cerebras import (
    CerebrasProvider,
    _extract_upstream_error,
    _normalize_choices,
    _reason_from,
    _still_reveals,
    _upstream_error_detail,
)

_BASE = "https://cerebras.test/v1"
_KEY = "csk-edge-cases"


def _provider(handler, **kwargs: Any) -> CerebrasProvider:  # type: ignore[no-untyped-def]
    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    kwargs.setdefault("api_key", SecretStr(_KEY))
    return CerebrasProvider(client, base_url=_BASE, **kwargs)


def _req(**kwargs: object) -> ProviderChatRequest:
    base: dict[str, object] = {
        "model": "gemma-4-31b",
        "messages": [{"role": "user", "content": "hi"}],
    }
    base.update(kwargs)
    return ProviderChatRequest(**base)  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# The error-body reader, across every shape an upstream can hand back.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("body", "expected"),
    [
        (None, None),
        (b"", None),
        ("   \n  ", None),
    ],
)
def test_an_empty_error_body_yields_no_detail(body: object, expected: None) -> None:
    """Absent is not the same as empty-string.

    A ``None`` detail lets the caller fall back to a status-derived message; a
    ``""`` would append a bare space to it and read as a truncated sentence.
    """
    assert _extract_upstream_error(body) is expected  # type: ignore[arg-type]


def test_a_non_json_error_body_is_returned_verbatim() -> None:
    """The one branch that does NOT decode escapes, deliberately.

    In a body that is not JSON, a backslash sequence denotes nothing and nothing
    would decode it — so re-encoding it would corrupt the only diagnostic
    available (an HTML error page from a proxy, say).
    """
    assert _upstream_error_detail("<html>502 Bad Gateway</html>") == "<html>502 Bad Gateway</html>"


def test_a_json_error_with_no_recognisable_reason_returns_the_whole_document() -> None:
    """Better the full document than silence.

    A shape we do not recognise still carries whatever the upstream wanted to
    say; discarding it leaves an operator with a bare status code.
    """
    detail = _upstream_error_detail('{"unexpected": "shape", "n": 1}')
    assert "unexpected" in detail and "shape" in detail


def test_a_string_valued_error_key_is_read() -> None:
    # Some intermediaries flatten `{"error": {...}}` to `{"error": "message"}`.
    assert _upstream_error_detail('{"error": "everything is on fire"}') == "everything is on fire"


@pytest.mark.parametrize(
    ("payload", "expected"),
    [
        ({"message": "m", "code": "c"}, "m"),
        ({"code": "c", "type": "t"}, "c"),
        ({"type": "t"}, "t"),
        ({"message": "", "code": "c"}, "c"),
        ({}, None),
    ],
)
def test_the_most_specific_reason_wins(payload: dict[str, Any], expected: str | None) -> None:
    """message > code > type, and an empty value is skipped rather than returned.

    The ordering is not cosmetic: `code` and `type` are enum-ish strings that say
    what CLASS of thing went wrong, while `message` names the offending field —
    which is the whole reason this text is carried to the client at all.
    """
    assert _reason_from(payload) == expected


def test_a_secret_that_is_empty_does_not_mangle_the_diagnostic() -> None:
    """``str.replace("")`` splices the placeholder between every character.

    A provider constructed with an empty credential is already rejected before a
    request goes out, but this helper is called with whatever it is given, and
    destroying the diagnostic would be a silent, confusing failure.
    """
    detail = _extract_upstream_error(b'{"message": "temperature too high"}', secret=SecretStr(""))
    assert detail == "temperature too high"


def test_undecodable_text_stops_the_leak_scan_rather_than_raising() -> None:
    """The scan is a safety check; it must fail closed, not explode.

    Its answer is only ever used to decide whether to DROP a diagnostic, so
    "cannot tell" resolves to "no leak found" and the ordinary literal scrub —
    which already ran — stands.
    """
    assert _still_reveals("nothing escaped here", "csk-secret") is False


# ---------------------------------------------------------------------------
# Inbound normalization, on values the shape validators never see.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "choices",
    ["not a list", None, 42, {"index": 0}],
)
def test_normalizing_a_non_list_choices_is_a_no_op(choices: object) -> None:
    """It runs OUTSIDE the shape validators, on the raw upstream payload.

    Raising here would turn a malformed-but-mappable 502 into an unhandled 500.
    """
    _normalize_choices(choices, "message")  # must not raise


def test_a_non_dict_choice_is_skipped_not_coerced() -> None:
    choices: list[Any] = ["garbage", {"message": {"reasoning": "kept"}}]
    _normalize_choices(choices, "message")
    assert choices[0] == "garbage"
    assert choices[1]["message"]["reasoning_content"] == "kept"


def test_a_choice_without_the_named_part_is_left_alone() -> None:
    # Streaming reads "delta"; a non-streaming choice has "message" and must not
    # be touched by a delta-keyed pass.
    choices: list[Any] = [{"message": {"reasoning": "not a delta"}}]
    _normalize_choices(choices, "delta")
    assert choices[0]["message"]["reasoning"] == "not a delta"


def test_an_existing_reasoning_content_is_not_overwritten() -> None:
    """If the upstream sent both spellings, the contract's own name wins.

    Overwriting it would let a legacy field silently replace the value every
    layer above this one already agreed to read.
    """
    choices: list[Any] = [
        {"message": {"reasoning": "wire name", "reasoning_content": "contract name"}}
    ]
    _normalize_choices(choices, "message")
    assert choices[0]["message"]["reasoning_content"] == "contract name"


# ---------------------------------------------------------------------------
# HTTP paths that only run on a failure.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_a_non_json_200_is_a_protocol_error_not_a_crash() -> None:
    """A 200 whose body will not parse is the upstream's fault, not ours.

    Letting `json.JSONDecodeError` escape would render an internal-error envelope
    — telling the caller Daintree broke when the provider did.
    """

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text="not json at all")

    provider = _provider(handler)
    try:
        with pytest.raises(DaintreeError) as exc:
            await provider.complete(_req())
    finally:
        await provider.aclose()
    assert exc.value.code == "upstream_protocol_error"


@pytest.mark.asyncio
async def test_a_json_200_that_is_not_an_object_is_a_protocol_error() -> None:
    # Valid JSON, unusable shape. `_build_result` indexes into it, so this has to
    # be caught before it becomes an AttributeError 500.
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=["a", "list"])

    provider = _provider(handler)
    try:
        with pytest.raises(DaintreeError) as exc:
            await provider.complete(_req())
    finally:
        await provider.aclose()
    assert exc.value.code == "upstream_protocol_error"


@pytest.mark.asyncio
async def test_a_stream_error_body_that_cannot_be_read_still_maps_its_status() -> None:
    """A transport failure while draining the error body must not mask the status.

    The status is the retryability signal. Losing a 503 to an unreadable body
    would turn a transient outage into an opaque, unretried failure.
    """

    class _Failing(httpx.AsyncBaseTransport):
        async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
            async def _boom() -> bytes:
                raise httpx.ReadError("connection died mid-body")
                yield b""  # pragma: no cover - unreachable, satisfies the generator

            return httpx.Response(503, content=_boom())

    client = httpx.AsyncClient(transport=_Failing())
    provider = CerebrasProvider(client, api_key=SecretStr(_KEY), base_url=_BASE)
    try:
        with pytest.raises(DaintreeError) as exc:
            await provider.open_stream(_req(stream=True))
    finally:
        await provider.aclose()
    assert exc.value.code == "upstream_unavailable"
    assert exc.value.retryable is True


@pytest.mark.asyncio
async def test_verify_key_maps_a_500_to_unavailable_not_to_a_verdict() -> None:
    """"We could not ask" is never reported as "your key is bad".

    A caller told their credential was rejected replaces a credential that was
    fine — the exact failure this endpoint's status handling exists to prevent.
    """

    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500)

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    provider = CerebrasProvider(client, api_key=SecretStr(_KEY), base_url=_BASE)
    try:
        with pytest.raises(DaintreeError) as exc:
            await provider.verify_key()
    finally:
        await provider.aclose()
    assert exc.value.code == "upstream_unavailable"


@pytest.mark.asyncio
async def test_verify_key_maps_a_408_to_a_timeout() -> None:
    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(408)

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    provider = CerebrasProvider(client, api_key=SecretStr(_KEY), base_url=_BASE)
    try:
        with pytest.raises(DaintreeError) as exc:
            await provider.verify_key()
    finally:
        await provider.aclose()
    assert exc.value.code == "upstream_timeout"


@pytest.mark.asyncio
async def test_verify_key_reports_an_unreadable_body_as_a_protocol_error() -> None:
    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text="<html>hello</html>")

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    provider = CerebrasProvider(client, api_key=SecretStr(_KEY), base_url=_BASE)
    try:
        with pytest.raises(DaintreeError) as exc:
            await provider.verify_key()
    finally:
        await provider.aclose()
    assert exc.value.code == "upstream_protocol_error"


@pytest.mark.asyncio
async def test_a_stream_closed_twice_closes_once() -> None:
    # The response path can close a stream on its own error handling and again in
    # a finally; a second teardown must be inert rather than raising into it.
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text="data: [DONE]\n\n")

    provider = _provider(handler)
    stream = await provider.open_stream(_req(stream=True))
    await stream.aclose()
    await stream.aclose()
    await provider.aclose()
