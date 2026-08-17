"""End-to-end: a stalled turn produces an attributable deadline error.

The unit tests pin the budget arithmetic; these pin that the arithmetic is
actually WIRED — that a stall in each phase surfaces as `turn_deadline_exceeded`
on the right channel, rather than hanging until the platform kills the request.
"""

from __future__ import annotations

import asyncio

import pytest
from fakes import FakeProvider

from daintree_assistant_server.services.deadlines import BudgetPolicy, RequestBudget


def _tiny_budget() -> RequestBudget:
    return RequestBudget(
        policy=BudgetPolicy(
            preparation=0.15, first_output=0.15, stream_idle=0.15, total=0.2
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

    tiny = BudgetPolicy(preparation=0.4, first_output=0.4, stream_idle=0.4, total=0.5)
    monkeypatch.setattr(orch, "RequestBudget", _budget_factory(tiny))

    class _StalledStream:
        """Opens fine, then never produces a chunk — the shape an inactivity
        timeout is bad at and a wall clock is good at."""

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

    async def _open(request):  # type: ignore[no-untyped-def]
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
    await container.aclose()


def _budget_factory(policy):  # type: ignore[no-untyped-def]
    """A drop-in for `RequestBudget` that always mints the given policy."""
    from daintree_assistant_server.services.deadlines import RequestBudget

    def _make(*args, **kwargs):  # type: ignore[no-untyped-def]
        kwargs.pop("policy", None)
        return RequestBudget(*args, policy=policy, **kwargs)

    return _make
