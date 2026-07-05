"""Shared transient-error retry for upstream provider calls.

Every DeepSeek request — the main completion, the stream open (before the first
public byte), the skill selector, and the utility tasks — funnels through
:func:`retry_upstream` so the retry policy is uniform and config-driven
(``MAX_UPSTREAM_RETRIES`` + ``RETRY_BACKOFF_BASE_SECONDS``).

Only :class:`DaintreeError` marked ``retryable`` is retried (transient failures:
5xx, timeouts, rate limits, transport errors); client errors propagate
immediately. Retrying is only ever safe *before* any public bytes are emitted —
a mid-stream failure stays terminal by design (the conversation continuation has
already been partially delivered and must not be replayed).
"""

from __future__ import annotations

import asyncio
import random
from collections.abc import Awaitable, Callable

from ..errors import DaintreeError

_MAX_BACKOFF_SECONDS = 5.0


async def _sleep_backoff(attempt: int, base_seconds: float) -> None:
    """Exponential backoff with additive jitter.

    The exponential term ``base * 2**attempt`` is capped at
    ``_MAX_BACKOFF_SECONDS``; up to one extra ``base`` of jitter is then added to
    de-synchronize concurrent retriers. ``base_seconds <= 0`` disables the wait.
    """
    if base_seconds <= 0:
        return
    delay = min(base_seconds * (2**attempt), _MAX_BACKOFF_SECONDS)
    delay += random.uniform(0, base_seconds)  # noqa: S311 - jitter, not security-sensitive
    await asyncio.sleep(delay)


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
            await _sleep_backoff(attempt, backoff_base_seconds)
    assert last is not None  # pragma: no cover - loop always returns or raises
    raise last
