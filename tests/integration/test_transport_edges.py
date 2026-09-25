"""The ASGI edges the MCP route sits behind: body limit and the entrypoint."""

from __future__ import annotations

from typing import Any

import pytest
from fastapi.testclient import TestClient

from daintree_assistant_server import __main__ as entrypoint
from daintree_assistant_server.config import Settings
from daintree_assistant_server.main import BodySizeLimitMiddleware

BODY = b'{"jsonrpc":"2.0","id":1,"method":"ping"}'


def test_a_chunked_body_over_the_limit_is_refused(client: TestClient) -> None:
    def chunks() -> Any:
        for _ in range(80):
            yield b" " * 1024

    response = client.post(
        "/v1/daintree/mcp", content=chunks(), headers={"Content-Type": "application/json"}
    )
    assert response.status_code == 413


def test_a_chunked_body_under_the_limit_is_replayed_intact(client: TestClient) -> None:
    def chunks() -> Any:
        yield BODY[:10]
        yield BODY[10:]

    response = client.post(
        "/v1/daintree/mcp", content=chunks(), headers={"Content-Type": "application/json"}
    )
    assert response.json() == {"jsonrpc": "2.0", "id": 1, "result": {"resultType": "complete"}}


def test_a_malformed_content_length_falls_back_to_counting() -> None:
    sent: list[dict[str, Any]] = []

    async def app(scope: Any, receive: Any, send: Any) -> None:
        message = await receive()
        sent.append(message)

    async def receive() -> dict[str, Any]:
        return {"type": "http.request", "body": b"{}", "more_body": False}

    async def send(message: Any) -> None:
        sent.append(message)

    import asyncio

    scope = {"type": "http", "headers": [(b"content-length", b"not-a-number")]}
    asyncio.run(BodySizeLimitMiddleware(app, max_bytes=1024)(scope, receive, send))
    assert sent == [{"type": "http.request", "body": b"{}", "more_body": False}]


def test_a_disconnect_mid_body_ends_quietly() -> None:
    called: list[bool] = []

    async def app(scope: Any, receive: Any, send: Any) -> None:
        called.append(True)

    async def receive() -> dict[str, Any]:
        return {"type": "http.disconnect"}

    async def send(message: Any) -> None:
        raise AssertionError("nothing should be sent")

    import asyncio

    asyncio.run(BodySizeLimitMiddleware(app, max_bytes=1024)({"type": "http"}, receive, send))
    assert called == []


def test_non_http_scopes_pass_straight_through() -> None:
    seen: list[str] = []

    async def app(scope: Any, receive: Any, send: Any) -> None:
        seen.append(scope["type"])

    import asyncio

    asyncio.run(BodySizeLimitMiddleware(app, max_bytes=1)({"type": "lifespan"}, None, None))
    assert seen == ["lifespan"]


def test_the_entrypoint_binds_the_configured_address(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[dict[str, Any]] = []
    monkeypatch.setattr(entrypoint, "get_settings", lambda: Settings(HOST="0.0.0.0", PORT=9999))
    monkeypatch.setattr(entrypoint.uvicorn, "run", lambda app, **kw: calls.append({"app": app, **kw}))
    entrypoint.main()
    assert calls[0]["app"] == "daintree_assistant_server.main:app"
    assert (calls[0]["host"], calls[0]["port"]) == ("0.0.0.0", 9999)
