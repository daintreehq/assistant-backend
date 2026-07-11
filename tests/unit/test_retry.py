"""Unit tests for the shared upstream-retry helper."""

from __future__ import annotations

import pytest

from daintree_assistant_server.errors import (
    DaintreeError,
    upstream_error,
    upstream_rate_limited,
    upstream_timeout,
)
from daintree_assistant_server.providers import retry as retry_module
from daintree_assistant_server.providers.retry import (
    _next_delay,
    _retry_after_seconds,
    retry_upstream,
)


async def _retry(op, retries: int = 3):
    return await retry_upstream(op, retries=retries, backoff_base_seconds=0.0)


@pytest.mark.asyncio
async def test_returns_first_success_without_retry() -> None:
    calls = [0]

    async def op() -> str:
        calls[0] += 1
        return "ok"

    assert await _retry(op) == "ok"
    assert calls[0] == 1


@pytest.mark.asyncio
async def test_retries_transient_then_succeeds() -> None:
    calls = [0]

    async def op() -> str:
        calls[0] += 1
        if calls[0] < 3:
            raise upstream_timeout()
        return "recovered"

    assert await _retry(op) == "recovered"
    assert calls[0] == 3  # 2 failures + success


@pytest.mark.asyncio
async def test_makes_one_plus_retries_attempts_then_raises() -> None:
    calls = [0]

    async def op() -> str:
        calls[0] += 1
        raise upstream_timeout()

    with pytest.raises(DaintreeError) as exc:
        await _retry(op, retries=3)
    assert exc.value.code == "upstream_timeout"
    assert calls[0] == 4  # 1 try + 3 retries


@pytest.mark.asyncio
async def test_non_retryable_raises_immediately() -> None:
    calls = [0]

    async def op() -> str:
        calls[0] += 1
        raise upstream_error("bad gateway")  # retryable=False by default

    with pytest.raises(DaintreeError) as exc:
        await _retry(op, retries=3)
    assert exc.value.code == "upstream_error"
    assert calls[0] == 1  # not retried


@pytest.mark.asyncio
async def test_zero_retries_makes_single_attempt() -> None:
    calls = [0]

    async def op() -> str:
        calls[0] += 1
        raise upstream_timeout()

    with pytest.raises(DaintreeError):
        await _retry(op, retries=0)
    assert calls[0] == 1


def test_default_retry_budget_is_one_retry_two_attempts() -> None:
    # The backend owns retries; the shipped posture is ONE retry (2 attempts).
    from daintree_assistant_server.config import Settings

    s = Settings(
        DAINTREE_API_KEY="k", DEEPSEEK_API_KEY="dk", STATE_SIGNING_KEY_CURRENT="sk"
    )
    assert s.max_upstream_retries == 1


# ---- Retry-After handling --------------------------------------------------


def test_retry_after_parsed_from_error_headers() -> None:
    assert _retry_after_seconds(upstream_rate_limited(retry_after="7")) == 7.0


def test_retry_after_capped_at_ten_seconds() -> None:
    assert _retry_after_seconds(upstream_rate_limited(retry_after="120")) == 10.0


def test_retry_after_absent_or_unusable_is_ignored() -> None:
    assert _retry_after_seconds(upstream_timeout()) is None  # no header at all
    # HTTP-date / garbage / non-positive forms fall back to computed backoff.
    assert _retry_after_seconds(upstream_rate_limited(retry_after="Wed, 21 Oct")) is None
    assert _retry_after_seconds(upstream_rate_limited(retry_after="-3")) is None


def test_next_delay_prefers_larger_retry_after() -> None:
    # backoff disabled (base 0) → the upstream hint alone drives the wait.
    assert _next_delay(0, 0.0, 7.0) == 7.0


def test_next_delay_keeps_backoff_when_retry_after_smaller() -> None:
    # base 4.0 at attempt 0 → at least 4.0s of backoff; a 0.5s hint must not shrink it.
    assert _next_delay(0, 4.0, 0.5) >= 4.0


@pytest.mark.asyncio
async def test_retry_sleeps_capped_retry_after(monkeypatch: pytest.MonkeyPatch) -> None:
    sleeps: list[float] = []

    class _FakeAsyncio:
        @staticmethod
        async def sleep(seconds: float) -> None:
            sleeps.append(seconds)

    monkeypatch.setattr(retry_module, "asyncio", _FakeAsyncio())
    calls = [0]

    async def op() -> str:
        calls[0] += 1
        if calls[0] == 1:
            raise upstream_rate_limited(retry_after="120")
        return "ok"

    assert await retry_upstream(op, retries=1, backoff_base_seconds=0.0) == "ok"
    assert sleeps == [10.0]  # honored, but never more than the 10s cap
