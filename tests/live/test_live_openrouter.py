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
    # The server holds NO upstream credential (OPENROUTER_API_KEY unset): every
    # request carries the real key as its bearer token, exactly as production
    # callers do — which also proves the caller-key threading end to end.
    settings = Settings(
        ENVIRONMENT="development",
        OPENROUTER_API_KEY="",
        STATE_SIGNING_KEY_CURRENT="live-test-signing-key",
        STATE_SALT="live-test-salt",
    )
    app = create_app(settings=settings)  # real provider (no fake injected)
    with TestClient(app) as client:
        assert app.state.ready is True
        client.headers["Authorization"] = f"Bearer {os.environ['OPENROUTER_API_KEY']}"
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


def test_live_cache_affinity_key_is_accepted_and_routes() -> None:
    # THE HARD ASSERTION — that a request carrying `session_id` still ROUTES.
    # `require_parameters: true` filters the eligible pool on exactly the parameters
    # we send. `session_id` is a ROUTER-level field, not a model parameter, so it
    # should not participate in that filter — but if it ever did, the policy would
    # match no endpoint and every main turn would 404. Nothing mocked can see that,
    # because the filter lives at OpenRouter.
    #
    # THE OBSERVATION — whether it actually pins. The two calls differ in their
    # first non-system message while sharing the large system prefix, so
    # OpenRouter's DEFAULT key (a hash of the first system and first non-system
    # message) would differ between them: if they still land on one endpoint, the
    # explicit key is what did it. That is the causal shape the weaker
    # identical-requests version could not show.
    #
    # It is reported as a WARNING, not an assertion. Stickiness is upstream
    # behaviour we observe rather than a contract we control — it only activates
    # where cache-read pricing beats prompt pricing — so a hard assert would make
    # an unrelated OpenRouter pricing change look like a regression here. A warning
    # survives `pytest -q` on a passing run, which a bare print does not.
    import uuid
    import warnings

    import httpx as _httpx

    from daintree_assistant_server.config import OPENROUTER_MODEL
    from daintree_assistant_server.providers.base import ProviderChatRequest
    from daintree_assistant_server.providers.openrouter import OpenRouterProvider

    # Fresh per run: a fixed key would inherit the previous run's sticky record
    # (they expire after 10 minutes of inactivity) and prove nothing about this one.
    key = f"daintree-main-livetest-{uuid.uuid4().hex[:12]}"
    shared_system = "You are terse." + " Answer briefly." * 200

    async def _call() -> list[tuple[str | None, int | None, str]]:
        client = _httpx.AsyncClient(
            timeout=_httpx.Timeout(connect=10.0, read=120.0, write=30.0, pool=10.0)
        )
        provider = OpenRouterProvider(client, api_key=SecretStr(os.environ["OPENROUTER_API_KEY"]))
        observed: list[tuple[str | None, int | None, str]] = []
        try:
            for turn in ("Say ok.", "Say fine instead."):
                result = await provider.complete(ProviderChatRequest(
                    model=OPENROUTER_MODEL,
                    messages=[
                        {"role": "system", "content": shared_system},
                        {"role": "user", "content": turn},
                    ],
                    max_tokens=8,
                    timeout=120.0,
                    cache_session_id=key,
                ))
                observed.append((result.endpoint, result.usage.cached_tokens, result.text))
        finally:
            await provider.aclose()
        return observed

    observed = asyncio.run(_call())
    # Both calls routed and produced a real completion. `complete` raises on an
    # unroutable request, so reaching here at all is the routing proof; asserting on
    # the payload keeps that from degrading into "two exceptions were not raised".
    assert len(observed) == 2
    for _endpoint, _cached, text in observed:
        assert isinstance(text, str)
    endpoints = [o[0] for o in observed]
    if None in endpoints:
        warnings.warn(
            f"affinity: endpoint not reported, stickiness unobservable ({endpoints})",
            stacklevel=1,
        )
    elif endpoints[0] != endpoints[1]:
        warnings.warn(
            f"affinity key did not pin: {endpoints[0]} -> {endpoints[1]} "
            f"(cached_tokens={[o[1] for o in observed]})",
            stacklevel=1,
        )
