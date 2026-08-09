"""The narrow provider protocol and its normalized request/result types.

Business logic depends only on these types, never on OpenRouter wire shapes. A
later provider (another hosted API, or a self-hosted PyTorch model) implements
the same protocol without touching request validation, skill selection, state,
prompt construction, or the public endpoints.
"""

from __future__ import annotations

import abc
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

    @classmethod
    def from_wire(cls, usage: dict[str, Any] | None) -> ProviderUsage:
        if not usage:
            return cls()
        details = usage.get("prompt_tokens_details") or {}
        cached = details.get("cached_tokens") if isinstance(details, dict) else None
        return cls(
            prompt_tokens=usage.get("prompt_tokens"),
            completion_tokens=usage.get("completion_tokens"),
            total_tokens=usage.get("total_tokens"),
            cached_tokens=cached,
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
    # (``thinking_disabled=False``). OpenRouter accepts the usual tiers
    # ("minimal"/"low"/"medium"/"high"/"xhigh"/"max") and translates them for the
    # endpoint that answers; DeepSeek collapses low/medium to high and xhigh to
    # max. ``None`` lets the endpoint default apply.
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
    # one (a router like OpenRouter fronts many hosts for the same weights, and
    # they differ by more than 10x in latency). Observability ONLY: nothing branches
    # on it, and ``None`` simply means "not reported" — a single-host provider never
    # sets it. It exists because a slow turn is otherwise undiagnosable: the log
    # says the call took 10s but not which of ~24 endpoints was responsible.
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

        Part of the protocol because sign-in needs an answer our structural auth
        cannot give — see api/daintree_auth.py. Implementations raise
        ``invalid_api_key`` for a definite rejection and ``upstream_error`` when the
        probe could not be completed, so "unusable" and "unknown" stay distinct.
        """
        ...

    async def aclose(self) -> None: ...
