"""Bring-your-own-key routing: the OpenAI adapter, header parsing, and dispatch.

The OpenAI body rules were each measured against ``gpt-6-luna`` on the live API:
tools are refused unless ``reasoning_effort`` is ``none`` (the field absent is
refused too), ``stop`` and ``max_tokens`` are refused outright. A body that gets
any of those wrong is a 400 on every executor turn for an OpenAI user.
"""

from __future__ import annotations

from typing import Any

import httpx
import pytest
from pydantic import SecretStr

from daintree_assistant_server.errors import DaintreeError
from daintree_assistant_server.providers.base import ProviderChatRequest
from daintree_assistant_server.providers.openai import OpenAIProvider, is_reasoning_model
from daintree_assistant_server.providers.upstream import (
    UPSTREAMS,
    UpstreamRoute,
    UpstreamRouter,
)

_TOOLS = [{"type": "function", "function": {"name": "add", "parameters": {"type": "object"}}}]


def _never_called(request: httpx.Request) -> httpx.Response:
    raise AssertionError(f"unexpected upstream call to {request.url}")


def _openai() -> OpenAIProvider:
    return OpenAIProvider(httpx.AsyncClient(transport=httpx.MockTransport(_never_called)))


def _request(**overrides: Any) -> ProviderChatRequest:
    base: dict[str, Any] = {
        "model": "gpt-6-luna",
        "messages": [
            {"role": "user", "content": "hi"},
            {"role": "assistant", "content": "", "reasoning_content": "thought"},
        ],
        "max_tokens": 100,
        "temperature": 0.2,
        "stop": ["END"],
    }
    base.update(overrides)
    return ProviderChatRequest(**base)


# ---- OpenAI body ------------------------------------------------------------


def test_tools_force_reasoning_off_on_a_reasoning_model() -> None:
    body = _openai()._build_body(
        _request(tools=_TOOLS, thinking_disabled=False, reasoning_effort="high"), stream=True
    )
    assert body["reasoning_effort"] == "none"
    assert body["temperature"] == 0.2


def test_max_tokens_is_spelled_the_way_openai_accepts() -> None:
    body = _openai()._build_body(_request(), stream=False)
    assert body["max_completion_tokens"] == 100
    assert "max_tokens" not in body


def test_stop_and_the_unified_reasoning_object_never_reach_a_reasoning_model() -> None:
    body = _openai()._build_body(_request(), stream=False)
    assert "stop" not in body
    assert "reasoning" not in body


def test_an_effort_above_none_drops_temperature() -> None:
    body = _openai()._build_body(
        _request(thinking_disabled=False, reasoning_effort="low"), stream=False
    )
    assert body["reasoning_effort"] == "low"
    assert "temperature" not in body


def test_replayed_chain_of_thought_is_dropped_without_mutating_the_request() -> None:
    request = _request()
    body = _openai()._build_body(request, stream=False)
    assert all("reasoning_content" not in m and "reasoning" not in m for m in body["messages"])
    assert request.messages[1]["reasoning_content"] == "thought"


@pytest.mark.parametrize("model", ["o3", "o4-mini", "gpt-5"])
def test_the_o_series_never_gets_none(model: str) -> None:
    """Measured: o3 and o4-mini 400 on ``none`` and answer tools at ``low``."""
    body = _openai()._build_body(_request(model=model, tools=_TOOLS), stream=False)
    assert body["reasoning_effort"] == "low"
    assert "temperature" not in body


@pytest.mark.parametrize("model", ["gpt-6-sol", "gpt-5.6-terra"])
def test_the_gpt_5x_and_6_family_takes_none_with_tools(model: str) -> None:
    body = _openai()._build_body(_request(model=model, tools=_TOOLS), stream=False)
    assert body["reasoning_effort"] == "none"


def test_an_exhausted_openai_balance_is_a_credit_error_not_a_rate_limit() -> None:
    err = _openai()._map_http_error(
        429, httpx.Headers(), "You exceeded your current quota, please check your plan."
    )
    assert err.code == "provider_insufficient_credits"
    assert not err.retryable
    plain = _openai()._map_http_error(429, httpx.Headers(), "Rate limit reached for requests")
    assert plain.code == "upstream_rate_limited"


def test_openrouter_byok_sends_the_reasoning_floor_only_to_glm() -> None:
    """Measured: a reasoning object empties the pool for non-reasoning models."""
    from daintree_assistant_server.providers.openrouter_fallback import OpenRouterByokProvider

    adapter = OpenRouterByokProvider(
        httpx.AsyncClient(transport=httpx.MockTransport(_never_called)), reasoning_effort="low"
    )
    glm = adapter._build_body(_request(model="z-ai/glm-5.3-flash", tools=_TOOLS), stream=True)
    other = adapter._build_body(_request(model="openai/gpt-4.1-mini", tools=_TOOLS), stream=True)
    assert glm["reasoning"] == {"effort": "low"}
    assert "reasoning" not in other
    assert other["provider"]["require_parameters"] is True
    assert "ignore" not in other["provider"]


def test_a_non_reasoning_model_gets_a_plain_chat_body() -> None:
    body = _openai()._build_body(_request(model="gpt-4.1", tools=_TOOLS), stream=False)
    assert "reasoning_effort" not in body
    assert body["stop"] == ["END"]
    assert body["max_completion_tokens"] == 100


@pytest.mark.parametrize(
    ("model", "expected"),
    [("gpt-6-luna", True), ("gpt-5.6-sol", True), ("o3", True), ("gpt-4.1", False)],
)
def test_reasoning_models_are_recognised_by_id(model: str, expected: bool) -> None:
    assert is_reasoning_model(model) is expected


# ---- header parsing ---------------------------------------------------------


def test_no_headers_is_the_server_funded_path() -> None:
    assert UpstreamRoute.parse(None, None, None) is None
    assert UpstreamRoute.parse("", " ", "") is None


def test_an_omitted_model_takes_the_recommendation() -> None:
    route = UpstreamRoute.parse("Baseten", None, "  key-1  ")
    assert route is not None
    assert route.provider == "baseten"
    assert route.model == UPSTREAMS["baseten"].recommended_model
    assert route.api_key.get_secret_value() == "key-1"


def test_the_key_never_appears_in_a_route_repr() -> None:
    route = UpstreamRoute.parse("openai", "gpt-6-luna", "sk-secret-value")
    assert "sk-secret-value" not in repr(route)


@pytest.mark.parametrize(
    ("provider", "model", "key"),
    [
        ("anthropic", None, "sk-secret-zz"),  # a host we do not serve
        ("sk-secret-zz", None, "sk-secret-zz"),  # a key pasted into the host header
        (None, None, "sk-secret-zz"),  # a key with no host
        ("openai", None, None),  # a host with no key
        ("openai", "gpt 6", "sk-secret-zz"),  # whitespace in a model id
    ],
)
def test_an_ambiguous_route_is_refused_rather_than_guessed(
    provider: str | None, model: str | None, key: str | None
) -> None:
    with pytest.raises(DaintreeError) as info:
        UpstreamRoute.parse(provider, model, key)
    assert info.value.status_code == 400
    assert info.value.code == "invalid_upstream"
    if key:
        assert key not in info.value.message
    if provider:
        assert provider not in info.value.message or provider in UPSTREAMS


# ---- dispatch ---------------------------------------------------------------


class _Recorder:
    def __init__(self, name: str) -> None:
        self.name = name
        self.seen: list[ProviderChatRequest] = []
        self.verified: list[SecretStr | None] = []
        self.closed = 0

    async def complete(self, request: ProviderChatRequest) -> str:
        self.seen.append(request)
        return self.name

    async def verify_key(self, api_key: SecretStr | None = None) -> dict[str, Any]:
        self.verified.append(api_key)
        return {"models": []}

    async def aclose(self) -> None:
        self.closed += 1


async def test_the_router_sends_a_routed_request_to_its_host_with_the_callers_key() -> None:
    default, openai = _Recorder("default"), _Recorder("openai")
    router = UpstreamRouter(default, {"openai": openai})  # type: ignore[arg-type]
    route = UpstreamRoute.parse("openai", None, "sk-caller")
    assert route is not None

    routed = route.apply(_request(model="zai-org/GLM-5.3-Flash"))
    assert await router.complete(routed) == "openai"  # type: ignore[comparison-overlap]
    assert openai.seen[0].model == "gpt-6-luna"
    assert openai.seen[0].api_key is not None
    assert openai.seen[0].api_key.get_secret_value() == "sk-caller"

    assert await router.complete(_request()) == "default"  # type: ignore[comparison-overlap]


async def test_a_routed_request_without_a_key_never_falls_back_to_the_server() -> None:
    default, openai = _Recorder("default"), _Recorder("openai")
    router = UpstreamRouter(default, {"openai": openai})  # type: ignore[arg-type]
    with pytest.raises(DaintreeError):
        await router.complete(_request(upstream="openai"))
    assert not default.seen and not openai.seen


async def test_verify_route_probes_the_callers_host_and_verify_key_the_servers() -> None:
    default, baseten = _Recorder("default"), _Recorder("baseten")
    router = UpstreamRouter(default, {"baseten": baseten})  # type: ignore[arg-type]
    route = UpstreamRoute.parse("baseten", None, "caller-key")
    assert route is not None
    await router.verify_route(route)
    await router.verify_key()
    assert [k.get_secret_value() for k in baseten.verified if k] == ["caller-key"]
    assert default.verified == [None]


async def test_a_shared_adapter_is_closed_once() -> None:
    shared = _Recorder("shared")
    router = UpstreamRouter(shared, {"baseten": shared, "openai": shared})  # type: ignore[arg-type]
    await router.aclose()
    assert shared.closed == 1


async def test_a_rejected_caller_key_is_reported_as_the_callers_not_the_servers() -> None:
    from daintree_assistant_server.errors import provider_invalid_api_key

    class _Rejecting(_Recorder):
        async def complete(self, request: ProviderChatRequest) -> str:
            raise provider_invalid_api_key()

    router = UpstreamRouter(_Recorder("default"), {"openrouter": _Rejecting("or")})  # type: ignore[arg-type]
    route = UpstreamRoute.parse("openrouter", None, "sk-or-dead")
    assert route is not None
    with pytest.raises(DaintreeError) as info:
        await router.complete(route.apply(_request()))
    assert info.value.code == "provider_invalid_api_key"
    assert "your API key" in info.value.message
    assert "deployment" not in info.value.message
    assert info.value.param == "X-Daintree-Upstream-Key"


async def test_a_stream_failure_is_told_to_the_caller_with_the_key_scrubbed() -> None:
    from daintree_assistant_server.errors import upstream_request_rejected
    from daintree_assistant_server.providers.base import ProviderStream

    class _Failing(ProviderStream):
        async def prefetch_first_event(self) -> dict[str, Any] | None:
            raise upstream_request_rejected("bad request: key sk-or-leaky was not valid here")

        def __aiter__(self) -> ProviderStream:
            return self

        async def __anext__(self) -> dict[str, Any]:
            raise StopAsyncIteration

        async def aclose(self) -> None:
            return None

    class _Streams(_Recorder):
        async def open_stream(self, request: ProviderChatRequest) -> ProviderStream:
            return _Failing()

    router = UpstreamRouter(_Recorder("default"), {"openrouter": _Streams("or")})  # type: ignore[arg-type]
    route = UpstreamRoute.parse("openrouter", "some/model", "sk-or-leaky")
    assert route is not None
    stream = await router.open_stream(route.apply(_request()))
    with pytest.raises(DaintreeError) as info:
        await stream.prefetch_first_event()
    assert "sk-or-leaky" not in info.value.message
    assert "some/model" in info.value.message
    assert info.value.param == "X-Daintree-Upstream-Model"
    assert str(info.value) == info.value.message


async def test_a_model_the_host_does_not_serve_names_the_model_and_is_not_retried() -> None:
    from daintree_assistant_server.errors import upstream_unavailable

    class _NotFound(_Recorder):
        async def complete(self, request: ProviderChatRequest) -> str:
            raise upstream_unavailable(
                "The upstream provider has no endpoint for this request (HTTP 404)."
            )

    router = UpstreamRouter(_Recorder("default"), {"openai": _NotFound("oa")})  # type: ignore[arg-type]
    route = UpstreamRoute.parse("openai", "gpt-typo", "sk-x")
    assert route is not None
    with pytest.raises(DaintreeError) as info:
        await router.complete(route.apply(_request()))
    assert "gpt-typo" in info.value.message
    assert info.value.retryable is False


def test_an_in_band_quota_429_is_a_credit_error() -> None:
    from daintree_assistant_server.providers.openrouter import map_stream_error

    err = map_stream_error(
        {"code": 429, "type": "insufficient_quota", "message": "You exceeded your current quota."}
    )
    assert err.code == "provider_insufficient_credits"
    assert map_stream_error({"code": 429, "message": "slow down"}).code == "upstream_rate_limited"


@pytest.mark.parametrize("model", ["sk-secret-zz", "prefix-sk-secret-zz", "secret"])
def test_a_key_pasted_into_the_model_field_is_refused_without_quoting_it(model: str) -> None:
    with pytest.raises(DaintreeError) as info:
        UpstreamRoute.parse("openai", model, "sk-secret-zz")
    assert info.value.code == "invalid_upstream"
    assert "secret" not in info.value.message


def test_openrouter_routing_preferences_reach_the_provider_block() -> None:
    from daintree_assistant_server.providers.openrouter_fallback import OpenRouterByokProvider

    route = UpstreamRoute.parse(
        "openrouter", None, "sk-or", sort="Price", data_collection="deny", zdr="true"
    )
    assert route is not None
    adapter = OpenRouterByokProvider(
        httpx.AsyncClient(transport=httpx.MockTransport(_never_called)), reasoning_effort="low"
    )
    body = adapter._build_body(route.apply(_request(model="z-ai/glm-5.3-flash")), stream=True)
    assert body["provider"]["sort"] == "price"
    assert body["provider"]["data_collection"] == "deny"
    assert body["provider"]["zdr"] is True
    assert body["provider"]["require_parameters"] is True

    plain = UpstreamRoute.parse("openrouter", None, "sk-or")
    assert plain is not None
    default = adapter._build_body(plain.apply(_request(model="z-ai/glm-5.3-flash")), stream=True)
    assert default["provider"]["sort"] == "latency"
    assert "data_collection" not in default["provider"] and "zdr" not in default["provider"]


@pytest.mark.parametrize(
    ("provider", "kwargs"),
    [
        ("openai", {"sort": "price"}),  # OpenRouter only
        ("baseten", {"zdr": "true"}),
        ("openrouter", {"sort": "cheapest"}),
        ("openrouter", {"data_collection": "maybe"}),
        ("openrouter", {"zdr": "yes"}),
    ],
)
def test_a_routing_preference_that_cannot_be_honoured_is_refused(
    provider: str, kwargs: dict[str, str]
) -> None:
    with pytest.raises(DaintreeError) as info:
        UpstreamRoute.parse(provider, None, "sk-x", **kwargs)
    assert info.value.code == "invalid_upstream"
