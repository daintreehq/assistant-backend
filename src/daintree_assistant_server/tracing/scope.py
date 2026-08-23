"""One trace's lifetime, and the three rules that make it safe to wire in.

**1. It cannot fail the turn.** Every public method swallows the exceptions its
own storage raises and records the fact. A storage outage, a full disk, a sink
that raises on every call — none of it reaches the request. The alternative is
always the same bug: an observability feature that takes the production path down
with it.

``asyncio.CancelledError`` is the deliberate exception to that, and it is not an
oversight: cancellation is how the turn's own deadlines and a client disconnect
are delivered, so swallowing it here would keep a request alive past the budget
that was supposed to end it. It propagates, and the caller's `finally` terminates
the trace.

**2. It cannot change the turn.** Nothing here is read back into selection, prompt
assembly, or the model request. A trace is a record of a decision, never an input
to one; if that ever stops being true, a capture flag becomes a behaviour flag and
the traces stop describing production.

**3. It cannot outlive the response un-awaited.** Cloud Run may suspend CPU the
moment a response completes, so a fire-and-forget upload after the last byte is a
coin flip. Uploads therefore start EARLY (while the model is still generating) and
are tracked in a bounded pending set that ``complete()`` drains under a small
grace budget. What does not make the budget is marked incomplete rather than
waited on forever — a user does not hold the connection open for a training
record.
"""

from __future__ import annotations

import asyncio
import contextlib
import time
from datetime import UTC, datetime
from typing import Any

from ..observability import get_logger
from .canonical import as_blob, sha256_hex
from .models import (
    ContentRef,
    ModelInvocationTrace,
    TraceEvent,
    TraceHeader,
    TraceOutcome,
)
from .sinks.base import TraceSink

logger = get_logger(__name__)


def _utc_now() -> str:
    return datetime.now(UTC).isoformat().replace("+00:00", "Z")


class TraceScope:
    """The live trace for one backend request round."""

    def __init__(
        self,
        *,
        trace_id: str,
        header: TraceHeader,
        sink: TraceSink,
        capture_content: bool,
        max_event_bytes: int,
        max_pending_bytes: int,
        flush_grace_seconds: float,
    ) -> None:
        self._trace_id = trace_id
        self._header = header
        self._sink = sink
        self._capture_content = capture_content
        self._max_event_bytes = max_event_bytes
        self._max_pending_bytes = max_pending_bytes
        self._flush_grace_seconds = flush_grace_seconds

        self._started = time.perf_counter()
        self._sequence = 0
        self._events: list[TraceEvent] = []
        self._invocations: list[ModelInvocationTrace] = []
        #: digest -> byte length. The per-trace dedup index, and the reason a
        #: 20-round conversation does not store its history 20 times.
        self._blobs: dict[str, int] = {}
        self._pending: set[asyncio.Task[None]] = set()
        #: Bytes handed to uploads that have not finished. A GAUGE: it rises when a
        #: write is scheduled and falls when it settles, so it bounds how much is
        #: in flight at once rather than how much the trace stored in total. The
        #: two are very different on a long tool-calling turn, and it is the
        #: in-flight figure that bounds memory on a Cloud Run instance.
        self._pending_bytes = 0
        #: Cumulative, for the operational log. Never used for backpressure.
        self._stored_bytes = 0
        self._dropped: list[str] = []
        #: Digests whose upload failed. Kept apart from `_failures` because the
        #: manifest LISTS these blobs: attesting to an object that is not there is
        #: the one claim the completion marker must never make.
        self._failed_blobs: set[str] = set()
        self._failures = 0
        self._closed = False

    @property
    def trace_id(self) -> str:
        return self._trace_id

    @property
    def failures(self) -> int:
        return self._failures

    # -- content ---------------------------------------------------------

    def _store(self, content: Any, content_type: str | None) -> ContentRef | None:
        """Hash, dedup, and schedule an upload. Returns the ref, never the bytes.

        ``content`` may be a zero-argument CALLABLE, and callers on hot paths
        should pass one: building a multi-megabyte payload for a scope that is
        going to discard it is exactly the cost the null path exists to avoid.
        The thunk is invoked only once capture is known to be on.
        """
        if content is None:
            return None
        if callable(content):
            content = content()
            if content is None:
                return None
        raw, media_type = as_blob(content)
        if content_type:
            media_type = content_type
        digest = sha256_hex(raw)

        if len(raw) > self._max_event_bytes:
            # Recording a truncated blob under a hash of the WHOLE thing would be
            # a lie an exporter cannot detect. Drop it and say so instead.
            self._dropped.append(f"oversize:{digest[:12]}")
            return None

        if digest in self._blobs:
            return ContentRef(sha256=digest, byte_length=len(raw), media_type=media_type)

        if self._pending_bytes + len(raw) > self._max_pending_bytes:
            self._dropped.append(f"backpressure:{digest[:12]}")
            return None

        self._blobs[digest] = len(raw)
        self._pending_bytes += len(raw)
        self._stored_bytes += len(raw)
        self._schedule(
            self._sink.put_blob(self._trace_id, digest, raw, media_type),
            size=len(raw),
            digest=digest,
        )
        return ContentRef(sha256=digest, byte_length=len(raw), media_type=media_type)

    def _schedule(self, coro: Any, *, size: int = 0, digest: str | None = None) -> None:
        """Run a sink write concurrently, tracked so ``complete()`` can drain it.

        The strong reference in ``_pending`` is load-bearing: asyncio holds only a
        WEAK one to a bare task, so a write could otherwise be garbage-collected
        mid-flight. The same set is what ``_terminate`` drains.
        """
        task = asyncio.create_task(self._guard(coro, digest=digest))
        self._pending.add(task)

        def _settled(finished: asyncio.Task[None]) -> None:
            self._pending.discard(finished)
            # Release the reservation whatever happened — success, failure or
            # cancellation. Releasing only on success would let a run of failing
            # uploads exhaust the budget and start dropping content for a reason
            # that has nothing to do with how much is actually in flight.
            self._pending_bytes = max(0, self._pending_bytes - size)

        task.add_done_callback(_settled)

    async def _guard(self, coro: Any, *, digest: str | None = None) -> None:
        try:
            await coro
        except asyncio.CancelledError:
            if digest:
                self._failed_blobs.add(digest)
            raise
        except Exception:
            if digest:
                self._failed_blobs.add(digest)
            # Content-free by construction: the count, never the payload or the
            # exception text, which could quote the content it failed to write.
            self._failures += 1
            logger.warning("trace_write_failed", trace_id=self._trace_id)

    # -- recording -------------------------------------------------------

    async def record_event(
        self,
        event_type: str,
        *,
        attributes: dict[str, Any] | None = None,
        content: Any = None,
        content_type: str | None = None,
        parent_event_ids: tuple[str, ...] = (),
    ) -> str:
        """Append one event. Returns its id so a later event can parent to it."""
        if self._closed:
            # Past termination there is nothing left to drain a new upload, and
            # the manifest has already been written. Dropping is the only honest
            # answer; scheduling would leave a task outliving the response.
            return ""
        try:
            self._sequence += 1
            event_id = f"evt_{self._trace_id[4:]}_{self._sequence:05d}"
            ref = self._store(content, content_type) if self._capture_content else None
            self._events.append(
                TraceEvent(
                    event_id=event_id,
                    sequence=self._sequence,
                    event_type=event_type,
                    occurred_at=_utc_now(),
                    monotonic_offset_ms=int((time.perf_counter() - self._started) * 1000),
                    parent_event_ids=parent_event_ids,
                    attributes=dict(attributes or {}),
                    content_ref=ref,
                )
            )
            return event_id
        except Exception:
            self._failures += 1
            logger.warning("trace_write_failed", trace_id=self._trace_id)
            return ""

    async def record_model_invocation(self, invocation: ModelInvocationTrace) -> str:
        try:
            self._invocations.append(invocation)
            await self.record_event(
                f"{invocation.stage}.invocation",
                attributes={
                    "invocation_id": invocation.invocation_id,
                    "provider": invocation.provider,
                    "model": invocation.model,
                    "attempt": invocation.attempt,
                    "accepted": invocation.accepted,
                    "discard_reason": invocation.discard_reason,
                },
            )
            return invocation.invocation_id
        except Exception:
            self._failures += 1
            logger.warning("trace_write_failed", trace_id=self._trace_id)
            return ""

    def store_content(self, content: Any, *, content_type: str | None = None) -> ContentRef | None:
        """Store a payload and return its ref, without emitting an event.

        For the large things a model invocation points at — a request body, an
        assembled response — which belong to the invocation record rather than to
        a moment in the event stream.
        """
        if not self._capture_content:
            return None
        try:
            return self._store(content, content_type)
        except Exception:
            self._failures += 1
            return None

    # -- termination -----------------------------------------------------

    async def complete(self, outcome: TraceOutcome) -> None:
        """Drain within the grace budget, then write the manifest and marker."""
        await self._terminate(outcome)

    async def abort(self, outcome: TraceOutcome) -> None:
        """Terminate a trace that did not finish cleanly.

        Deliberately still writes the marker: a trace of a FAILED turn is a
        complete record of a failure, which is different from a truncated record
        of anything. ``outcome.status`` carries the distinction, and the exporter
        filters on that rather than on the marker's absence.
        """
        await self._terminate(outcome)

    async def _terminate(self, outcome: TraceOutcome) -> None:
        if self._closed:
            return
        self._closed = True
        try:
            await self._sink.put_events(self._trace_id, [e.as_dict() for e in self._events])

            incomplete = self._dropped[:]
            if self._pending:
                # A BOUNDED wait. Uploads have been running since the model started,
                # so on the normal path there is nothing here; the budget exists for
                # the abnormal one, and blowing through it would hold the user's
                # connection open for a training record.
                done, pending = await asyncio.wait(
                    self._pending, timeout=self._flush_grace_seconds
                )
                if pending:
                    incomplete.append(f"flush_timeout:{len(pending)}")
                    for task in pending:
                        task.cancel()
                    with contextlib.suppress(Exception):
                        await asyncio.gather(*pending, return_exceptions=True)

            # Failed blobs are inspected AFTER the drain, never before: most
            # uploads are still in flight when termination starts, so a check
            # taken first sees an empty set and the marker then attests to
            # objects that failed while it was waiting. That is the one claim the
            # completion marker must never make.
            if self._failed_blobs:
                incomplete.append(f"blob_write_failed:{len(self._failed_blobs)}")

            # And they are REMOVED from the manifest. Listing a blob the store
            # does not have would send an exporter looking for it and fail hash
            # verification on a trace whose real problem is a write that never
            # landed — a misdiagnosis wearing the shape of corruption.
            stored = {
                digest: length
                for digest, length in self._blobs.items()
                if digest not in self._failed_blobs
            }

            status = outcome.status
            if incomplete and status == "completed":
                status = "incomplete"

            await self._sink.finish(
                self._trace_id,
                {
                    "trace_id": self._trace_id,
                    "header": self._header.as_dict(),
                    "outcome": {
                        **outcome.as_dict(),
                        "status": status,
                        "incomplete_reason": outcome.incomplete_reason
                        or (", ".join(incomplete) if incomplete else None),
                    },
                    "event_count": len(self._events),
                    "blobs": [
                        {"sha256": digest, "byte_length": length}
                        for digest, length in sorted(stored.items())
                    ],
                    "invocations": [inv.as_dict() for inv in self._invocations],
                    "write_failures": self._failures,
                },
            )
        except Exception:
            self._failures += 1
            logger.warning("trace_write_failed", trace_id=self._trace_id)

    def log_fields(self) -> dict[str, Any]:
        """Content-free metrics for the OPERATIONAL log. No hashes.

        POINT-IN-TIME. The orchestrator emits these on the `respond` record, which
        is written while the turn is still going, so they describe the trace SO
        FAR. There is deliberately no `trace_complete` here: at the only moment
        this is called it could never be true, and a field that is always false is
        worse than an absent one — it reads as a fleet-wide failure. Completion
        lives in the trace's own manifest, which is the thing that knows.

        A stable content hash in Cloud Logging is a cross-session fingerprint —
        two turns carrying the same digest are two turns carrying the same text,
        which is exactly the inference the content-free contract exists to
        prevent. Hashes live in the restricted trace manifest and nowhere else.
        """
        return {
            "trace_event_count": len(self._events),
            "trace_blob_count": len(self._blobs),
            "trace_bytes_stored": self._stored_bytes,
            "trace_bytes_in_flight": self._pending_bytes,
            "trace_write_failures": self._failures,
        }
