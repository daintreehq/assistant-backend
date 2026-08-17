"""End-to-end: a stalled turn is recovered where it can be, attributed where it
cannot.

The unit tests pin the budget arithmetic; these pin that the arithmetic is
actually WIRED. The line they hold is which stalls are recoverable: a stall in the
stream OPEN is retried, because nothing of the turn is public yet and a replay is
invisible — while a stall mid-answer stays terminal, because visible content has
committed and replaying it would duplicate tokens in front of the user. Either
way the failure that survives is attributable and arrives on the right channel
rather than hanging until the platform kills the request.
"""

from __future__ import annotations

import asyncio

import pytest
from fakes import FakeProvider, FakeStream

from daintree_assistant_server.services.deadlines import BudgetPolicy, RequestBudget


def _tiny_budget() -> RequestBudget:
    return RequestBudget(
        policy=BudgetPolicy(
            preparation=0.15, open_stall=0.15, first_output=0.15, stream_idle=0.15, total=0.2
        )
    )


@pytest.mark.asyncio
async def test_a_stalled_preparation_becomes_an_attributable_error(
    settings, fake_provider: FakeProvider
) -> None:
    """Preparation is before the user has seen anything, so the failure must name
    that phase — "the model was never reached" is a different investigation from
    a slow generation."""
    from daintree_assistant_server.errors import DaintreeError
    from daintree_assistant_server.main import build_container

    container = build_container(settings, fake_provider)

    # Selection is LOCAL now — the provider is never touched during prep, so a
    # stalled PROVIDER cannot stall preparation. The phase's slow component is
    # the selector itself; stall that — and record that the stall was actually
    # entered, or an unrelated slow phase could pass this test.
    entered = asyncio.Event()

    async def _never_returns(*args, **kwargs):  # type: ignore[no-untyped-def]
        entered.set()
        await asyncio.sleep(30)

    container.orchestrator.selector.select = _never_returns  # type: ignore[method-assign]

    from daintree_assistant_server.contracts.daintree_api import RespondRequest

    body = RespondRequest.model_validate(
        {
            "protocol_version": 2,
            "session": {"id": "s", "turn_id": "t"},
            # A substantive request: a bare greeting takes the no-task fast path
            # and never reaches the selector, so it could not exercise this deadline.
            "input": {"messages": [{"role": "user", "content": "rename my terminals"}]},
            "startup": {},
        }
    )
    from pydantic import SecretStr

    with pytest.raises(DaintreeError) as exc:
        await container.orchestrator._prepare(
            body,
            upstream_model="m",
            request_id="req_1",
            api_key=SecretStr("k"),
            budget=_tiny_budget(),
        )
    assert exc.value.code == "turn_deadline_exceeded"
    assert exc.value.status_code == 504
    assert "preparation" in exc.value.message
    assert entered.is_set(), "the stalled selector was never reached"
    await container.aclose()


@pytest.mark.asyncio
async def test_a_stalled_stream_ends_with_a_terminal_error_not_a_fabricated_done(
    settings, fake_provider: FakeProvider, monkeypatch
) -> None:
    """After the HTTP 200 commits, a deadline can only be reported in-band.

    The critical part is what it must NOT do: emit `done`. A truncated answer
    presented as a complete one is worse than an error, because the user acts on
    it.

    The tiny budget is injected into the REAL path by patching the policy the
    orchestrator mints from — an earlier version of this test wrapped the stream
    in its own `asyncio.timeout` and would have passed with every streaming
    deadline deleted.
    """
    from daintree_assistant_server.main import build_container
    from daintree_assistant_server.services import chat_orchestrator as orch
    from daintree_assistant_server.services.deadlines import BudgetPolicy

    tiny = BudgetPolicy(preparation=0.4, open_stall=0.4, first_output=0.4, stream_idle=0.4, total=0.5)
    monkeypatch.setattr(orch, "RequestBudget", _budget_factory(tiny))

    class _StalledStream:
        """Answers, then goes quiet mid-sentence.

        This is the half a wall clock catches and an inactivity timeout does not
        argue with: the connection is open and the transport is healthy, but the
        generation stopped. It must stay TERMINAL — visible content is already
        public, so replaying it would show the user duplicated tokens.
        """

        usage = None
        endpoint = None
        closed = False

        async def prefetch_first_event(self):  # type: ignore[no-untyped-def]
            return {"choices": [{"delta": {"content": "partial"}}]}

        def __aiter__(self):  # type: ignore[no-untyped-def]
            return self

        async def __anext__(self):  # type: ignore[no-untyped-def]
            await asyncio.sleep(30)
            raise StopAsyncIteration

        async def aclose(self) -> None:
            type(self).closed = True

    opens = 0

    async def _open(request):  # type: ignore[no-untyped-def]
        nonlocal opens
        opens += 1
        return _StalledStream()

    fake_provider.open_stream = _open  # type: ignore[method-assign]
    container = build_container(settings, fake_provider)

    from pydantic import SecretStr

    from daintree_assistant_server.contracts.daintree_api import RespondRequest

    body = RespondRequest.model_validate(
        {
            "protocol_version": 2,
            "session": {"id": "s", "turn_id": "t"},
            # A substantive request: a bare greeting takes the no-task fast path
            # and never reaches the selector, so it could not exercise this deadline.
            "input": {"messages": [{"role": "user", "content": "rename my terminals"}]},
            "startup": {},
            "generation": {"stream": True},
        }
    )
    outcome = await container.orchestrator.complete(
        body, upstream_model="m", api_key=SecretStr("k"), request_id="req_2"
    )
    assert outcome.stream is not None

    parts: list[str] = []
    # An outer bound purely as a TEST-failure guard: if the server's own deadline
    # is broken this fails loudly instead of hanging the suite. It is 20x the
    # budget, so it can never be what actually stops the stream.
    async with asyncio.timeout(10.0):
        async for chunk in outcome.stream:
            parts.append(chunk)
    joined = "".join(parts)

    assert "event: meta" in joined
    assert "event: error" in joined, "the server's own deadline never fired"
    assert "turn_deadline_exceeded" in joined
    assert "event: done" not in joined, "a truncated turn was reported as complete"
    assert _StalledStream.closed, "the upstream stream leaked on deadline"
    # A mid-answer stall is NOT retryable: the content is already public, so a
    # replay would duplicate it. One open, one copy of the partial answer.
    assert opens == 1, "a stall after visible content was replayed"
    assert joined.count("partial") == 1, "the partial answer was emitted twice"
    await container.aclose()


@pytest.mark.asyncio
async def test_an_upstream_that_never_sends_a_first_event_is_retried_not_fatal(
    settings, fake_provider: FakeProvider, monkeypatch
) -> None:
    """The incident this fix exists for.

    An endpoint accepted the connection and never sent a first SSE event. The open
    deadline raised `DeadlineExceeded`, which `retry_upstream` cannot catch (it
    handles `DaintreeError` only), so the configured retry never ran and the turn
    died at the stall — one round after the same request had opened in 1.2s, with
    the overwhelming majority of the total budget unspent.

    Nothing of the turn is public at that point: `meta` carries no model output. So
    the correct behaviour is to open again and let the workflow continue, which is
    what this asserts — a completed turn with real content and a `done`, not a
    typed error that reads as "your workflow was too long".
    """
    from daintree_assistant_server.main import build_container
    from daintree_assistant_server.services import chat_orchestrator as orch

    # An open allowance far too short for the first attempt, inside a total with
    # ample room for the second. That is the real incident's shape (30s of 300s
    # spent), compressed.
    policy = BudgetPolicy(
        preparation=5.0, open_stall=0.3, first_output=8.0, stream_idle=5.0, total=10.0
    )
    monkeypatch.setattr(orch, "RequestBudget", _budget_factory(policy))

    opens = 0
    stalled: list[_NeverFirstEvent] = []

    async def _open(request):  # type: ignore[no-untyped-def]
        nonlocal opens
        opens += 1
        if opens == 1:
            stream = _NeverFirstEvent()
            stalled.append(stream)
            return stream
        return FakeStream(
            [
                {"choices": [{"delta": {"content": "recovered"}}]},
                {"choices": [{"delta": {}, "finish_reason": "stop"}]},
            ],
            endpoint="Cloudflare",
        )

    fake_provider.open_stream = _open  # type: ignore[method-assign]
    container = build_container(settings, fake_provider)
    joined = await _drain(container, request_id="req_3")

    assert opens == 2, "the stalled open was not retried"
    assert "recovered" in joined, "the retried attempt's content never reached the client"
    assert "event: done" in joined, "a recoverable stall still ended the turn"
    assert "turn_deadline_exceeded" not in joined
    assert "upstream_timeout" not in joined, "a recovered stall leaked an error to the client"
    # Exactly one `meta`: the retry happens under a single public turn, and a second
    # metadata event would tell the client its skill set had changed mid-turn.
    assert joined.count("event: meta") == 1
    assert stalled[0].closed, "the stalled half-open stream leaked"
    await container.aclose()


@pytest.mark.asyncio
async def test_an_open_that_stalls_past_every_attempt_ends_honestly(
    settings, fake_provider: FakeProvider, monkeypatch
) -> None:
    """The retry is not a promise that the turn survives — only that it is tried.

    When every attempt stalls the client must get a terminal, correctly-named
    error and NO `done`: a turn reported complete with no answer in it is worse
    than a failure, because the caller acts on it. `upstream_timeout` is also the
    honest code — the upstream did time out — and the CLI's own retry allowlist
    (`cli/internal/backend/retry.go`) replays that one where it treats
    `turn_deadline_exceeded` as final.
    """
    from daintree_assistant_server.main import build_container
    from daintree_assistant_server.services import chat_orchestrator as orch

    policy = BudgetPolicy(
        preparation=5.0, open_stall=0.3, first_output=8.0, stream_idle=5.0, total=10.0
    )
    monkeypatch.setattr(orch, "RequestBudget", _budget_factory(policy))

    streams: list[_NeverFirstEvent] = []

    async def _open(request):  # type: ignore[no-untyped-def]
        stream = _NeverFirstEvent()
        streams.append(stream)
        return stream

    fake_provider.open_stream = _open  # type: ignore[method-assign]
    container = build_container(settings, fake_provider)
    joined = await _drain(container, request_id="req_4")

    assert len(streams) == 2, "the open was not retried before giving up"
    assert all(s.closed for s in streams), "a stalled half-open stream leaked"
    assert "event: error" in joined
    assert "upstream_timeout" in joined, "the terminal error misnames the failure"
    assert "event: done" not in joined, "an answerless turn was reported as complete"
    await container.aclose()


@pytest.mark.asyncio
async def test_an_open_stall_with_no_budget_left_is_terminal_not_retried(
    settings, fake_provider: FakeProvider, monkeypatch
) -> None:
    """The guard that keeps the retry bounded.

    Retrying is only ever the right answer while there is a turn left to spend it
    on. Once the TOTAL is gone the failure is ours, it is attributable, and it must
    not start another attempt — otherwise the per-attempt allowance composes into
    exactly the unbounded turn the budget exists to prevent.
    """
    from daintree_assistant_server.main import build_container
    from daintree_assistant_server.services import chat_orchestrator as orch

    # The total expires DURING the first attempt: the open allowance is clamped to
    # what little remains, so the stall exhausts the turn rather than the attempt.
    policy = BudgetPolicy(
        preparation=0.5, open_stall=0.5, first_output=0.5, stream_idle=0.5, total=0.5
    )
    monkeypatch.setattr(orch, "RequestBudget", _budget_factory(policy))

    opens = 0

    async def _open(request):  # type: ignore[no-untyped-def]
        nonlocal opens
        opens += 1
        return _NeverFirstEvent()

    fake_provider.open_stream = _open  # type: ignore[method-assign]
    container = build_container(settings, fake_provider)
    joined = await _drain(container, request_id="req_5")

    assert opens == 1, "a retry started with no budget to run it in"
    assert "turn_deadline_exceeded" in joined
    assert "total deadline" in joined, "the exhausted phase was not named"
    assert "event: done" not in joined
    await container.aclose()


@pytest.mark.asyncio
async def test_a_stall_before_anything_visible_is_still_attributed_to_first_output(
    settings, fake_provider: FakeProvider, monkeypatch
) -> None:
    """`first_output` lost its limit but kept its meaning.

    It is now the LABEL the per-gap deadline reports when the gap it cut had
    produced nothing a user could see — a different investigation from a stream
    that died mid-answer, and the distinction is the only reason the phase still
    exists. A reasoning fragment is not visible output, so this stream is alive by
    the transport's reckoning and silent by the user's.
    """
    from daintree_assistant_server.main import build_container
    from daintree_assistant_server.services import chat_orchestrator as orch

    # Generous total, tight gap: the idle limit must be what trips, or the phase
    # reported would be `total` and this would pass without proving anything.
    policy = BudgetPolicy(
        preparation=5.0, open_stall=5.0, first_output=8.0, stream_idle=0.3, total=10.0
    )
    monkeypatch.setattr(orch, "RequestBudget", _budget_factory(policy))

    class _ReasonsThenStalls:
        usage = None
        endpoint = None

        def __init__(self) -> None:
            self.closed = False

        async def prefetch_first_event(self):  # type: ignore[no-untyped-def]
            return {"choices": [{"delta": {"reasoning_content": "hmm"}}]}

        def __aiter__(self):  # type: ignore[no-untyped-def]
            return self

        async def __anext__(self):  # type: ignore[no-untyped-def]
            await asyncio.sleep(30)
            raise StopAsyncIteration

        async def aclose(self) -> None:
            self.closed = True

    async def _open(request):  # type: ignore[no-untyped-def]
        return _ReasonsThenStalls()

    fake_provider.open_stream = _open  # type: ignore[method-assign]
    container = build_container(settings, fake_provider)
    joined = await _drain(container, request_id="req_6")

    assert "turn_deadline_exceeded" in joined
    assert "first_output" in joined, (
        "a stall before any visible token was attributed to the wrong phase — the "
        "operator would investigate a dead mid-answer stream instead of a silent one"
    )
    assert "stream_idle" not in joined
    await container.aclose()


@pytest.mark.asyncio
async def test_a_stalled_open_whose_teardown_also_hangs_still_retries(
    settings, fake_provider: FakeProvider, monkeypatch
) -> None:
    """The deadline has to survive its own cleanup.

    Tearing down the half-open stream happens in a `finally` that runs while the
    open deadline is cancelling the prefetch, and `asyncio.timeout` waits for that
    cleanup before it converts the cancellation into `TimeoutError`. So an
    `aclose()` hanging on the same dead upstream — the likeliest way for a close to
    hang, since it is the same socket — would swallow the timeout entirely: no
    retryable error, no retry, no fallback, just a request running until the
    platform kills it. Bounding the close is what keeps the stall recoverable.
    """
    from daintree_assistant_server.main import build_container
    from daintree_assistant_server.services import chat_orchestrator as orch

    monkeypatch.setattr(orch, "_CLOSE_GRACE_SECONDS", 0.1)
    policy = BudgetPolicy(
        preparation=5.0, open_stall=0.3, first_output=8.0, stream_idle=5.0, total=10.0
    )
    monkeypatch.setattr(orch, "RequestBudget", _budget_factory(policy))

    class _StallsThenRefusesToClose:
        usage = None
        endpoint = None

        async def prefetch_first_event(self):  # type: ignore[no-untyped-def]
            await asyncio.sleep(30)
            return None

        def __aiter__(self):  # type: ignore[no-untyped-def]
            return self

        async def __anext__(self):  # type: ignore[no-untyped-def]
            raise StopAsyncIteration

        async def aclose(self) -> None:
            await asyncio.sleep(30)

    opens = 0

    async def _open(request):  # type: ignore[no-untyped-def]
        nonlocal opens
        opens += 1
        if opens == 1:
            return _StallsThenRefusesToClose()
        return FakeStream(
            [
                {"choices": [{"delta": {"content": "recovered"}}]},
                {"choices": [{"delta": {}, "finish_reason": "stop"}]},
            ]
        )

    fake_provider.open_stream = _open  # type: ignore[method-assign]
    container = build_container(settings, fake_provider)
    joined = await _drain(container, request_id="req_9")

    assert opens == 2, "a hanging teardown swallowed the deadline and blocked the retry"
    assert "recovered" in joined
    assert "event: done" in joined
    await container.aclose()


@pytest.mark.asyncio
async def test_invisible_chunks_cannot_hold_a_user_answerless_forever(
    settings, fake_provider: FakeProvider, monkeypatch
) -> None:
    """The hole a purely per-gap deadline leaves, and why `first_output` still has
    a limit.

    Every chunk resets the idle allowance — so an endpoint emitting a reasoning
    fragment (or a role-only delta, or a keepalive) just inside every gap is
    "alive" forever by that measure while the user sits in front of nothing. The
    first-output allowance is cumulative from the open and is NOT renewed by
    chunks that carry nothing visible, so this ends in a bounded, attributable
    failure instead of a 900-second wait.
    """
    from daintree_assistant_server.main import build_container
    from daintree_assistant_server.services import chat_orchestrator as orch

    # Chunks arrive far inside the idle gap, so only the cumulative allowance can
    # be what stops this.
    policy = BudgetPolicy(
        preparation=5.0, open_stall=5.0, first_output=0.4, stream_idle=3.0, total=10.0
    )
    monkeypatch.setattr(orch, "RequestBudget", _budget_factory(policy))

    class _ThinksForever:
        usage = None
        endpoint = None

        async def prefetch_first_event(self):  # type: ignore[no-untyped-def]
            return {"choices": [{"delta": {"reasoning_content": "hmm"}}]}

        def __aiter__(self):  # type: ignore[no-untyped-def]
            return self

        async def __anext__(self):  # type: ignore[no-untyped-def]
            await asyncio.sleep(0.02)
            return {"choices": [{"delta": {"reasoning_content": "still hmm"}}]}

        async def aclose(self) -> None:
            return None

    async def _open(request):  # type: ignore[no-untyped-def]
        return _ThinksForever()

    fake_provider.open_stream = _open  # type: ignore[method-assign]
    container = build_container(settings, fake_provider)
    joined = await _drain(container, request_id="req_7")

    assert "turn_deadline_exceeded" in joined
    assert "first_output" in joined, "the endless non-visible stream escaped its bound"
    assert "event: done" not in joined
    await container.aclose()


@pytest.mark.asyncio
async def test_a_recovered_stall_reports_its_cost_as_incomplete(
    settings, fake_provider: FakeProvider, monkeypatch
) -> None:
    """An abandoned attempt may have been billed, and we will never know.

    The upstream took the request and prefilled it before going quiet; whether it
    charged for that prefill is not observable from here, because the usage
    trailer never arrived. Reporting the surviving attempt's cost as the turn's
    complete total would understate a real charge on the caller's own key — which
    is precisely the case `turn_accounting.turn_cost` documents `complete` for.
    """
    import json

    from daintree_assistant_server.main import build_container
    from daintree_assistant_server.services import chat_orchestrator as orch

    policy = BudgetPolicy(
        preparation=5.0, open_stall=0.3, first_output=8.0, stream_idle=5.0, total=10.0
    )
    monkeypatch.setattr(orch, "RequestBudget", _budget_factory(policy))

    opens = 0

    async def _open(request):  # type: ignore[no-untyped-def]
        nonlocal opens
        opens += 1
        if opens == 1:
            return _NeverFirstEvent()
        return FakeStream(
            [
                {"choices": [{"delta": {"content": "recovered"}}]},
                {
                    "choices": [{"delta": {}, "finish_reason": "stop"}],
                    "usage": {
                        "prompt_tokens": 100,
                        "completion_tokens": 10,
                        "cost": 0.01,
                    },
                },
            ],
            endpoint="Cloudflare",
        )

    fake_provider.open_stream = _open  # type: ignore[method-assign]
    container = build_container(settings, fake_provider)
    joined = await _drain(container, request_id="req_8")

    done = [
        json.loads(block.split("data: ", 1)[1])
        for block in joined.split("\n\n")
        if block.startswith("event: done")
    ]
    assert len(done) == 1, "the recovered turn did not complete"
    cost = done[0].get("cost")
    assert cost is not None, "the turn reported no cost at all"
    assert cost["complete"] is False, (
        "the turn claimed a complete cost while an abandoned open attempt may have "
        "billed a prefill we never got to observe"
    )
    await container.aclose()


async def _drain(container, *, request_id: str) -> str:  # type: ignore[no-untyped-def]
    """Run one streamed turn to completion and return the raw SSE text.

    The outer bound is a TEST-failure guard only: if the server's own deadline is
    broken this fails loudly instead of hanging the suite.
    """
    from pydantic import SecretStr

    from daintree_assistant_server.contracts.daintree_api import RespondRequest

    body = RespondRequest.model_validate(
        {
            "protocol_version": 2,
            "session": {"id": "s", "turn_id": "t"},
            "input": {"messages": [{"role": "user", "content": "rename my terminals"}]},
            "startup": {},
            "generation": {"stream": True},
        }
    )
    outcome = await container.orchestrator.complete(
        body, upstream_model="m", api_key=SecretStr("k"), request_id=request_id
    )
    assert outcome.stream is not None
    parts: list[str] = []
    async with asyncio.timeout(10.0):
        async for chunk in outcome.stream:
            parts.append(chunk)
    return "".join(parts)


class _NeverFirstEvent:
    """Headers sent, first SSE event never completed.

    The precise failure mode: `httpx`'s read timeout is an inactivity bound (840s
    here) so it does not fire, and `_chunk_iter` never sees this stream because
    iteration has not started.

    State is per INSTANCE, not per class: a retry makes two of these, and a class
    flag would report the first one's teardown as proof of the second's.
    """

    usage = None
    endpoint = None

    def __init__(self) -> None:
        self.closed = False

    async def prefetch_first_event(self):  # type: ignore[no-untyped-def]
        await asyncio.sleep(30)
        return None

    def __aiter__(self):  # type: ignore[no-untyped-def]
        return self

    async def __anext__(self):  # type: ignore[no-untyped-def]
        raise StopAsyncIteration

    async def aclose(self) -> None:
        self.closed = True


def _budget_factory(policy):  # type: ignore[no-untyped-def]
    """A drop-in for `RequestBudget` that always mints the given policy."""
    from daintree_assistant_server.services.deadlines import RequestBudget

    def _make(*args, **kwargs):  # type: ignore[no-untyped-def]
        kwargs.pop("policy", None)
        return RequestBudget(*args, policy=policy, **kwargs)

    return _make
