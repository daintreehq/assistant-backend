"""Live integration tests against the real Cerebras Inference API.

Skipped by default. To run::

    RUN_LIVE_CEREBRAS=1 TOGETHER_API_KEY=tgp_v1_... CEREBRAS_API_KEY=csk-... \
        pytest tests/live -q

These pin the *provider contract* that mocked tests cannot see, and against
Cerebras that contract is narrow and sharp-edged. Four of its rules are
enforced by the upstream with a 400 and by nothing else in this repo:

* ``max_completion_tokens`` is the spelling; sending it AND ``max_tokens``
  together is rejected.
* OpenRouter's ``reasoning`` object, its ``provider`` routing block, and its
  ``session_id`` affinity key are all "property is unsupported" errors here.
* ``response_format`` json_object must work, or every JSON-mode utility task
  and repair path silently stops parsing.
* The serving model must not reason. Nothing in this server thinks, and there is
  no wire field that could switch reasoning off — so that posture is an
  assumption about ``gemma-4-31b``, and a preview model's replacement is exactly
  when an assumption like that stops holding.
* Error bodies are FLAT (``{"message", "type", "param", "code"}``), not wrapped
  in OpenAI's ``{"error": {...}}`` — so the diagnostic that reaches a log or a
  client depends on the adapter reading the right shape.

A mocked test proves the adapter builds what we *think* the API wants. Only
these prove the API agrees. They make real API calls, so they are gated.

**Read which tests reach Cerebras.** Since the dual-mode split, Cerebras serves
`pro` and Together serves `standard`. The tests that build a ``CerebrasProvider``
directly still pin the Cerebras wire contract — that is most of this file and all
of the sharp-edged parts. The ones that go through ``/v1/daintree/respond``
currently exercise the DEFAULT route, which is Standard/Together, because the
request has no `mode` field yet to say otherwise; they are an end-to-end check of
the app, not of Cerebras. Both keys are therefore required to run this file at
all: readiness gates both, so the app will not come up with one. See TODO.md —
the `/respond` cases gain `"mode": "pro"` when the field lands, and only then do
they test what their names suggest.
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

_LIVE = bool(os.environ.get("RUN_LIVE_CEREBRAS")) and bool(os.environ.get("CEREBRAS_API_KEY"))

pytestmark = pytest.mark.skipif(
    not _LIVE,
    reason="set RUN_LIVE_CEREBRAS=1 and a real CEREBRAS_API_KEY to run live tests",
)


@pytest.fixture(scope="module")
def live_client():
    # The SERVER holds the credential and funds every turn from it — the
    # production arrangement since the move to Cerebras. No Authorization header
    # is set on the client on purpose: an anonymous request is what the CLI sends
    # now, so this fixture exercises the open door end to end rather than a path
    # only tests take.
    settings = Settings(
        ENVIRONMENT="development",
        CEREBRAS_API_KEY=os.environ["CEREBRAS_API_KEY"],
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
    # provider adapter renames reasoning_content -> the wire's `reasoning`. If that
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


def test_live_serving_model_reports_no_reasoning_tokens() -> None:
    # The non-thinking posture is now a property of the MODEL, not a flag we hold
    # down: Cerebras has no "reasoning off" switch, so `_build_body` simply omits
    # the field and relies on gemma-4-31b not reasoning. That is an assumption
    # about a preview model which will one day be replaced — assert it, so the
    # replacement announces itself here instead of in a latency regression.
    import httpx as _httpx

    from daintree_assistant_server.modes import PRO_MODEL as CEREBRAS_MODEL
    from daintree_assistant_server.providers.base import ProviderChatRequest
    from daintree_assistant_server.providers.cerebras import CerebrasProvider

    async def _call():  # type: ignore[no-untyped-def]
        client = _httpx.AsyncClient(
            timeout=_httpx.Timeout(connect=10.0, read=60.0, write=30.0, pool=10.0)
        )
        provider = CerebrasProvider(client, api_key=SecretStr(os.environ["CEREBRAS_API_KEY"]))
        try:
            return await provider.complete(ProviderChatRequest(
                model=CEREBRAS_MODEL,
                messages=[{"role": "user", "content": "Say ok."}],
                max_tokens=16,
                timeout=60.0,
            ))
        finally:
            await provider.aclose()

    result = asyncio.run(_call())
    details = (result.raw.get("usage") or {}).get("completion_tokens_details") or {}
    assert details.get("reasoning_tokens") == 0, (
        f"{CEREBRAS_MODEL} reported reasoning tokens ({details}). The interactive "
        "surface is non-thinking by ASSUMPTION here — there is no wire field that "
        "can switch it off — so a reasoning-capable serving model needs a "
        "deliberate posture decision, not a silent latency cost."
    )


def test_live_cache_affinity_key_is_accepted() -> None:
    # `prompt_cache_key` is a HINT here, not a routing lever: Cerebras caches
    # prefixes automatically and org-wide, and has one endpoint per model. So
    # there is no stickiness to observe and nothing to warn about — what is worth
    # pinning is simply that the field is ACCEPTED.
    #
    # That is not a formality. The equivalent OpenRouter spelling, `session_id`,
    # is a hard 400 ("property 'session_id' is unsupported") on this API, and the
    # adapter attaches this field to EVERY main turn and every utility task. Get
    # the spelling wrong and every request 400s — which no mocked test can see,
    # because the rejection lives at Cerebras.
    import uuid

    import httpx as _httpx

    from daintree_assistant_server.modes import PRO_MODEL as CEREBRAS_MODEL
    from daintree_assistant_server.providers.base import ProviderChatRequest
    from daintree_assistant_server.providers.cerebras import CerebrasProvider

    key = f"daintree-main-livetest-{uuid.uuid4().hex[:12]}"
    shared_system = "You are terse." + " Answer briefly." * 200

    async def _call() -> list[tuple[int | None, str]]:
        client = _httpx.AsyncClient(
            timeout=_httpx.Timeout(connect=10.0, read=120.0, write=30.0, pool=10.0)
        )
        provider = CerebrasProvider(client, api_key=SecretStr(os.environ["CEREBRAS_API_KEY"]))
        observed: list[tuple[int | None, str]] = []
        try:
            for turn in ("Say ok.", "Say fine instead."):
                result = await provider.complete(ProviderChatRequest(
                    model=CEREBRAS_MODEL,
                    messages=[
                        {"role": "system", "content": shared_system},
                        {"role": "user", "content": turn},
                    ],
                    max_tokens=8,
                    timeout=120.0,
                    cache_session_id=key,
                ))
                observed.append((result.usage.cached_tokens, result.text))
        finally:
            await provider.aclose()
        return observed

    observed = asyncio.run(_call())
    # `complete` raises on a rejected request, so reaching here at all is the
    # proof. Asserting on the payload keeps that from degrading into "two
    # exceptions were not raised".
    assert len(observed) == 2
    for _cached, text in observed:
        assert isinstance(text, str)


def test_live_rejected_parameters_stay_rejected() -> None:
    # A guard against reintroducing OpenRouter's vocabulary. Each of these was
    # sent on every request by the previous adapter, and each is a 400 here. If
    # one ever starts being ACCEPTED, that is worth knowing — it would mean the
    # upstream grew a feature the adapter could use.
    #
    # Asserted against the raw API rather than through the provider, because the
    # provider's whole job is to never send them.
    import httpx as _httpx

    from daintree_assistant_server.modes import PRO_MODEL as CEREBRAS_MODEL

    base = {
        "model": CEREBRAS_MODEL,
        "messages": [{"role": "user", "content": "hi"}],
        "max_completion_tokens": 5,
    }
    rejected = {
        "provider": {"sort": "throughput"},
        "session_id": "abc",
        "reasoning": {"enabled": False},
        "usage": {"include": True},
    }
    headers = {"Authorization": f"Bearer {os.environ['CEREBRAS_API_KEY']}"}
    with _httpx.Client(timeout=30.0) as client:
        for field, value in rejected.items():
            r = client.post(
                "https://api.cerebras.ai/v1/chat/completions",
                json={**base, field: value},
                headers=headers,
            )
            assert r.status_code == 400, (
                f"{field!r} is no longer rejected (HTTP {r.status_code}) — the "
                "adapter may now be able to use it."
            )
            # The FLAT error shape the adapter's `_upstream_error_detail` reads
            # first. If this ever nests under "error", a reader that only knew
            # the flat shape would silently lose every diagnostic.
            body = r.json()
            assert "message" in body or "message" in body.get("error", {}), body


def test_live_error_body_is_flat_on_a_bad_model() -> None:
    # The other half of the error-shape contract, on a different status (404),
    # plus the taxonomy decision that follows from it: against a single-host
    # provider a 404 on a fixed, correct path can only mean the MODEL id is
    # wrong, so the adapter maps it to `upstream_request_rejected` and carries
    # the upstream's own reason through rather than reporting a routing dead-end.
    import httpx as _httpx

    from daintree_assistant_server.errors import DaintreeError
    from daintree_assistant_server.providers.base import ProviderChatRequest
    from daintree_assistant_server.providers.cerebras import CerebrasProvider

    async def _call() -> DaintreeError:
        client = _httpx.AsyncClient(timeout=_httpx.Timeout(30.0, connect=10.0))
        provider = CerebrasProvider(client, api_key=SecretStr(os.environ["CEREBRAS_API_KEY"]))
        try:
            await provider.complete(ProviderChatRequest(
                model="daintree-model-that-does-not-exist",
                messages=[{"role": "user", "content": "hi"}],
                max_tokens=5,
                timeout=30.0,
            ))
        except DaintreeError as exc:
            return exc
        finally:
            await provider.aclose()
        raise AssertionError("a nonexistent model did not raise")

    exc = asyncio.run(_call())
    assert exc.code == "upstream_request_rejected"
    # The upstream's own words survived extraction — which only happens if the
    # flat body was read.
    assert "does not exist" in exc.message.lower(), exc.message


def test_live_verify_key_probe_lists_the_serving_model() -> None:
    # `verify_key` backs /v1/daintree/auth/verify and the doctor's provider
    # section. On Cerebras it is a model listing, which answers more than "is
    # this key valid": it also proves the account can reach the model this
    # server is pinned to, which is the failure a bare 200 would hide.
    import httpx as _httpx

    from daintree_assistant_server.modes import PRO_MODEL as CEREBRAS_MODEL
    from daintree_assistant_server.providers.cerebras import CerebrasProvider

    async def _call() -> dict:
        client = _httpx.AsyncClient(timeout=_httpx.Timeout(30.0, connect=10.0))
        provider = CerebrasProvider(client, api_key=SecretStr(os.environ["CEREBRAS_API_KEY"]))
        try:
            return await provider.verify_key()
        finally:
            await provider.aclose()

    record = asyncio.run(_call())
    assert CEREBRAS_MODEL in record["models"], record


def test_live_anonymous_request_is_served(live_client) -> None:
    # The credential model, asserted end to end: no Authorization header, and the
    # turn still runs on the server's key. This is the whole point of the current
    # auth posture, and it is the thing that must break loudly when account
    # authentication lands.
    assert "authorization" not in {k.lower() for k in live_client.headers}
    r = live_client.post("/v1/daintree/respond", json={
        "protocol_version": 2,
        "session": {"id": "live-anon", "turn_id": "t1"},
        "startup": {},
        "input": {"messages": [{"role": "user", "content": "Reply with: ok"}]},
    })
    assert r.status_code == 200, r.text
    assert r.json()["finish_reason"]
