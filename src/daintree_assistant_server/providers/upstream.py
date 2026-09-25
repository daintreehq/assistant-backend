"""Bring-your-own-key upstream routing.

A caller may name the host, model and key that pay for its turns. Daintree
collects those in its assistant settings and the CLI forwards them as three
headers (``api/deps.py``). Everything else — runbook selection, prompt assembly,
tools, utility tasks — runs exactly as it does on the server's own key; only the
final provider call is redirected.

**A caller-selected request never fails over.** The server's own failover would
answer an outage on the caller's host with the SERVER's OpenRouter key, which is
both a cost the server did not agree to and a model the caller did not pick. A
BYOK turn is routed straight to its host's adapter and fails as itself.

**One adapter per host, shared by every caller.** The adapters are built without
a key, and each call's ``api_key`` rides the request (``_resolve_key``), which is
the per-call seam this package was built around. The same host's pooled
connections serve every caller safely because the ``Authorization`` header is
built per call.
"""

from __future__ import annotations

import contextlib
from collections.abc import Mapping
from dataclasses import dataclass, field, replace
from typing import Any

from pydantic import SecretStr

from ..errors import DaintreeError, invalid_request, upstream_error
from .base import (
    ChatProvider,
    ProviderChatRequest,
    ProviderChatResult,
    ProviderStream,
    ProviderToolCallResult,
    ProviderUsage,
)


@dataclass(frozen=True)
class UpstreamSpec:
    """One host a caller may choose, and the model we recommend on it."""

    id: str
    label: str
    recommended_model: str


#: The hosts a caller may pick. Keep in step with Daintree's own list
#: (``shared/config/assistantProviders.ts``), which is what the settings UI shows.
UPSTREAMS: Mapping[str, UpstreamSpec] = {
    spec.id: spec
    for spec in (
        UpstreamSpec("baseten", "Baseten", "zai-org/GLM-5.3-Flash"),
        UpstreamSpec("openrouter", "OpenRouter", "z-ai/glm-5.3-flash"),
        UpstreamSpec("openai", "OpenAI", "gpt-6-luna"),
    )
}

_MAX_MODEL_ID_LENGTH = 200

#: The request headers a caller names its upstream with. Headers rather than
#: body fields so the key never lands in a request body, a trace or a dump.
PROVIDER_HEADER = "X-Daintree-Upstream-Provider"
MODEL_HEADER = "X-Daintree-Upstream-Model"
KEY_HEADER = "X-Daintree-Upstream-Key"
#: OpenRouter only: how to rank the endpoints serving the model, and which data
#: policies an endpoint must meet. OpenRouter's own `provider` routing fields.
SORT_HEADER = "X-Daintree-Upstream-Sort"
DATA_COLLECTION_HEADER = "X-Daintree-Upstream-Data-Collection"
ZDR_HEADER = "X-Daintree-Upstream-Zdr"

_SORTS = ("latency", "price", "throughput")
_DATA_COLLECTION = ("allow", "deny")
_BOOLEANS = {"true": True, "false": False}


@dataclass(frozen=True)
class UpstreamRoute:
    """The caller's choice for one request: host, model and credential."""

    provider: str
    model: str
    api_key: SecretStr = field(repr=False)
    #: OpenRouter routing preferences, each ``None`` when the caller stated none.
    sort: str | None = None
    data_collection: str | None = None
    zdr: bool | None = None

    @classmethod
    def parse(
        cls,
        provider: str | None,
        model: str | None,
        api_key: str | None,
        *,
        sort: str | None = None,
        data_collection: str | None = None,
        zdr: str | None = None,
    ) -> UpstreamRoute | None:
        """Build a route from the three header values, or ``None`` for none.

        All three absent is the server-funded path. A key without a host, or a
        host we do not serve, is a caller error rather than something to guess
        past: silently spending the server's key for a caller who meant to spend
        their own would be the worst reading of an ambiguous request.
        """
        provider = (provider or "").strip().lower()
        model = (model or "").strip()
        key = (api_key or "").strip()
        if not provider and not key and not model:
            return None
        if provider not in UPSTREAMS:
            # Never quotes the value: a key pasted into the wrong header would be
            # echoed into the error body and the logs.
            raise invalid_request(
                f"{PROVIDER_HEADER} must name one of: {', '.join(UPSTREAMS)}.",
                param=PROVIDER_HEADER,
                code="invalid_upstream",
            )
        if not key:
            raise invalid_request(
                f"No API key was supplied for {UPSTREAMS[provider].label}.",
                param=KEY_HEADER,
                code="invalid_upstream",
            )
        if model and key in model:
            # A key pasted into the model field would be logged on an upstream
            # failure and echoed by /auth/verify. The message never quotes either.
            # One direction only: a short model id such as `o3` is a substring of
            # a random key often enough that the reverse test refuses real ids.
            raise invalid_request(
                "The model field holds the API key. Put the key in the key field.",
                param=MODEL_HEADER,
                code="invalid_upstream",
            )
        if len(model) > _MAX_MODEL_ID_LENGTH or any(c.isspace() for c in model):
            raise invalid_request(
                "The upstream model id is malformed.", param=MODEL_HEADER, code="invalid_upstream"
            )
        routing = _parse_routing(provider, sort, data_collection, zdr)
        return cls(
            provider=provider,
            model=model or UPSTREAMS[provider].recommended_model,
            api_key=SecretStr(key),
            **routing,
        )

    def apply(self, request: ProviderChatRequest) -> ProviderChatRequest:
        """The same request, addressed to this route's host, model and key."""
        routing = {
            k: v
            for k, v in (
                ("sort", self.sort),
                ("data_collection", self.data_collection),
                ("zdr", self.zdr),
            )
            if v is not None
        }
        return replace(
            request,
            model=self.model,
            api_key=self.api_key,
            upstream=self.provider,
            upstream_routing=routing or None,
        )


def _parse_routing(
    provider: str, sort: str | None, data_collection: str | None, zdr: str | None
) -> dict[str, Any]:
    """OpenRouter's routing preferences, validated, or a 400 naming the header.

    Refused on any other host rather than ignored: a caller that asked for
    zero data retention and silently did not get it is the worst outcome here.
    """
    sort = (sort or "").strip().lower() or None
    data_collection = (data_collection or "").strip().lower() or None
    zdr_text = (zdr or "").strip().lower() or None
    if provider != "openrouter" and (sort or data_collection or zdr_text):
        raise invalid_request(
            "Routing preferences apply to OpenRouter only.",
            param=(
                SORT_HEADER if sort else DATA_COLLECTION_HEADER if data_collection else ZDR_HEADER
            ),
            code="invalid_upstream",
        )
    if sort is not None and sort not in _SORTS:
        raise invalid_request(
            f"{SORT_HEADER} must be one of: {', '.join(_SORTS)}.",
            param=SORT_HEADER,
            code="invalid_upstream",
        )
    if data_collection is not None and data_collection not in _DATA_COLLECTION:
        raise invalid_request(
            f"{DATA_COLLECTION_HEADER} must be allow or deny.",
            param=DATA_COLLECTION_HEADER,
            code="invalid_upstream",
        )
    if zdr_text is not None and zdr_text not in _BOOLEANS:
        raise invalid_request(
            f"{ZDR_HEADER} must be true or false.", param=ZDR_HEADER, code="invalid_upstream"
        )
    return {
        "sort": sort,
        "data_collection": data_collection,
        "zdr": _BOOLEANS[zdr_text] if zdr_text is not None else None,
    }


class UpstreamRouter:
    """A :class:`ChatProvider` that dispatches on ``request.upstream``.

    ``None`` goes to the server's own serving provider, unchanged. A named host
    goes to that host's keyless adapter, with the caller's key on the request.
    """

    def __init__(self, default: ChatProvider, upstreams: Mapping[str, ChatProvider]) -> None:
        self._default = default
        self._upstreams = dict(upstreams)

    @property
    def default(self) -> ChatProvider:
        return self._default

    def _for(self, request: ProviderChatRequest) -> ChatProvider:
        if request.upstream is None:
            return self._default
        adapter = self._upstreams.get(request.upstream)
        if adapter is None:
            raise upstream_error(f"Upstream provider {request.upstream!r} is not available.")
        if request.api_key is None:
            # A routed request without the caller's key would fall back to the
            # adapter's default — and BYOK adapters have none. Fail loudly here
            # rather than with a confusing "no credential" from deeper down.
            raise upstream_error("A routed upstream request carried no API key.")
        return adapter

    async def complete(self, request: ProviderChatRequest) -> ProviderChatResult:
        adapter = self._for(request)
        try:
            return await adapter.complete(request)
        except DaintreeError as exc:
            raise _caller_facing(exc, request) from exc

    async def open_stream(self, request: ProviderChatRequest) -> ProviderStream:
        adapter = self._for(request)
        try:
            stream = await adapter.open_stream(request)
        except DaintreeError as exc:
            raise _caller_facing(exc, request) from exc
        if request.upstream is None:
            return stream
        # Most turns stream, and most in-band failures arrive on the first read,
        # after `open_stream` has returned — so the rewrite has to ride the stream.
        return _RoutedStream(stream, request)

    async def structured_tool_call(
        self,
        request: ProviderChatRequest,
        *,
        function_name: str,
        function_schema: dict[str, Any],
    ) -> ProviderToolCallResult:
        adapter = self._for(request)
        try:
            return await adapter.structured_tool_call(
                request, function_name=function_name, function_schema=function_schema
            )
        except DaintreeError as exc:
            raise _caller_facing(exc, request) from exc

    async def verify_key(self, api_key: SecretStr | None = None) -> dict[str, Any]:
        """The SERVER's own key — what readiness and ``/doctor`` ask about."""
        return await self._default.verify_key(api_key)

    async def verify_route(self, route: UpstreamRoute) -> dict[str, Any]:
        """Whether the caller's key works on the host they picked."""
        adapter = self._upstreams.get(route.provider)
        if adapter is None:
            raise upstream_error(f"Upstream provider {route.provider!r} is not available.")
        return await adapter.verify_key(route.api_key)

    async def aclose(self) -> None:
        # Each distinct adapter once: the BYOK adapters share one client, and an
        # injected test provider may stand in for all of them and the default.
        seen: set[int] = set()
        for adapter in (self._default, *self._upstreams.values()):
            if id(adapter) in seen:
                continue
            seen.add(id(adapter))
            with contextlib.suppress(Exception):
                await adapter.aclose()


#: The provider-credential codes whose default message blames the DEPLOYMENT's
#: key. On a caller-routed request the key is the caller's own, so the same code
#: (which is what the CLI classifies by) carries a message that says so.
_CALLER_KEY_MESSAGES: Mapping[str, str] = {
    "provider_invalid_api_key": (
        "{label} rejected your API key. Check it in Daintree's assistant settings."
    ),
    "provider_insufficient_credits": (
        "Your {label} account has insufficient credits for this request."
    ),
    "provider_key_forbidden": (
        "{label} refused this request for your API key — its model permissions, "
        "spend limits or guardrails."
    ),
}


def _caller_facing(exc: DaintreeError, request: ProviderChatRequest) -> DaintreeError:
    """The same failure, told to the caller whose key and model it was.

    A FRESH error rather than a mutation, so `str(exc)` and `args` agree with the
    message that is sent. Every routed message has the caller's key scrubbed out:
    in-band provider prose is passed through on the stream path, and a provider
    that echoes the credential would otherwise put it in the SSE error body.

    `param` is the marker a client reads — the code alone cannot tell the
    caller's key from the deployment's, and the CLI's copy for these codes
    blames the deployment. It names the key header for a credential failure and
    the model header for a model the host would not run.
    """
    if request.upstream is None:
        return exc
    spec = UPSTREAMS.get(request.upstream)
    label = spec.label if spec else request.upstream
    message = exc.message
    param = exc.param
    retryable = exc.retryable
    template = _CALLER_KEY_MESSAGES.get(exc.code or "")
    if template is not None:
        message = template.format(label=label)
        param = KEY_HEADER
    elif exc.code == "upstream_unavailable" and "HTTP 404" in exc.message:
        # A 404 from the caller's own host means the model id they typed is not
        # one it serves. Retrying or blaming an outage would hide a typo.
        message = (
            f"{label} does not serve the model {request.model!r}. "
            "Check the model in Daintree's assistant settings."
        )
        param = MODEL_HEADER
        retryable = False
    elif exc.code in ("upstream_request_rejected", "context_window_exceeded"):
        message = f"{label} refused the request for model {request.model!r}: {exc.message}"
        param = MODEL_HEADER
    if request.api_key is not None:
        secret = request.api_key.get_secret_value()
        if secret:
            message = message.replace(secret, "[redacted]")
    if message == exc.message and param == exc.param and retryable == exc.retryable:
        return exc
    return DaintreeError(
        status_code=exc.status_code,
        message=message,
        type=exc.type,
        code=exc.code,
        param=param,
        headers=dict(exc.headers),
        retryable=retryable,
    )


class _RoutedStream(ProviderStream):
    """A caller-routed stream whose failures are told to the caller.

    Transparent otherwise: usage, endpoint and abandoned attempts read through, so
    the accounting chain sees exactly what the adapter's own stream reports.
    """

    def __init__(self, inner: ProviderStream, request: ProviderChatRequest) -> None:
        self._inner = inner
        self._request = request

    @property
    def usage(self) -> ProviderUsage | None:
        return self._inner.usage

    @usage.setter
    def usage(self, value: ProviderUsage | None) -> None:
        self._inner.usage = value

    @property
    def endpoint(self) -> str | None:
        return self._inner.endpoint

    @endpoint.setter
    def endpoint(self, value: str | None) -> None:
        self._inner.endpoint = value

    @property
    def abandoned_attempts(self) -> int:
        return self._inner.abandoned_attempts

    @abandoned_attempts.setter
    def abandoned_attempts(self, value: int) -> None:
        self._inner.abandoned_attempts = value

    async def prefetch_first_event(self) -> dict[str, Any] | None:
        try:
            return await self._inner.prefetch_first_event()
        except DaintreeError as exc:
            raise _caller_facing(exc, self._request) from exc

    def __aiter__(self) -> ProviderStream:
        return self

    async def __anext__(self) -> dict[str, Any]:
        try:
            return await self._inner.__anext__()
        except DaintreeError as exc:
            raise _caller_facing(exc, self._request) from exc

    async def aclose(self) -> None:
        await self._inner.aclose()
