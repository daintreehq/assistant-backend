"""The narrow provider protocol and its normalized request/result types.

Business logic depends only on these types, never on a vendor's wire shapes. A
later provider (another hosted API, or a self-hosted PyTorch model) implements
the same protocol without touching request validation, runbook selection, state,
prompt construction, or the public endpoints. Swapping one OpenAI-shaped host for another
touched only ``providers/`` and the config that names the models, which is the
property this file exists to keep.
"""

from __future__ import annotations

import abc
import math
import re
from dataclasses import dataclass, field
from typing import Any, Literal, Protocol, runtime_checkable

from pydantic import SecretStr

# Where a usage record's cached-token count was read from. Serving hosts
# document BOTH shapes and which one appears depends on the model, so the
# parser reads both and this records which one actually arrived — the only way to tell
# "this host nests it" from "this host reports no reuse", which are the same
# ``cached_tokens=None`` otherwise. Diagnostic only; nothing branches on it.
CachedTokenSource = Literal["nested", "flat"]


#: A token count encoded as a string, in the only spelling that is unambiguous:
#: ASCII digits and nothing else. No sign, no whitespace, no exponent, no
#: separators, no Unicode digits (``str.isdigit()`` accepts superscripts).
_CANONICAL_DIGITS = re.compile(r"\A[0-9]{1,19}\Z")


def _token_count(value: Any) -> int | None:
    """A non-negative token count off the wire, or ``None`` for "not reported".

    Token fields used to be forwarded raw, which made ``ProviderUsage``'s
    ``int | None`` annotation a claim rather than a fact: a host reporting
    ``"prompt_tokens": "1024"`` put a string into the field, and the first thing
    to touch it arithmetically raised — ``turn_accounting.cache_ratio`` divides
    by it, mid-turn, on the paid path. The same care ``cost`` below already gets,
    for the same reason.

    Rejecting is not free, though, and that is the reason this coerces where it
    safely can rather than demanding a bare ``int``. Downstream every count is
    projected to the client with ``or 0``, so a value refused HERE does not
    surface as "not reported" — it surfaces as a confident zero, which is the
    lie this module exists to avoid. So a form that can be read exactly one way
    is read: an integral float (``1024.0`` is a legitimate JSON encoding of
    1024) and a canonical all-digits string both become the count they plainly
    are. Anything needing a judgement call — a sign, whitespace, an exponent, a
    fraction — is refused, because guessing at a malformed count is worse than
    admitting we do not have one.

    ``bool`` is rejected explicitly because it subclasses ``int``, so ``True``
    would otherwise land as a token count of 1.
    """
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value if value >= 0 else None
    if isinstance(value, float):
        if not math.isfinite(value) or not value.is_integer() or value < 0:
            return None
        return int(value)
    if isinstance(value, str) and _CANONICAL_DIGITS.match(value):
        return int(value)
    return None


@dataclass(frozen=True)
class ProviderUsage:
    """Token usage, with ``None`` meaning "not reported"."""

    prompt_tokens: int | None = None
    completion_tokens: int | None = None
    total_tokens: int | None = None
    cached_tokens: int | None = None
    # Which of the two documented shapes supplied ``cached_tokens``. ``None`` on
    # an aggregate (a sum has no single source) and on any record that reported
    # no reuse at all.
    cached_tokens_source: CachedTokenSource | None = None
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
        # CACHED TOKENS ARRIVE IN ONE OF TWO PLACES. Hosts document the count
        # nested under `prompt_tokens_details` for some models and at the top
        # level for others, so reading only the nested one reports zero reuse on
        # half the catalogue — and a cache metric that under-reports is worse than
        # absent, because it reads as "prefix caching is not working" and invites
        # optimisation against a measurement rather than the wire.
        #
        # BOTH are read, and neither is a fallback for the other in the naive
        # sense of "use nested unless it is missing". That rule has a hole: an
        # OpenAI-shaped host commonly emits `prompt_tokens_details` as a FIXED
        # SKELETON — `{"cached_tokens": 0, "audio_tokens": 0, ...}` — whether or
        # not it maintains those fields. Preferring the nested value there would
        # let a zero it never filled in mask a real flat count, which is the
        # exact under-report this fallback was added to fix, now arriving through
        # the fix itself.
        #
        # So: a REPORT beats a ZERO. Zero and absent make the same operational
        # claim ("no reuse"), while a non-zero count is strictly more
        # information, and the only way the two fields can disagree by one being
        # zero is that the zero is a skeleton. When they agree, or only one is
        # present, there is nothing to choose. Two DIFFERENT non-zero values is a
        # should-never-happen — one number reported twice, differently — and
        # nested wins so the outcome is at least deterministic;
        # `cached_tokens_source` is what makes the disagreement visible in the
        # logs, because a host whose shape changed under us shows up as this
        # field flipping.
        #
        # They are never SUMMED. A host emitting both is reporting one number
        # twice, and adding them would double the reuse on exactly the requests
        # where the cache is doing most work.
        details = usage.get("prompt_tokens_details")
        nested = _token_count(details.get("cached_tokens")) if isinstance(details, dict) else None
        flat = _token_count(usage.get("cached_tokens"))
        cached: int | None
        source: CachedTokenSource | None
        if nested and not flat:
            cached, source = nested, "nested"
        elif flat and not nested:
            cached, source = flat, "flat"
        elif nested is not None:
            cached, source = nested, "nested"
        elif flat is not None:
            cached, source = flat, "flat"
        else:
            cached, source = None, None
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
            prompt_tokens=_token_count(usage.get("prompt_tokens")),
            completion_tokens=_token_count(usage.get("completion_tokens")),
            total_tokens=_token_count(usage.get("total_tokens")),
            cached_tokens=cached,
            cached_tokens_source=source,
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
    # The upstream credential for THIS call. **The serving path no longer sets
    # it**: ``auth.py`` ignores the ``Authorization`` header entirely and the
    # server funds every turn from its own ``BASETEN_API_KEY``, so the
    # orchestrator threads ``None`` and the provider's configured default is what
    # actually pays. The field stays live and tested as the slot an
    # account-mapped credential will occupy once account authentication lands
    # (``TODO.md``); benchmarks and live tests are its only callers today. It
    # rides the request rather
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
    # there is no node to pin and no spelling the host advertises. **It is
    # therefore computed and NEVER sent** — ``_build_body`` drops it, and
    # ``tests/unit/test_provider_openrouter.py::test_no_routing_or_affinity_field_is_ever_sent``
    # asserts neither vendor's spelling (``session_id``, ``prompt_cache_key``)
    # reaches the wire. An unadvertised parameter is a needless 400 risk for a
    # field that would buy nothing against automatic prefix caching.
    #
    # It survives on the normalized request because it costs nothing there and
    # states which calls belong to one growing prefix — information no one can
    # recover after the fact, so the day a host advertises a spelling the answer
    # is already computed (docs/PROVIDER_POLICY.md section 3a).
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
