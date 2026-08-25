"""`PreambleGate` — the one place "shown or dropped" is decided.

The fast call and the executor call now leave together, and Cerebras is EXPECTED
to win. Expected, not guaranteed, and everything here is about the other case.

The rule the whole file circles: a preamble is worth showing only while the
screen is still empty. Once the executor's first visible byte has reached the
client, a line describing work the reader can already watch happening is worse
than silence — so it is dropped, unshown, still billed and still traced.
"""

from __future__ import annotations

import asyncio
from typing import Any

import pytest
import structlog.testing

from daintree_assistant_server.providers.base import ProviderUsage
from daintree_assistant_server.services.fast_response import (
    FastResponsePolicyDecision,
    FastResponseResult,
    PreambleGate,
)

VISIBLE = FastResponsePolicyDecision(run=True, visible=True, reason="eligible")
SHADOW = FastResponsePolicyDecision(run=True, visible=False, reason="eligible")
SKIPPED = FastResponsePolicyDecision(run=False, visible=False, reason="mode_off")

LINE = FastResponseResult(
    content="Tracing the failing step back to the change that could explain it.",
    source="model",
    usage=ProviderUsage(prompt_tokens=10, completion_tokens=4, total_tokens=14),
)


async def _settled(result: FastResponseResult) -> asyncio.Task[FastResponseResult]:
    """A task that has already finished, the way a fast call that won looks."""

    async def done() -> FastResponseResult:
        return result

    task = asyncio.create_task(done())
    await task
    return task


def _running() -> asyncio.Task[FastResponseResult]:
    """A task that will never finish on its own — the call that is losing."""

    async def never() -> FastResponseResult:
        await asyncio.Event().wait()
        raise AssertionError("unreachable")

    return asyncio.create_task(never())


def _gate(task: asyncio.Task[FastResponseResult] | None, policy: Any = VISIBLE) -> PreambleGate:
    return PreambleGate(task, policy, request_id="req_1")


# ---------------------------------------------------------------------------
# The happy path: Cerebras wins, the line is shown.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_a_finished_call_is_claimed_once_and_only_once() -> None:
    gate = _gate(await _settled(LINE))
    assert gate.poll() is LINE
    assert gate.claimed is True
    # A second poll must produce nothing: the gate is consulted from four places
    # in the stream loop, and the bare-intent re-roll re-runs the open race — two
    # `preamble` events in one turn would render the line twice.
    assert gate.poll() is None


@pytest.mark.asyncio
async def test_claiming_a_line_is_not_the_same_as_delivering_it() -> None:
    """The emitter yields a `status` event, then the `preamble` event. A client
    that disconnects between the two received nothing, and tracing the turn as
    `accepted=True` would put a line in the record that never reached anyone.
    Only the emitter knows the yield returned, so only it may say `shown`."""
    gate = _gate(await _settled(LINE))
    assert gate.poll() is LINE
    assert gate.claimed is True
    assert gate.shown is False
    gate.mark_shown()
    assert gate.shown is True


@pytest.mark.asyncio
async def test_a_still_running_call_yields_nothing_and_stays_pending() -> None:
    task = _running()
    gate = _gate(task)
    assert gate.poll() is None
    assert gate.pending is True
    task.cancel()


# ---------------------------------------------------------------------------
# Losing the race.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_a_line_that_lands_after_the_first_visible_byte_is_dropped() -> None:
    """The case the whole gate exists for. The answer is on screen; a preview of
    work the reader can already watch is worse than nothing."""
    task = _running()
    gate = _gate(task)
    gate.close_window("executor_output_first")
    task.cancel()

    assert gate.poll() is None
    assert gate.shown is False
    assert gate.drop_reason == "executor_output_first"


@pytest.mark.asyncio
async def test_a_closed_window_refuses_even_a_finished_valid_line() -> None:
    """Finished is not the same as in time. A gate that showed a completed result
    regardless of when it completed would emit the preamble mid-answer."""
    gate = _gate(await _settled(LINE))
    gate.close_window("executor_output_first")
    assert gate.poll() is None
    assert gate.shown is False


@pytest.mark.asyncio
async def test_closing_the_window_does_not_cancel_the_call() -> None:
    """The request is already sent and already billed. Cancelling buys nothing
    and loses the usage record, which still has to reach the turn's cost."""
    task = _running()
    gate = _gate(task)
    gate.close_window("executor_output_first")
    assert task.cancelled() is False
    assert task.done() is False
    task.cancel()


@pytest.mark.asyncio
async def test_closing_after_the_line_was_shown_is_not_a_drop() -> None:
    """Every turn that shows a preamble then produces output, so this path runs
    on the SUCCESS case too. Recording a drop there would make the drop rate —
    the number a rollback triggers on — read as 100%."""
    gate = _gate(await _settled(LINE))
    assert gate.poll() is LINE
    gate.close_window("executor_output_first")
    assert gate.drop_reason is None


@pytest.mark.asyncio
async def test_a_drop_is_logged_with_its_reason() -> None:
    task = _running()
    gate = _gate(task)
    with structlog.testing.capture_logs() as logs:
        gate.close_window("executor_output_first")
    task.cancel()
    entry = next(e for e in logs if e.get("event") == "fast_response_dropped")
    assert entry["reason"] == "executor_output_first"


@pytest.mark.asyncio
async def test_closing_twice_records_one_drop() -> None:
    """`close_window` is called from `_mark_first_output`, which runs on every
    delta that reaches the client — so it is called many times per turn."""
    task = _running()
    gate = _gate(task)
    with structlog.testing.capture_logs() as logs:
        gate.close_window("executor_output_first")
        gate.close_window("executor_output_first")
        gate.close_window("executor_output_first")
    task.cancel()
    assert sum(e.get("event") == "fast_response_dropped" for e in logs) == 1


# ---------------------------------------------------------------------------
# Everything that is not "a line to show", and why each is different.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_shadow_mode_settles_the_call_but_shows_nothing() -> None:
    """Shadow produces real, valid content that must never reach a client — and
    must still be accounted and traced, which is the entire point of the mode."""
    gate = _gate(await _settled(LINE), SHADOW)
    assert gate.poll() is None
    assert gate.shown is False
    assert gate.usage == LINE.usage


@pytest.mark.asyncio
async def test_a_declining_model_shows_nothing_and_is_not_a_drop() -> None:
    """Silence is the model's own judgement, not a failure and not a late
    arrival. Recording it as either would corrupt both numbers."""
    declined = FastResponseResult(source="declined", reason="model_declined")
    gate = _gate(await _settled(declined))
    assert gate.poll() is None
    assert gate.shown is False
    gate.close_window("executor_output_first")
    # The window closed on a gate that had nothing to lose, so no drop.
    assert gate.drop_reason is None


@pytest.mark.asyncio
async def test_a_failed_call_is_settled_rather_than_left_pending() -> None:
    failed = FastResponseResult(degraded=True, reason="provider_error")
    gate = _gate(await _settled(failed))
    assert gate.poll() is None
    assert gate.pending is False
    assert gate.finish().reason == "provider_error"


@pytest.mark.asyncio
async def test_a_gate_with_no_task_is_inert() -> None:
    gate = _gate(None, SKIPPED)
    assert gate.pending is False
    assert gate.poll() is None
    assert gate.usage is None
    # The skip reason the policy already computed, not a generic one: nothing
    # else downstream observes those per-case reasons.
    assert gate.finish().reason == "mode_off"


# ---------------------------------------------------------------------------
# Nothing here may fail or delay a turn.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_a_cancelled_call_does_not_propagate_the_cancellation() -> None:
    """The turn's own cancellation is delivered at its own await points. Raising
    here would convert an optimisation's demise into the turn's."""
    task = _running()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    gate = _gate(task)
    assert gate.poll() is None
    assert gate.finish().reason == "cancelled"


@pytest.mark.asyncio
async def test_a_call_that_raised_is_absorbed_with_its_own_reason() -> None:
    async def explode() -> FastResponseResult:
        raise RuntimeError("boom")

    task = asyncio.create_task(explode())
    await asyncio.gather(task, return_exceptions=True)
    gate = _gate(task)
    assert gate.poll() is None
    assert gate.finish().reason == "collect_failed"


@pytest.mark.asyncio
async def test_finish_never_waits_on_an_unfinished_call() -> None:
    """It runs at the terminal event of a turn that is otherwise complete.
    Blocking that for up to `FAST_RESPONSE_TIMEOUT_SECONDS` would let this stage
    delay the very thing it exists to make feel fast."""
    task = _running()
    gate = _gate(task)
    started = asyncio.get_running_loop().time()
    result = gate.finish()
    assert asyncio.get_running_loop().time() - started < 0.05
    # Named for what happened — dispatched and never landed — not for the
    # policy's reason, which belongs to a stage that never started at all.
    assert result.reason == "unsettled"
    assert gate.usage is None
    task.cancel()


@pytest.mark.asyncio
async def test_finish_is_idempotent_and_keeps_the_usage_for_the_cost_total() -> None:
    """Called before the `done` event AND again in the terminal `finally`. Two
    different answers would put two different totals on one turn."""
    gate = _gate(await _settled(LINE))
    assert gate.poll() is LINE
    assert gate.finish() is gate.finish()
    assert gate.usage == LINE.usage


# ---------------------------------------------------------------------------
# Accounting: one turn, one total.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_finish_latches_so_a_late_arrival_cannot_change_the_total() -> None:
    """`finish()` runs twice per turn — before `done`, which carries the cost to
    the client, and in the terminal `finally`, which writes the log and the trace.

    A task landing between the two used to be summed into the log but not into
    `done`, so one turn reported two different totals. The first answer is final.
    """
    async def late() -> FastResponseResult:
        return LINE

    task = asyncio.create_task(late())
    gate = _gate(task)
    # Before `done`: the call has not landed.
    first = gate.finish()
    assert first.reason == "unsettled"
    # It lands during the compaction await / the `done` yield.
    await task
    # The terminal `finally` must not now report something else.
    assert gate.finish() is first
    assert gate.usage is None


@pytest.mark.asyncio
async def test_a_latched_unknown_is_flagged_rather_than_summed_as_zero() -> None:
    """`turn_cost` reads `fast_response=None` as "the stage did not run" and sums
    an honest zero. That is the wrong reading for a request that went out and came
    back uncountable, so the flag is what clears `complete` on the reported cost."""
    task = _running()
    gate = _gate(task)
    gate.finish()
    assert gate.usage is None
    assert gate.billed_unknown is True
    task.cancel()


@pytest.mark.asyncio
async def test_a_call_that_never_reached_the_provider_is_not_an_unknown_cost() -> None:
    """`provider_unavailable` and `no_usable_context` never touch the wire and
    never bill. Flagging them would clear `complete` on turns that are in fact
    completely known — an unknown that cries wolf stops meaning anything."""
    never_sent = FastResponseResult(degraded=True, reason="no_usable_context")
    gate = _gate(await _settled(never_sent))
    gate.finish()
    assert gate.billed_unknown is False


@pytest.mark.asyncio
async def test_a_timeout_IS_an_unknown_cost() -> None:
    """`latency_ms` is set exactly when a provider call was attempted. A timeout
    is client-side: the host may well have served and billed the request."""
    timed_out = FastResponseResult(degraded=True, reason="timeout", latency_ms=3000)
    gate = _gate(await _settled(timed_out))
    gate.finish()
    assert gate.usage is None
    assert gate.billed_unknown is True


@pytest.mark.asyncio
async def test_a_stage_that_never_ran_is_not_an_unknown_cost() -> None:
    gate = _gate(None, SKIPPED)
    gate.finish()
    assert gate.billed_unknown is False


@pytest.mark.asyncio
async def test_a_reported_usage_is_never_an_unknown() -> None:
    gate = _gate(await _settled(LINE))
    assert gate.poll() is LINE
    gate.finish()
    assert gate.billed_unknown is False


# ---------------------------------------------------------------------------
# `_abandon_open` — the disposal a directly-awaited call never needed.
#
# The executor's open runs as a TASK now, purely so the preamble can be raced
# against it. That is what creates these two leak shapes, and no end-to-end test
# reaches either: every streaming test drains its stream to completion.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_abandoning_a_still_connecting_open_cancels_it() -> None:
    """Reached when the client disconnects while both calls are still in flight —
    `GeneratorExit` at the yield inside the race, straight into the `finally`."""
    from daintree_assistant_server.services.chat_orchestrator import ChatOrchestrator

    orchestrator = object.__new__(ChatOrchestrator)
    orchestrator._background_tasks = set()

    async def connecting() -> Any:
        await asyncio.Event().wait()

    task = asyncio.create_task(connecting())
    await asyncio.sleep(0)
    orchestrator._abandon_open(task, request_id="req_1")

    await asyncio.gather(task, return_exceptions=True)
    assert task.cancelled() is True


@pytest.mark.asyncio
async def test_abandoning_a_completed_open_closes_the_stream_nobody_took() -> None:
    """The nastier shape: the open SUCCEEDED and is holding a live upstream
    connection that nothing is going to read. Cancelling is not enough — the
    stream has to be closed, or it outlives the turn that opened it."""
    from fakes import FakeStream

    from daintree_assistant_server.services.chat_orchestrator import ChatOrchestrator

    orchestrator = object.__new__(ChatOrchestrator)
    orchestrator._background_tasks = set()
    stream = FakeStream([])

    async def opened() -> Any:
        return (stream, None, 0)

    task = asyncio.create_task(opened())
    await task
    orchestrator._abandon_open(task, request_id="req_1")

    # The close is scheduled as background work — the `finally` it runs in can be
    # reached through GeneratorExit, where awaiting is not reliably permitted.
    await asyncio.gather(*list(orchestrator._background_tasks), return_exceptions=True)
    assert stream.closed is True


@pytest.mark.asyncio
async def test_abandoning_a_failed_open_does_nothing() -> None:
    """The `DaintreeError` path also reaches the `finally` with `opened is None`,
    and there is no stream to close. Re-raising or re-cancelling here would turn
    an already-handled failure into a second one."""
    from daintree_assistant_server.services.chat_orchestrator import ChatOrchestrator

    orchestrator = object.__new__(ChatOrchestrator)
    orchestrator._background_tasks = set()

    async def failed() -> Any:
        raise RuntimeError("open failed")

    task = asyncio.create_task(failed())
    await asyncio.gather(task, return_exceptions=True)
    orchestrator._abandon_open(task, request_id="req_1")
    assert orchestrator._background_tasks == set()
