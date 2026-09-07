"""Optional trace storage cannot hold a turn's preparation hostage."""

from __future__ import annotations

import asyncio
from typing import Any

import pytest

from daintree_assistant_server.tracing import DefaultTraceRecorder, NullTraceSink, decide
from daintree_assistant_server.tracing.recorder import NULL_SCOPE


class StalledSink(NullTraceSink):
    def __init__(self) -> None:
        self.started = asyncio.Event()
        self.cancelled = False

    async def open(self, trace_id: str, header: dict[str, Any]) -> None:
        self.started.set()
        try:
            await asyncio.Event().wait()
        finally:
            self.cancelled = True


def recorder(sink: StalledSink) -> DefaultTraceRecorder:
    return DefaultTraceRecorder(
        sink=sink,
        identity_key=b"test-trace-identity-key",
        eligibility=decide(mode="metadata", is_hardened=True),
        max_event_bytes=1024,
        max_pending_bytes=4096,
        flush_grace_seconds=0.02,
    )


async def test_trace_open_times_out_without_failing_the_turn() -> None:
    sink = StalledSink()
    scope = await asyncio.wait_for(
        recorder(sink).start_trace(request_id="test", session_id="test", turn_id="test"),
        timeout=1,
    )
    assert scope is NULL_SCOPE
    assert sink.cancelled


async def test_caller_cancellation_still_abandons_trace_open() -> None:
    sink = StalledSink()
    task = asyncio.create_task(
        recorder(sink).start_trace(request_id="test", session_id="test", turn_id="test")
    )
    await sink.started.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert sink.cancelled
