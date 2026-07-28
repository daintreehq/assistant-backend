"""Integration tests for health, models, capabilities, skills, and tasks endpoints."""

from __future__ import annotations

import json

from fakes import make_result, selection_result
from unit.test_task_runner import EXPECTED_TASK_IDS

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
    assert r.json()["protocol"] == {"min": 2, "max": 2}


def test_capabilities(client, auth) -> None:
    r = client.get("/v1/daintree/capabilities", headers=auth)
    body = r.json()
    assert body["respond"]["system_messages_accepted"] is False
    assert body["respond"]["max_active_skills"] == 3
    assert body["respond"]["stream_events"] == ["meta", "status", "delta", "done", "error"]
    assert body["respond"]["thinking_enabled"] is True  # hardcoded posture, not config
    assert body["respond"]["multimodal_images"]["remote_urls"] is False
    # Exact set, not a count: the ids are a frozen wire contract the Go CLI hardcodes
    # (internal/backend/tasks.go + workflowtasks.go). A count-only assertion passed
    # straight through the 2026-07-07 de-versioning incident — same 12 tasks, all
    # renamed, every CLI task call 404'd. See EXPECTED_TASK_IDS in
    # tests/unit/test_task_runner.py.
    assert set(body["tasks"]) == EXPECTED_TASK_IDS
    assert body["skills"]["manual_resolve"] is True
    wf = body["workflow_intelligence"]
    assert wf["turn_context_field"] == "workflow_state"
    assert wf["tasks"] == ["workflow_plan", "workflow_reconcile", "workflow_resume_digest"]
    assert wf["max_workflow_digests"] == 5
    assert wf["max_workflow_state_bytes"] == 16384
    assert set(wf["tasks"]) <= set(body["tasks"])


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
    fake_provider.selection_result = selection_result([SKILL_PREP])
    r = client.post("/v1/daintree/skills/resolve", headers=auth, json={
        "protocol_version": 2, "session_id": "s", "action": "find", "query": "prep branch",
    })
    assert r.status_code == 200
    body = r.json()
    assert [s["id"] for s in body["skills"]["active"]] == [SKILL_PREP]
    assert len(body["skills"]["prelude"]["tool_executions"]) == 1
    assert body["state"].startswith("dst1.")


def test_skills_resolve_load_and_clear(client, auth, app) -> None:
    r = client.post("/v1/daintree/skills/resolve", headers=auth, json={
        "protocol_version": 2, "session_id": "s", "action": "load", "skill_ids": [SKILL_PREP],
    })
    body = r.json()
    assert [s["id"] for s in body["skills"]["active"]] == [SKILL_PREP]
    state = body["state"]

    r2 = client.post("/v1/daintree/skills/resolve", headers=auth, json={
        "protocol_version": 2, "session_id": "s", "action": "clear", "state": state,
    })
    body2 = r2.json()
    assert body2["skills"]["active"] == []
    assert body2["skills"]["prelude"]["tool_executions"] == []


def test_skills_resolve_find_requires_query(client, auth) -> None:
    r = client.post("/v1/daintree/skills/resolve", headers=auth, json={
        "protocol_version": 2, "session_id": "s", "action": "find",
    })
    assert r.status_code == 400


def test_skills_resolve_load_requires_ids(client, auth) -> None:
    r = client.post("/v1/daintree/skills/resolve", headers=auth, json={
        "protocol_version": 2, "session_id": "s", "action": "load",
    })
    assert r.status_code == 400


def test_tasks_endpoint(client, auth, fake_provider) -> None:
    fake_provider.complete_result = make_result(
        content=json.dumps({"reason": "ok", "confidence": 0.8, "matched": True})
    )
    r = client.post("/v1/daintree/tasks", headers=auth, json={
        "task": "terminal_judge", "input": {"question": "pass?", "tail": "OK"},
    })
    assert r.status_code == 200
    body = r.json()
    assert body["object"] == "daintree.task.result"
    assert body["output"]["matched"] is True
    assert body["prompt_version"] == "terminal_judge"


def test_workflow_plan_task_endpoint(client, auth, fake_provider) -> None:
    plan = {
        "goal": "Fix watcher tests",
        "status": "active",
        "nodes": [
            {"id": "n_orient", "title": "Orient on repo", "kind": "orient", "status": "ready"},
            {
                "id": "n_fix",
                "title": "Spawn agent for edits",
                "kind": "delegate",
                "depends_on": ["n_orient"],
                "tool_name": "agentTask.spawnForEdits",
            },
        ],
        "next_action": {"label": "Orient", "tool_name": "context.snapshot"},
    }
    fake_provider.complete_result = make_result(content=json.dumps(plan))
    r = client.post("/v1/daintree/tasks", headers=auth, json={
        "task": "workflow_plan",
        "input": {
            "goal": "Fix watcher tests",
            "tool_inventory": [
                {"name": "context.snapshot", "risk": "read"},
                {"name": "agentTask.spawnForEdits", "risk": "project"},
            ],
        },
    })
    assert r.status_code == 200
    body = r.json()
    assert body["prompt_version"] == "workflow_plan"
    assert [n["id"] for n in body["output"]["nodes"]] == ["n_orient", "n_fix"]
    assert body["output"]["next_action"]["tool_name"] == "context.snapshot"


def test_workflow_reconcile_task_endpoint(client, auth, fake_provider) -> None:
    fake_provider.complete_result = make_result(content=json.dumps({
        "patch": {"node_updates": [{"node_id": "n_fix", "status": "done"}]},
        "summary": "Agent finished.",
    }))
    r = client.post("/v1/daintree/tasks", headers=auth, json={
        "task": "workflow_reconcile",
        "input": {
            "workflow": {"id": "wf_1", "nodes": [{"id": "n_fix", "status": "waiting"}]},
            "reason": "queue_event",
        },
    })
    assert r.status_code == 200
    body = r.json()
    assert body["output"]["patch"]["node_updates"][0]["status"] == "done"


def test_workflow_resume_digest_task_endpoint(client, auth, fake_provider) -> None:
    fake_provider.complete_result = make_result(content=json.dumps({
        "ranked": [{"workflow_id": "wf_1", "rank": 1, "headline": "Ready to verify"}],
        "suggested_focus_workflow_id": "wf_1",
    }))
    r = client.post("/v1/daintree/tasks", headers=auth, json={
        "task": "workflow_resume_digest",
        "input": {"workflows": [{"id": "wf_1", "goal": "Fix tests", "status": "active"}]},
    })
    assert r.status_code == 200
    body = r.json()
    assert body["output"]["suggested_focus_workflow_id"] == "wf_1"
    assert body["output"]["ranked"][0]["headline"] == "Ready to verify"


def test_tasks_unknown_404(client, auth) -> None:
    r = client.post("/v1/daintree/tasks", headers=auth, json={"task": "nope", "input": {}})
    assert r.status_code == 404
    assert r.json()["error"]["code"] == "unknown_task"


def test_tasks_invalid_input_400(client, auth) -> None:
    r = client.post("/v1/daintree/tasks", headers=auth, json={"task": "terminal_judge", "input": {}})
    assert r.status_code == 400


def test_oversized_body_413(client, auth) -> None:
    headers = {**auth, "Content-Length": str(20 * 1024 * 1024)}
    r = client.post("/v1/daintree/respond", headers=headers, content=b'{"protocol_version":2}')
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
            json={"task": "terminal_judge", "input": {"question": "q"}},
        )
        assert r.status_code == 503
