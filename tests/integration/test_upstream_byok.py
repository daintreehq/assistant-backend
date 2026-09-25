"""Bring your own key, end to end through the routes.

The unit suite pins the adapters and the router. This pins the part only the
routes can get wrong: that the three headers reach the provider call on BOTH
spend routes, that the server-funded path is untouched when they are absent,
and that a malformed or missing route is refused before anything is spent.
"""

from __future__ import annotations

import json
from typing import Any

import pytest
from conftest import respond_body
from fakes import make_result
from fastapi.testclient import TestClient

from daintree_assistant_server.config import Settings
from daintree_assistant_server.main import build_container, create_app

_BYOK = {
    "X-Daintree-Upstream-Provider": "openai",
    "X-Daintree-Upstream-Model": "gpt-6-luna",
    "X-Daintree-Upstream-Key": "sk-caller-own",
}


def _task_body() -> dict[str, Any]:
    return {"task": "terminal_judge", "input": {"question": "q"}}


def test_a_turn_runs_on_the_callers_host_model_and_key(
    client: TestClient, fake_provider: Any
) -> None:
    fake_provider.complete_result = make_result(content="done")
    r = client.post(
        "/v1/daintree/respond",
        headers=_BYOK,
        json=respond_body([{"role": "user", "content": "rename my terminals"}]),
    )
    assert r.status_code == 200, r.text
    assert fake_provider.requests
    for req in fake_provider.requests:
        assert req.upstream == "openai"
        assert req.model == "gpt-6-luna"
        assert req.api_key is not None
        assert req.api_key.get_secret_value() == "sk-caller-own"
    assert "sk-caller-own" not in r.text


def test_a_task_runs_on_the_callers_host_model_and_key(
    client: TestClient, fake_provider: Any
) -> None:
    fake_provider.complete_result = make_result(
        content=json.dumps({"reason": "ok", "confidence": 0.9, "matched": True})
    )
    r = client.post("/v1/daintree/tasks", headers=_BYOK, json=_task_body())
    assert r.status_code == 200, r.text
    assert fake_provider.requests
    assert all(req.upstream == "openai" for req in fake_provider.requests)
    assert all(req.model == "gpt-6-luna" for req in fake_provider.requests)


def test_without_the_headers_the_server_funded_path_is_unchanged(
    client: TestClient, fake_provider: Any
) -> None:
    fake_provider.complete_result = make_result(content="done")
    r = client.post(
        "/v1/daintree/respond",
        json=respond_body([{"role": "user", "content": "rename my terminals"}]),
    )
    assert r.status_code == 200
    assert all(req.upstream is None and req.api_key is None for req in fake_provider.requests)


@pytest.mark.parametrize("path", ["/v1/daintree/respond", "/v1/daintree/tasks"])
def test_a_host_without_a_key_is_refused_before_anything_is_spent(
    client: TestClient, fake_provider: Any, path: str
) -> None:
    body = respond_body([{"role": "user", "content": "hi"}]) if "respond" in path else _task_body()
    r = client.post(path, headers={"X-Daintree-Upstream-Provider": "baseten"}, json=body)
    assert r.status_code == 400
    assert r.json()["error"]["code"] == "invalid_upstream"
    assert fake_provider.requests == []


def _require_byok_client(settings: Settings, fake_provider: Any, app: Any) -> TestClient:
    strict = settings.model_copy(update={"require_caller_upstream": True})
    application = create_app(
        settings=strict, provider=fake_provider, selector=app.state.injected_selector
    )
    application.state.container = build_container(
        strict, fake_provider, app.state.injected_selector
    )
    application.state.ready = True
    return TestClient(application, raise_server_exceptions=False)


def test_a_byok_only_deployment_refuses_a_turn_without_a_key(
    settings: Settings, fake_provider: Any, app: Any
) -> None:
    with _require_byok_client(settings, fake_provider, app) as client:
        r = client.post(
            "/v1/daintree/respond", json=respond_body([{"role": "user", "content": "hi"}])
        )
        assert r.status_code == 400
        assert r.json()["error"]["code"] == "upstream_required"
        assert fake_provider.requests == []


def test_a_byok_only_deployment_needs_no_server_key() -> None:
    settings = Settings(
        BASETEN_API_KEY="",
        STATE_SIGNING_KEY_CURRENT="k",
        STATE_SALT="s",
        REQUIRE_CALLER_UPSTREAM=True,
    )
    assert "BASETEN_API_KEY" not in settings.missing_secrets()


def test_verify_answers_for_the_callers_own_key(client: TestClient, fake_provider: Any) -> None:
    r = client.post("/v1/daintree/auth/verify", headers=_BYOK)
    assert r.status_code == 200
    body = r.json()
    assert body["valid"] is True
    assert body["server_holds_credential"] is False
    assert body["provider"] == "openai"
    assert "label" not in body
    assert [k.get_secret_value() for k in fake_provider.verify_key_calls] == ["sk-caller-own"]


def test_verify_reports_a_rejected_caller_key_as_a_verdict(
    client: TestClient, fake_provider: Any
) -> None:
    from daintree_assistant_server.errors import invalid_api_key

    fake_provider.verify_key_error = invalid_api_key()
    r = client.post("/v1/daintree/auth/verify", headers=_BYOK)
    assert r.status_code == 200
    assert r.json()["valid"] is False
    assert r.json()["reason"] == "provider_rejected"


def test_a_request_id_that_is_the_upstream_key_is_never_adopted(client: TestClient) -> None:
    r = client.get(
        "/healthz",
        headers={"X-Request-Id": "sk-caller-own", "X-Daintree-Upstream-Key": "sk-caller-own"},
    )
    assert r.headers["X-Request-Id"] != "sk-caller-own"


def test_verify_on_a_byok_only_deployment_needs_the_headers(
    settings: Settings, fake_provider: Any, app: Any
) -> None:
    with _require_byok_client(settings, fake_provider, app) as client:
        r = client.post("/v1/daintree/auth/verify")
        assert r.status_code == 400
        assert r.json()["error"]["code"] == "upstream_required"


def test_openrouter_routing_headers_reach_the_provider_call(
    client: TestClient, fake_provider: Any
) -> None:
    fake_provider.complete_result = make_result(content="done")
    r = client.post(
        "/v1/daintree/respond",
        headers={
            "X-Daintree-Upstream-Provider": "openrouter",
            "X-Daintree-Upstream-Key": "sk-or-caller",
            "X-Daintree-Upstream-Sort": "price",
            "X-Daintree-Upstream-Data-Collection": "deny",
            "X-Daintree-Upstream-Zdr": "true",
        },
        json=respond_body([{"role": "user", "content": "hi"}]),
    )
    assert r.status_code == 200, r.text
    assert fake_provider.requests[-1].upstream_routing == {
        "sort": "price",
        "data_collection": "deny",
        "zdr": True,
    }
