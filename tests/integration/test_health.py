"""Liveness, readiness and version."""

from __future__ import annotations

from typing import Any

from fastapi.testclient import TestClient

from daintree_assistant_server import __version__
from daintree_assistant_server.config import Settings
from daintree_assistant_server.main import create_app


def test_liveness_on_both_paths(client: TestClient) -> None:
    for path in ("/healthz", "/health"):
        assert client.get(path).json() == {"status": "ok"}


def test_ready_reports_both_catalogs(client: TestClient, app: Any) -> None:
    catalogs = app.state.container.catalogs
    body = client.get("/readyz").json()
    assert body == {
        "status": "ready",
        "catalog_revision": catalogs.control.revision,
        "runbooks": len(catalogs.control.selectable_ids),
        "inbox_catalog_revision": catalogs.inbox.revision,
        "inbox_runbooks": len(catalogs.inbox.selectable_ids),
    }


def test_version(client: TestClient) -> None:
    assert client.get("/version").json() == {"server_version": __version__, "build_sha": ""}


def test_root_redirects_a_browser_to_the_product_page(client: TestClient) -> None:
    response = client.get("/", follow_redirects=False)
    assert response.status_code == 302


def test_a_failed_startup_is_unready_not_dead(monkeypatch: Any) -> None:
    def broken(*args: Any, **kwargs: Any) -> Any:
        raise RuntimeError("bad artifact at /secret/path")

    monkeypatch.setattr("daintree_assistant_server.main.build_container", broken)
    for environment, detail in (("development", "bad artifact"), ("production", "startup")):
        app = create_app(settings=Settings(ENVIRONMENT=environment))
        with TestClient(app) as client:
            assert client.get("/healthz").status_code == 200
            ready = client.get("/readyz")
            assert ready.status_code == 503
            assert detail in ready.json()["error"]
            if environment == "production":
                assert "/secret/path" not in ready.text
            search = client.post(
                "/v1/daintree/mcp",
                json={"jsonrpc": "2.0", "id": 1, "method": "tools/list"},
            )
            assert search.status_code == 503


def test_request_id_is_echoed_or_minted(client: TestClient) -> None:
    assert client.get("/healthz", headers={"X-Request-Id": "abc-123"}).headers[
        "x-request-id"
    ] == "abc-123"
    minted = client.get("/healthz", headers={"X-Request-Id": "has spaces\n"}).headers[
        "x-request-id"
    ]
    assert minted.startswith("req_")
