"""Together adapter: the three things that are NOT inherited.

``TogetherProvider`` is deliberately a thin subclass of ``OpenRouterProvider`` —
same body, same SSE parsing, same error mapping — so the parent's suite already
covers almost all of it, and re-testing inherited behaviour here would only
create two places to update. What is NOT covered by the parent is exactly what
this file pins: the three overridden class attributes, and the key-record decoder
whose parent implementation is WRONG for this host.

That decoder is not hypothetical. It shipped broken: the parent requires
``{"data": {...}}`` (OpenRouter's ``/key`` shape) and Together's ``/models``
answers a bare JSON array, so every VALID Together key raised
``upstream_protocol_error`` — ``/v1/daintree/auth/verify`` returning 502 and the
doctor route reporting ``provider_unreachable`` for a credential that had just
proved itself. Nothing caught it because the adapter had no tests at all.
"""

from __future__ import annotations

import httpx
import pytest
from pydantic import SecretStr

from daintree_assistant_server.config import SERVING_HOST, Settings
from daintree_assistant_server.errors import DaintreeError
from daintree_assistant_server.providers.cerebras import CerebrasProvider
from daintree_assistant_server.providers.together import TogetherProvider

#: Together's real ``/models`` shape, trimmed: a bare top-level array whose items
#: carry an ``id``. Captured from the live API rather than imagined — the whole
#: bug was an assumption about this shape.
_MODELS_BODY = [
    {"id": "deepseek-ai/DeepSeek-V4-Flash-0731", "object": "model", "type": "chat"},
    {"id": "moonshotai/Kimi-K3", "object": "model", "type": "chat"},
]


def _provider(handler: object) -> TogetherProvider:
    transport = httpx.MockTransport(handler)  # type: ignore[arg-type]
    return TogetherProvider(
        httpx.AsyncClient(transport=transport), api_key=SecretStr("tgp_v1_test")
    )


@pytest.mark.asyncio
async def test_a_valid_key_verifies_against_the_bare_model_list() -> None:
    """The regression that shipped: a good key must not read as a protocol error.

    Reporting "we could not tell" about a credential the provider just accepted
    is the one answer this endpoint must never give when it does in fact know.
    """
    seen: dict[str, object] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["url"] = str(request.url)
        seen["headers"] = dict(request.headers)
        return httpx.Response(200, json=_MODELS_BODY)

    record = await _provider(handler).verify_key(None)

    assert record["models"] == [
        "deepseek-ai/DeepSeek-V4-Flash-0731",
        "moonshotai/Kimi-K3",
    ]
    # `/models`, because Together has no `/key` route — the parent's path would
    # 404 here, which maps to "could not verify" rather than a verdict.
    assert seen["url"] == "https://api.together.xyz/v1/models"


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


def test_the_container_builds_the_adapter_serving_host_names() -> None:
    """The claim ``SERVING_HOST`` makes must be true of the object built.

    A mismatch here does not fail loudly — both adapters answer the same calls —
    it just sends every turn to a host the configuration says we left, with the
    other host's credential.
    """
    from daintree_assistant_server.main import _build_provider

    settings = Settings(
        STATE_SIGNING_KEY_CURRENT="x" * 32,
        TOGETHER_API_KEY="tgp_v1_test",
        CEREBRAS_API_KEY="csk-test",
    )
    provider = _build_provider(settings)
    expected = TogetherProvider if SERVING_HOST == "together" else CerebrasProvider
    assert isinstance(provider, expected)
    # And it is pointed at that host, not merely an instance of its class.
    assert provider._base_url.startswith(  # noqa: SLF001
        "https://api.together.xyz" if SERVING_HOST == "together" else "https://api.cerebras.ai"
    )
