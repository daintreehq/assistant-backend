"""Baseten adapter: the things that are NOT inherited.

``BasetenProvider`` is deliberately a thin subclass of ``OpenRouterProvider`` —
same SSE parsing, same error mapping, same ``max_tokens`` spelling — so the
parent's suite already covers almost all of it, and re-testing inherited
behaviour here would only create two places to update. What is NOT covered by
the parent is exactly what this file pins: the overridden class attributes, the
key-record decoder whose parent implementation is WRONG for this host, and —
the part with real money attached — the THINKING POSTURE.

That last one is why this file matters more than the Together one it replaces.
Every other adapter here can reach a non-thinking call: the parent sends
``reasoning: {"enabled": false}``, and Cerebras omits the field because its model
does not reason. GLM-5.3-Flash can do neither. It reasons unconditionally, and an
ABSENT ``reasoning_effort`` means ``high`` — so the failure mode is not an error
but a silently more expensive server, on every turn and every utility task, which
no amount of green elsewhere would surface. Measured on the live API: the field
absent reasoned on 6 of 6 trials (median 479.5 tokens); ``low`` reasoned on 1 of
6 (median 0).

The key-record decoder is not hypothetical either. The equivalent bug shipped
once on the previous host: the parent requires ``{"data": {...}}`` (OpenRouter's
``/key`` shape), so a valid key raised ``upstream_protocol_error`` —
``/v1/daintree/auth/verify`` returning 502 and the doctor route reporting
``provider_unreachable`` for a credential that had just proved itself.
"""

from __future__ import annotations

import copy

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
from daintree_assistant_server.providers.baseten import BasetenProvider

#: Baseten's real ``/models`` shape, trimmed: OpenAI's ``{"object": "list",
#: "data": [...]}``. Captured from the live API rather than imagined — a LIST
#: under ``data``, which is neither OpenRouter's mapping nor the bare top-level
#: array the retired Together adapter had to decode.
_MODELS_BODY = {
    "object": "list",
    "data": [
        {"id": "zai-org/GLM-5.3-Flash", "object": "model", "context_length": 1048576},
        {"id": "moonshotai/Kimi-K3", "object": "model", "context_length": 262144},
    ],
}


def _never_called(request: httpx.Request) -> httpx.Response:
    """Body construction is pure: reaching the transport at all is the failure."""
    raise AssertionError(f"_build_body must not open a request (got {request.url})")


def _provider(handler: object, **kwargs: object) -> BasetenProvider:
    transport = httpx.MockTransport(handler)  # type: ignore[arg-type]
    return BasetenProvider(
        httpx.AsyncClient(transport=transport),
        api_key=SecretStr("baseten-test-key"),
        **kwargs,  # type: ignore[arg-type]
    )


def _body(
    provider: BasetenProvider, *, stream: bool = False, **kwargs: object
) -> dict[str, object]:
    request = ProviderChatRequest(
        model=EXECUTOR_MODEL,
        messages=[{"role": "user", "content": "hi"}],
        **kwargs,  # type: ignore[arg-type]
    )
    return provider._build_body(request, stream=stream)  # noqa: SLF001


#: Both wire shapes. The posture must not depend on which one a turn takes: an
#: edit that sent the effort only on the non-streaming path would pass every
#: assertion written against `stream=False` while every ORDINARY turn — which is
#: streaming — silently ran at Baseten's `high` default.
_STREAM = [pytest.param(False, id="non_streaming"), pytest.param(True, id="streaming")]


# ---------------------------------------------------------------------------
# The thinking posture. The expensive half of this adapter.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("stream", _STREAM)
@pytest.mark.parametrize("disabled", [True, False])
def test_the_unified_reasoning_object_is_never_sent(stream: bool, disabled: bool) -> None:
    """The parent's switch does not work here and must not be inherited.

    Baseten accepts unknown top-level fields silently, so sending it would not
    error — it would simply be a body claiming a posture the host never applied,
    which is indistinguishable from working until the bill arrives.
    """
    body = _body(_provider(_never_called), stream=stream, thinking_disabled=disabled)
    assert "reasoning" not in body


#: Every meaningful (thinking_disabled, reasoning_effort) a caller produces, and
#: the effort each must put on the wire. There is no row whose answer is "send
#: nothing": omission is `high` on this host, so an absent field is never right.
_POSTURES = [
    # `task_runner` and `prompt_assembler` under the shipped posture, and
    # `structured_tool_call`, which rebuilds its sub-request this way.
    pytest.param(True, None, "low", id="disabled_no_effort__the_shipped_posture"),
    # `thinking_disabled` wins: a forced tool call wants one object back cheaply.
    pytest.param(True, "max", "low", id="disabled_beats_an_explicit_effort"),
    # A caller asking for more than the floor is honoured, not clamped.
    pytest.param(False, "max", "max", id="an_explicit_effort_survives"),
    pytest.param(False, "high", "high", id="the_interactive_tier_survives"),
    # The gap Codex found: enabled-but-unspecified must still not omit the field.
    pytest.param(False, None, "low", id="enabled_but_unspecified_falls_to_the_floor"),
]


@pytest.mark.parametrize("stream", _STREAM)
@pytest.mark.parametrize("disabled,requested,expected", _POSTURES)
def test_the_effort_sent_for_every_posture(
    stream: bool, disabled: bool, requested: str | None, expected: str
) -> None:
    """An ABSENT ``reasoning_effort`` means ``high`` on this host, not "off".

    That is the whole inversion, and why this is a table rather than one case:
    the literal reading of ``thinking_disabled`` — send no reasoning field —
    buys the most expensive posture available on every row.
    """
    body = _body(
        _provider(_never_called),
        stream=stream,
        thinking_disabled=disabled,
        reasoning_effort=requested,
    )
    assert body["reasoning_effort"] == expected


def test_the_shipped_posture_is_the_configured_floor() -> None:
    """Ties the table above to the constant the container actually injects."""
    assert EXECUTOR_REASONING_EFFORT == "low"
    body = _body(_provider(_never_called), thinking_disabled=True)
    assert body["reasoning_effort"] == EXECUTOR_REASONING_EFFORT


def test_the_configured_effort_is_injected_rather_than_hardcoded() -> None:
    """The product decision lives in ``config`` and is passed at construction."""
    body = _body(_provider(_never_called, reasoning_effort="high"), thinking_disabled=True)
    assert body["reasoning_effort"] == "high"


def test_replayed_chain_of_thought_keeps_this_host_s_own_spelling() -> None:
    """The parent renames ``reasoning_content`` to ``reasoning`` on the way out.

    This host spells it ``reasoning_content`` in its own responses, so the
    rename is undone rather than the field dropped — otherwise the CoT that
    produced a tool-call turn is handed over under a name the host does not read
    and is silently lost, which no error would ever reveal.
    """
    provider = _provider(_never_called)
    request = ProviderChatRequest(
        model=EXECUTOR_MODEL,
        messages=[
            {"role": "user", "content": "hi"},
            {"role": "assistant", "content": "4", "reasoning_content": "a sum"},
        ],
    )
    messages = provider._build_body(request, stream=False)["messages"]  # noqa: SLF001
    assert messages[1]["reasoning_content"] == "a sum"
    assert "reasoning" not in messages[1]


def test_building_a_body_does_not_mutate_the_request() -> None:
    """`_build_body` must be a pure function of its input.

    The parent passes rows it did not rewrite through BY REFERENCE, so an undo
    that popped `reasoning` in place would edit the caller's own dict — and every
    retry, the bare-intent re-roll and both response paths read that same prepared
    request afterwards. Today's assembler never emits a raw `reasoning` key so
    this is unreachable in production; it is pinned because the aliasing is
    invisible until something downstream reads a request it did not write.
    """
    provider = _provider(_never_called)
    messages = [
        {"role": "user", "content": "hi"},
        {"role": "assistant", "content": "4", "reasoning": "a sum"},
    ]
    request = ProviderChatRequest(model=EXECUTOR_MODEL, messages=messages)
    before = copy.deepcopy(messages)

    body = provider._build_body(request, stream=False)  # noqa: SLF001

    assert messages == before, "_build_body mutated the caller's messages"
    # ...and the body still carries the host's own spelling.
    assert body["messages"][1]["reasoning_content"] == "a sum"
    assert "reasoning" not in body["messages"][1]


def test_an_existing_reasoning_content_is_not_overwritten_by_a_raw_reasoning() -> None:
    """A row carrying BOTH keys keeps the canonical one.

    Malformed input rather than anything the parent produces, but the tie has to
    break somewhere — and breaking it toward the raw `reasoning` would resolve the
    contradiction in favour of the spelling this adapter exists to undo.
    """
    provider = _provider(_never_called)
    request = ProviderChatRequest(
        model=EXECUTOR_MODEL,
        messages=[
            {
                "role": "assistant",
                "content": "4",
                "reasoning": "the raw one",
                "reasoning_content": "the canonical one",
            }
        ],
    )
    row = provider._build_body(request, stream=False)["messages"][0]  # noqa: SLF001
    assert row["reasoning_content"] == "the canonical one"
    assert "reasoning" not in row


# ---------------------------------------------------------------------------
# Key verification.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_a_valid_key_verifies_against_the_model_list() -> None:
    """A good key must not read as a protocol error.

    Reporting "we could not tell" about a credential the provider just accepted
    is the one answer this endpoint must never give when it does in fact know.
    """
    seen: dict[str, object] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["url"] = str(request.url)
        return httpx.Response(200, json=_MODELS_BODY)

    record = await _provider(handler).verify_key(None)

    assert record["models"] == ["zai-org/GLM-5.3-Flash", "moonshotai/Kimi-K3"]
    # `/models`, because Baseten has no `/key` route — the parent's path would
    # 404 here, which maps to "could not verify" rather than a verdict.
    assert seen["url"] == "https://inference.baseten.co/v1/models"


@pytest.mark.asyncio
async def test_a_rejected_key_is_a_verdict_not_an_unknown() -> None:
    """401 must stay ``invalid_api_key``: the one answer worth acting on."""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(401, json={"error": {"message": "invalid"}})

    with pytest.raises(DaintreeError) as exc:
        await _provider(handler).verify_key(None)
    assert exc.value.code == "invalid_api_key"


@pytest.mark.asyncio
async def test_a_200_that_is_not_a_model_list_is_an_error_not_an_empty_record() -> None:
    """Degrading an unreadable body to ``{"models": []}`` would read as success.

    ``valid: true`` off a body we did not understand is the false positive the
    whole verify path exists to prevent, so an unexpected shape is an error.
    This also pins the difference from the parent, whose decoder wants exactly
    the mapping being refused here.
    """

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"data": {"label": "not a list"}})

    with pytest.raises(DaintreeError) as exc:
        await _provider(handler).verify_key(None)
    assert exc.value.code == "upstream_protocol_error"


@pytest.mark.asyncio
async def test_no_openrouter_attribution_headers_are_sent() -> None:
    """Inherited headers would name a vendor that is not serving the request.

    Harmless in bytes; wrong in fact, and the kind of thing that survives for a
    year because nothing reads it.
    """
    seen: dict[str, object] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["headers"] = {k.lower() for k in request.headers}
        return httpx.Response(200, json=_MODELS_BODY)

    await _provider(handler).verify_key(None)
    assert "http-referer" not in seen["headers"]  # type: ignore[operator]
    assert "x-title" not in seen["headers"]  # type: ignore[operator]


def test_the_container_builds_this_adapter_at_this_origin_with_this_key() -> None:
    """Right class, right URL, right credential, right effort — together.

    None of the four fails loudly on its own. Every adapter in this package
    answers the same calls, so a wrong class just speaks a dialect the host does
    not; a wrong URL sends the server's key somewhere it was never meant to go; a
    wrong key is a cross-vendor disclosure that both other assertions pass
    straight through; and a container that forgot to inject the effort would
    quietly serve every turn at the floor's default rather than the pinned one.
    """
    from daintree_assistant_server.main import _build_provider

    settings = Settings(
        STATE_SIGNING_KEY_CURRENT="x" * 32,
        BASETEN_API_KEY="baseten-test-key",
    )
    provider = _build_provider(settings)
    assert isinstance(provider, BasetenProvider)
    # EXACTLY the origin. `startswith` would accept
    # `https://inference.baseten.co.evil.example`, which is precisely the shape a
    # misdirected credential takes.
    assert provider._base_url == f"https://{EXECUTOR_ORIGIN}/v1"  # noqa: SLF001
    assert provider._resolve_key(None).get_secret_value() == "baseten-test-key"  # noqa: SLF001
    assert provider._reasoning_effort == EXECUTOR_REASONING_EFFORT  # noqa: SLF001
    # And the model the container will ask it for is the pinned one.
    assert EXECUTOR_MODEL == "zai-org/GLM-5.3-Flash"
