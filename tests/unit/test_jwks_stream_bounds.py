"""Bound identity-provider reads before they can monopolise authentication."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator

import httpx
import pytest

from daintree_assistant_server.services import supabase_auth
from daintree_assistant_server.services.supabase_auth import JwksCache, TokenError


class ChunkStream(httpx.AsyncByteStream):
    def __init__(self, *, stall: bool = False) -> None:
        self.reads = 0
        self.closed = False
        self.stall = stall

    async def __aiter__(self) -> AsyncIterator[bytes]:
        for _ in range(8):
            self.reads += 1
            if self.stall:
                await asyncio.Event().wait()
            yield b" " * (supabase_auth.MAX_JWKS_BYTES // 2)

    async def aclose(self) -> None:
        self.closed = True


@pytest.mark.parametrize("declared", [None, "999999999"])
async def test_oversized_jwks_stops_reading_and_closes_the_response(declared: str | None) -> None:
    stream = ChunkStream()
    headers = {"Content-Length": declared} if declared else {}
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(lambda _: httpx.Response(200, headers=headers, stream=stream))
    ) as client:
        cache = JwksCache(url="https://issuer.example.test/jwks", client=client)
        with pytest.raises(TokenError) as error:
            await cache.get("test-key")
    assert error.value.code == "auth_dependency_unavailable"
    assert stream.reads == (0 if declared else 3)
    assert stream.closed


async def test_stalled_jwks_releases_the_refresh_lock(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(supabase_auth, "JWKS_FETCH_TIMEOUT_SECONDS", 0.01, raising=False)
    stream = ChunkStream(stall=True)
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(lambda _: httpx.Response(200, stream=stream))
    ) as client:
        cache = JwksCache(url="https://issuer.example.test/jwks", client=client)
        with pytest.raises(TokenError) as error:
            await asyncio.wait_for(cache.get("test-key"), timeout=1)
    assert error.value.code == "auth_dependency_unavailable"
    assert stream.closed
    assert not cache._lock.locked()
