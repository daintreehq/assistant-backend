"""Two hosts for one model: the wire the fallback sends, and when it is reached.

Split in two, because two very different things can regress here.

The WIRE half pins ``OpenRouterFallbackProvider``'s body. Everything it changes
is silent when wrong: an endpoint that ignores ``response_format`` returns prose
rather than an error, an omitted ``reasoning`` effort means ``high`` on these
weights rather than a 400, and a ``provider`` block that quietly stopped being
sent looks exactly like one that is being honoured. None of it surfaces as a
failure — only as a slower, more expensive, or subtly wrong answer on the one
path nobody watches, because it only runs during an outage.

The BEHAVIOUR half pins the breaker. Its whole job is a judgement about time, and
both directions of getting it wrong are expensive and invisible: too eager and a
single 500 moves every subsequent turn onto a second host; too reluctant and a
ten-minute outage puts a failed primary attempt — usually a full timeout — in
front of every turn in it. Neither shows up as a red test unless someone writes
one, so the state machine is driven here against a fake clock rather than a real
one.

What is deliberately NOT re-tested: anything inherited. Both classes are thin
subclasses/wrappers over adapters with their own suites, and re-asserting SSE
parsing or error mapping here would only create a second place to update.
"""

from __future__ import annotations

import asyncio
from typing import Any

import httpx
import pytest
from pydantic import SecretStr
from structlog.testing import capture_logs

from daintree_assistant_server.config import (
    EXECUTOR_FALLBACK_FAILURE_THRESHOLD,
    EXECUTOR_FALLBACK_IGNORED_PROVIDERS,
    EXECUTOR_FALLBACK_MODEL,
    EXECUTOR_FALLBACK_PROBE_INTERVAL_SECONDS,
    EXECUTOR_FALLBACK_STALL_HOLDOFF_SECONDS,
    EXECUTOR_MODEL,
    EXECUTOR_PROVIDER,
    EXECUTOR_REASONING_EFFORT,
)
from daintree_assistant_server.errors import (
    DaintreeError,
    provider_key_forbidden,
    upstream_context_window_exceeded,
    upstream_rate_limited,
    upstream_request_rejected,
    upstream_timeout,
    upstream_unavailable,
)
from daintree_assistant_server.providers.base import (
    ProviderChatRequest,
    ProviderChatResult,
    ProviderStream,
    ProviderToolCallResult,
    ProviderUsage,
)
from daintree_assistant_server.providers.failover import (
    FailoverHealth,
    FailoverProvider,
    PrimaryDecision,
)
from daintree_assistant_server.providers.openrouter import OpenRouterProvider
from daintree_assistant_server.providers.openrouter_fallback import (
    OpenRouterFallbackProvider,
)

# ---------------------------------------------------------------------------
# The wire.
# ---------------------------------------------------------------------------


def _never_called(request: httpx.Request) -> httpx.Response:
    """Body construction is pure: reaching the transport at all is the failure."""
    raise AssertionError(f"_build_body must not open a request (got {request.url})")


def _fallback_provider(**kwargs: Any) -> OpenRouterFallbackProvider:
    return OpenRouterFallbackProvider(
        httpx.AsyncClient(transport=httpx.MockTransport(_never_called)),
        api_key=SecretStr("sk-or-v1-test"),
        **kwargs,
    )


def _body(provider: OpenRouterFallbackProvider, **kwargs: Any) -> dict[str, Any]:
    request = ProviderChatRequest(
        model=EXECUTOR_FALLBACK_MODEL,
        messages=[{"role": "user", "content": "hi"}],
        **kwargs,
    )
    return provider._build_body(request, stream=False)  # noqa: SLF001


def test_the_fallback_sorts_the_endpoint_pool_by_latency() -> None:
    """The user-visible cost of a failover is time-to-first-byte.

    OpenRouter's default balance is price-and-throughput weighted, which is the
    wrong optimum for a turn that has already lost a second or more waiting out a
    dead primary. This is also the ONE place a `provider` block is sent at all, so
    it stops being sent by accident rather than by decision — which would look
    identical from the outside and cost only latency.
    """
    body = _body(_fallback_provider())
    assert body["provider"]["sort"] == "latency"


def test_the_fallback_excludes_the_primary_from_its_own_pool() -> None:
    """Routing back to OpenAI through a second hop cannot help.

    OpenRouter serves these weights from OpenAI, Azure and Bedrock. This
    adapter only runs BECAUSE OpenAI just failed, so that one endpoint is
    guaranteed to buy a slower copy of the failure that already happened —
    turning a genuine second opinion into a retry wearing a different hostname.
    """
    body = _body(_fallback_provider())
    assert body["provider"]["ignore"] == list(EXECUTOR_FALLBACK_IGNORED_PROVIDERS)
    assert EXECUTOR_PROVIDER in body["provider"]["ignore"]


def test_the_fallback_refuses_endpoints_that_would_drop_a_parameter() -> None:
    """OpenRouter's default is to DROP an unsupported parameter, not to avoid the
    endpoint — and the parameters this server sends fail silently when dropped.

    A JSON utility task whose `response_format` went missing gets prose back,
    spends its one repair retry on the same missing constraint, and then 502s;
    nothing in that chain names the real cause. With twenty candidate endpoints,
    requiring support costs nothing worth having.
    """
    assert _body(_fallback_provider())["provider"]["require_parameters"] is True


def test_the_fallback_still_allows_openrouter_s_own_second_choice() -> None:
    """A fallback that could not fall back would be a new single point of failure —
    the exact shape of the problem this whole feature exists to remove."""
    assert _body(_fallback_provider())["provider"]["allow_fallbacks"] is True


def test_the_fallback_pins_the_same_reasoning_effort_as_the_primary() -> None:
    """The point of this host is to be a SUBSTITUTE, not a second product.

    A fallback that reasoned differently would answer differently from the
    primary it stands in for, so it sends the primary's one effort whatever the
    request says.
    """
    body = _body(_fallback_provider(), thinking_disabled=True)
    assert body["reasoning"] == {"effort": EXECUTOR_REASONING_EFFORT}
    assert "enabled" not in body["reasoning"]


def test_an_empty_exclusion_list_sends_no_ignore_field() -> None:
    """`ignore: []` is not "exclude nothing" to every router; omit it instead."""
    body = _body(_fallback_provider(ignore_providers=()))
    assert "ignore" not in body["provider"]


def test_the_fallback_never_sends_parameters_the_model_refuses() -> None:
    """Under `require_parameters`, an unsupported field filters the pool to empty.

    gpt-6-luna refuses `temperature` and `stop` on every endpoint, so sending
    either would leave OpenRouter no endpoint at all — a fallback that fails
    precisely when it is needed.
    """
    body = _body(_fallback_provider(), temperature=0.2, stop=["x"])
    assert "temperature" not in body
    assert "stop" not in body


def test_a_requested_effort_does_not_open_a_second_posture() -> None:
    """The primary ignores a per-request effort; the fallback must too."""
    body = _body(
        _fallback_provider(), thinking_disabled=False, reasoning_effort="high"
    )
    assert body["reasoning"] == {"effort": EXECUTOR_REASONING_EFFORT}


def test_the_ordinary_openrouter_adapter_still_sends_no_routing_block() -> None:
    """The exception must not leak into the sibling that compaction runs on.

    ``test_provider_openrouter.py::test_no_routing_or_affinity_field_is_ever_sent``
    is the primary guard; this one states the relationship from the other side, so
    a future edit that moves the routing block UP into the parent (the obvious
    "why is this duplicated" refactor) fails here as well as there.
    """
    parent = OpenRouterProvider(
        httpx.AsyncClient(transport=httpx.MockTransport(_never_called)),
        api_key=SecretStr("sk-or-v1-test"),
    )
    body = parent._build_body(  # noqa: SLF001
        ProviderChatRequest(model="anything", messages=[{"role": "user", "content": "hi"}]),
        stream=False,
    )
    assert "provider" not in body


def test_the_routing_block_is_rebuilt_per_request() -> None:
    """A shared mutable handed to ``json=`` on every call is one stray edit from
    being permanently wrong, and nothing downstream would notice."""
    provider = _fallback_provider()
    first = _body(provider)
    first["provider"]["sort"] = "price"
    assert _body(provider)["provider"]["sort"] == "latency"


# ---------------------------------------------------------------------------
# Test doubles for the behaviour half.
# ---------------------------------------------------------------------------


class _Stream(ProviderStream):
    """A stream that can fail at the prefetch, mid-iteration, or not at all."""

    def __init__(
        self,
        chunks: list[dict[str, Any]] | None = None,
        *,
        prefetch_error: Exception | None = None,
        iter_error: Exception | None = None,
        endpoint: str | None = None,
        hang_on_close: bool = False,
    ) -> None:
        self._chunks = list(chunks or [])
        self._prefetch_error = prefetch_error
        self._hang_on_close = hang_on_close
        self._iter_error = iter_error
        self.usage: ProviderUsage | None = None
        self.endpoint = endpoint
        self.closed = False

    async def prefetch_first_event(self) -> dict[str, Any] | None:
        if self._prefetch_error is not None:
            raise self._prefetch_error
        return self._chunks.pop(0) if self._chunks else None

    def __aiter__(self) -> _Stream:
        return self

    async def __anext__(self) -> dict[str, Any]:
        if self._iter_error is not None:
            raise self._iter_error
        if not self._chunks:
            raise StopAsyncIteration
        return self._chunks.pop(0)

    async def aclose(self) -> None:
        if self._hang_on_close:
            # A close that never returns, which is what a dead socket produces.
            await asyncio.sleep(3600)
        self.closed = True


class _Host:
    """A scriptable :class:`ChatProvider` that records what it was asked for."""

    def __init__(self, *, name: str) -> None:
        self.name = name
        self.requests: list[ProviderChatRequest] = []
        self.stream_requests: list[ProviderChatRequest] = []
        self.error: Exception | None = None
        self.stream_error: Exception | None = None
        self.stream: _Stream | None = None
        self.closed = False

    def _result(self, request: ProviderChatRequest) -> ProviderChatResult:
        return ProviderChatResult(
            id=f"{self.name}-1",
            created=0,
            model=request.model,
            choices=[{"message": {"role": "assistant", "content": self.name}}],
            usage=ProviderUsage(),
            endpoint=self.name,
        )

    async def complete(self, request: ProviderChatRequest) -> ProviderChatResult:
        self.requests.append(request)
        if self.error is not None:
            raise self.error
        return self._result(request)

    async def structured_tool_call(
        self,
        request: ProviderChatRequest,
        *,
        function_name: str,
        function_schema: dict[str, Any],
    ) -> ProviderToolCallResult:
        self.requests.append(request)
        if self.error is not None:
            raise self.error
        return ProviderToolCallResult(
            name=function_name, arguments_json="{}", usage=ProviderUsage(), endpoint=self.name
        )

    async def open_stream(self, request: ProviderChatRequest) -> ProviderStream:
        self.stream_requests.append(request)
        if self.stream_error is not None:
            raise self.stream_error
        self.stream = self.stream or _Stream([{"choices": [{"delta": {"content": self.name}}]}])
        return self.stream

    async def verify_key(self, api_key: SecretStr | None = None) -> dict[str, Any]:
        return {"host": self.name}

    async def aclose(self) -> None:
        self.closed = True


class _Clock:
    """A monotonic source that only moves when a test says so."""

    def __init__(self) -> None:
        self.now = 1_000.0

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


def _pair(clock: _Clock | None = None) -> tuple[_Host, _Host, FailoverProvider]:
    primary, fallback = _Host(name="primary"), _Host(name="fallback")
    health = FailoverHealth(clock=clock) if clock is not None else FailoverHealth()
    return (
        primary,
        fallback,
        FailoverProvider(
            primary,
            fallback,
            fallback_model=EXECUTOR_FALLBACK_MODEL,
            primary_name="openai",
            fallback_name="openrouter",
            health=health,
        ),
    )


def _request() -> ProviderChatRequest:
    return ProviderChatRequest(
        model=EXECUTOR_MODEL, messages=[{"role": "user", "content": "hi"}]
    )


# ---------------------------------------------------------------------------
# Which failures are worth a second opinion.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_a_healthy_primary_never_reaches_the_fallback() -> None:
    primary, fallback, provider = _pair()
    result = await provider.complete(_request())
    assert result.text == "primary"
    assert fallback.requests == []


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "error",
    [
        upstream_unavailable(),
        upstream_timeout(),
        upstream_rate_limited(),
    ],
    ids=["5xx", "timeout", "rate-limited"],
)
async def test_a_host_fault_is_answered_by_the_other_host(error: Exception) -> None:
    primary, fallback, provider = _pair()
    primary.error = error
    result = await provider.complete(_request())
    assert result.text == "fallback"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "error",
    [upstream_request_rejected("bad body"), upstream_context_window_exceeded()],
    ids=["rejected", "too-long"],
)
async def test_a_request_fault_is_not_retried_on_a_second_host(error: Exception) -> None:
    """Both hosts serve the same weights at the same window, so a refusal about
    the REQUEST is the same refusal there — and sending it again buys a second
    bill for an answer we already have."""
    primary, fallback, provider = _pair()
    primary.error = error
    with pytest.raises(DaintreeError) as caught:
        await provider.complete(_request())
    assert caught.value.code == error.code
    assert fallback.requests == []


@pytest.mark.asyncio
async def test_a_request_fault_does_not_count_against_the_primary_s_health() -> None:
    """The host ANSWERED — it refused us. That is a healthy host giving a correct
    verdict, and counting it toward the breaker would open it on a run of our own
    bad requests, moving traffic to a second host that would refuse them too."""
    primary, fallback, provider = _pair()
    primary.error = upstream_request_rejected("bad body")
    for _ in range(EXECUTOR_FALLBACK_FAILURE_THRESHOLD + 2):
        with pytest.raises(DaintreeError) as caught:
            await provider.complete(_request())
        assert caught.value.code == "upstream_request_rejected"
    assert provider.health.is_open is False
    assert provider.health.consecutive_failures == 0


# ---------------------------------------------------------------------------
# What the fallback is actually sent.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_the_fallback_is_addressed_by_its_own_id_for_these_weights() -> None:
    primary, fallback, provider = _pair()
    primary.error = upstream_unavailable()
    await provider.complete(_request())
    assert fallback.requests[0].model == EXECUTOR_FALLBACK_MODEL


@pytest.mark.asyncio
async def test_the_caller_s_request_is_never_mutated() -> None:
    """A rewritten model id on the caller's own object would outlive the failover:
    the retry layer, the bare-intent re-roll and both response paths all re-read
    the same request, so one failed turn would pin the rest of the session to the
    second host."""
    primary, fallback, provider = _pair()
    primary.error = upstream_unavailable()
    request = _request()
    await provider.complete(request)
    assert request.model == EXECUTOR_MODEL


@pytest.mark.asyncio
async def test_a_forced_tool_call_fails_over_too() -> None:
    primary, fallback, provider = _pair()
    primary.error = upstream_unavailable()
    result = await provider.structured_tool_call(
        _request(), function_name="pick", function_schema={"type": "object"}
    )
    assert result.endpoint == "fallback"
    assert fallback.requests[0].model == EXECUTOR_FALLBACK_MODEL


@pytest.mark.asyncio
async def test_the_key_probe_asks_the_primary_only() -> None:
    """Readiness and /doctor ask whether THIS deployment can serve. A working
    fallback credential must not be able to make a dead primary key look fine."""
    primary, fallback, provider = _pair()
    assert await provider.verify_key() == {"host": "primary"}


@pytest.mark.asyncio
async def test_closing_closes_both_hosts() -> None:
    primary, fallback, provider = _pair()
    await provider.aclose()
    assert primary.closed and fallback.closed


# ---------------------------------------------------------------------------
# Streaming: failover reaches exactly as far as the first event, and no further.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_a_failed_open_falls_over_to_the_other_host() -> None:
    primary, fallback, provider = _pair()
    primary.stream_error = upstream_unavailable()
    stream = await provider.open_stream(_request())
    first = await stream.prefetch_first_event()
    assert first is not None
    assert first["choices"][0]["delta"]["content"] == "fallback"


@pytest.mark.asyncio
async def test_a_host_that_sends_headers_then_nothing_still_hands_off() -> None:
    """The outage shape a plain open-level fallback misses entirely.

    A host can accept the connection, answer with headers, and never complete an
    SSE event. That failure surfaces at the PREFETCH, which is a separate call —
    so the returned stream carries the same policy one step further, to the last
    moment before any model output exists.
    """
    primary, fallback, provider = _pair()
    primary.stream = _Stream(prefetch_error=upstream_timeout())
    stream = await provider.open_stream(_request())
    first = await stream.prefetch_first_event()
    assert first is not None
    assert first["choices"][0]["delta"]["content"] == "fallback"
    assert fallback.stream_requests[0].model == EXECUTOR_FALLBACK_MODEL


@pytest.mark.asyncio
async def test_the_abandoned_stream_is_closed_before_the_handoff() -> None:
    """It holds a pooled connection to a host that is already failing, and leaking
    one per turn under an outage is how a connection pool empties."""
    primary, fallback, provider = _pair()
    primary.stream = _Stream(prefetch_error=upstream_timeout())
    stream = await provider.open_stream(_request())
    await stream.prefetch_first_event()
    assert primary.stream.closed is True


@pytest.mark.asyncio
async def test_a_handoff_marks_the_turn_s_cost_incomplete() -> None:
    """The abandoned open may already have been billed a prefill it never got to
    report. The orchestrator folds this into its own count, so the turn reports an
    incomplete cost rather than a confident total that omits a real charge."""
    primary, fallback, provider = _pair()
    primary.stream = _Stream(prefetch_error=upstream_timeout())
    stream = await provider.open_stream(_request())
    await stream.prefetch_first_event()
    assert stream.abandoned_attempts == 1


@pytest.mark.asyncio
async def test_a_clean_stream_abandons_nothing() -> None:
    primary, fallback, provider = _pair()
    stream = await provider.open_stream(_request())
    await stream.prefetch_first_event()
    assert stream.abandoned_attempts == 0


@pytest.mark.asyncio
async def test_a_mid_generation_failure_is_terminal() -> None:
    """Content the user has already seen cannot be replayed on a second host —
    the same rule that stops `retry_upstream` after the first chunk."""
    primary, fallback, provider = _pair()
    primary.stream = _Stream(
        [{"choices": [{"delta": {"content": "half a "}}]}],
        iter_error=upstream_unavailable(),
    )
    stream = await provider.open_stream(_request())
    await stream.prefetch_first_event()
    with pytest.raises(DaintreeError) as caught:
        await stream.__anext__()
    assert caught.value.code == "upstream_unavailable"
    assert fallback.stream_requests == []


@pytest.mark.asyncio
async def test_usage_and_endpoint_follow_the_host_that_actually_answered() -> None:
    """Both are read off the stream AFTER the fact — usage for the turn's
    accounting, endpoint to name the host in a slow-turn log — so a copy taken at
    construction would attribute the fallback's tokens to the primary."""
    primary, fallback, provider = _pair()
    primary.stream = _Stream(prefetch_error=upstream_timeout())
    fallback.stream = _Stream([{"choices": [{"delta": {}}]}], endpoint="Parasail")
    fallback.stream.usage = ProviderUsage(prompt_tokens=11, cost=0.002)
    stream = await provider.open_stream(_request())
    await stream.prefetch_first_event()
    assert stream.endpoint == "Parasail"
    assert stream.usage is not None and stream.usage.cost == 0.002


# ---------------------------------------------------------------------------
# The breaker.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_one_failure_does_not_move_the_next_turn_off_the_primary() -> None:
    """The "do not overreact" half. A single 500 is noise: the turn that hit it was
    already answered by the fallback, so nothing is bought by moving every
    subsequent turn to a second host over an event that has already passed."""
    primary, fallback, provider = _pair()
    primary.error = upstream_unavailable()
    await provider.complete(_request())
    primary.error = None
    result = await provider.complete(_request())
    assert result.text == "primary"


@pytest.mark.asyncio
async def test_a_success_resets_the_run_of_failures() -> None:
    primary, fallback, provider = _pair()
    for _ in range(EXECUTOR_FALLBACK_FAILURE_THRESHOLD - 1):
        primary.error = upstream_unavailable()
        await provider.complete(_request())
    primary.error = None
    await provider.complete(_request())
    assert provider.health.consecutive_failures == 0

    # The run has to start over: one more failure must not be the trip.
    primary.error = upstream_unavailable()
    await provider.complete(_request())
    assert provider.health.is_open is False


@pytest.mark.asyncio
async def test_a_run_of_failures_stops_spending_turns_on_the_dead_host() -> None:
    """What tripping actually buys. While the breaker is closed every turn pays
    the primary's failure first, and during an outage that failure is usually a
    TIMEOUT — so an open breaker is what stops a ten-minute incident from adding
    the connect-or-stall budget to the front of every single turn in it."""
    clock = _Clock()
    primary, fallback, provider = _pair(clock)
    primary.error = upstream_unavailable()
    for _ in range(EXECUTOR_FALLBACK_FAILURE_THRESHOLD):
        await provider.complete(_request())
    assert provider.health.is_open is True

    calls_before = len(primary.requests)
    result = await provider.complete(_request())
    assert result.text == "fallback"
    assert len(primary.requests) == calls_before, "the dead host was called anyway"


@pytest.mark.asyncio
async def test_the_primary_is_tested_again_after_the_probe_interval() -> None:
    """The "retry every few minutes" half: a 10-to-15-minute incident is re-probed
    three to five times, so recovery is noticed within minutes of it happening."""
    clock = _Clock()
    primary, fallback, provider = _pair(clock)
    primary.error = upstream_unavailable()
    for _ in range(EXECUTOR_FALLBACK_FAILURE_THRESHOLD):
        await provider.complete(_request())

    clock.advance(EXECUTOR_FALLBACK_PROBE_INTERVAL_SECONDS - 1)
    calls_before = len(primary.requests)
    await provider.complete(_request())
    assert len(primary.requests) == calls_before, "probed early"

    clock.advance(2)
    primary.error = None
    result = await provider.complete(_request())
    assert result.text == "primary"
    assert provider.health.is_open is False, "a successful probe must close it"


@pytest.mark.asyncio
async def test_exactly_one_turn_pays_for_each_probe() -> None:
    """The cost of being wrong about recovery is ONE turn, not all of them.

    A second concurrent caller reaching a half-open breaker would double that cost
    for no extra information, so it goes straight to the fallback and waits for
    nothing.
    """
    clock = _Clock()
    primary, fallback, provider = _pair(clock)
    primary.error = upstream_unavailable()
    for _ in range(EXECUTOR_FALLBACK_FAILURE_THRESHOLD):
        await provider.complete(_request())
    clock.advance(EXECUTOR_FALLBACK_PROBE_INTERVAL_SECONDS + 1)

    assert provider.health.before_call() is PrimaryDecision.PROBE
    assert provider.health.before_call() is PrimaryDecision.SKIP


@pytest.mark.asyncio
async def test_a_failed_probe_re_arms_a_full_interval() -> None:
    """Otherwise the breaker is immediately probe-eligible again and every
    subsequent turn becomes a probe — which is the closed behaviour it just
    decided to stop paying for."""
    clock = _Clock()
    primary, fallback, provider = _pair(clock)
    primary.error = upstream_unavailable()
    for _ in range(EXECUTOR_FALLBACK_FAILURE_THRESHOLD):
        await provider.complete(_request())

    clock.advance(EXECUTOR_FALLBACK_PROBE_INTERVAL_SECONDS + 1)
    await provider.complete(_request())  # the probe, and it fails
    calls_before = len(primary.requests)

    await provider.complete(_request())
    assert len(primary.requests) == calls_before
    assert provider.health.seconds_until_probe() == pytest.approx(
        EXECUTOR_FALLBACK_PROBE_INTERVAL_SECONDS
    )


@pytest.mark.asyncio
async def test_a_reserved_probe_is_returned_when_a_call_is_cancelled() -> None:
    """A leaked reservation stops the breaker probing FOR GOOD — the one failure
    mode of this design that never recovers on its own."""
    clock = _Clock()
    primary, fallback, provider = _pair(clock)
    primary.error = upstream_unavailable()
    for _ in range(EXECUTOR_FALLBACK_FAILURE_THRESHOLD):
        await provider.complete(_request())
    clock.advance(EXECUTOR_FALLBACK_PROBE_INTERVAL_SECONDS + 1)

    primary.error = asyncio.CancelledError()
    with pytest.raises(asyncio.CancelledError):
        await provider.complete(_request())

    # Still probe-eligible: a cancellation is not evidence either way, so the slot
    # comes back rather than the breaker learning something it did not observe.
    assert provider.health.before_call() is PrimaryDecision.PROBE


@pytest.mark.asyncio
async def test_a_stalled_prefetch_counts_against_the_primary() -> None:
    """THE SILENT STALL. The orchestrator bounds open-plus-prefetch with a wall
    clock, so a host that sends headers and then goes quiet arrives as a
    CANCELLATION rather than as an error with a status code. Not counting it would
    leave the one outage mode that produces no status code invisible to the whole
    mechanism.
    """
    primary, fallback, provider = _pair()
    primary.stream = _Stream(prefetch_error=asyncio.CancelledError())
    stream = await provider.open_stream(_request())
    with pytest.raises(asyncio.CancelledError):
        await stream.prefetch_first_event()
    assert provider.health.consecutive_failures == 1


def test_a_closed_breaker_reports_no_probe_countdown() -> None:
    assert FailoverHealth().seconds_until_probe() is None


def test_the_shipped_thresholds_are_the_ones_documented() -> None:
    """Both numbers are load-bearing prose in CLAUDE.md and in the module
    docstring, and both are the kind of constant that gets "tuned" in a hurry
    during an incident and never put back."""
    assert EXECUTOR_FALLBACK_FAILURE_THRESHOLD == 3
    assert EXECUTOR_FALLBACK_PROBE_INTERVAL_SECONDS == 180.0


@pytest.mark.asyncio
async def test_recovery_is_announced_rather_than_left_to_silence() -> None:
    """The one transition with no natural log line of its own.

    A failure logs `failover_engaged`; an open breaker logs
    `failover_primary_skipped` on every call; and then, on recovery, the noise
    simply stops. "It went quiet" is indistinguishable from traffic stopping, so
    the line an operator is actually waiting for during an incident has to be
    emitted deliberately.

    Captured through `structlog.testing` rather than `caplog`: once
    `configure_logging` has run — which it has, in any session that touched the
    app — the logger factory prints to stdout and never reaches stdlib logging at
    all, so a `caplog` assertion here passes or fails on TEST ORDER rather than on
    behaviour.
    """
    clock = _Clock()
    primary, fallback, provider = _pair(clock)
    primary.error = upstream_unavailable()
    for _ in range(EXECUTOR_FALLBACK_FAILURE_THRESHOLD):
        await provider.complete(_request())
    clock.advance(EXECUTOR_FALLBACK_PROBE_INTERVAL_SECONDS + 1)
    primary.error = None

    with capture_logs() as logs:
        await provider.complete(_request())
    assert [entry["event"] for entry in logs] == ["failover_recovered"]


def test_an_ordinary_success_announces_nothing() -> None:
    """`record_success` reports recovery, not health. A line on every good turn
    would make the recovery line worthless."""
    assert FailoverHealth().record_success(PrimaryDecision.USE) is False


@pytest.mark.asyncio
async def test_a_skipped_primary_is_not_logged_as_a_warning() -> None:
    """It happens on EVERY call for as long as the outage lasts. At warning it
    would bury the one line that says why under minutes of lines saying nothing
    new — while `failover_engaged`, which IS a surprise, stays at warning."""
    clock = _Clock()
    primary, fallback, provider = _pair(clock)
    primary.error = upstream_unavailable()
    with capture_logs() as logs:
        for _ in range(EXECUTOR_FALLBACK_FAILURE_THRESHOLD):
            await provider.complete(_request())
        engaged = [entry for entry in logs if entry["event"] == "failover_engaged"]
        assert len(engaged) == EXECUTOR_FALLBACK_FAILURE_THRESHOLD
        assert all(entry["log_level"] == "warning" for entry in engaged)

        logs.clear()
        await provider.complete(_request())
    assert [entry["log_level"] for entry in logs] == ["info"]
    assert [entry["event"] for entry in logs] == ["failover_primary_skipped"]


@pytest.mark.asyncio
async def test_an_open_breaker_streams_straight_from_the_fallback() -> None:
    """The streaming path needs its own skip. It is the path that carries real
    traffic, and a breaker that only shortcut `complete` would leave every
    interactive turn in an outage still paying the dead host's timeout."""
    clock = _Clock()
    primary, fallback, provider = _pair(clock)
    primary.error = upstream_unavailable()
    for _ in range(EXECUTOR_FALLBACK_FAILURE_THRESHOLD):
        await provider.complete(_request())

    stream = await provider.open_stream(_request())
    first = await stream.prefetch_first_event()
    assert first is not None
    assert first["choices"][0]["delta"]["content"] == "fallback"
    assert primary.stream_requests == []


@pytest.mark.asyncio
async def test_a_request_fault_on_the_open_is_not_retried_on_a_second_host() -> None:
    primary, fallback, provider = _pair()
    primary.stream_error = upstream_request_rejected("bad tool schema")
    with pytest.raises(DaintreeError) as caught:
        await provider.open_stream(_request())
    assert caught.value.code == "upstream_request_rejected"
    assert fallback.stream_requests == []
    assert provider.health.consecutive_failures == 0


@pytest.mark.asyncio
async def test_a_request_fault_at_the_prefetch_is_not_handed_off() -> None:
    primary, fallback, provider = _pair()
    primary.stream = _Stream(prefetch_error=upstream_context_window_exceeded())
    stream = await provider.open_stream(_request())
    with pytest.raises(DaintreeError) as caught:
        await stream.prefetch_first_event()
    assert caught.value.code == "context_window_exceeded"
    assert fallback.stream_requests == []
    assert stream.abandoned_attempts == 0


@pytest.mark.asyncio
async def test_a_cancelled_open_returns_the_probe_slot() -> None:
    clock = _Clock()
    primary, fallback, provider = _pair(clock)
    primary.error = upstream_unavailable()
    for _ in range(EXECUTOR_FALLBACK_FAILURE_THRESHOLD):
        await provider.complete(_request())
    clock.advance(EXECUTOR_FALLBACK_PROBE_INTERVAL_SECONDS + 1)

    primary.stream_error = asyncio.CancelledError()
    with pytest.raises(asyncio.CancelledError):
        await provider.open_stream(_request())
    assert provider.health.before_call() is PrimaryDecision.PROBE


@pytest.mark.asyncio
async def test_the_wrapper_iterates_and_closes_the_stream_it_is_holding() -> None:
    primary, fallback, provider = _pair()
    primary.stream = _Stream([{"choices": [{"delta": {"content": "a"}}]}, {"x": 1}])
    stream = await provider.open_stream(_request())
    await stream.prefetch_first_event()
    assert [chunk async for chunk in stream] == [{"x": 1}]
    await stream.aclose()
    assert primary.stream.closed is True


@pytest.mark.asyncio
async def test_usage_and_endpoint_are_writable_through_the_wrapper() -> None:
    """The base class declares both as plain mutable attributes, so the wrapper
    has to stay assignable — a read-only proxy would break the first caller that
    treats a stream the way its own type says it may."""
    primary, fallback, provider = _pair()
    stream = await provider.open_stream(_request())
    stream.usage = ProviderUsage(prompt_tokens=7)
    stream.endpoint = "Together"
    assert primary.stream is not None
    assert primary.stream.endpoint == "Together"
    assert primary.stream.usage is not None and primary.stream.usage.prompt_tokens == 7


@pytest.mark.asyncio
async def test_the_breaker_counts_how_often_it_has_given_up_on_the_primary() -> None:
    """A per-process tally, reported by /doctor. One open is an incident; a
    breaker that has opened nine times today is a host that should be replaced
    rather than tolerated, and nothing else in the record says so."""
    clock = _Clock()
    primary, fallback, provider = _pair(clock)
    assert provider.health.opened_count == 0
    primary.error = upstream_unavailable()
    for _ in range(EXECUTOR_FALLBACK_FAILURE_THRESHOLD):
        await provider.complete(_request())
    assert provider.health.opened_count == 1


# ---------------------------------------------------------------------------
# The failures that must NOT be laundered onto a second host.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_a_403_is_not_resent_to_a_different_host() -> None:
    """`provider_key_forbidden` is AMBIGUOUS, and only one reading is safe.

    It covers model permissions and spend limits — and it also covers a host's
    own moderation refusal, with nothing in the status or the body separating
    them. Failing over would therefore sometimes take a prompt one host declined
    to serve and send it, whole, to a different one. The price of the safe
    reading is that a genuine permissions 403 does not fail over: deterministic,
    operator-fixable, and very likely refused by the second host too.
    """
    primary, fallback, provider = _pair()
    primary.error = provider_key_forbidden()
    with pytest.raises(DaintreeError) as caught:
        await provider.complete(_request())
    assert caught.value.code == "provider_key_forbidden"
    assert fallback.requests == []


@pytest.mark.asyncio
async def test_a_caller_pinned_credential_never_reaches_the_other_host() -> None:
    """A per-call key is scoped to the host it was issued for.

    The adapter prefers a request's own key over its configured default, so
    carrying one across would take a caller's OpenAI credential and send it as a
    Bearer token to OpenRouter — disclosing a secret to a third party, on the one
    path nobody is watching because it only runs during an outage. Nothing on the
    serving path pins a key today; the field is kept live for the
    account-mapped credential that would make this worst.
    """
    primary, fallback, provider = _pair()
    primary.error = upstream_unavailable()
    await provider.complete(
        ProviderChatRequest(
            model=EXECUTOR_MODEL,
            messages=[{"role": "user", "content": "hi"}],
            api_key=SecretStr("an-openai-scoped-key"),
        )
    )
    assert fallback.requests[0].api_key is None


# ---------------------------------------------------------------------------
# The silent stall — the outage shape that produces no status code.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_a_stall_puts_the_callers_own_retry_onto_the_fallback() -> None:
    """The defect this holdoff exists for, stated as the caller sees it.

    A stalled prefetch reaches us as a CANCELLATION from the orchestrator's wall
    clock, which converts it to a retryable timeout and retries immediately.
    Counting the failure is not enough: the breaker is still two failures from
    opening, so the retry re-enters the same silent host and the turn spends its
    whole budget stalling twice while a healthy second host is never called —
    the exact outage this feature exists for, arriving in the one form it could
    not see.
    """
    clock = _Clock()
    primary, fallback, provider = _pair(clock)
    primary.stream = _Stream(prefetch_error=asyncio.CancelledError())
    stream = await provider.open_stream(_request())
    with pytest.raises(asyncio.CancelledError):
        await stream.prefetch_first_event()

    # The caller's retry, from a breaker that is nowhere near open.
    assert provider.health.is_open is False
    retried = await provider.open_stream(_request())
    first = await retried.prefetch_first_event()
    assert first is not None
    assert first["choices"][0]["delta"]["content"] == "fallback"


@pytest.mark.asyncio
async def test_the_stall_holdoff_expires_rather_than_latching() -> None:
    """A client hanging up mid-open is cancelled the same way and cannot be told
    apart from here. That misreading has to cost half a minute, not an hour."""
    clock = _Clock()
    primary, fallback, provider = _pair(clock)
    primary.stream = _Stream(prefetch_error=asyncio.CancelledError())
    stream = await provider.open_stream(_request())
    with pytest.raises(asyncio.CancelledError):
        await stream.prefetch_first_event()

    clock.advance(EXECUTOR_FALLBACK_STALL_HOLDOFF_SECONDS + 1)
    primary.stream = None
    await provider.open_stream(_request())
    assert primary.stream_requests, "the holdoff never expired"


@pytest.mark.asyncio
async def test_a_primary_success_clears_a_stall_holdoff_outright() -> None:
    """Evidence beats a timer: a host that just answered is reachable."""
    clock = _Clock()
    primary, fallback, provider = _pair(clock)
    provider.health.note_stall()
    assert provider.health.before_call() is PrimaryDecision.SKIP
    provider.health.record_success(PrimaryDecision.USE)
    assert provider.health.before_call() is PrimaryDecision.USE


@pytest.mark.asyncio
async def test_a_bug_in_this_process_is_not_counted_against_the_host() -> None:
    """An `AttributeError` here would fail identically against the fallback.
    Counting our own defects would open the breaker on a code path the second
    host cannot rescue."""
    primary, fallback, provider = _pair()
    primary.stream = _Stream(prefetch_error=AttributeError("a bug"))
    stream = await provider.open_stream(_request())
    with pytest.raises(AttributeError):
        await stream.prefetch_first_event()
    assert provider.health.consecutive_failures == 0
    assert provider.health.seconds_until_probe() is None


# ---------------------------------------------------------------------------
# Late failures, and the one-shot lifecycle.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_a_host_that_dies_after_one_token_still_counts_against_the_breaker() -> None:
    """Otherwise it is invisible to the whole mechanism.

    Every such turn reaches `record_success` on its first frame and resets the
    counter, so traffic keeps choosing a host that has not completed a generation
    in hours. The turn itself stays terminal — replaying seen content is the one
    thing failover must never do — but that is a fact about the turn, not the host.
    """
    primary, fallback, provider = _pair()
    for _ in range(EXECUTOR_FALLBACK_FAILURE_THRESHOLD):
        primary.stream = _Stream(
            [{"choices": [{"delta": {"content": "a"}}]}],
            iter_error=upstream_unavailable(),
        )
        stream = await provider.open_stream(_request())
        await stream.prefetch_first_event()
        with pytest.raises(DaintreeError):
            await stream.__anext__()
    assert provider.health.is_open is True


@pytest.mark.asyncio
async def test_only_a_completed_stream_closes_a_half_open_breaker() -> None:
    """A probe's first frame is not its verdict.

    If it were, a host that answers with a role-only event and then goes quiet
    would close the breaker on every probe and never reopen it — the mechanism
    reporting recovery from the one symptom that is not recovery.
    """
    clock = _Clock()
    primary, fallback, provider = _pair(clock)
    primary.error = upstream_unavailable()
    for _ in range(EXECUTOR_FALLBACK_FAILURE_THRESHOLD):
        await provider.complete(_request())
    clock.advance(EXECUTOR_FALLBACK_PROBE_INTERVAL_SECONDS + 1)

    primary.error = None
    primary.stream = _Stream([{"choices": [{"delta": {"role": "assistant"}}]}])
    stream = await provider.open_stream(_request())
    await stream.prefetch_first_event()
    assert provider.health.is_open is True, "a first frame closed the breaker"

    async for _ in stream:
        pass
    assert provider.health.is_open is False


@pytest.mark.asyncio
async def test_an_abandoned_stream_is_evidence_of_nothing() -> None:
    """Nobody waited to find out how it went. A client that disconnects
    mid-reply must neither exonerate the primary nor convict it."""
    primary, fallback, provider = _pair()
    provider.health.record_failure(PrimaryDecision.USE)
    primary.stream = _Stream([{"choices": [{"delta": {"content": "a"}}]}])
    stream = await provider.open_stream(_request())
    await stream.prefetch_first_event()
    await stream.aclose()
    assert provider.health.consecutive_failures == 1


@pytest.mark.asyncio
async def test_a_completed_fallback_stream_says_nothing_about_the_primary() -> None:
    """The fallback finishing is evidence about the FALLBACK.

    While the breaker is open the primary is not called at all, so a clean
    termination on the second host is a generation the first one had no part in.
    Crediting it would close the breaker on the strength of a turn that never
    touched the failing host — and every subsequent turn would then re-enter the
    outage to discover it is still there, which is the cost the breaker exists to
    stop paying.
    """
    clock = _Clock()
    primary, fallback, provider = _pair(clock)
    primary.error = upstream_unavailable()
    for _ in range(EXECUTOR_FALLBACK_FAILURE_THRESHOLD):
        await provider.complete(_request())
    assert provider.health.is_open is True

    stream = await provider.open_stream(_request())
    assert primary.stream_requests == [], "the breaker is open; the primary must be skipped"
    async for _ in stream:
        pass

    assert provider.health.is_open is True
    assert provider.health.consecutive_failures == EXECUTOR_FALLBACK_FAILURE_THRESHOLD


@pytest.mark.asyncio
async def test_a_late_request_fault_is_not_held_against_the_primary() -> None:
    """A bad body fails identically on both hosts.

    Mid-generation breaks are the one place a request fault can still surface, and
    counting one would open the breaker over a defect in what this server sent —
    moving every turn onto a second host that is about to reject the same bytes.
    The turn stays terminal, as any post-first-frame failure does; only the
    VERDICT about the host is withheld.
    """
    primary, fallback, provider = _pair()
    for _ in range(EXECUTOR_FALLBACK_FAILURE_THRESHOLD):
        primary.stream = _Stream(
            [{"choices": [{"delta": {"content": "a"}}]}],
            iter_error=upstream_request_rejected("bad body"),
        )
        stream = await provider.open_stream(_request())
        await stream.prefetch_first_event()
        with pytest.raises(DaintreeError) as caught:
            await stream.__anext__()
        assert caught.value.code == "upstream_request_rejected"

    assert fallback.stream_requests == [], "a request fault must not be replayed elsewhere"
    assert provider.health.consecutive_failures == 0
    assert provider.health.is_open is False


@pytest.mark.asyncio
async def test_a_second_prefetch_is_not_a_second_chance_at_failover() -> None:
    """By then the first event has been forwarded, so handing off would replay a
    turn whose content the user has already seen."""
    primary, fallback, provider = _pair()
    primary.stream = _Stream(
        [{"choices": [{"delta": {"content": "a"}}]}],
        prefetch_error=None,
    )
    stream = await provider.open_stream(_request())
    await stream.prefetch_first_event()
    primary.stream._prefetch_error = upstream_unavailable()  # noqa: SLF001
    with pytest.raises(DaintreeError):
        await stream.prefetch_first_event()
    assert fallback.stream_requests == []


@pytest.mark.asyncio
async def test_a_stale_primary_that_will_not_close_does_not_eat_the_handoff() -> None:
    """The close talks to the host that just failed, and the fallback's open is
    queued behind it — so an unbounded close spends the caller's whole open
    deadline and the second host is never reached at all."""
    primary, fallback, provider = _pair()
    primary.stream = _Stream(prefetch_error=upstream_timeout(), hang_on_close=True)
    stream = await provider.open_stream(_request())
    async with asyncio.timeout(5):
        first = await stream.prefetch_first_event()
    assert first is not None
    assert first["choices"][0]["delta"]["content"] == "fallback"


# ---------------------------------------------------------------------------
# The probe permit: one reservation, one outcome, always returned.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_a_stream_closed_before_prefetching_returns_its_probe() -> None:
    """The orchestrator disposes of an open nobody consumed, so this really
    happens — and a probe abandoned that way would hold its reservation forever,
    leaving every later request skipping a primary that recovered hours ago. The
    one failure mode of this design that never heals on its own."""
    clock = _Clock()
    primary, fallback, provider = _pair(clock)
    primary.error = upstream_unavailable()
    for _ in range(EXECUTOR_FALLBACK_FAILURE_THRESHOLD):
        await provider.complete(_request())
    clock.advance(EXECUTOR_FALLBACK_PROBE_INTERVAL_SECONDS + 1)

    primary.error = None
    stream = await provider.open_stream(_request())
    await stream.aclose()
    assert provider.health.before_call() is PrimaryDecision.PROBE


def test_a_stale_outcome_cannot_release_someone_elses_probe() -> None:
    """A `USE` call already in flight when the breaker opened settles LATER than
    the probe reserved after it. Clearing the flag unconditionally would hand
    back a reservation it does not own, admitting a second concurrent probe
    against a host we just decided to stop spending turns on."""
    clock = _Clock()
    health = FailoverHealth(clock=clock)
    stale = health.before_call()  # USE, taken while closed
    for _ in range(EXECUTOR_FALLBACK_FAILURE_THRESHOLD):
        health.record_failure(PrimaryDecision.USE)
    clock.advance(EXECUTOR_FALLBACK_PROBE_INTERVAL_SECONDS + 1)
    assert health.before_call() is PrimaryDecision.PROBE

    health.record_failure(stale)
    assert health.before_call() is PrimaryDecision.SKIP, "the stale call freed the probe"


def test_a_stale_failure_does_not_push_the_next_probe_further_out() -> None:
    """Nor report one incident as several. A slow call that started before the
    breaker opened must not re-arm the interval on arrival."""
    clock = _Clock()
    health = FailoverHealth(clock=clock)
    for _ in range(EXECUTOR_FALLBACK_FAILURE_THRESHOLD):
        health.record_failure(PrimaryDecision.USE)
    assert health.opened_count == 1

    clock.advance(EXECUTOR_FALLBACK_PROBE_INTERVAL_SECONDS - 1)
    health.record_failure(PrimaryDecision.USE)
    assert health.opened_count == 1
    assert health.seconds_until_probe() == pytest.approx(1.0)


# ---------------------------------------------------------------------------
# Cost: a failed-over turn does not report a confident total.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_a_failed_over_turn_reports_an_incomplete_cost() -> None:
    """The abandoned primary attempt may already have been billed a prefill it
    never reported. The fallback's own number is confident and SHORT, which is
    the one report the accounting chain exists to prevent."""
    primary, fallback, provider = _pair()
    primary.error = upstream_unavailable()
    result = await provider.complete(_request())
    assert result.usage.cost_complete is False


@pytest.mark.asyncio
async def test_a_forced_tool_call_that_failed_over_reports_the_same() -> None:
    primary, fallback, provider = _pair()
    primary.error = upstream_unavailable()
    result = await provider.structured_tool_call(
        _request(), function_name="pick", function_schema={"type": "object"}
    )
    assert result.usage.cost_complete is False


@pytest.mark.asyncio
async def test_a_skipped_primary_does_not_make_the_cost_unknown() -> None:
    """No primary request was made, so there is nothing unreported. Marking every
    turn of an outage incomplete would make the flag mean nothing."""
    clock = _Clock()
    primary, fallback, provider = _pair(clock)
    primary.error = upstream_unavailable()
    for _ in range(EXECUTOR_FALLBACK_FAILURE_THRESHOLD):
        await provider.complete(_request())
    result = await provider.complete(_request())
    assert result.usage.cost_complete is True


@pytest.mark.asyncio
async def test_an_ordinary_turn_reports_a_complete_cost() -> None:
    primary, fallback, provider = _pair()
    result = await provider.complete(_request())
    assert result.usage.cost_complete is True


@pytest.mark.asyncio
async def test_a_forced_tool_call_on_the_primary_reports_a_complete_cost() -> None:
    """The mirror of the failed-over case, and the reason `cost_complete` means
    anything.

    Nothing was abandoned on this turn, so there is no unreported prefill and the
    number the host gave is the whole of it. A wrapper that marked every tool call
    incomplete — the easy way to make the failover case pass — would flatten the
    flag to a constant and the accounting chain would stop being able to tell a
    partial sum from an exact one on the path where it matters.
    """
    primary, fallback, provider = _pair()
    result = await provider.structured_tool_call(
        _request(), function_name="pick", function_schema={"type": "object"}
    )
    assert result.endpoint == "primary"
    assert fallback.requests == []
    assert result.usage.cost_complete is True


# ---------------------------------------------------------------------------
# Wiring.
# ---------------------------------------------------------------------------


def _settings(**overrides: Any) -> Any:
    from daintree_assistant_server.config import Settings

    base = {
        "STATE_SIGNING_KEY_CURRENT": "k" * 40,
        "STATE_SALT": "s" * 40,
        "OPENAI_API_KEY": "openai-key",
    }
    base.update(overrides)
    return Settings(**base)  # type: ignore[arg-type]


def test_a_second_credential_arms_the_failover() -> None:
    from daintree_assistant_server.main import _build_provider

    provider = _build_provider(_settings(OPENROUTER_API_KEY="sk-or-v1-x"))
    assert isinstance(provider, FailoverProvider)
    assert provider.health.is_open is False


def test_without_a_second_credential_the_deployment_is_exactly_what_it_was() -> None:
    """`OPENROUTER_API_KEY` is ungated at readiness on purpose, so this is a
    CORRECT deployment rather than a broken one — and what it gets must be the
    bare adapter, not a wrapper standing in front of nothing."""
    from daintree_assistant_server.main import _build_provider
    from daintree_assistant_server.providers.openai import OpenAIProvider

    provider = _build_provider(_settings())
    assert isinstance(provider, OpenAIProvider)
