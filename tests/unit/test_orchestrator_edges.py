"""Orchestrator branches that only run when something is already malformed.

These are the paths a well-behaved upstream and a well-behaved client never
reach, which is precisely why they rot unnoticed: the happy path covers the
module's percentage while the guards underneath it stop being exercised. The
per-module floor in ``tools/coverage_floors.py`` exists to catch that, and the
orchestrator is on the list because a guard that fails here turns a mapped 502
into an unhandled 500 mid-stream.
"""

from __future__ import annotations

import asyncio
from typing import Any

import pytest

from daintree_assistant_server.errors import DaintreeError
from daintree_assistant_server.services.chat_orchestrator import (
    ChatOrchestrator,
    _carries_output,
)

# ---------------------------------------------------------------------------
# `_carries_output` — the deadline's definition of "the user saw something".
#
# It reads raw upstream chunks, BEFORE any shape validation, so every branch has
# to survive a payload that is merely JSON-shaped. Getting it wrong in the
# permissive direction is the expensive one: a stream that produces nothing a
# user can see would satisfy the first-output deadline indefinitely.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("chunk", "why"),
    [
        ({}, "an empty chunk"),
        ({"choices": []}, "no choices at all"),
        ({"choices": None}, "a null choices"),
        ({"choices": ["not a dict"]}, "a non-dict choice"),
        ({"choices": [{"delta": "not a dict"}]}, "a non-dict delta"),
        ({"choices": [{"delta": {}}]}, "an empty delta"),
        ({"choices": [{"delta": {"content": ""}}]}, "empty content"),
        ({"choices": [{"delta": {"reasoning_content": "thinking"}}]}, "reasoning only"),
    ],
)
def test_a_chunk_with_nothing_visible_does_not_count_as_output(
    chunk: dict[str, Any], why: str
) -> None:
    assert _carries_output(chunk) is False, why


@pytest.mark.parametrize(
    "delta",
    [
        {"content": "hello"},
        {"tool_calls": [{"index": 0, "function": {"name": "x"}}]},
    ],
)
def test_visible_content_and_tool_calls_both_count(delta: dict[str, Any]) -> None:
    """A tool call is output even though the user reads no prose.

    The client dispatches it, so the turn demonstrably progressed — treating only
    prose as output would let a pure tool-call round trip the deadline.
    """
    assert _carries_output({"choices": [{"delta": delta}]}) is True


# ---------------------------------------------------------------------------
# `resolve` — the manual runbook endpoint's rejection path.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_an_unknown_resolve_action_is_a_400_naming_the_field(
    orchestrator: ChatOrchestrator,
) -> None:
    """Contract validation should catch this first; the guard is the backstop.

    It matters because the alternative is falling through with `decision` and
    `active_ids` unbound — an UnboundLocalError, reported to the caller as an
    internal error, for what is plainly a bad request.
    """
    with pytest.raises(DaintreeError) as exc:
        await orchestrator.resolve(action="teleport", session_id="s", state_token=None,
                                   api_key=None)
    assert exc.value.status_code == 400
    assert exc.value.param == "action"


@pytest.mark.asyncio
async def test_loading_an_unknown_runbook_id_warns_instead_of_failing(
    orchestrator: ChatOrchestrator,
) -> None:
    """A stale client asking for a deleted runbook gets the rest of its request.

    Failing the call would make a catalog edit break every client still holding
    the old id, for a request the server can honour in part.
    """
    result = await orchestrator.resolve(
        action="load",
        session_id="s",
        state_token=None,
        api_key=None,
        runbook_ids=["daintree.does.not.exist"],
    )
    assert "unknown_runbook_id_ignored" in result["warnings"]


# ---------------------------------------------------------------------------
# `drain_background_tasks` — the shutdown bound.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_draining_with_nothing_in_flight_returns_immediately(
    orchestrator: ChatOrchestrator,
) -> None:
    await orchestrator.drain_background_tasks(timeout=0.01)


@pytest.mark.asyncio
async def test_a_task_that_outlives_the_timeout_is_cancelled_not_awaited(
    orchestrator: ChatOrchestrator,
) -> None:
    """The bound is the whole point of this method.

    A detached preparation task owns a live upstream call, so an unbounded await
    here would hang shutdown after a Ctrl-C for as long as the model felt like
    taking. The cancelled task is also gathered, so it cannot resurface as an
    orphaned "exception was never retrieved" warning.
    """

    async def _forever() -> None:
        await asyncio.sleep(30)

    task = asyncio.create_task(_forever())
    orchestrator._background_tasks.add(task)
    task.add_done_callback(orchestrator._background_tasks.discard)

    await orchestrator.drain_background_tasks(timeout=0.01)
    assert task.cancelled() or task.done()


@pytest.mark.asyncio
async def test_a_task_that_finishes_inside_the_timeout_is_not_cancelled(
    orchestrator: ChatOrchestrator,
) -> None:
    done = asyncio.Event()

    async def _quick() -> None:
        done.set()

    task = asyncio.create_task(_quick())
    orchestrator._background_tasks.add(task)
    task.add_done_callback(orchestrator._background_tasks.discard)

    await orchestrator.drain_background_tasks(timeout=5.0)
    assert done.is_set()
    assert not task.cancelled()
