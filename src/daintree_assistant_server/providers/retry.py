"""Shared transient-error retry for upstream provider calls.

Every DeepSeek request — the main completion, the stream open (before the first
model delta), the runbook selector, and the utility tasks — funnels through
:func:`retry_upstream` so the retry policy is uniform and config-driven
(``MAX_UPSTREAM_RETRIES`` + ``RETRY_BACKOFF_BASE_SECONDS``).

Only :class:`DaintreeError` marked ``retryable`` is retried (transient failures:
5xx, timeouts, rate limits, transport errors); client errors propagate
immediately. Retrying is only ever safe before model output is emitted (the
stream's metadata-only progress event may already be public); a mid-generation
failure stays terminal because conversation content has already been delivered
and must not be replayed.

When the upstream supplied a ``Retry-After`` header (surfaced on the error's
``headers``), it is authoritative: the provider knows its own recovery window
better than our exponential guess, so its (finite, non-negative) value replaces
the computed backoff — including ``0`` (retry immediately). A directive LONGER
than 10s means the provider wants us gone for longer than a live turn can wait,
so the attempt budget is abandoned and the error propagates instead of retrying
early into a guaranteed second rejection.
"""

from __future__ import annotations

import asyncio
import math
import random
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime

from ..errors import DaintreeError

_MAX_BACKOFF_SECONDS = 5.0
# Hard ceiling on an upstream-directed Retry-After wait; a longer directive
# aborts the retry budget entirely (see module docstring).
_MAX_RETRY_AFTER_SECONDS = 10.0
# Least time in which a retry could plausibly do anything. Below this, starting
# another attempt spends the remaining budget to reach the same failure.
_MIN_ATTEMPT_SECONDS = 1.0


def _retry_after_seconds(exc: DaintreeError) -> float | None:
    """Extract the ``Retry-After`` delay (seconds) from an error, if any.

    Handles both RFC forms — delta-seconds (leniently: floats accepted) and
    HTTP-date. Returns ``None`` for absent, unparseable, non-finite, or negative
    values; the caller falls back to computed backoff. The value is NOT capped
    here — the caller compares it against ``_MAX_RETRY_AFTER_SECONDS`` to decide
    between honoring it and giving up.
    """
    raw = exc.headers.get("Retry-After")
    if not raw:
        return None
    try:
        seconds = float(raw)
    except ValueError:
        try:
            when = parsedate_to_datetime(raw)
        except (TypeError, ValueError):
            return None
        if when.tzinfo is None:
            when = when.replace(tzinfo=UTC)
        seconds = (when - datetime.now(UTC)).total_seconds()
    if not math.isfinite(seconds) or seconds < 0:
        return None
    return seconds


def _next_delay(attempt: int, base_seconds: float, retry_after: float | None) -> float:
    """Compute the wait before the next attempt.

    A provider-directed ``retry_after`` (already validated and within the cap)
    is authoritative, including ``0.0``. Otherwise: exponential backoff with
    additive jitter — ``base * 2**attempt`` capped at ``_MAX_BACKOFF_SECONDS``,
    plus up to one extra ``base`` of jitter to de-synchronize concurrent
    retriers. ``base_seconds <= 0`` disables the backoff.
    """
    if retry_after is not None:
        return retry_after
    delay = 0.0
    if base_seconds > 0:
        delay = min(base_seconds * (2**attempt), _MAX_BACKOFF_SECONDS)
        delay += random.uniform(0, base_seconds)  # noqa: S311 - jitter, not security-sensitive
    return delay


async def retry_upstream[T](
    operation: Callable[[], Awaitable[T]],
    *,
    retries: int,
    backoff_base_seconds: float,
    remaining_seconds: Callable[[], float] | None = None,
) -> T:
    """Run ``operation``, retrying transient upstream failures.

    Makes ``1 + max(0, retries)`` attempts. Re-raises immediately on a
    non-retryable :class:`DaintreeError`, and re-raises the last error once the
    attempts are exhausted.

    ``remaining_seconds`` reports the turn's remaining budget. When supplied, the
    attempts **share** it rather than each getting a fresh one — the difference
    between a bounded system and one that merely looks bounded. Without it, N
    attempts at T seconds each cost N×T, which is exactly how a per-attempt
    timeout composes into an unbounded turn. Optional so standalone tooling
    (benchmarks, live tests) can retry without inventing a budget.
    """
    attempts = 1 + max(0, retries)
    last: DaintreeError | None = None
    for attempt in range(attempts):
        # Checked at the TOP of each retry, not only before sleeping. The budget
        # can be exhausted by the sleep itself or by event-loop delay, and an
        # attempt started with no time left spends the caller's remaining budget
        # only to reach the same failure — replacing a clean, attributable
        # deadline error with a confusing upstream one.
        out_of_time = (
            attempt > 0
            and remaining_seconds is not None
            and remaining_seconds() < _MIN_ATTEMPT_SECONDS
        )
        if out_of_time:
            assert last is not None
            raise last
        try:
            return await operation()
        except DaintreeError as exc:
            last = exc
            if not exc.retryable or attempt == attempts - 1:
                raise
            retry_after = _retry_after_seconds(exc)
            if retry_after is not None and retry_after > _MAX_RETRY_AFTER_SECONDS:
                # A fixed policy, not a budget decision: the provider asked for a
                # longer pause than a live turn can absorb, so retrying earlier
                # would just buy a second rejection. Applies even with plenty of
                # budget left.
                raise
            delay = _next_delay(attempt, backoff_base_seconds, retry_after)
            # Sleeping past the deadline turns a clean, attributable timeout into
            # a confusing upstream error raised after the turn was already over.
            # Surface the failure we have instead.
            if (
                remaining_seconds is not None
                and remaining_seconds() < delay + _MIN_ATTEMPT_SECONDS
            ):
                raise
            if delay > 0:
                await asyncio.sleep(delay)
    assert last is not None  # pragma: no cover - loop always returns or raises
    raise last
