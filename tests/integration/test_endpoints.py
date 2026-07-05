"""Integration tests for health, models, capabilities, skills, and tasks endpoints."""

from __future__ import annotations

import json

from fakes import make_result, tool_call_for

SKILL_PREP = "daintree.workflow.prep-branch-for-review"


def test_healthz(client) -> None:
    r = client.get("/healthz")
    assert r.status_code == 200
    assert r.json() == {"status": "ok"}


def test_readyz(client, app) -> None:
    r = client.get("/readyz")
    assert r.status_code == 200
    body = r.json()
    assert body["status"] == "ready"
    # Assert the endpoint reports the app's ACTUAL catalog size, not a magic number:
    # apply-mode skill learning grows the real catalog, and that must not redden this.
    assert body["skills"] == len(app.state.container.catalog)


def test_version(client) -> None:
    r = client.get("/version")
    assert r.status_code == 200
    assert "server_version" in r.json()
    assert r.json()["protocol"] == {"min": 1, "max": 1}


def test_capabilities(client, auth) -> None:
    r = client.get("/v1/daintree/capabilities", headers=auth)
    body = r.json()
    assert body["respond"]["system_messages_accepted"] is False
    assert body["respond"]["max_active_skills"] == 3
    assert body["respond"]["stream_events"] == ["meta", "status", "delta", "done", "error"]
    assert body["respond"]["thinking_enabled"] is False  # default off; enable via MAIN_THINKING_ENABLED
    assert body["respond"]["multimodal_images"]["remote_urls"] is False
    assert len(body["tasks"]) == 9
    assert body["skills"]["manual_resolve"] is True


def test_skills_list_and_etag(client, auth, app) -> None:
    r = client.get("/v1/daintree/skills", headers=auth)
    assert r.status_code == 200
    body = r.json()
    # The list reports every catalog skill (robust to the catalog growing via learning).
    assert len(body["data"]) == len(app.state.container.catalog)
    assert all("body" not in s for s in body["data"])
    assert all("required_tools" in s for s in body["data"])
    etag = r.headers["etag"]

    r304 = client.get("/v1/daintree/skills", headers={**auth, "If-None-Match": etag})
    assert r304.status_code == 304


def test_skills_resolve_find(client, auth, fake_provider) -> None:
    fake_provider.tool_call_result = tool_call_for([SKILL_PREP])
    r = client.post("/v1/daintree/skills/resolve", headers=auth, json={
        "protocol_version": 1, "session_id": "s", "action": "find", "query": "prep branch",
    })
    assert r.status_code == 200
    body = r.json()
    assert [s["id"] for s in body["skills"]["active"]] == [SKILL_PREP]
    assert len(body["skills"]["prelude"]["tool_executions"]) == 1
    assert body["state"].startswith("dst1.")


def test_skills_resolve_load_and_clear(client, auth, app) -> None:
    r = client.post("/v1/daintree/skills/resolve", headers=auth, json={
        "protocol_version": 1, "session_id": "s", "action": "load", "skill_ids": [SKILL_PREP],
    })
    body = r.json()
    assert [s["id"] for s in body["skills"]["active"]] == [SKILL_PREP]
    state = body["state"]

    r2 = client.post("/v1/daintree/skills/resolve", headers=auth, json={
        "protocol_version": 1, "session_id": "s", "action": "clear", "state": state,
    })
    body2 = r2.json()
    assert body2["skills"]["active"] == []
    assert body2["skills"]["prelude"]["tool_executions"] == []


def test_skills_resolve_find_requires_query(client, auth) -> None:
    r = client.post("/v1/daintree/skills/resolve", headers=auth, json={
        "protocol_version": 1, "session_id": "s", "action": "find",
    })
    assert r.status_code == 400


def test_skills_resolve_load_requires_ids(client, auth) -> None:
    r = client.post("/v1/daintree/skills/resolve", headers=auth, json={
        "protocol_version": 1, "session_id": "s", "action": "load",
    })
    assert r.status_code == 400


def test_tasks_endpoint(client, auth, fake_provider) -> None:
    fake_provider.complete_result = make_result(
        content=json.dumps({"reason": "ok", "confidence": 0.8, "matched": True})
    )
    r = client.post("/v1/daintree/tasks", headers=auth, json={
        "task": "terminal_judge.v1", "input": {"question": "pass?", "tail": "OK"},
    })
    assert r.status_code == 200
    body = r.json()
    assert body["object"] == "daintree.task.result"
    assert body["output"]["matched"] is True
    assert body["prompt_version"] == "terminal_judge.v1"


def test_tasks_unknown_404(client, auth) -> None:
    r = client.post("/v1/daintree/tasks", headers=auth, json={"task": "nope", "input": {}})
    assert r.status_code == 404
    assert r.json()["error"]["code"] == "unknown_task"


def test_tasks_invalid_input_400(client, auth) -> None:
    r = client.post("/v1/daintree/tasks", headers=auth, json={"task": "terminal_judge.v1", "input": {}})
    assert r.status_code == 400


def test_oversized_body_413(client, auth) -> None:
    headers = {**auth, "Content-Length": str(20 * 1024 * 1024)}
    r = client.post("/v1/daintree/respond", headers=headers, content=b'{"protocol_version":1}')
    assert r.status_code == 413


def test_dev_boots_and_serves_unauthenticated_without_api_key() -> None:
    """Local dev with no DAINTREE_API_KEY: boots ready and serves unauthenticated."""
    from fastapi.testclient import TestClient

    from daintree_assistant_server.config import Settings
    from daintree_assistant_server.main import create_app

    settings = Settings(
        ENVIRONMENT="development", DAINTREE_API_KEY="", DEEPSEEK_API_KEY="d",
        STATE_SIGNING_KEY_CURRENT="sk", STATE_SALT="x",
    )
    app = create_app(settings=settings)  # real lifespan (no injected provider)
    with TestClient(app) as client:
        assert app.state.ready is True
        # No Authorization header → allowed in development.
        assert client.get("/v1/daintree/capabilities").status_code == 200


def test_prod_not_ready_without_api_key() -> None:
    from fastapi.testclient import TestClient

    from daintree_assistant_server.config import Settings
    from daintree_assistant_server.main import create_app

    settings = Settings(
        ENVIRONMENT="production", DAINTREE_API_KEY="", DEEPSEEK_API_KEY="d" * 32,
        STATE_SIGNING_KEY_CURRENT="s" * 48, STATE_SALT="a-real-salt-value",
    )
    app = create_app(settings=settings)
    with TestClient(app) as client:
        assert app.state.ready is False
        assert client.get("/readyz").status_code == 503


def test_prod_not_ready_when_skill_learning_enabled(fake_provider) -> None:
    """Skill learning is local-dev-only: enabling it in production fails readiness,
    even with an injected provider (the guard runs before the secret checks)."""
    from fastapi.testclient import TestClient

    from daintree_assistant_server.config import Settings
    from daintree_assistant_server.main import create_app

    settings = Settings(
        ENVIRONMENT="production",
        DAINTREE_API_KEY="x" * 24,
        DEEPSEEK_API_KEY="d" * 32,
        STATE_SIGNING_KEY_CURRENT="s" * 48,
        STATE_SALT="a-real-salt-value",
        SKILL_LEARNING_MODE="apply",
    )
    app = create_app(settings=settings, provider=fake_provider)
    with TestClient(app) as client:
        assert app.state.ready is False
        assert "SKILL_LEARNING_MODE" in (app.state.ready_error or "")
        assert client.get("/readyz").status_code == 503


def test_not_ready_returns_503(fake_provider) -> None:
    """A startup failure (here: empty signing key) leaves the app not-ready."""
    from fastapi.testclient import TestClient

    from daintree_assistant_server.config import Settings
    from daintree_assistant_server.main import create_app

    broken = Settings(
        DAINTREE_API_KEY="test-api-key",
        DEEPSEEK_API_KEY="d",
        STATE_SIGNING_KEY_CURRENT="",  # makes build_container raise -> not ready
    )
    app = create_app(settings=broken, provider=fake_provider)
    with TestClient(app) as client:
        assert app.state.ready is False
        assert client.get("/healthz").status_code == 200  # liveness still ok
        assert client.get("/readyz").status_code == 503
        r = client.post(
            "/v1/daintree/tasks",
            headers={"Authorization": "Bearer test-api-key"},
            json={"task": "terminal_judge.v1", "input": {"question": "q"}},
        )
        assert r.status_code == 503
