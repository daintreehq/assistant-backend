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


def test_retry_after_returned_uncapped_for_caller_decision() -> None:
    # The parser no longer caps; retry_upstream compares against the 10s ceiling
    # and gives up (rather than retrying early) when the directive exceeds it.
    assert _retry_after_seconds(upstream_rate_limited(retry_after="120")) == 120.0


def test_retry_after_http_date_form_is_parsed() -> None:
    from datetime import UTC, datetime, timedelta
    from email.utils import format_datetime

    when = datetime.now(UTC) + timedelta(seconds=5)
    parsed = _retry_after_seconds(upstream_rate_limited(retry_after=format_datetime(when)))
    assert parsed is not None
    assert 3.0 < parsed <= 5.0


def test_retry_after_zero_is_honored() -> None:
    # "Retry-After: 0" means retry immediately — not "fall back to backoff".
    assert _retry_after_seconds(upstream_rate_limited(retry_after="0")) == 0.0
    assert _next_delay(0, 4.0, 0.0) == 0.0


def test_retry_after_absent_or_unusable_is_ignored() -> None:
    assert _retry_after_seconds(upstream_timeout()) is None  # no header at all
    # Garbage / negative / non-finite forms fall back to computed backoff.
    assert _retry_after_seconds(upstream_rate_limited(retry_after="soonish")) is None
    assert _retry_after_seconds(upstream_rate_limited(retry_after="-3")) is None
    assert _retry_after_seconds(upstream_rate_limited(retry_after="nan")) is None
    assert _retry_after_seconds(upstream_rate_limited(retry_after="inf")) is None


def test_next_delay_provider_value_is_authoritative() -> None:
    # backoff disabled (base 0) → the upstream directive alone drives the wait...
    assert _next_delay(0, 0.0, 7.0) == 7.0
    # ...and it REPLACES a larger computed backoff rather than being maxed with it.
    assert _next_delay(0, 4.0, 0.5) == 0.5


@pytest.mark.asyncio
async def test_retry_after_beyond_cap_gives_up_instead_of_early_retry(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    sleeps: list[float] = []

    class _FakeAsyncio:
        @staticmethod
        async def sleep(seconds: float) -> None:
            sleeps.append(seconds)

    monkeypatch.setattr(retry_module, "asyncio", _FakeAsyncio())
    calls = [0]

    async def op() -> str:
        calls[0] += 1
        raise upstream_rate_limited(retry_after="120")

    with pytest.raises(DaintreeError) as exc:
        await retry_upstream(op, retries=1, backoff_base_seconds=0.0)
    assert exc.value.code == "upstream_rate_limited"
    assert calls[0] == 1  # retrying at 10s into a 120s outage buys a second 429
    assert sleeps == []


@pytest.mark.asyncio
async def test_retry_sleeps_provider_directive_within_cap(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
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
            raise upstream_rate_limited(retry_after="2")
        return "ok"

    assert await retry_upstream(op, retries=1, backoff_base_seconds=0.0) == "ok"
    assert sleeps == [2.0]
