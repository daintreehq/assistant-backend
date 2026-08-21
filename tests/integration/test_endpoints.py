"""Integration tests for health, models, capabilities, skills, and tasks endpoints."""

from __future__ import annotations

import json

import pytest
from fakes import make_result, selection_result
from unit.test_task_runner import EXPECTED_TASK_IDS

SKILL_PREP = "daintree.workflow.prep-branch-for-review"


def test_healthz(client) -> None:
    r = client.get("/healthz")
    assert r.status_code == 200
    assert r.json() == {"status": "ok"}


def test_health_alias(client) -> None:
    """/health must stay live: it is the only externally reachable liveness path.

    Google's frontend reserves /healthz on Cloud Run and answers it itself, so
    that path never reaches this process from outside the container. Deleting
    this alias would leave external uptime monitoring with nothing to probe.
    """
    r = client.get("/health")
    assert r.status_code == 200
    assert r.json() == {"status": "ok"}


def test_root_redirects_to_the_product_page(client) -> None:
    """A browser that reaches the API origin gets sent to the product page."""
    r = client.get("/", follow_redirects=False)
    assert r.status_code == 302
    assert r.headers["location"] == "https://daintree.org/assistant"


def test_root_redirect_answers_head(client) -> None:
    """HEAD / must redirect too, not 405.

    A FastAPI route registers only the methods it names, so this needs HEAD listed
    explicitly alongside GET — link previewers and uptime monitors send it.
    """
    r = client.head("/", follow_redirects=False)
    assert r.status_code == 302
    assert r.headers["location"] == "https://daintree.org/assistant"


def test_root_redirect_does_not_capture_api_paths(client) -> None:
    """The redirect must stay scoped to "/" alone.

    The CLI's DefaultBaseURL is this same origin (https://assistant.daintree.org),
    so widening this to the host — or to any prefix that covers /v1/daintree/ —
    would answer every real API call with a redirect to a marketing page. An
    unknown API path must still 404 rather than redirect.
    """
    r = client.get("/v1/daintree/does-not-exist", follow_redirects=False)
    assert r.status_code == 404


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


def test_capabilities(client, auth, app) -> None:
    r = client.get("/v1/daintree/capabilities", headers=auth)
    assert r.status_code == 200
    body = r.json()
    assert body["respond"]["system_messages_accepted"] is False
    # The CLI's ONLY signal that `runtime.display` is safe to send. `runtime` is
    # extra="forbid", so a client that sends the block to a backend advertising
    # false here 422s the whole turn — asserted as a literal so dropping the
    # field cannot pass silently.
    assert body["respond"]["display_context"] is True
    assert body["respond"]["max_active_skills"] == 3
    assert body["respond"]["stream_events"] == ["meta", "status", "delta", "done", "error"]
    # Hardcoded posture, not config. Asserted as a literal rather than against the
    # constant on purpose: this field is a wire contract the CLI gates its thinking UI
    # on, so the test must fail — and force the CLI to be updated in lockstep — when
    # the posture moves, not silently track it.
    assert body["respond"]["thinking_enabled"] is False
    assert body["respond"]["multimodal_images"]["remote_urls"] is False
    # Exact set, not a count: the ids are a frozen wire contract the Go CLI hardcodes
    # (internal/backend/tasks.go + workflowtasks.go). A count-only assertion passed
    # straight through the 2026-07-07 de-versioning incident — same 12 tasks, all
    # renamed, every CLI task call 404'd. See EXPECTED_TASK_IDS in
    # tests/unit/test_task_runner.py.
    assert set(body["tasks"]) == EXPECTED_TASK_IDS
    assert body["skills"]["manual_resolve"] is True
    # Same gate as `display_context` above, for `selection.pinned_skill_ids`.
    # `Selection` is extra="forbid", so a CLI that sends pins to a backend
    # advertising false here 422s the whole turn before the model runs — which is
    # why this is a literal the CLI can branch on rather than something inferred.
    assert body["skills"]["pinned_skill_ids"] is True
    # The id/title listing a client validates a skill name against before spending a
    # turn on it. Asserted as one exact equality rather than a count: that pins
    # cardinality, the full-catalog scope (pinned skills included), the exact key set
    # (no body, no metadata leaking in), the titles and the id ordering at once, and
    # stays correct as the catalog grows. `catalog_revision` must come from the same
    # snapshot — it is what the list is keyed on.
    catalog = app.state.container.catalog
    assert body["skills"]["catalog_revision"] == catalog.revision
    listing = body["skills"]["catalog"]
    assert listing == [{"id": s.id, "title": s.title} for s in catalog.skills]
    # Sorted-by-id is a DOCUMENTED property of the wire shape, and the equality
    # above cannot pin it: it compares against `Catalog.skills`, so if the catalog
    # stopped sorting, both sides would go unsorted together and still match.
    assert [e["id"] for e in listing] == sorted(e["id"] for e in listing)
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


def test_capabilities_catalog_matches_skills_endpoint(client, auth) -> None:
    """The two public catalog views must not drift apart.

    `skills.catalog` on capabilities and `data` on /v1/daintree/skills project the
    SAME catalog at two widths, and the API doc now sends clients from one to the
    other for the full metadata. Nothing else would notice if they diverged — each
    endpoint has its own test and both would stay green while the pair silently
    disagreed about which skills exist.
    """
    caps = client.get("/v1/daintree/capabilities", headers=auth).json()
    skills = client.get("/v1/daintree/skills", headers=auth).json()

    assert caps["skills"]["catalog_revision"] == skills["catalog_revision"]
    assert caps["skills"]["catalog"] == [
        {"id": s["id"], "title": s["title"]} for s in skills["data"]
    ]


def test_skills_resolve_find(client, auth, fake_provider, app) -> None:
    fake_provider.selection_result = selection_result([SKILL_PREP])
    r = client.post("/v1/daintree/skills/resolve", headers=auth, json={
        "protocol_version": 2, "session_id": "s", "action": "find", "query": "prep branch",
    })
    assert r.status_code == 200
    body = r.json()
    # `find` runs the selector, so the pick is front-loaded with its domain foundation.
    foundation = app.state.container.catalog.domain_foundation(SKILL_PREP)
    assert [s["id"] for s in body["skills"]["active"]] == [SKILL_PREP, foundation]
    assert len(body["skills"]["prelude"]["tool_executions"]) == len(body["skills"]["newly_loaded"])
    assert body["state"].startswith("dst1.")


def test_skills_resolve_load_and_clear(client, auth, app) -> None:
    r = client.post("/v1/daintree/skills/resolve", headers=auth, json={
        "protocol_version": 2, "session_id": "s", "action": "load", "skill_ids": [SKILL_PREP],
    })
    body = r.json()
    active = [s["id"] for s in body["skills"]["active"]]
    # An explicit load is foundation-closed like the automatic path: this response signs
    # a state token that later rounds replay verbatim, so an unclosed set here would
    # strand every following turn without the domain's base judgment.
    assert active[0] == SKILL_PREP
    catalog = app.state.container.catalog
    foundation_id = catalog.domain_foundation(SKILL_PREP)
    assert foundation_id is not None and foundation_id in active
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


def test_a_repaired_task_omits_a_partial_cost_from_the_body(
    client, auth, fake_provider
) -> None:
    """The wire half of the withholding rule, asserted where a client reads it.

    A repaired task billed twice and reported once. `usage.cost` has no
    completeness flag to qualify a short figure with, so the key must be absent
    entirely — a client summing a session total treats absent as unknown, and
    would treat a present float as exact.
    """
    from daintree_assistant_server.providers.base import ProviderUsage

    results = iter([
        make_result(content="not json at all", usage=ProviderUsage(cost=0.0002)),
        make_result(
            content=json.dumps({"reason": "ok", "confidence": 0.8, "matched": True}),
            usage=ProviderUsage(),
        ),
    ])
    fake_provider.complete_result = lambda _req: next(results)
    r = client.post("/v1/daintree/tasks", headers=auth, json={
        "task": "terminal_judge", "input": {"question": "pass?", "tail": "OK"},
    })
    assert r.status_code == 200
    body = r.json()
    assert body["output"]["matched"] is True
    assert "cost" not in body["usage"]


def test_an_ordinary_task_still_carries_its_cost_in_the_body(
    client, auth, fake_provider
) -> None:
    """Negative control: the rule must not strip the cost from every task."""
    from daintree_assistant_server.providers.base import ProviderUsage

    fake_provider.complete_result = make_result(
        content=json.dumps({"reason": "ok", "confidence": 0.8, "matched": True}),
        usage=ProviderUsage(cost=0.0002),
    )
    r = client.post("/v1/daintree/tasks", headers=auth, json={
        "task": "terminal_judge", "input": {"question": "pass?", "tail": "OK"},
    })
    assert r.status_code == 200
    assert r.json()["usage"]["cost"] == pytest.approx(0.0002)


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


def test_every_route_ignores_an_authorization_header_entirely(client, fake_provider) -> None:
    """Not "a key is optional" — the header is never read.

    Each of these shapes used to be a 401: a malformed bearer at our own door, or
    (worse) a well-formed stale OpenRouter key forwarded to Cerebras and rejected
    as the caller's. Both are served now, because there is no per-request
    credential for a bearer to supply and nothing for its shape to be wrong
    about.
    """
    from conftest import respond_body

    fake_provider.complete_result = make_result(
        content=json.dumps({"reason": "ok", "confidence": 0.9, "matched": True})
    )
    for headers in (
        {"Authorization": "Bearer sk-or-v1-a-stale-openrouter-key"},
        {"Authorization": "Token x"},
        {"Authorization": "Bearer bad key"},
        {"Authorization": "Bearer"},
    ):
        assert client.get("/v1/daintree/capabilities", headers=headers).status_code == 200
        r = client.post(
            "/v1/daintree/respond",
            headers=headers,
            json=respond_body([{"role": "user", "content": "rename my terminals"}]),
        )
        assert r.status_code == 200, headers
        r = client.post(
            "/v1/daintree/tasks",
            headers=headers,
            json={"task": "terminal_judge", "input": {"question": "q"}},
        )
        assert r.status_code == 200, headers
        assert client.get("/v1/daintree/skills", headers=headers).status_code == 200
        assert client.post("/v1/daintree/auth/verify", headers=headers).status_code == 200
    # Probes and version never require a key (Cloud Run probes them).
    assert client.get("/healthz").status_code == 200
    assert client.get("/readyz").status_code == 200
    assert client.get("/version").status_code == 200


def test_every_route_is_served_without_an_authorization_header(client) -> None:
    """The current credential model, on the wire, across the whole surface.

    Callers send no key; the server funds the turn from its own. Every one of
    these assertions is expected to flip when account authentication lands —
    which is exactly why they are enumerated rather than spot-checked.
    """
    from conftest import respond_body

    assert client.get("/v1/daintree/capabilities").status_code == 200
    assert client.get("/v1/daintree/skills").status_code == 200
    assert client.post(
        "/v1/daintree/skills/resolve", json={"action": "clear", "session_id": "s"}
    ).status_code == 200
    assert client.post(
        "/v1/daintree/respond",
        json=respond_body([{"role": "user", "content": "rename my terminals"}]),
    ).status_code == 200


def test_no_caller_credential_reaches_any_provider_call(client, fake_provider) -> None:
    """The serving path names no credential at all; the provider spends its own.

    Asserted across BOTH calls of a turn because that is how the old bug would
    come back — one handler reinstating `api_key=principal.api_key` while the
    others stay correct.
    """
    from conftest import respond_body

    fake_provider.selection_result = selection_result([SKILL_PREP])
    fake_provider.complete_result = make_result(content="done")
    r = client.post(
        "/v1/daintree/respond",
        headers={"Authorization": "Bearer sk-or-v1-caller-abc"},
        json=respond_body([{"role": "user", "content": "prep my branch"}]),
    )
    assert r.status_code == 200
    assert len(fake_provider.selection_requests) == 1, "selector call not recorded"
    assert fake_provider.requests, "main call not recorded"
    for req in [*fake_provider.selection_requests, *fake_provider.requests]:
        assert req.api_key is None


def test_no_caller_credential_reaches_a_task_call(client, fake_provider) -> None:
    fake_provider.complete_result = make_result(
        content=json.dumps({"reason": "ok", "confidence": 0.9, "matched": True})
    )
    r = client.post(
        "/v1/daintree/tasks",
        headers={"Authorization": "Bearer sk-or-v1-task-key"},
        json={"task": "terminal_judge", "input": {"question": "q"}},
    )
    assert r.status_code == 200
    assert fake_provider.requests
    assert all(req.api_key is None for req in fake_provider.requests)


def test_oversized_body_413(client, auth) -> None:
    headers = {**auth, "Content-Length": str(20 * 1024 * 1024)}
    r = client.post("/v1/daintree/respond", headers=headers, content=b'{"protocol_version":2}')
    assert r.status_code == 413


def test_a_deployment_without_the_upstream_key_is_not_ready() -> None:
    """CEREBRAS_API_KEY gates READINESS, and that placement is the whole point.

    Without it nothing can be answered at all. Discovering that on the first turn
    surfaces as an upstream 401 that reads like the caller's fault — an operator
    would go looking at the caller's credentials for a problem that is entirely
    in the deployment. Liveness stays green either way, so the container is not
    killed for a missing secret it cannot fix by restarting.
    """
    from fastapi.testclient import TestClient

    from daintree_assistant_server.config import Settings
    from daintree_assistant_server.main import create_app

    settings = Settings(
        ENVIRONMENT="development", CEREBRAS_API_KEY="",
        STATE_SIGNING_KEY_CURRENT="sk", STATE_SALT="x",
    )
    app = create_app(settings=settings)  # real lifespan (no injected provider)
    with TestClient(app) as client:
        assert app.state.ready is False
        assert client.get("/readyz").status_code == 503
        assert client.get("/healthz").status_code == 200


def test_prod_boots_ready_with_only_server_owned_secrets() -> None:
    """Production readiness needs no INBOUND key: there is nothing to configure
    on the door. Only the server's own material — the signing key, the salt, and
    the upstream credential — is validated."""
    from fastapi.testclient import TestClient

    from daintree_assistant_server.config import Settings
    from daintree_assistant_server.main import create_app

    settings = Settings(
        ENVIRONMENT="production", CEREBRAS_API_KEY="csk-server-owned",
        STATE_SIGNING_KEY_CURRENT="s" * 48, STATE_SALT="a-real-salt-value",
    )
    app = create_app(settings=settings)
    with TestClient(app) as client:
        assert app.state.ready is True
        assert client.get("/readyz").status_code == 200
        assert client.get("/healthz").status_code == 200
        # And the open door holds in production too — it is not a dev shortcut.
        assert client.get("/v1/daintree/capabilities").status_code == 200


def test_prod_not_ready_when_prompt_dumping_is_enabled(fake_provider) -> None:
    """Prompt dumping is local-dev-only: enabling it in production fails readiness,
    even with an injected provider (the guard runs before the secret checks).

    It writes FULL prompts to disk — a forensics tool locally, a disclosure risk
    anywhere else. This is the last entry in `local_only_problems`; skill
    learning, which used to be the other, has been removed entirely.
    """
    from fastapi.testclient import TestClient

    from daintree_assistant_server.config import Settings
    from daintree_assistant_server.main import create_app

    settings = Settings(
        ENVIRONMENT="production",
        CEREBRAS_API_KEY="csk-server-owned",
        STATE_SIGNING_KEY_CURRENT="s" * 48,
        STATE_SALT="a-real-salt-value",
        DAINTREE_DUMP_UPSTREAM_DIR="/tmp/daintree-dump",
    )
    app = create_app(settings=settings, provider=fake_provider)
    with TestClient(app) as client:
        assert app.state.ready is False
        assert "DAINTREE_DUMP_UPSTREAM_DIR" in (app.state.ready_error or "")
        assert client.get("/readyz").status_code == 503


def test_not_ready_returns_503(fake_provider) -> None:
    """A startup failure (here: empty signing key) leaves the app not-ready."""
    from fastapi.testclient import TestClient

    from daintree_assistant_server.config import Settings
    from daintree_assistant_server.main import create_app

    broken = Settings(
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
