"""Unit tests for the shared upstream-retry helper."""

from __future__ import annotations

import pytest

from daintree_assistant_server.errors import DaintreeError, upstream_error, upstream_timeout
from daintree_assistant_server.providers.retry import retry_upstream


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
