"""Speculative streaming — starting the main call before selection finishes.

The turn's first byte waits on the skill selector, a separate model call on the
critical path. When the prior active set is already known-good, the main stream
can be opened privately ALONGSIDE that classification and adopted if the
selector agrees; if it disagrees, the guess is discarded and the canonical
request is opened instead. Latency is bought with tokens, deliberately.

Everything here exists to make that safe rather than fast:

* **Nothing crosses the boundary until it is claimed.** A private producer fills
  a bounded buffer; not one of its status, reasoning, content, tool or usage
  events reaches the client before ``claim()`` succeeds. Authoritative metadata
  always goes first.
* **Claim and expiry are one atomic event-loop transition.** Both mark their
  flag before the first ``await``, so a late claim cleanly falls back to a
  canonical open rather than racing a half-torn-down producer.
* **Backpressure is on both chunks and bytes.** An oversized indivisible event
  abandons the attempt rather than growing without bound.
* **A discarded attempt still costs the caller money.** Its usage is recorded on
  the object so the turn can bill it — see ``turn_accounting.turn_cost``.

Extracted from ``chat_orchestrator`` unchanged: this is a self-contained
concurrency concern, and reading it next to prompt assembly and response
building made all three harder to follow.
"""

from __future__ import annotations

import asyncio
import json
import time
from collections import deque
from collections.abc import AsyncGenerator, Awaitable, Callable
from typing import TYPE_CHECKING, Any

from ..errors import DaintreeError
from ..observability import get_logger
from ..providers.base import ProviderChatRequest, ProviderStream, ProviderUsage

if TYPE_CHECKING:  # pragma: no cover - import cycle exists only for typing
    # `_PrepOwnedStream` merely cancels this task; it never reads the result.
    # Importing the type only under TYPE_CHECKING keeps the precise annotation
    # without a runtime cycle back into the orchestrator.
    from .chat_orchestrator import _PreparedTurn

logger = get_logger("daintree.respond")

# How long teardown of a discarded speculative producer may take before we stop
# waiting on it. A slow close must never become the turn's outcome.
_SPECULATION_CLOSE_TIMEOUT_SECONDS = 2.0


class _PrepOwnedStream:
    """The streaming body, with explicit ownership of the eager prep task.

    The response body may be closed before it is ever polled (an endpoint
    failure after ``complete()`` returned, or a client that vanished before the
    body started). Closing an un-started async generator never enters its body,
    which would leave selection running and a committed speculation idling
    until its claim timeout. This wrapper cancels prep in exactly that window;
    once iteration has started, closure is delegated to the generator, whose
    finally-chain already cancels prep and closes the provider.
    """

    def __init__(
        self,
        gen: AsyncGenerator[str, None],
        prep_task: asyncio.Task[_PreparedTurn],
    ) -> None:
        self._gen = gen
        self._prep_task = prep_task
        self._started = False

    def __aiter__(self) -> _PrepOwnedStream:
        return self

    async def __anext__(self) -> str:
        self._started = True
        return await self._gen.__anext__()

    async def aclose(self) -> None:
        if not self._started:
            self._prep_task.cancel()
            await asyncio.gather(self._prep_task, return_exceptions=True)
            if (
                self._prep_task.done()
                and not self._prep_task.cancelled()
                and self._prep_task.exception() is None
            ):
                # Prep finished before the cancel landed: nobody will ever claim
                # its speculation, so discard it now instead of letting the
                # producer idle out its claim timeout.
                prepared = self._prep_task.result()
                if prepared.speculation is not None:
                    await prepared.speculation.discard()
        await self._gen.aclose()


class _SpeculationBufferOverflow(Exception):
    """A pre-handoff provider event exceeded the private byte budget."""


class _SpeculativeStream(ProviderStream):
    """A private, bounded producer for a possible main-stream winner.

    The producer is the sole reader of the upstream ``ProviderStream``. While the
    selector is pending it advances generation into a byte-and-chunk bounded
    deque; after an exact selection match, ``_stream_body`` claims this object and
    consumes that deque followed by live chunks through the same iterator. A
    mismatch (or abandoned handoff) cancels the producer and awaits its upstream
    close, so no half-open request or unobserved task survives.

    No public wire object is built here. Chunks, errors, and usage remain private
    until the orchestrator has committed the authoritative selector result.
    """

    def __init__(
        self,
        *,
        request: ProviderChatRequest,
        open_stream: Callable[
            [ProviderChatRequest],
            Awaitable[tuple[ProviderStream, dict[str, Any] | None]],
        ],
        max_chunks: int,
        max_bytes: int,
        claim_timeout_seconds: float,
        request_id: str,
        release_slot: Callable[[], None],
    ) -> None:
        self._request = request
        self._open_stream = open_stream
        self._max_chunks = max_chunks
        self._max_bytes = max_bytes
        self._claim_timeout_seconds = claim_timeout_seconds
        self._request_id = request_id
        self._release_slot_callback = release_slot
        self._slot_released = False
        self._cleanup_pending: set[asyncio.Task[Any]] = set()

        self._condition = asyncio.Condition()
        self._buffer: deque[tuple[dict[str, Any], int]] = deque()
        self._buffered_bytes = 0
        self._peak_buffered_bytes = 0
        self._peak_buffered_chunks = 0
        self._prefetched_chunks = 0
        self._prefetched_bytes = 0
        self._done = False
        self._error: BaseException | None = None
        self._discarded = False
        self._claimed = False
        self._committed = False
        self._stream_completed = False
        self._upstream_opened = False
        self.claim_failure_reason: str | None = None
        self._started_at = time.perf_counter()
        self.open_ms: int | None = None
        self.usage: ProviderUsage | None = None
        self.endpoint: str | None = None
        self._expiry_task: asyncio.Task[None] | None = None
        self._producer_task = asyncio.create_task(
            self._produce(), name=f"skill-speculation:{request_id}"
        )
        self._producer_task.add_done_callback(self._consume_task_exception)

    @property
    def upstream_opened(self) -> bool:
        """Whether an upstream stream actually opened — i.e. whether this
        speculation could have cost the caller anything at all.

        The distinction matters only for accounting, and it cuts both ways. An
        open that generated tokens bills the caller whether or not a byte of it
        was ever used, so throwing it away without counting it under-reports
        their turn. An open that never happened bills nothing, so counting it as
        unmeasurable spend would mark an exact total as a floor. ``usage`` alone
        cannot tell the two apart: a stream cancelled mid-flight and a stream
        that never opened both report ``None``.
        """
        return self._upstream_opened

    @staticmethod
    def _consume_task_exception(task: asyncio.Task[Any]) -> None:
        if not task.cancelled():
            task.exception()

    @staticmethod
    def _chunk_bytes(chunk: dict[str, Any]) -> int:
        # This is also the representation used on the upstream/public JSON wires,
        # making it a useful deterministic memory-pressure proxy.
        return len(
            json.dumps(chunk, separators=(",", ":"), ensure_ascii=False, default=str).encode(
                "utf-8"
            )
        )

    async def _put(self, chunk: dict[str, Any]) -> bool:
        size = self._chunk_bytes(chunk)
        # Before handoff this is optional work: fail the private attempt rather
        # than let one malicious/malformed event defeat its byte budget. ``claim``
        # observes the error and opens canonically. After claim the source is the
        # canonical response, so an indivisible event must be delivered intact.
        if size > self._max_bytes and not self._claimed:
            raise _SpeculationBufferOverflow
        async with self._condition:
            await self._condition.wait_for(
                lambda: self._discarded
                or (
                    len(self._buffer) < self._max_chunks
                    and (
                        not self._buffer
                        or self._buffered_bytes + size <= self._max_bytes
                    )
                )
            )
            if self._discarded:
                return False
            self._buffer.append((chunk, size))
            self._buffered_bytes += size
            self._prefetched_chunks += 1
            self._prefetched_bytes += size
            self._peak_buffered_chunks = max(
                self._peak_buffered_chunks, len(self._buffer)
            )
            self._peak_buffered_bytes = max(
                self._peak_buffered_bytes, self._buffered_bytes
            )
            self._condition.notify_all()
            return True

    async def _set_terminal(self, error: BaseException | None) -> None:
        async with self._condition:
            if error is not None:
                self._error = error
            self._done = True
            self._condition.notify_all()

    async def _produce(self) -> None:
        upstream: ProviderStream | None = None
        error: BaseException | None = None
        try:
            upstream, first_event = await self._open_stream(self._request)
            self._upstream_opened = True
            self.open_ms = round((time.perf_counter() - self._started_at) * 1000)
            # Mirror the serving endpoint out as soon as the open completes: the
            # claim path logs it, and waiting for the `finally` (where usage is
            # published) would always be too late for that line.
            self.endpoint = upstream.endpoint
            if first_event is not None and not await self._put(first_event):
                return
            async for chunk in upstream:
                if not await self._put(chunk):
                    return
            self._stream_completed = True
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # propagated privately by ``__anext__``
            error = exc
            # Publish the failure before close cleanup so claim can distinguish an
            # exhausted pre-open retry budget (preserve as terminal) from a private
            # post-open failure (safe to replay through one canonical fallback).
            async with self._condition:
                self._error = exc
                self._condition.notify_all()
        finally:
            if upstream is not None:
                self.usage = upstream.usage
                self.endpoint = upstream.endpoint or self.endpoint
                try:
                    async with asyncio.timeout(_SPECULATION_CLOSE_TIMEOUT_SECONDS):
                        await upstream.aclose()
                except TimeoutError:
                    logger.warning(
                        "skill_speculation_close_timeout",
                        request_id=self._request_id,
                        timeout_ms=round(_SPECULATION_CLOSE_TIMEOUT_SECONDS * 1000),
                    )
                except Exception as exc:  # pragma: no cover - provider close is best-effort
                    logger.warning(
                        "skill_speculation_close_failed",
                        request_id=self._request_id,
                        error_type=type(exc).__name__,
                    )
            await self._set_terminal(error)

    def commit(self) -> None:
        """Transfer the matching source toward the public iterator.

        The iterator does not actually own it until ``claim`` runs. The deadline
        closes it if FastAPI/the client never begins body iteration.
        """
        if self._discarded or self._committed:
            return
        self._committed = True
        self._expiry_task = asyncio.create_task(
            self._expire_unclaimed(), name=f"skill-speculation-handoff:{self._request_id}"
        )

    async def _expire_unclaimed(self) -> None:
        try:
            await asyncio.sleep(self._claim_timeout_seconds)
            if not self._claimed:
                await self.discard()
                logger.info(
                    "skill_speculation_abandoned",
                    request_id=self._request_id,
                    claim_timeout_ms=round(self._claim_timeout_seconds * 1000),
                )
        except asyncio.CancelledError:
            return

    async def claim(self) -> bool:
        """Claim a committed winner, or decline a failed/expired source.

        An exhausted open/prefetch retry budget is claimed and preserved as the
        terminal SSE result after authoritative metadata; it never receives a
        race-dependent second budget. A private post-open failure or buffer-cap
        overflow declines the source so the caller can open canonically. Once
        claimed, later failures keep ordinary terminal SSE semantics.
        """
        if self._discarded or not self._committed:
            self.claim_failure_reason = (
                "discarded" if self._discarded else "not_committed"
            )
            return False
        self._claimed = True
        expiry = self._expiry_task
        if expiry is not None and expiry is not asyncio.current_task():
            expiry.cancel()
            await asyncio.gather(expiry, return_exceptions=True)
        if self._error is not None:
            # `_open_stream_with_retry` already spent the canonical retry budget.
            # Preserve that exhausted error for SSE after authoritative `meta`
            # instead of race-dependently granting a second full budget. A private
            # failure after a stream DID open is replay-safe (no model bytes were
            # public), so it may still fall back to one fresh canonical stream.
            if isinstance(self._error, DaintreeError) and not self._upstream_opened:
                self.claim_failure_reason = "upstream_open_exhausted"
                self._release_slot()
                return True
            self.claim_failure_reason = (
                "buffer_overflow"
                if isinstance(self._error, _SpeculationBufferOverflow)
                else "private_stream_error"
            )
            await self.discard()
            return False
        # Claimed means the request is now the one canonical main stream, not an
        # overlapping speculative attempt. Its bounded buffer is immediately
        # owned/drained by the public iterator, so release overlap capacity here.
        self._release_slot()
        return True

    async def prefetch_first_event(self) -> dict[str, Any] | None:
        try:
            return await self.__anext__()
        except StopAsyncIteration:
            return None

    def __aiter__(self) -> _SpeculativeStream:
        return self

    async def __anext__(self) -> dict[str, Any]:
        async with self._condition:
            await self._condition.wait_for(lambda: bool(self._buffer) or self._done)
            if self._buffer:
                chunk, size = self._buffer.popleft()
                self._buffered_bytes -= size
                self._condition.notify_all()
                return chunk
            if self._error is not None:
                raise self._error
            raise StopAsyncIteration

    async def discard(self) -> None:
        """Cancel production, await closure, and make buffered chunks unreachable."""
        # Mark synchronously before the first await. This makes expiry vs claim an
        # atomic event-loop transition: either claim sets ``_claimed`` first and
        # expiry does nothing, or expiry/discard sets ``_discarded`` first and a
        # late claim cleanly falls back to the canonical request.
        newly_discarded = not self._discarded
        self._discarded = True
        if newly_discarded:
            async with self._condition:
                self._buffer.clear()
                self._buffered_bytes = 0
                self._condition.notify_all()

        current = asyncio.current_task()
        expiry = self._expiry_task
        if expiry is not None and expiry is not current:
            expiry.cancel()
        producer = self._producer_task
        if not producer.done() and producer is not current:
            producer.cancel()

        waits = [
            task
            for task in (producer, expiry)
            if task is not None and task is not current
        ]
        defer_slot_release = False
        try:
            if waits:
                done, pending = await asyncio.wait(
                    waits, timeout=_SPECULATION_CLOSE_TIMEOUT_SECONDS
                )
                for task in done:
                    if not task.cancelled():
                        task.exception()
                if pending:
                    defer_slot_release = self._defer_slot_release(waits)
                    logger.warning(
                        "skill_speculation_cleanup_timeout",
                        request_id=self._request_id,
                        timeout_ms=round(_SPECULATION_CLOSE_TIMEOUT_SECONDS * 1000),
                    )
        except BaseException:
            # A caller cancellation must not make overlap capacity available while
            # a cancellation-resistant producer still owns an upstream request.
            defer_slot_release = self._defer_slot_release(waits)
            raise
        finally:
            # A loser occupies overlap capacity until its upstream has actually
            # closed. Idempotence also covers selector/request cancellation races.
            if not defer_slot_release:
                self._release_slot()

    async def aclose(self) -> None:
        await self.discard()

    def _release_slot(self) -> None:
        if self._slot_released:
            return
        self._slot_released = True
        self._release_slot_callback()

    def _defer_slot_release(self, tasks: list[asyncio.Task[Any]]) -> bool:
        pending = {task for task in tasks if not task.done()}
        if not pending:
            return False
        for task in pending - self._cleanup_pending:
            task.add_done_callback(self._cleanup_finished)
        self._cleanup_pending.update(pending)
        return True

    def _cleanup_finished(self, task: asyncio.Task[Any]) -> None:
        if not task.cancelled():
            task.exception()
        self._cleanup_pending.discard(task)
        if not self._cleanup_pending:
            self._release_slot()

    def stats(self) -> dict[str, Any]:
        return {
            "open_ms": self.open_ms,
            "buffered_chunks": len(self._buffer),
            "buffered_bytes": self._buffered_bytes,
            "peak_buffered_chunks": self._peak_buffered_chunks,
            "peak_buffered_bytes": self._peak_buffered_bytes,
            "prefetched_chunks": self._prefetched_chunks,
            "prefetched_bytes": self._prefetched_bytes,
            "stream_completed": self._stream_completed,
        }

