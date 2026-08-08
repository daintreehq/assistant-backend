"""Live integration tests against the real OpenRouter API.

Skipped by default. To run::

    RUN_LIVE_OPENROUTER=1 OPENROUTER_API_KEY=sk-or-v1-... pytest tests/live -q

These pin the *provider contract* that mocked tests can't see. Routing through
OpenRouter widens what that contract covers: besides the selector's non-thinking
``response_format`` json_object call (a regression there makes the selector
degrade open and silently stop loading skills), these also prove that the
endpoint-selection policy in ``_PROVIDER_ROUTING`` actually matches an endpoint
for every parameter mix the server sends — a too-strict filter fails as a 404 at
runtime and nothing mocked would catch it. They make real, paid API calls, so
they are gated.
"""

from __future__ import annotations

import asyncio
import json
import os

import pytest
from fastapi.testclient import TestClient
from pydantic import SecretStr

from daintree_assistant_server.config import Settings
from daintree_assistant_server.main import create_app

_LIVE = bool(os.environ.get("RUN_LIVE_OPENROUTER")) and bool(os.environ.get("OPENROUTER_API_KEY"))

pytestmark = pytest.mark.skipif(
    not _LIVE,
    reason="set RUN_LIVE_OPENROUTER=1 and a real OPENROUTER_API_KEY to run live tests",
)


@pytest.fixture(scope="module")
def live_client():
    settings = Settings(
        ENVIRONMENT="development",
        DAINTREE_API_KEY="",  # unauthenticated dev access
        OPENROUTER_API_KEY=os.environ["OPENROUTER_API_KEY"],
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
        "protocol_version": 2,
        "session": {"id": "live-sel", "turn_id": "t1"},
        "startup": {},
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
        "protocol_version": 2,
        "session": {"id": "live-stream", "turn_id": "t1"},
        "startup": {},
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


def test_live_tool_round_trip_replays_reasoning(live_client) -> None:
    # The turn after a tool call replays the assistant's chain-of-thought, which the
    # provider adapter renames reasoning_content -> OpenRouter's `reasoning`. If that
    # rename regressed, this is where it shows: the field is dropped or rejected on a
    # real request, not on a mock.
    r = live_client.post("/v1/daintree/respond", json={
        "protocol_version": 2,
        "session": {"id": "live-tools", "turn_id": "t1"},
        "startup": {},
        "input": {
            "messages": [
                {"role": "user", "content": "Check git status."},
                {
                    "role": "assistant",
                    "content": "",
                    "reasoning_content": "The user wants repo state; call git__status.",
                    "tool_calls": [{
                        "id": "c1",
                        "type": "function",
                        "function": {"name": "git__status", "arguments": "{}"},
                    }],
                },
                {"role": "tool", "tool_call_id": "c1", "content": "On branch main, clean."},
            ],
            "tools": [{
                "type": "function",
                "function": {"name": "git__status", "description": "Show git status",
                             "parameters": {"type": "object", "properties": {}}},
            }],
        },
    })
    assert r.status_code == 200
    assert r.json()["finish_reason"]


def test_live_skill_builder_model_is_routable(live_client) -> None:
    # The builder/improver name a DIFFERENT model on the same provider, in json mode
    # and with NO temperature. Both of those interact with the routing policy
    # (require_parameters filters on exactly the parameters sent), so a policy that
    # matches no endpoint for this mix would break skill learning silently — learning
    # fails open by design. Exercise the provider directly: learning is off here.
    import httpx as _httpx

    from daintree_assistant_server.config import SKILL_BUILDER_MODEL
    from daintree_assistant_server.providers.base import ProviderChatRequest
    from daintree_assistant_server.providers.openrouter import OpenRouterProvider

    async def _call() -> str:
        client = _httpx.AsyncClient(
            timeout=_httpx.Timeout(connect=10.0, read=180.0, write=30.0, pool=10.0)
        )
        provider = OpenRouterProvider(
            client, api_key=SecretStr(os.environ["OPENROUTER_API_KEY"])
        )
        try:
            result = await provider.complete(ProviderChatRequest(
                model=SKILL_BUILDER_MODEL,
                messages=[
                    {"role": "system", "content": 'Reply ONLY with JSON: {"ok": true}'},
                    {"role": "user", "content": "Confirm."},
                ],
                max_tokens=4000,
                response_format={"type": "json_object"},
                thinking_disabled=False,
                reasoning_effort="high",
                timeout=180.0,
            ))
            return result.text
        finally:
            await provider.aclose()

    assert json.loads(asyncio.run(_call()))["ok"] is True


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
