"""Forked terminal spawns, end to end through the HTTP endpoint.

The unit tests prove each component decides correctly and the wire tests prove
the orchestrator's generator yields the right events. Neither answers the
question this feature actually rests on: **does a real client, posting a real
body to `/v1/daintree/respond`, ever see the internal call — and can it send the
result back?**

Those are different questions from the ones below the HTTP layer, because the
round trip crosses code the orchestrator tests skip entirely: request
validation, the reserved-namespace rules at the door, the error envelope, and
the SSE framing. The last one matters most. `POST` -> four spawn calls -> the
CLI runs them -> `POST` again carrying those calls and their results is the ONLY
path this feature ships on, and every hop of it is a place a synthesized call
could turn out to be something the server itself refuses.

So the centrepiece here is the CONTINUATION ROUND: the four calls the server
just emitted are spliced back into a transcript exactly as a client would, and
posted. If the ids, the shape or the pairing were wrong, that request is a 400
from our own validator — and the user would have watched four terminals start
and then lost the session.
"""

from __future__ import annotations

import json
from typing import Any

import pytest
from conftest import detach_docs_client, respond_body, scripted_selector
from fakes import FakeProvider, make_result

from daintree_assistant_server.config import Settings
from daintree_assistant_server.main import build_container, create_app
from daintree_assistant_server.services.forked_terminal_calls import (
    FORK_BRANCH_MESSAGE_NAME,
    FORK_TOOL_WIRE_NAME,
    SPAWN_TOOL_WIRE_NAME,
)

URL = "/v1/daintree/respond"
HEADERS = {"Authorization": "Bearer test-api-key"}

SPAWN_TOOL: dict[str, Any] = {
    "type": "function",
    "function": {
        "name": SPAWN_TOOL_WIRE_NAME,
        "description": "Spawn a visible agent in a worktree.",
        "parameters": {
            "type": "object",
            "additionalProperties": False,
            "required": ["title", "taskPrompt"],
            "properties": {
                "title": {"type": "string"},
                "taskPrompt": {"type": "string"},
                "acceptanceCriteria": {"type": "string"},
                "agentId": {"type": "string"},
                "worktreeId": {"type": "string"},
                "mode": {"type": "string", "enum": ["edit", "explore"]},
                "watch": {"type": "boolean"},
                "watchGoal": {"type": "string"},
            },
        },
    },
}

BRANCH_IDS = ("backend", "frontend", "tests", "performance")

PLAN: dict[str, Any] = {
    "sharedGoal": "Prepare the application for release.",
    "sharedConstraints": ["Run the relevant tests."],
    "defaults": {"agentId": "claude", "mode": "edit", "watch": True},
    "branches": [
        {
            "id": bid,
            "worktreeId": f"/repo/{bid}",
            "brief": f"Own the {bid} scope; touch nothing else; tests must pass.",
        }
        for bid in BRANCH_IDS
    ],
}


def _fork_stream(plan: Any = None) -> list[dict[str, Any]]:
    """An upstream stream whose entire reply is one internal fork call."""
    arguments = plan if isinstance(plan, str) else json.dumps(plan or PLAN)
    chunks: list[dict[str, Any]] = [
        {
            "choices": [
                {
                    "delta": {
                        "tool_calls": [
                            {
                                "index": 0,
                                "id": "call_internal_1",
                                "type": "function",
                                "function": {
                                    "name": FORK_TOOL_WIRE_NAME,
                                    "arguments": "",
                                },
                            }
                        ]
                    }
                }
            ]
        }
    ]
    for i in range(0, len(arguments), 80):
        chunks.append(
            {
                "choices": [
                    {
                        "delta": {
                            "tool_calls": [
                                {
                                    "index": 0,
                                    "function": {"arguments": arguments[i : i + 80]},
                                }
                            ]
                        }
                    }
                ]
            }
        )
    chunks.append({"choices": [{"delta": {}, "finish_reason": "tool_calls"}]})
    return chunks


def _expansion_answer(_request: Any) -> Any:
    return make_result(
        tool_calls=[
            {
                "id": "model_call",
                "type": "function",
                "function": {
                    "name": SPAWN_TOOL_WIRE_NAME,
                    "arguments": json.dumps(
                        {
                            "title": "release work",
                            "taskPrompt": "Do the assigned scope carefully.",
                            "acceptanceCriteria": "Tests pass; scope respected.",
                        }
                    ),
                },
            }
        ],
        finish_reason="tool_calls",
    )


def _fork_main_then_expansions() -> Any:
    """Non-streaming: the first completion forks, every later one expands."""
    state = {"forked": False}

    def answer(request: Any) -> Any:
        if not state["forked"]:
            state["forked"] = True
            return make_result(
                tool_calls=[
                    {
                        "id": "call_internal_1",
                        "type": "function",
                        "function": {
                            "name": FORK_TOOL_WIRE_NAME,
                            "arguments": json.dumps(PLAN),
                        },
                    }
                ],
                finish_reason="tool_calls",
            )
        return _expansion_answer(request)

    return answer


@pytest.fixture
def fork_app(settings: Settings, fake_provider: FakeProvider) -> Any:
    """An app with forked spawns ENABLED, built the way production builds one.

    Through `create_app`/`build_container` rather than by hand: the wire tests
    inject a rewriter directly, so they would keep passing if the real wiring
    were removed. This is the fixture that would not.
    """

    def build(**overrides: Any) -> Any:
        merged = settings.model_copy(
            update={"forked_terminal_calls_mode": "enabled", **overrides}
        )
        seam = scripted_selector(fake_provider, merged)
        app = create_app(settings=merged, provider=fake_provider, selector=seam)
        container = build_container(merged, fake_provider, seam)
        detach_docs_client(container)
        app.state.container = container
        app.state.ready = True
        return app

    return build


def _turn(**body: Any) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "messages": [{"role": "user", "content": "split the release work four ways"}],
        "session_id": "fork-1",
        "turn_id": "t1",
        "tools": [SPAWN_TOOL],
        **body,
    }
    return respond_body(**payload)


def _post(app: Any, payload: dict[str, Any]) -> tuple[Any, str]:
    """One turn, returning the response and its full body text.

    A stream has to be CONSUMED for the upstream call to happen at all, so the
    text is drained here rather than left to the caller.
    """
    from fastapi.testclient import TestClient

    streaming = bool(payload.get("generation", {}).get("stream"))
    with TestClient(app) as client:
        detach_docs_client(app.state.container)
        if not streaming:
            response = client.post(URL, headers=HEADERS, json=payload)
            return response, response.text
        with client.stream("POST", URL, headers=HEADERS, json=payload) as response:
            return response, "".join(response.iter_text())


def _sse(text: str) -> list[tuple[str, dict[str, Any]]]:
    events: list[tuple[str, dict[str, Any]]] = []
    name = ""
    for line in text.splitlines():
        if line.startswith("event: "):
            name = line[len("event: ") :]
        elif line.startswith("data: ") and name:
            events.append((name, json.loads(line[len("data: ") :])))
    return events


def _streamed_calls(text: str) -> list[dict[str, Any]]:
    return [
        call
        for name, payload in _sse(text)
        if name == "delta"
        for call in (payload.get("tool_calls") or [])
    ]


# ---------------------------------------------------------------------------
# The round trip.
# ---------------------------------------------------------------------------


def test_a_streamed_fork_reaches_the_client_as_four_ordinary_spawns(
    fork_app: Any, fake_provider: FakeProvider
) -> None:
    fake_provider.stream_chunks = _fork_stream()
    fake_provider.complete_result = _expansion_answer
    response, text = _post(fork_app(), _turn(generation={"stream": True}))

    assert response.status_code == 200
    # The whole HTTP body, not a parsed view of it: the guarantee is about bytes
    # reaching a client, and a parser that skipped a field would hide a leak.
    assert FORK_TOOL_WIRE_NAME not in text
    assert FORK_BRANCH_MESSAGE_NAME not in text

    calls = _streamed_calls(text)
    assert [c["function"]["name"] for c in calls] == [SPAWN_TOOL_WIRE_NAME] * 4
    assert [c["index"] for c in calls] == [0, 1, 2, 3]
    assert len({c["id"] for c in calls}) == 4


def test_a_non_streamed_fork_reaches_the_client_as_four_ordinary_spawns(
    fork_app: Any, fake_provider: FakeProvider
) -> None:
    fake_provider.complete_result = _fork_main_then_expansions()
    response, text = _post(fork_app(), _turn(generation={"stream": False}))

    assert response.status_code == 200
    assert FORK_TOOL_WIRE_NAME not in text
    calls = response.json()["message"]["tool_calls"]
    assert [c["function"]["name"] for c in calls] == [SPAWN_TOOL_WIRE_NAME] * 4
    # No `index` on this path — `ToolCall` forbids the field, so a stray one
    # would have been a 500 rendering the body rather than a wire nicety.
    assert all("index" not in call for call in calls)


def test_the_client_can_post_the_result_of_a_fork_back(
    fork_app: Any, fake_provider: FakeProvider
) -> None:
    """THE round trip: spawn four, run them, continue the conversation.

    This is the assertion the whole feature depends on and the one no test below
    the HTTP layer can make. The four calls are SYNTHESIZED — their ids come from
    the backend, not the model — so if they were malformed, duplicated, or paired
    wrongly with their results, this second request is a 400 from Daintree's own
    transcript validator and the session is lost after the terminals started.
    """
    fake_provider.complete_result = _fork_main_then_expansions()
    app = fork_app()
    first, _ = _post(app, _turn(generation={"stream": False}))
    assert first.status_code == 200
    calls = first.json()["message"]["tool_calls"]

    # Exactly what the CLI sends next: the assistant turn that opened the batch,
    # then one result per call, then the user's next message.
    continuation: list[dict[str, Any]] = [
        {"role": "user", "content": "split the release work four ways"},
        {"role": "assistant", "content": None, "tool_calls": calls},
    ]
    for call in calls:
        continuation.append(
            {
                "role": "tool",
                "tool_call_id": call["id"],
                "content": json.dumps({"terminalId": f"term_{call['id'][-1]}"}),
            }
        )
    continuation.append({"role": "user", "content": "how are they doing?"})

    fake_provider.complete_result = make_result(content="All four are running.")
    second, _ = _post(
        app,
        _turn(
            messages=continuation,
            turn_id="t2",
            round=1,
            generation={"stream": False},
        ),
    )
    assert second.status_code == 200, second.text


def test_the_continuation_transcript_reaches_the_model_intact(
    fork_app: Any, fake_provider: FakeProvider
) -> None:
    # And what the MODEL sees on that second turn is four ordinary spawn calls
    # and their results — no trace of the function that produced them.
    fake_provider.complete_result = _fork_main_then_expansions()
    app = fork_app()
    first, _ = _post(app, _turn(generation={"stream": False}))
    calls = first.json()["message"]["tool_calls"]

    continuation: list[dict[str, Any]] = [
        {"role": "user", "content": "split the release work four ways"},
        {"role": "assistant", "content": None, "tool_calls": calls},
    ]
    for call in calls:
        continuation.append(
            {"role": "tool", "tool_call_id": call["id"], "content": "spawned"}
        )

    fake_provider.complete_result = make_result(content="Running.")
    _post(app, _turn(messages=continuation, turn_id="t2", round=1))

    # The CONVERSATION rows only. The system prompt names the internal function
    # on purpose — it is the rule instructing it — so scanning the whole array
    # would fail on the prompt rather than on a leak. What must never appear is a
    # replayed CALL to it in the transcript, which is what the assistant and tool
    # rows below carry.
    conversation = json.dumps(
        [m for m in fake_provider.requests[-1].messages if m.get("role") != "system"]
    )
    assert FORK_TOOL_WIRE_NAME not in conversation
    assert conversation.count(SPAWN_TOOL_WIRE_NAME) >= 4


# ---------------------------------------------------------------------------
# The door: what a client may not send.
# ---------------------------------------------------------------------------


def test_a_client_offering_the_internal_tool_is_refused(fork_app: Any) -> None:
    # What makes the name unspoofable rather than merely private. Asserted
    # through HTTP because the rule lives at the request boundary, which every
    # orchestrator-level test skips.
    payload = _turn(
        tools=[
            SPAWN_TOOL,
            {
                "type": "function",
                "function": {
                    "name": FORK_TOOL_WIRE_NAME,
                    "parameters": {"type": "object", "properties": {}},
                },
            },
        ]
    )
    response, _ = _post(fork_app(), payload)
    assert response.status_code == 400
    assert response.json()["error"]["code"] == "reserved_tool_name"


def test_a_client_replaying_the_internal_call_is_refused(fork_app: Any) -> None:
    payload = _turn(
        messages=[
            {"role": "user", "content": "go"},
            {
                "role": "assistant",
                "content": None,
                "tool_calls": [
                    {
                        "id": "c1",
                        "type": "function",
                        "function": {
                            "name": FORK_TOOL_WIRE_NAME,
                            "arguments": json.dumps(PLAN),
                        },
                    }
                ],
            },
            {"role": "tool", "tool_call_id": "c1", "content": "ok"},
        ]
    )
    response, _ = _post(fork_app(), payload)
    assert response.status_code == 400
    assert response.json()["error"]["code"] == "reserved_tool_name"


def test_a_client_naming_a_reserved_internal_message_is_refused(fork_app: Any) -> None:
    # The names that switch a completion into branch-expansion mode. A client
    # able to wear one could address machinery it cannot see.
    payload = _turn(
        messages=[
            {"role": "user", "content": "go", "name": FORK_BRANCH_MESSAGE_NAME},
        ]
    )
    response, _ = _post(fork_app(), payload)
    assert response.status_code == 400
    assert response.json()["error"]["code"] == "reserved_message_name"


def test_a_client_forcing_the_internal_tool_is_refused(fork_app: Any) -> None:
    payload = _turn(tool_choice={"name": FORK_TOOL_WIRE_NAME})
    response, _ = _post(fork_app(), payload)
    assert response.status_code == 400
    assert response.json()["error"]["code"] == "reserved_tool_name"


# ---------------------------------------------------------------------------
# Failure, as a client experiences it.
# ---------------------------------------------------------------------------


def test_an_unusable_plan_is_a_typed_error_envelope(
    fork_app: Any, fake_provider: FakeProvider
) -> None:
    fake_provider.complete_result = make_result(
        tool_calls=[
            {
                "id": "call_internal_1",
                "type": "function",
                "function": {"name": FORK_TOOL_WIRE_NAME, "arguments": "{not json"},
            }
        ],
        finish_reason="tool_calls",
    )
    response, text = _post(fork_app(), _turn(generation={"stream": False}))
    assert response.status_code == 502
    assert response.json()["error"]["code"] == "fork_plan_invalid"
    # The rejected plan is never echoed back — it quotes the user's goal and
    # their project's file scopes.
    assert "not json" not in text


def test_a_streamed_failure_ends_with_an_error_not_a_done(
    fork_app: Any, fake_provider: FakeProvider
) -> None:
    fake_provider.stream_chunks = _fork_stream("{not json")
    response, text = _post(fork_app(), _turn(generation={"stream": True}))
    # The HTTP 200 committed long before the failure, so this can only be
    # reported in band.
    assert response.status_code == 200
    events = _sse(text)
    assert events[-1][0] == "error"
    assert events[-1][1]["error"]["code"] == "fork_plan_invalid"
    assert "done" not in [name for name, _ in events]
    assert FORK_TOOL_WIRE_NAME not in text


def test_an_expansion_outage_still_delivers_four_spawns(
    fork_app: Any, fake_provider: FakeProvider
) -> None:
    # The user has already waited for the main call. Degrading prompt quality
    # beats losing the turn — and the deterministic fallback has to survive the
    # real schema, not just the trimmed one the unit tests use.
    from daintree_assistant_server.errors import upstream_unavailable

    def answer(request: Any) -> Any:
        if not getattr(answer, "forked", False):
            answer.forked = True  # type: ignore[attr-defined]
            return make_result(
                tool_calls=[
                    {
                        "id": "call_internal_1",
                        "type": "function",
                        "function": {
                            "name": FORK_TOOL_WIRE_NAME,
                            "arguments": json.dumps(PLAN),
                        },
                    }
                ],
                finish_reason="tool_calls",
            )
        raise upstream_unavailable("no capacity")

    fake_provider.complete_result = answer
    response, _ = _post(fork_app(), _turn(generation={"stream": False}))
    assert response.status_code == 200
    calls = response.json()["message"]["tool_calls"]
    assert len(calls) == 4
    for call, branch in zip(calls, PLAN["branches"], strict=True):
        args = json.loads(call["function"]["arguments"])
        assert args["worktreeId"] == branch["worktreeId"]
        assert args["title"] and args["taskPrompt"]


# ---------------------------------------------------------------------------
# The disabled deployment.
# ---------------------------------------------------------------------------


def test_a_disabled_deployment_offers_neither_the_tool_nor_the_rule(
    fork_app: Any, fake_provider: FakeProvider
) -> None:
    fake_provider.complete_result = make_result(content="Sure.")
    _post(
        fork_app(forked_terminal_calls_mode="disabled"),
        _turn(generation={"stream": False}),
    )
    sent = fake_provider.requests[-1]
    assert FORK_TOOL_WIRE_NAME not in [t["function"]["name"] for t in (sent.tools or [])]
    system = "\n".join(
        str(m.get("content", "")) for m in sent.messages if m.get("role") == "system"
    )
    assert "Branch expansion mode:" not in system


def test_an_enabled_deployment_offers_both(
    fork_app: Any, fake_provider: FakeProvider
) -> None:
    fake_provider.complete_result = make_result(content="Sure.")
    _post(fork_app(), _turn(generation={"stream": False}))
    sent = fake_provider.requests[-1]
    assert FORK_TOOL_WIRE_NAME in [t["function"]["name"] for t in (sent.tools or [])]
    system = "\n".join(
        str(m.get("content", "")) for m in sent.messages if m.get("role") == "system"
    )
    assert "Branch expansion mode:" in system
