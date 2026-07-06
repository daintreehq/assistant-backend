"""The v2 run protocol over HTTP: turns, SSE replay + live tail, leases, replay."""

from __future__ import annotations

import json
import time
from typing import Any

import httpx
from fakes import FakeProvider, make_text_chunks
from fastapi.testclient import TestClient


def tool_call_chunks() -> list[dict[str, Any]]:
    return [
        {
            "choices": [
                {
                    "index": 0,
                    "delta": {
                        "role": "assistant",
                        "tool_calls": [
                            {
                                "index": 0,
                                "id": "call_1",
                                "type": "function",
                                "function": {"name": "fs__read", "arguments": "{}"},
                            }
                        ],
                    },
                    "finish_reason": None,
                }
            ]
        },
        {"choices": [{"index": 0, "delta": {}, "finish_reason": "tool_calls"}]},
    ]


def tools_then_text(provider: FakeProvider) -> None:
    def chunks(request: Any) -> list[dict[str, Any]]:
        if any(m.get("role") == "tool" for m in request.messages):
            return make_text_chunks("finished")
        return tool_call_chunks()

    provider.stream_chunks = chunks


def create_run_body(content: str = "hello") -> dict[str, Any]:
    return {
        "session_id": "sess-1",
        "project_id": "proj-1",
        "turn": {
            "turn_id": "t1",
            "messages": [{"role": "user", "content": content}],
            "tools": [
                {
                    "type": "function",
                    "function": {"name": "fs__read", "description": "read a file"},
                }
            ],
        },
    }


def wait_status(
    client: TestClient, auth: dict[str, str], run_id: str, statuses: set[str]
) -> dict[str, Any]:
    deadline = time.monotonic() + 5.0
    last: dict[str, Any] = {}
    while time.monotonic() < deadline:
        resp = client.get(f"/v2/runs/{run_id}", headers=auth)
        assert resp.status_code == 200
        last = resp.json()["run"]
        if last["status"] in statuses:
            return last
        time.sleep(0.02)
    raise AssertionError(f"run stuck in {last.get('status')}; wanted {statuses}")


def parse_sse_with_ids(text: str) -> list[dict[str, Any]]:
    events: list[dict[str, Any]] = []
    import json as _json

    for block in text.strip().split("\n\n"):
        if not block.strip() or block.startswith(":"):
            continue
        event: dict[str, Any] = {}
        for line in block.splitlines():
            if line.startswith("id:"):
                event["id"] = int(line[3:].strip())
            elif line.startswith("event:"):
                event["event"] = line[6:].strip()
            elif line.startswith("data:"):
                event["data"] = _json.loads(line[5:].strip())
        if event:
            events.append(event)
    return events


# ---- lifecycle over HTTP ------------------------------------------------------


def test_run_create_and_complete_text_turn(
    client: TestClient, auth: dict[str, str], fake_provider: FakeProvider
) -> None:
    fake_provider.stream_chunks = make_text_chunks("hi from v2")
    resp = client.post("/v2/runs", json=create_run_body(), headers=auth)
    assert resp.status_code == 201
    body = resp.json()
    run_id = body["run"]["run_id"]
    assert body["turn"]["duplicate"] is False

    run = wait_status(client, auth, run_id, {"idle"})
    assert run["current_round"] == 1
    assert run["last_seq"] > 0

    # SSE replay (bounded): the durable story of the turn, with resume ids.
    resp = client.get(f"/v2/runs/{run_id}/events?live=false", headers=auth)
    assert resp.status_code == 200
    events = parse_sse_with_ids(resp.text)
    names = [e["event"] for e in events]
    for expected in (
        "run.created",
        "turn.submitted",
        "selection.completed",
        "round.started",
        "assistant.message",
        "turn.completed",
    ):
        assert expected in names, names
    assert all("id" in e for e in events)
    # Cursor resume: nothing before the cursor is replayed.
    cursor = events[2]["id"]
    resp = client.get(f"/v2/runs/{run_id}/events?live=false&after={cursor}", headers=auth)
    resumed = parse_sse_with_ids(resp.text)
    assert resumed and resumed[0]["id"] == events[3]["id"]

    # The durable assistant text is in the log (deltas are advisory only).
    assistant = next(e for e in events if e["event"] == "assistant.message")
    assert assistant["data"]["payload"]["message"]["content"] == "hi from v2 "


def test_live_sse_tail_delivers_new_events_and_deltas(
    app: Any, auth: dict[str, str], fake_provider: FakeProvider
) -> None:
    """The live tail must bridge the replay/live boundary with no gap: open the
    stream after turn 1, replay it, then watch turn 2 arrive live — including
    ephemeral model.delta events, which never appear in the persisted replay.

    Runs a REAL uvicorn server on an ephemeral port: both TestClient and
    httpx's ASGITransport buffer response bodies, so neither can consume a
    never-ending SSE stream.
    """
    import socket
    import threading

    import uvicorn

    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]
    # lifespan="off": the fixture app already carries a ready container.
    server = uvicorn.Server(
        uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning", lifespan="off")
    )
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    try:
        deadline = time.monotonic() + 10.0
        while not server.started:
            assert time.monotonic() < deadline, "uvicorn never started"
            time.sleep(0.02)
        base = f"http://127.0.0.1:{port}"

        with httpx.Client(base_url=base, timeout=10.0) as http:
            fake_provider.stream_chunks = make_text_chunks("first answer")
            resp = http.post("/v2/runs", json=create_run_body(), headers=auth)
            assert resp.status_code == 201
            run_id = resp.json()["run"]["run_id"]
            while http.get(f"/v2/runs/{run_id}", headers=auth).json()["run"][
                "status"
            ] != "idle":
                assert time.monotonic() < deadline, "turn 1 never completed"
                time.sleep(0.02)

            seen: list[tuple[str, bool]] = []  # (event name, is_persisted)
            completions = 0
            with http.stream(
                "GET", f"/v2/runs/{run_id}/events?after=0", headers=auth
            ) as stream:
                event_name = ""
                for line in stream.iter_lines():
                    if line.startswith("event:"):
                        event_name = line[6:].strip()
                    elif line.startswith("data:"):
                        payload = json.loads(line[5:].strip())
                        seen.append((event_name, payload.get("seq") is not None))
                        if event_name == "turn.completed":
                            completions += 1
                            if completions == 1:
                                # Replay of turn 1 consumed — produce turn 2
                                # LIVE while the stream stays open.
                                fake_provider.stream_chunks = make_text_chunks(
                                    "second answer"
                                )
                                body = dict(create_run_body()["turn"], turn_id="t2")
                                r = http.post(
                                    f"/v2/runs/{run_id}/turns", json=body, headers=auth
                                )
                                assert r.status_code == 200
                            elif completions == 2:
                                break
    finally:
        server.should_exit = True
        thread.join(timeout=5)

    names = [n for n, _ in seen]
    assert names.count("turn.completed") == 2
    # Turn 2's story arrived over the live tail, deltas included.
    second_half = names[names.index("turn.completed") + 1 :]
    assert "turn.submitted" in second_half
    assert "assistant.message" in second_half
    assert "model.delta" in second_half
    # Ephemeral deltas carry no seq; persisted events always do.
    assert all(persisted is False for n, persisted in seen if n == "model.delta")
    assert all(persisted is True for n, persisted in seen if n == "assistant.message")


def test_turn_idempotency_and_busy_over_http(
    client: TestClient, auth: dict[str, str], fake_provider: FakeProvider
) -> None:
    tools_then_text(fake_provider)
    resp = client.post("/v2/runs", json=create_run_body(), headers=auth)
    run_id = resp.json()["run"]["run_id"]
    wait_status(client, auth, run_id, {"awaiting_tools"})

    # Identical resubmission of the same turn → duplicate, not double-run.
    body = create_run_body()["turn"]
    resp = client.post(f"/v2/runs/{run_id}/turns", json=body, headers=auth)
    assert resp.status_code == 200
    assert resp.json()["duplicate"] is True

    # Same turn_id, different payload → conflict.
    changed = dict(body, messages=[{"role": "user", "content": "other"}])
    resp = client.post(f"/v2/runs/{run_id}/turns", json=changed, headers=auth)
    assert resp.status_code == 409
    assert resp.json()["error"]["code"] == "turn_conflict"

    # A NEW turn while a turn is in flight → run_busy.
    fresh = dict(body, turn_id="t2")
    resp = client.post(f"/v2/runs/{run_id}/turns", json=fresh, headers=auth)
    assert resp.status_code == 409
    assert resp.json()["error"]["code"] == "run_busy"


def test_lease_claim_result_fences_over_http(
    client: TestClient, auth: dict[str, str], fake_provider: FakeProvider
) -> None:
    tools_then_text(fake_provider)
    resp = client.post("/v2/runs", json=create_run_body(), headers=auth)
    run_id = resp.json()["run"]["run_id"]
    wait_status(client, auth, run_id, {"awaiting_tools"})

    leases = client.get(f"/v2/runs/{run_id}/leases", headers=auth).json()["leases"]
    assert len(leases) == 1 and leases[0]["status"] == "pending"
    lease_id = leases[0]["lease_id"]

    # Pending sweep surfaces it for reconnecting executors.
    sweep = client.get("/v2/tool-leases", headers=auth).json()["leases"]
    assert any(x["lease_id"] == lease_id for x in sweep)

    # Claim; a second executor is fenced out.
    resp = client.post(
        f"/v2/tool-leases/{lease_id}/claim", json={"executor_id": "cli-a"}, headers=auth
    )
    assert resp.status_code == 200 and resp.json()["outcome"] == "claimed"
    resp = client.post(
        f"/v2/tool-leases/{lease_id}/claim", json={"executor_id": "cli-b"}, headers=auth
    )
    assert resp.status_code == 409
    assert resp.json()["error"]["code"] == "lease_claimed"

    # Result: exactly once; identical retry is a duplicate; different is a conflict.
    result = {"executor_id": "cli-a", "status": "ok", "content": "data"}
    resp = client.post(f"/v2/tool-leases/{lease_id}/result", json=result, headers=auth)
    assert resp.status_code == 200 and resp.json()["outcome"] == "recorded"
    resp = client.post(f"/v2/tool-leases/{lease_id}/result", json=result, headers=auth)
    assert resp.status_code == 200 and resp.json()["outcome"] == "duplicate"
    resp = client.post(
        f"/v2/tool-leases/{lease_id}/result",
        json=dict(result, content="tampered"),
        headers=auth,
    )
    assert resp.status_code == 409
    assert resp.json()["error"]["code"] == "result_conflict"

    # The turn continued exactly once and completed.
    run = wait_status(client, auth, run_id, {"idle"})
    assert run["current_round"] == 2


def test_cancel_over_http(
    client: TestClient, auth: dict[str, str], fake_provider: FakeProvider
) -> None:
    tools_then_text(fake_provider)
    resp = client.post("/v2/runs", json=create_run_body(), headers=auth)
    run_id = resp.json()["run"]["run_id"]
    wait_status(client, auth, run_id, {"awaiting_tools"})
    resp = client.post(f"/v2/runs/{run_id}/cancel", headers=auth)
    assert resp.status_code == 200
    wait_status(client, auth, run_id, {"idle"})
    # Cancelling again → no in-flight turn.
    resp = client.post(f"/v2/runs/{run_id}/cancel", headers=auth)
    assert resp.status_code == 409


def test_replay_endpoint_verifies_round(
    client: TestClient, auth: dict[str, str], fake_provider: FakeProvider
) -> None:
    fake_provider.stream_chunks = make_text_chunks("answer")
    resp = client.post("/v2/runs", json=create_run_body(), headers=auth)
    run_id = resp.json()["run"]["run_id"]
    wait_status(client, auth, run_id, {"idle"})
    resp = client.get(f"/v2/runs/{run_id}/rounds/1/replay?include_request=true", headers=auth)
    assert resp.status_code == 200
    body = resp.json()
    assert body["match"] is True
    assert body["request"]["messages"]
    resp = client.get(f"/v2/runs/{run_id}/rounds/9/replay", headers=auth)
    assert resp.status_code == 404


# ---- validation & auth ---------------------------------------------------------


def test_turn_rejects_non_user_roles(client: TestClient, auth: dict[str, str]) -> None:
    body = create_run_body()
    body["turn"]["messages"] = [{"role": "assistant", "content": "smuggled"}]
    resp = client.post("/v2/runs", json=body, headers=auth)
    assert resp.status_code == 400
    assert resp.json()["error"]["code"] == "unsupported_role"

    body["turn"]["messages"] = [{"role": "system", "content": "root"}]
    resp = client.post("/v2/runs", json=body, headers=auth)
    assert resp.status_code == 400
    assert resp.json()["error"]["code"] == "system_messages_not_allowed"


def test_wakeup_validation_and_lifecycle(
    client: TestClient, auth: dict[str, str], fake_provider: FakeProvider
) -> None:
    fake_provider.stream_chunks = make_text_chunks("ok")
    resp = client.post("/v2/runs", json=create_run_body(), headers=auth)
    run_id = resp.json()["run"]["run_id"]
    wait_status(client, auth, run_id, {"idle"})

    resp = client.post(f"/v2/runs/{run_id}/wakeups", json={}, headers=auth)
    assert resp.status_code == 400
    resp = client.post(
        f"/v2/runs/{run_id}/wakeups",
        json={"delay_seconds": 60, "fire_at": time.time()},
        headers=auth,
    )
    assert resp.status_code == 400

    resp = client.post(
        f"/v2/runs/{run_id}/wakeups",
        json={"delay_seconds": 3600, "note": "later"},
        headers=auth,
    )
    assert resp.status_code == 201
    wakeup_id = resp.json()["wakeup"]["wakeup_id"]
    listed = client.get(f"/v2/runs/{run_id}/wakeups", headers=auth).json()["wakeups"]
    assert listed and listed[0]["status"] == "scheduled"
    resp = client.delete(f"/v2/runs/{run_id}/wakeups/{wakeup_id}", headers=auth)
    assert resp.status_code == 200


def test_heartbeat_reports_pending_work(
    client: TestClient, auth: dict[str, str], fake_provider: FakeProvider
) -> None:
    tools_then_text(fake_provider)
    resp = client.post("/v2/runs", json=create_run_body(), headers=auth)
    run_id = resp.json()["run"]["run_id"]
    wait_status(client, auth, run_id, {"awaiting_tools"})
    resp = client.post(
        "/v2/executors/heartbeat",
        json={
            "executor_id": "cli-a",
            "project_id": "proj-1",
            "tools": [{"name": "fs__read", "risk": "read"}],
        },
        headers=auth,
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["executor_id"] == "cli-a"
    assert len(body["pending_leases"]) == 1
    # Executor presence now shows on the run view.
    run = client.get(f"/v2/runs/{run_id}", headers=auth).json()["run"]
    assert run["executor_present"] is True


def test_v2_requires_auth(client: TestClient) -> None:
    resp = client.post("/v2/runs", json=create_run_body())
    assert resp.status_code == 401
    resp = client.get("/v2/runs")
    assert resp.status_code == 401
