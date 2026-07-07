"""Live integration tests against the real DeepSeek API.

Skipped by default. To run::

    RUN_LIVE_DEEPSEEK=1 DEEPSEEK_API_KEY=sk-... pytest tests/live -q

These pin the *provider contract* that mocked tests can't see — most importantly
that the selector's non-thinking ``response_format`` json_object call succeeds
and parses (a regression there makes the selector degrade open and silently
stop loading skills). They make real, paid API calls, so they are gated.
"""

from __future__ import annotations

import json
import os

import pytest
from fastapi.testclient import TestClient

from daintree_assistant_server.config import Settings
from daintree_assistant_server.main import create_app

_LIVE = bool(os.environ.get("RUN_LIVE_DEEPSEEK")) and bool(os.environ.get("DEEPSEEK_API_KEY"))

pytestmark = pytest.mark.skipif(
    not _LIVE,
    reason="set RUN_LIVE_DEEPSEEK=1 and a real DEEPSEEK_API_KEY (sk-...) to run live tests",
)


@pytest.fixture(scope="module")
def live_client():
    settings = Settings(
        ENVIRONMENT="development",
        DAINTREE_API_KEY="",  # unauthenticated dev access
        DEEPSEEK_API_KEY=os.environ["DEEPSEEK_API_KEY"],
        STATE_SIGNING_KEY_CURRENT="live-test-signing-key",
        STATE_SALT="live-test-salt",
    )
    app = create_app(settings=settings)  # real provider (no fake injected)
    with TestClient(app) as client:
        assert app.state.ready is True
        yield client


def test_live_capabilities(live_client) -> None:
    r = live_client.get("/v1/daintree/capabilities")
    assert r.status_code == 200
    assert r.json()["respond"]["model"] == "daintree-assistant"


def test_live_respond_selector_not_degraded(live_client) -> None:
    # Skill-relevant query with a tool offered. The selector's non-thinking
    # json_object call MUST succeed and parse — if the upstream contract breaks it,
    # the selector degrades open (this assertion is the regression guard).
    r = live_client.post("/v1/daintree/respond", json={
        "protocol_version": 1,
        "session": {"id": "live-sel", "turn_id": "t1"},
        "input": {
            "messages": [{"role": "user", "content": "Rename my terminals so I can tell the agents apart."}],
            "tools": [{
                "type": "function",
                "function": {"name": "context__snapshot", "description": "Snapshot Daintree state.",
                             "parameters": {"type": "object", "properties": {}}},
            }],
        },
    })
    assert r.status_code == 200
    d = r.json()
    assert d["skills"]["selector"]["ran"] is True
    assert d["skills"]["selector"]["degraded"] is False
    assert "skill_selector_failed_open" not in d["warnings"]
    assert d["finish_reason"]
    assert d["state"].startswith("dst1.")
    assert d["usage"]["prompt_tokens"] > 0


def test_live_respond_streaming(live_client) -> None:
    events: list[str] = []
    content = ""
    with live_client.stream("POST", "/v1/daintree/respond", json={
        "protocol_version": 1,
        "session": {"id": "live-stream", "turn_id": "t1"},
        "input": {"messages": [{"role": "user", "content": "Reply with the single word: ready"}]},
        "generation": {"stream": True},
    }) as s:
        assert s.status_code == 200
        last_event = None
        for line in s.iter_lines():
            if line.startswith("event:"):
                last_event = line[len("event:"):].strip()
                events.append(last_event)
            elif line.startswith("data:") and last_event == "delta":
                content += json.loads(line[len("data:"):].strip()).get("content", "") or ""
    assert events[0] == "meta"
    assert events[-1] == "done"
    assert "delta" in events


def test_live_task_terminal_judge(live_client) -> None:
    r = live_client.post("/v1/daintree/tasks", json={
        "task": "terminal_judge",
        "input": {"goal": "tests pass", "question": "Did the tests pass?",
                  "tail": "All 12 tests passed, 0 failed."},
    })
    assert r.status_code == 200
    out = r.json()["output"]
    assert isinstance(out["matched"], bool)
    assert out["matched"] is True
