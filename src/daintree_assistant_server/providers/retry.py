"""Shared transient-error retry for upstream provider calls.

Every DeepSeek request — the main completion, the stream open (before the first
model delta), the skill selector, and the utility tasks — funnels through
:func:`retry_upstream` so the retry policy is uniform and config-driven
(``MAX_UPSTREAM_RETRIES`` + ``RETRY_BACKOFF_BASE_SECONDS``).

Only :class:`DaintreeError` marked ``retryable`` is retried (transient failures:
5xx, timeouts, rate limits, transport errors); client errors propagate
immediately. Retrying is only ever safe before model output is emitted (the
stream's metadata-only progress event may already be public); a mid-generation
failure stays terminal because conversation content has already been delivered
and must not be replayed.

When the upstream supplied a ``Retry-After`` header (surfaced on the error's
``headers``), it is honored: if it is larger than the computed backoff, the wait
becomes ``min(retry_after, 10s)`` — the provider knows its own recovery window
better than our exponential guess, but we never stall a caller more than 10s.
"""

from __future__ import annotations

import asyncio
import random
from collections.abc import Awaitable, Callable

from ..errors import DaintreeError

_MAX_BACKOFF_SECONDS = 5.0
# Hard cap on an upstream-directed Retry-After wait; anything longer is the
# provider telling us to go away for a while, and the caller's turn cannot wait.
_MAX_RETRY_AFTER_SECONDS = 10.0


def _retry_after_seconds(exc: DaintreeError) -> float | None:
    """Extract a usable ``Retry-After`` delay (seconds) from an error, if any.

    Only the delta-seconds form is honored; the HTTP-date form (rare from model
    providers) and garbage values fall back to the computed backoff. The value
    is capped at ``_MAX_RETRY_AFTER_SECONDS``.
    """
    raw = exc.headers.get("Retry-After")
    if not raw:
        return None
    try:
        seconds = float(raw)
    except ValueError:
        return None
    if seconds <= 0:
        return None
    return min(seconds, _MAX_RETRY_AFTER_SECONDS)


def _next_delay(attempt: int, base_seconds: float, retry_after: float | None) -> float:
    """Compute the wait before the next attempt.

    Exponential backoff with additive jitter: ``base * 2**attempt`` capped at
    ``_MAX_BACKOFF_SECONDS``, plus up to one extra ``base`` of jitter to
    de-synchronize concurrent retriers. ``base_seconds <= 0`` disables the
    backoff. A (pre-capped) upstream ``retry_after`` larger than the computed
    backoff replaces it.
    """
    delay = 0.0
    if base_seconds > 0:
        delay = min(base_seconds * (2**attempt), _MAX_BACKOFF_SECONDS)
        delay += random.uniform(0, base_seconds)  # noqa: S311 - jitter, not security-sensitive
    if retry_after is not None and retry_after > delay:
        delay = retry_after
    return delay


async def retry_upstream[T](
    operation: Callable[[], Awaitable[T]],
    *,
    retries: int,
    backoff_base_seconds: float,
) -> T:
    """Run ``operation``, retrying transient upstream failures.

    Makes ``1 + max(0, retries)`` attempts. Re-raises immediately on a
    non-retryable :class:`DaintreeError`, and re-raises the last error once the
    attempts are exhausted.
    """
    attempts = 1 + max(0, retries)
    last: DaintreeError | None = None
    for attempt in range(attempts):
        try:
            return await operation()
        except DaintreeError as exc:
            last = exc
            if not exc.retryable or attempt == attempts - 1:
                raise
            delay = _next_delay(attempt, backoff_base_seconds, _retry_after_seconds(exc))
            if delay > 0:
                await asyncio.sleep(delay)
    assert last is not None  # pragma: no cover - loop always returns or raises
    raise last
