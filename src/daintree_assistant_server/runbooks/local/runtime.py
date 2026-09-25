"""The inference worker pool that sits between async request handlers and ONNX.

## Why a pool at all

Cloud Run runs this service at ``--concurrency=8`` on 1 vCPU. ONNX inference is
CPU-bound and synchronous: calling it directly from the event loop would block
every other coroutine on the instance for the duration — not just the turn doing
the ranking, but the seven others streaming responses. So inference has to leave
the loop.

It also must not fan out without bound. Eight concurrent requests each starting a
forward pass on one core do not run eight times faster; they run slower than one
at a time, because the core context-switches instead of computing. The pool is
what converts unbounded concurrency into bounded parallelism.

``onnxruntime`` releases the GIL inside ``Session.run``, so a *thread* pool gives
real parallelism here — no processes, no shared-memory marshalling, no second
copy of the weights per worker. Sessions are thread-safe for concurrent ``run``
calls, so one shared session keeps resident memory flat as the pool grows.

## Why it fails open

Every rejection path — queue full, wait exceeded, inference raised — returns
``None`` rather than raising. That mirrors the posture the prompt-based selector
already has: selection failing degrades the turn to the prior active set and
never blocks the user. A local ranker that could 500 a turn would be a strictly
worse component than the network call it replaces.
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from typing import TypeVar

from ...observability import get_logger

_log = get_logger("daintree.runbooks.inference")

T = TypeVar("T")

# Worker threads. Matched to the container's CPU allocation (--cpu=1), NOT to
# the host's core count: os.cpu_count() inside a Cloud Run container reports the
# NODE's cores, so sizing from it would spawn a dozen workers to share one.
#
# One thread per vCPU, with each ONNX session pinned to one intra-op thread
# (``setwise._session``), gives exactly as many concurrent forward
# passes as there are cores to run them on. More workers than cores does not add
# throughput here — it adds context switching to work that is already saturating
# the core it has.
WORKER_THREADS = 1

# How many searches may be admitted (running or waiting) before new ones are
# refused. Must be at least Cloud Run's `--concurrency` (cloudbuild.yaml,
# pinned by tests/unit/test_runtime_sizing.py), so the queue is never the
# binding constraint: every request the platform lets in gets a place.
#
# Measured on one worker thread (2026-09-25): ~12ms of CPU per search, and
# throughput is flat at ~87/s from 2 concurrent requests to 32. Concurrency
# past that point buys no throughput — it only trades queueing latency (p50
# 181ms at 16 in flight, 359ms at 32) for fewer instances.
QUEUE_DEPTH = 16

# Wall-clock ceiling on one ranking, queue wait included.
#
# A bound on pathology, not on load: a full queue of QUEUE_DEPTH searches at
# ~12ms each drains in ~200ms (allow ~2x for a slower server core), and a
# timeout returns a retryable tool error rather than hanging the caller. When
# it fires, the slot stays held until the thread actually finishes (see
# ``run``), so a wedged worker drains admission instead of poisoning every
# later search with the same wait.
RANK_TIMEOUT_SECONDS = 1.5


@dataclass(frozen=True)
class PoolStats:
    """What the pool is doing, for logs and the benchmark harness."""

    submitted: int
    completed: int
    rejected_full: int
    timed_out: int
    failed: int
    #: How long admitted work spent waiting for a worker, in milliseconds.
    #:
    #: The span the classifier's own timings cannot see, and the one that
    #: separates "the model got slower" from "the instance got busier" — a
    #: distinction a single aggregate ``selection_ms`` erases, and the two want
    #: opposite responses. Max as well as mean because contention is a tail
    #: property: eight simultaneous rankings on one vCPU measured a 280ms worst
    #: case against a p50 in the low teens, and a mean hides that entirely.
    queue_wait_ms_total: float = 0.0
    queue_wait_ms_max: float = 0.0
    #: How many admitted jobs actually STARTED. The mean's denominator: dividing
    #: by ``completed`` would inflate it by every job that timed out or raised
    #: after waiting, which are exactly the jobs a contention investigation
    #: cares about.
    queue_waits: int = 0

    @property
    def degraded(self) -> int:
        return self.rejected_full + self.timed_out + self.failed

    @property
    def queue_wait_ms_mean(self) -> float:
        return self.queue_wait_ms_total / self.queue_waits if self.queue_waits else 0.0


class InferencePool:
    """Bounded thread pool for CPU-bound ranking work."""

    def __init__(
        self,
        *,
        workers: int = WORKER_THREADS,
        queue_depth: int = QUEUE_DEPTH,
        timeout_seconds: float = RANK_TIMEOUT_SECONDS,
    ) -> None:
        self._executor = ThreadPoolExecutor(
            max_workers=workers, thread_name_prefix="runbook-rank"
        )
        # The semaphore counts ADMITTED work, not running work: queue_depth
        # includes the `workers` currently executing. Bounding admission rather
        # than the executor's own queue is what makes rejection immediate — an
        # unbounded ThreadPoolExecutor queue would accept everything and only
        # reveal the overload as latency.
        self._slots = asyncio.Semaphore(queue_depth)
        self._queue_depth = queue_depth
        self._timeout = timeout_seconds
        self._submitted = 0
        self._completed = 0
        self._rejected_full = 0
        self._timed_out = 0
        self._failed = 0
        self._queue_wait_ms_total = 0.0
        self._queue_wait_ms_max = 0.0
        self._queue_waits = 0
        self._closed = False

    async def run(self, fn: Callable[[], T]) -> T | None:
        """Run ``fn`` on a worker. ``None`` means the caller should fail open."""
        if self._closed:
            return None
        self._submitted += 1

        if self._slots.locked():
            # Every admission slot is taken. This is not an ordinary busy moment:
            # QUEUE_DEPTH matches Cloud Run's own --concurrency, so reaching it
            # means more rankings are in flight than there are turns on the
            # instance. Refuse rather than grow an unbounded backlog.
            #
            # Note this is NOT the "instance is busy" path — that one queues, and
            # should, because the alternative to a short wait is a ~2.0s upstream
            # call (see RANK_TIMEOUT_SECONDS).
            self._rejected_full += 1
            _log.warning("runbook_rank_pool_full", depth=self._queue_depth)
            return None

        await self._slots.acquire()
        loop = asyncio.get_running_loop()

        # THE WAIT IS INSIDE THE EXECUTOR, not on the semaphore.
        #
        # Timing `acquire()` measures nothing, which is how this was written
        # first. The semaphore holds `queue_depth` permits and the `locked()`
        # check above refuses the caller outright when they are gone, so
        # `acquire()` either succeeds immediately or never runs. The real
        # contention — the thing worth a metric — is the executor's own queue:
        # `WORKER_THREADS` is 1, so the second concurrent ranking waits for the
        # first forward pass to finish, and none of that is visible from out here.
        #
        # So the clock is started at submission and stopped by the worker itself,
        # on the thread, immediately before `fn` runs. Written from the worker
        # thread and read from the loop; a float assignment is atomic under the
        # GIL and this is a diagnostic counter, so no lock.
        submitted_at = time.perf_counter()

        def timed() -> T:
            waited_ms = (time.perf_counter() - submitted_at) * 1000.0
            self._queue_wait_ms_total += waited_ms
            self._queue_wait_ms_max = max(self._queue_wait_ms_max, waited_ms)
            self._queue_waits += 1
            return fn()

        future = loop.run_in_executor(self._executor, timed)
        # The slot is released when the UNDERLYING work finishes, not when this
        # coroutine stops waiting for it. asyncio's timeout cannot interrupt a
        # thread mid-forward-pass; releasing in a `finally` here meant a wedged
        # inference freed its slot while still occupying the worker, so every
        # subsequent turn was admitted behind it, burned the full timeout, and
        # failed open — an unbounded queue wearing a bounded one's clothes.
        # Tied to the future, a wedged worker keeps its slot, admission dries up
        # to QUEUE_DEPTH-1 immediately, and overflow turns get their instant
        # fail-open instead of a guaranteed timeout.
        def _release(f: asyncio.Future[T]) -> None:
            # Consume the exception so an error that arrives AFTER our timeout
            # does not log "exception was never retrieved" from the loop.
            if not f.cancelled():
                f.exception()
            self._slots.release()

        future.add_done_callback(_release)
        try:
            result = await asyncio.wait_for(asyncio.shield(future), timeout=self._timeout)
            # Counted here rather than in an `else:` clause — a `try` body that
            # returns skips `else` entirely, so the completion counter would have
            # read zero forever while the pool worked perfectly.
            self._completed += 1
            return result
        except TimeoutError:
            self._timed_out += 1
            _log.warning("runbook_rank_timeout", timeout_s=self._timeout)
            return None
        except Exception:  # noqa: BLE001 - fail open on anything the model does
            self._failed += 1
            _log.warning("runbook_rank_failed", exc_info=True)
            return None

    def stats(self) -> PoolStats:
        return PoolStats(
            submitted=self._submitted,
            completed=self._completed,
            rejected_full=self._rejected_full,
            timed_out=self._timed_out,
            failed=self._failed,
            queue_wait_ms_total=round(self._queue_wait_ms_total, 3),
            queue_wait_ms_max=round(self._queue_wait_ms_max, 3),
            queue_waits=self._queue_waits,
        )

    def close(self) -> None:
        """Stop accepting work and release the threads.

        ``wait=False`` because a shutdown must not block on an in-flight forward
        pass. The sessions are owned by the ranker, not by the pool, and a
        half-finished ranking has no side effects to unwind.
        """
        self._closed = True
        self._executor.shutdown(wait=False, cancel_futures=True)
