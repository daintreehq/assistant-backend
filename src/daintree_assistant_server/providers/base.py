"""The narrow provider protocol and its normalized request/result types.

Business logic depends only on these types, never on a vendor's wire shapes. A
later provider (another hosted API, or a self-hosted PyTorch model) implements
the same protocol without touching request validation, skill selection, state,
prompt construction, or the public endpoints. Swapping one OpenAI-shaped host for another
touched only ``providers/`` and the config that names the models, which is the
property this file exists to keep.
"""

from __future__ import annotations

import abc
import math
from dataclasses import dataclass, field
from typing import Any, Protocol, runtime_checkable

from pydantic import SecretStr


@dataclass(frozen=True)
class ProviderUsage:
    """Token usage, with ``None`` meaning "not reported"."""

    prompt_tokens: int | None = None
    completion_tokens: int | None = None
    total_tokens: int | None = None
    cached_tokens: int | None = None
    # What this call actually charged, in provider credits (USD). It is not a
    # per-token estimate we compute: it is what the provider says it billed, so it
    # already accounts for any cache discount. `None` means "not reported".
    #
    # **Whether the serving host reports one is UNVERIFIED** — there is no live
    # contract suite (see TODO.md), so `None` on every call is a possibility
    # nothing here rules out. The field stays either way, because "not reported"
    # is exactly what `None` already means, and because the whole accounting
    # chain below — `cost_complete`, the floor-vs-total rules, the client-facing
    # `cost` block — was built to survive an unknown rather than invent a zero.
    cost: float | None = None
    # Whether ``cost`` accounts for EVERY billed call folded into this record.
    #
    # A record straight off the wire describes one call, so it is always True:
    # the call either reported a cost or it did not, and ``cost is None`` already
    # says "unknown". The bit only ever matters for an AGGREGATE — a bare-intent
    # re-roll, or a JSON-repair retry summed into the accepted
    # generation. There, if one constituent reported and another did not, `cost`
    # is a non-``None`` number that is nonetheless SHORT of what the caller paid,
    # and nothing about the float itself reveals that. Without this flag the
    # partial sum reaches the client indistinguishable from an exact one — the
    # "zero means free" lie wearing its subtlest hat.
    #
    # Set only by the summing helpers (``turn_accounting.sum_usage`` and the two
    # local variants in the selector and the task runner). ``from_wire`` never
    # clears it.
    cost_complete: bool = True

    @classmethod
    def from_wire(cls, usage: dict[str, Any] | None) -> ProviderUsage:
        if not usage:
            return cls()
        details = usage.get("prompt_tokens_details") or {}
        cached = details.get("cached_tokens") if isinstance(details, dict) else None
        # Defensive: a provider that reports cost as a string, a bool, NaN, or
        # not at all must not turn a good usage record into an exception — nor
        # into a fake zero. `bool` is checked explicitly because it subclasses
        # `int`, so `"cost": false` would otherwise become 0.0 and be reported to
        # the caller as "this was free". Non-finite values are rejected for the
        # same reason plus a practical one: NaN and Infinity are not valid JSON
        # and would produce a body no strict client can parse.
        raw_cost = usage.get("cost")
        cost: float | None = None
        if isinstance(raw_cost, (int, float)) and not isinstance(raw_cost, bool):
            candidate = float(raw_cost)
            if math.isfinite(candidate):
                cost = candidate
        return cls(
            prompt_tokens=usage.get("prompt_tokens"),
            completion_tokens=usage.get("completion_tokens"),
            total_tokens=usage.get("total_tokens"),
            cached_tokens=cached,
            cost=cost,
        )


@dataclass
class ProviderChatRequest:
    """A normalized chat request. ``messages`` are OpenAI-wire-shaped dicts."""

    model: str
    messages: list[dict[str, Any]]
    tools: list[dict[str, Any]] | None = None
    tool_choice: Any | None = None
    temperature: float | None = None
    max_tokens: int | None = None
    response_format: dict[str, Any] | None = None
    stop: str | list[str] | None = None
    stream: bool = False
    include_usage: bool = False
    thinking_disabled: bool = True
    # Reasoning effort, only meaningful when thinking is enabled
    # (``thinking_disabled=False``) AND the model reasons at all. ``None`` lets
    # the model's default apply. Nothing in this server sets it: the posture is
    # hardcoded off and the disable rides the unified ``reasoning`` object.
    reasoning_effort: str | None = None
    timeout: float | None = None
    # The upstream credential for THIS call — on the serving path, always the
    # CALLER's own key (their bearer token), threaded explicitly from the route
    # handler. ``None`` means "use the provider's configured default", which the
    # server does not have (main.py builds it keyless); only standalone tooling
    # (benchmarks, live tests) relies on a default. It rides the request rather
    # than the provider instance because one shared provider serves the selector,
    # orchestrator, task runner, and learner, so a per-process key cannot
    # represent "this turn's key". ``repr=False`` keeps it out of the dataclass
    # repr — belt and braces alongside SecretStr's own masking, since this is a
    # plain dataclass that coerces nothing and untyped callers (benchmarks)
    # could put a bare string here without mypy objecting.
    api_key: SecretStr | None = field(default=None, repr=False)
    # An OPAQUE cache-affinity key for this call, or ``None`` to say nothing.
    #
    # A ROUTER that fronts many hosts for the same weights needs this to send a
    # growing conversation back to the node already holding its prefix. The
    # serving host is a single host with automatic org-scoped prefix caching, so
    # here the key is a hint the upstream may ignore rather than the mechanism
    # the cache runs on — it is still sent, under the OpenAI-standard `prompt_cache_key`
    # spelling, because it costs one short field and states the intent.
    #
    # WHAT IT NAMES depends on which calls share a prefix, and that is not always a
    # session. A conversation is keyed per session, a utility task per task id
    # (docs/PROVIDER_POLICY.md section 3a).
    #
    # It is NEVER the client's session id, whatever it names. Where the value does
    # derive from one, it is a salted pseudonym (see
    # ``ChatOrchestrator._cache_session_key``): the field crosses to a third party,
    # and a raw client identifier has no business doing so. The name says ``cache_``
    # precisely so a future caller does not read ``session_id`` and forward the real
    # one. ``repr=False`` keeps it out of logs alongside ``api_key`` — a
    # session-derived key is a stable correlator even hashed.
    cache_session_id: str | None = field(default=None, repr=False)


@dataclass
class ProviderChatResult:
    """A non-streaming completion result."""

    id: str
    created: int
    model: str
    choices: list[dict[str, Any]]
    usage: ProviderUsage
    finish_reason: str | None = None
    # Which upstream endpoint actually served this call, when the provider reports
    # one. Observability ONLY: nothing branches on it, and ``None`` simply means
    # "not reported" — a single-host provider never sets it, which is every call
    # against a single host today. Kept for a future router, where a slow turn is
    # otherwise undiagnosable: the log says the call took 10s but not which of
    # many interchangeable endpoints was responsible.
    endpoint: str | None = None
    raw: dict[str, Any] = field(default_factory=dict)

    @property
    def message(self) -> dict[str, Any]:
        if not self.choices:
            return {}
        return self.choices[0].get("message", {}) or {}

    @property
    def text(self) -> str:
        return self.message.get("content") or ""

    @property
    def tool_calls(self) -> list[dict[str, Any]]:
        return self.message.get("tool_calls") or []


@dataclass
class ProviderToolCallResult:
    """The result of a forced single function call."""

    name: str
    arguments_json: str
    usage: ProviderUsage
    endpoint: str | None = None
    raw: dict[str, Any] = field(default_factory=dict)


class ProviderStream(abc.ABC):
    """An open upstream stream of OpenAI-shaped chunk dicts.

    Usage::

        stream = await provider.open_stream(req)
        first = await stream.prefetch_first_event()   # may raise / be None
        # ... retry open/prefetch before forwarding any model chunk ...
        if first is not None:
            yield first
        async for chunk in stream:                     # remaining chunks
            yield chunk
        await stream.aclose()
    """

    usage: ProviderUsage | None = None
    # Set once the first chunk naming it arrives; see ProviderChatResult.endpoint.
    endpoint: str | None = None

    @abc.abstractmethod
    async def prefetch_first_event(self) -> dict[str, Any] | None:
        """Read and return the first chunk (consuming it), or ``None`` if the
        stream ended immediately. Raises a mapped error on upstream failure."""

    @abc.abstractmethod
    def __aiter__(self) -> ProviderStream:
        ...

    @abc.abstractmethod
    async def __anext__(self) -> dict[str, Any]:
        ...

    @abc.abstractmethod
    async def aclose(self) -> None:
        ...


@runtime_checkable
class ChatProvider(Protocol):
    """The provider interface the orchestrator and task runner depend on."""

    async def complete(self, request: ProviderChatRequest) -> ProviderChatResult: ...

    async def open_stream(self, request: ProviderChatRequest) -> ProviderStream: ...

    async def structured_tool_call(
        self,
        request: ProviderChatRequest,
        *,
        function_name: str,
        function_schema: dict[str, Any],
    ) -> ProviderToolCallResult: ...

    async def verify_key(self, api_key: SecretStr | None = None) -> dict[str, Any]:
        """Ask the provider whether a credential is usable, spending no tokens.

        Part of the protocol because readiness and the doctor endpoint need an
        answer that a configured-but-wrong key would otherwise hide until the
        first real turn — see api/daintree_auth.py. Implementations raise
        ``invalid_api_key`` for a definite rejection and ``upstream_error`` when the
        probe could not be completed, so "unusable" and "unknown" stay distinct.
        """
        ...

    async def aclose(self) -> None: ...
