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
copy of the weights per worker. Sessions are shared across workers, which is safe
(see ``onnx_models``) and keeps resident memory flat as the pool grows.

## Why it fails open

Every rejection path — queue full, wait exceeded, inference raised — returns
``None`` rather than raising. That mirrors the posture the prompt-based selector
already has: selection failing degrades the turn to the prior active set and
never blocks the user. A local ranker that could 500 a turn would be a strictly
worse component than the network call it replaces.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from typing import TypeVar

_log = logging.getLogger(__name__)

T = TypeVar("T")

# Worker threads. Matched to the container's CPU allocation (--cpu=1), NOT to
# the host's core count: os.cpu_count() inside a Cloud Run container reports the
# NODE's cores, so sizing from it would spawn a dozen workers to share one.
#
# One thread per vCPU, with each ONNX session pinned to one intra-op thread
# (onnx_models._INTRA_OP_THREADS), gives exactly as many concurrent forward
# passes as there are cores to run them on. More workers than cores does not add
# throughput here — it adds context switching to work that is already saturating
# the core it has.
WORKER_THREADS = 1

# How many requests may be waiting for a worker before new ones are refused.
#
# Sized against the concurrency limit rather than picked round: at
# --concurrency=8 no more than 8 turns exist on an instance at once, so a depth
# of 8 holds every one of them and the queue can never be the binding
# constraint. Rejection here means the instance is in a state Cloud Run's own
# concurrency limit says is impossible.
QUEUE_DEPTH = 8

# Wall-clock ceiling on one ranking, queue wait included.
#
# WAITING IS ALMOST ALWAYS THE RIGHT ANSWER, which is not what the first version
# of this assumed. The alternative to waiting is failing open, and failing open
# costs a ~2.0s upstream selector call (p90 2.8s) — so refusing a request after
# 250ms to "save" it from a queue hands it a 2000ms bill instead of a 300ms one.
# Any timeout below roughly the upstream p50 makes the fallback the slower path
# and the circuit breaker actively harmful.
#
# 1.5s therefore, not 0.25s: still under the call it replaces, while deep enough
# that an eight-deep burst on a single vCPU (measured worst case 280ms here,
# perhaps 1s on a slower server core) rides it out rather than tripping it.
# This only fires when something is genuinely wrong — a wedged session, a
# pathological input — and it matters more once the upstream selector is
# removed, because then there is no fallback at all and this is the only bound.
RANK_TIMEOUT_SECONDS = 1.5


@dataclass(frozen=True)
class PoolStats:
    """What the pool is doing, for logs and the benchmark harness."""

    submitted: int
    completed: int
    rejected_full: int
    timed_out: int
    failed: int

    @property
    def degraded(self) -> int:
        return self.rejected_full + self.timed_out + self.failed


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
            max_workers=workers, thread_name_prefix="skill-rank"
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
            _log.warning("skill_rank_pool_full", extra={"depth": self._queue_depth})
            return None

        await self._slots.acquire()
        loop = asyncio.get_running_loop()
        future = loop.run_in_executor(self._executor, fn)
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
            _log.warning("skill_rank_timeout", extra={"timeout_s": self._timeout})
            return None
        except Exception:  # noqa: BLE001 - fail open on anything the model does
            self._failed += 1
            _log.warning("skill_rank_failed", exc_info=True)
            return None

    def stats(self) -> PoolStats:
        return PoolStats(
            submitted=self._submitted,
            completed=self._completed,
            rejected_full=self._rejected_full,
            timed_out=self._timed_out,
            failed=self._failed,
        )

    def close(self) -> None:
        """Stop accepting work and release the threads.

        ``wait=False`` because a shutdown must not block on an in-flight forward
        pass. The sessions are owned by the ranker, not by the pool, and a
        half-finished ranking has no side effects to unwind.
        """
        self._closed = True
        self._executor.shutdown(wait=False, cancel_futures=True)
