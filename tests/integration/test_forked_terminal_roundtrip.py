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
from daintree_assistant_server.main import create_app
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
    """An app with forked spawns ENABLED, wired the way production wires one.

    Deliberately NOT hand-assembled. The wire tests inject a rewriter straight
    into the orchestrator, so they would keep passing if the wiring in `main.py`
    were deleted; every test below would fail, because the container that serves
    these requests is the one `create_app`'s LIFESPAN builds through the real
    `build_container` — including the rewriter, the expansion service and the
    assembler's prompt flag.

    That is also why no container is installed here: entering `TestClient` runs
    the lifespan, which builds its own and replaces anything a fixture put on
    `app.state` (see the same note in `tests/conftest.py`). Installing one would
    be dead weight that reads like the thing under test.
    """

    def build(**overrides: Any) -> Any:
        merged = settings.model_copy(
            update={"forked_terminal_calls_mode": "enabled", **overrides}
        )
        seam = scripted_selector(fake_provider, merged)
        return create_app(settings=merged, provider=fake_provider, selector=seam)

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


class _Session:
    """Several turns against ONE client, the way a real conversation runs.

    A fresh `TestClient` per turn would re-run the lifespan, rebuild the
    container, and hand the second turn a provider the first turn already
    closed. The fake tolerates that; a real one would not, and a multi-turn test
    that quietly restarts the server between hops is not testing a conversation.
    """

    def __init__(self, app: Any) -> None:
        self._app = app
        self._client: Any = None

    def __enter__(self) -> _Session:
        from fastapi.testclient import TestClient

        self._client = TestClient(self._app).__enter__()
        detach_docs_client(self._app.state.container)
        return self

    def __exit__(self, *exc: Any) -> None:
        self._client.__exit__(*exc)

    def post(self, payload: dict[str, Any]) -> tuple[Any, str]:
        streaming = bool(payload.get("generation", {}).get("stream"))
        if not streaming:
            response = self._client.post(URL, headers=HEADERS, json=payload)
            return response, response.text
        with self._client.stream("POST", URL, headers=HEADERS, json=payload) as response:
            return response, "".join(response.iter_text())


def _post(app: Any, payload: dict[str, Any]) -> tuple[Any, str]:
    """One turn against a fresh client.

    A stream has to be CONSUMED for the upstream call to happen at all, so the
    text is drained here rather than left to the caller.
    """
    with _Session(app) as session:
        return session.post(payload)


def _sse(text: str) -> list[tuple[str, dict[str, Any]]]:
    events: list[tuple[str, dict[str, Any]]] = []
    name = ""
    for line in text.splitlines():
        if line.startswith("event: "):
            name = line[len("event: ") :]
        elif line.startswith("data: ") and name:
            events.append((name, json.loads(line[len("data: ") :])))
    return events


def _accumulate_like_the_client(text: str) -> list[dict[str, Any]]:
    """Rebuild whole tool calls from SSE fragments, as the CLI does.

    Mirrors `cli/internal/backend/sse.go`: fragments are folded into a map keyed
    by `index`, a missing index defaults to zero, non-empty fields overwrite and
    argument text concatenates. The `index` is fragment metadata and is dropped
    from the result — the inbound `ToolCall` contract forbids it — which is
    exactly the transformation a replay has to survive.
    """
    by_index: dict[int, dict[str, Any]] = {}
    order: list[int] = []
    for fragment in _streamed_calls(text):
        idx = fragment.get("index", 0)
        entry = by_index.get(idx)
        if entry is None:
            entry = {"id": "", "type": "function", "name": "", "arguments": ""}
            by_index[idx] = entry
            order.append(idx)
        if fragment.get("id"):
            entry["id"] = fragment["id"]
        function = fragment.get("function") or {}
        if function.get("name"):
            entry["name"] += function["name"]
        if function.get("arguments"):
            entry["arguments"] += function["arguments"]
    return [
        {
            "id": by_index[i]["id"],
            "type": "function",
            "function": {
                "name": by_index[i]["name"],
                "arguments": by_index[i]["arguments"] or "{}",
            },
        }
        for i in sorted(order)
    ]


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


def _assert_four_spawns(calls: list[dict[str, Any]]) -> None:
    """The shape a replay is only meaningful against.

    Without this the round-trip test below would pass just as happily on a
    backend that synthesized ONE valid spawn call — "whatever came back
    validates" is not the claim being made.
    """
    assert len(calls) == 4
    assert [c["function"]["name"] for c in calls] == [SPAWN_TOOL_WIRE_NAME] * 4
    assert len({c["id"] for c in calls}) == 4


def _results_for(calls: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [
        {
            "role": "tool",
            "tool_call_id": call["id"],
            "content": json.dumps({"terminalId": f"term_{i}"}),
        }
        for i, call in enumerate(calls)
    ]


def test_the_client_can_post_the_result_of_a_fork_back(
    fork_app: Any, fake_provider: FakeProvider
) -> None:
    """THE round trip: spawn four, run them, continue the same turn.

    Shaped as a real tool-continuation round rather than a fresh turn — same
    `turn_id`, the signed `state` carried back, `round` incremented, and the
    transcript ENDING on the four tool results. That distinction is the whole
    point: a continuation is the request the CLI actually sends after running a
    batch, and it takes a different path through selector cadence and state
    reuse than a new turn does.

    The four calls are SYNTHESIZED — their ids come from the backend, not the
    model — so if the ids, the shape or the pairing were wrong, this second
    request is a 400 from Daintree's own transcript validator, and the user has
    already watched four terminals start.
    """
    fake_provider.complete_result = _fork_main_then_expansions()
    with _Session(fork_app()) as session:
        first, _ = session.post(_turn(generation={"stream": False}))
        assert first.status_code == 200
        body = first.json()
        calls = body["message"]["tool_calls"]
        _assert_four_spawns(calls)

        fake_provider.complete_result = make_result(content="All four are running.")
        second, _ = session.post(
            _turn(
                messages=[
                    {"role": "user", "content": "split the release work four ways"},
                    {"role": "assistant", "content": None, "tool_calls": calls},
                    *_results_for(calls),
                ],
                # Same turn, next round, carrying the state the first hop minted.
                turn_id="t1",
                round=1,
                state=body.get("state"),
                generation={"stream": False},
            )
        )
    assert second.status_code == 200, second.text
    assert second.json()["message"]["content"] == "All four are running."


def test_the_streamed_batch_can_be_replayed_the_way_a_client_rebuilds_it(
    fork_app: Any, fake_provider: FakeProvider
) -> None:
    """The same round trip, over the path that actually ships.

    Streaming is where real turns live, and its calls carry an `index` that the
    inbound `ToolCall` contract FORBIDS — so the client has to accumulate the
    fragments and drop that field before replaying them. Taking the calls from
    the non-streaming JSON instead would leave a streaming-only serialization
    bug invisible: the batch would look right on the wire and be refused the
    moment it came back.
    """
    fake_provider.stream_chunks = _fork_stream()
    fake_provider.complete_result = _expansion_answer
    with _Session(fork_app()) as session:
        first, text = session.post(_turn(generation={"stream": True}))
        assert first.status_code == 200

        calls = _accumulate_like_the_client(text)
        _assert_four_spawns(calls)

        fake_provider.stream_chunks = None
        fake_provider.complete_result = make_result(content="All four are running.")
        second, _ = session.post(
            _turn(
                messages=[
                    {"role": "user", "content": "split the release work four ways"},
                    {"role": "assistant", "content": None, "tool_calls": calls},
                    *_results_for(calls),
                ],
                turn_id="t1",
                round=1,
                generation={"stream": False},
            )
        )
    assert second.status_code == 200, second.text


def test_the_continuation_transcript_reaches_the_model_intact(
    fork_app: Any, fake_provider: FakeProvider
) -> None:
    # What the MODEL sees on the second round is four ordinary spawn calls and
    # their results — the reconstruction of what it would have emitted itself,
    # with no trace of the function that produced them.
    fake_provider.complete_result = _fork_main_then_expansions()
    with _Session(fork_app()) as session:
        first, _ = session.post(_turn(generation={"stream": False}))
        calls = first.json()["message"]["tool_calls"]
        _assert_four_spawns(calls)
        before = len(fake_provider.requests)

        fake_provider.complete_result = make_result(content="Running.")
        second, _ = session.post(
            _turn(
                messages=[
                    {"role": "user", "content": "split the release work four ways"},
                    {"role": "assistant", "content": None, "tool_calls": calls},
                    *_results_for(calls),
                ],
                turn_id="t1",
                round=1,
                generation={"stream": False},
            )
        )
    # Asserted before reading the provider: a second request that failed early
    # would leave `requests[-1]` pointing at a first-turn EXPANSION call, and
    # every assertion below would then describe the wrong request.
    assert second.status_code == 200, second.text
    assert len(fake_provider.requests) == before + 1

    sent = fake_provider.requests[-1].messages
    assistant = [m for m in sent if m.get("role") == "assistant" and m.get("tool_calls")]
    assert len(assistant) == 1
    # Structural, not a substring count: the system prompt names the internal
    # function on purpose (it is the rule instructing it), so scanning the whole
    # array would fail on the prompt rather than on a leak.
    replayed = assistant[0]["tool_calls"]
    assert [c["function"]["name"] for c in replayed] == [SPAWN_TOOL_WIRE_NAME] * 4
    assert [c["id"] for c in replayed] == [c["id"] for c in calls]
    answered = [m["tool_call_id"] for m in sent if m.get("role") == "tool"]
    assert answered == [c["id"] for c in calls]


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


def test_spawning_four_by_hand_is_logged_as_a_direct_batch(
    fork_app: Any, fake_provider: FakeProvider
) -> None:
    """The upper bound on missed forks, end to end.

    The model had the fork tool and wrote four full spawn calls anyway. Whether
    that was WRONG depends on something this layer cannot see — four independent
    units should have forked, four rivals in a competition should not — which is
    why the event is named for the shape and read as a population to inspect.
    Zero of them means there are no missed forks at all.
    """
    import structlog.testing

    fake_provider.complete_result = make_result(
        tool_calls=[
            {
                "id": f"call_{i}",
                "type": "function",
                "function": {
                    "name": SPAWN_TOOL_WIRE_NAME,
                    "arguments": json.dumps(
                        {"title": f"unit {i}", "taskPrompt": "a long hand-written prompt"}
                    ),
                },
            }
            for i in range(4)
        ],
        finish_reason="tool_calls",
    )
    with structlog.testing.capture_logs() as logs:
        response, _ = _post(fork_app(), _turn(generation={"stream": False}))
    assert response.status_code == 200
    batches = [e for e in logs if e.get("event") == "direct_spawn_batch"]
    assert len(batches) == 1
    assert batches[0] == {
        **batches[0],
        "measured": True,
        "spawn_calls": 4,
    }


def test_a_successful_fork_is_not_counted_as_a_direct_batch(
    fork_app: Any, fake_provider: FakeProvider
) -> None:
    # The rewrite produces four spawn calls too. Counting those would report a
    # 100% miss rate on exactly the turns where the feature worked.
    import structlog.testing

    fake_provider.complete_result = _fork_main_then_expansions()
    with structlog.testing.capture_logs() as logs:
        response, _ = _post(fork_app(), _turn(generation={"stream": False}))
    # Asserted POSITIVELY first: absence of a log is also what a turn that
    # errored, or never forked at all, would produce.
    assert response.status_code == 200
    _assert_four_spawns(response.json()["message"]["tool_calls"])
    assert [e for e in logs if e.get("event") == "fork_rewrite"]
    assert not [e for e in logs if e.get("event") == "direct_spawn_batch"]


def test_the_direct_batch_metric_also_fires_on_the_streaming_path(
    fork_app: Any, fake_provider: FakeProvider
) -> None:
    # Streaming is where real turns live, and it reads its names from a
    # different source (the buffer's own record rather than the rewritten call
    # list). A metric that only worked non-streaming would report near-zero
    # misses forever.
    import structlog.testing

    calls = [
        {
            "index": i,
            "id": f"call_{i}",
            "type": "function",
            "function": {
                "name": SPAWN_TOOL_WIRE_NAME,
                "arguments": json.dumps({"title": f"unit {i}", "taskPrompt": "by hand"}),
            },
        }
        for i in range(4)
    ]
    fake_provider.stream_chunks = [
        {"choices": [{"delta": {"tool_calls": calls}}]},
        {"choices": [{"delta": {}, "finish_reason": "tool_calls"}]},
    ]
    with structlog.testing.capture_logs() as logs:
        response, text = _post(fork_app(), _turn(generation={"stream": True}))
    assert response.status_code == 200
    # A 200 is committed before the first byte, so it does not rule out an
    # in-band error partway through.
    assert _sse(text)[-1][0] == "done"
    batches = [e for e in logs if e.get("event") == "direct_spawn_batch"]
    assert len(batches) == 1
    assert batches[0]["spawn_calls"] == 4
    assert batches[0]["measured"] is True


# ---------------------------------------------------------------------------
# The transcript state machine, as an HTTP error envelope.
#
# The RULES here are `tests/unit/test_validation.py`'s and are not re-litigated:
# what these add is that a batch the SERVER synthesized is judged by them like
# any other, and that a client mishandling one gets a typed 400 rather than a
# 500 or a silently accepted half-batch. The positive case — four
# backend-minted ids replaying cleanly — is the feature-specific one.
# ---------------------------------------------------------------------------


def _forked_batch(app: Any, fake_provider: FakeProvider) -> list[dict[str, Any]]:
    fake_provider.complete_result = _fork_main_then_expansions()
    first, _ = _post(app, _turn(generation={"stream": False}))
    assert first.status_code == 200
    calls: list[dict[str, Any]] = first.json()["message"]["tool_calls"]
    _assert_four_spawns(calls)
    return calls


def _continuation(calls: list[dict[str, Any]], results: list[dict[str, Any]]) -> list[Any]:
    return [
        {"role": "user", "content": "split the release work four ways"},
        {"role": "assistant", "content": None, "tool_calls": calls},
        *results,
    ]


def test_the_four_results_may_come_back_in_any_order(
    fork_app: Any, fake_provider: FakeProvider
) -> None:
    # The CLI dispatches the batch concurrently, so the order they finish in is
    # not the order they were emitted in. A validator that required emission
    # order would 400 the common case.
    app = fork_app()
    calls = _forked_batch(app, fake_provider)
    shuffled = [calls[2], calls[0], calls[3], calls[1]]
    results = [
        {"role": "tool", "tool_call_id": c["id"], "content": "spawned"} for c in shuffled
    ]
    fake_provider.complete_result = make_result(content="All running.")
    response, _ = _post(
        app,
        _turn(messages=_continuation(calls, results), turn_id="t2", round=1),
    )
    assert response.status_code == 200, response.text


def test_answering_one_spawn_four_times_is_refused(
    fork_app: Any, fake_provider: FakeProvider
) -> None:
    # Every open call is answered exactly once. Four results all naming the
    # first spawn leaves three terminals unaccounted for while claiming the
    # batch is closed.
    app = fork_app()
    calls = _forked_batch(app, fake_provider)
    results = [
        {"role": "tool", "tool_call_id": calls[0]["id"], "content": "spawned"}
        for _ in range(4)
    ]
    response, _ = _post(
        app,
        _turn(messages=_continuation(calls, results), turn_id="t2", round=1),
    )
    assert response.status_code == 400
    assert response.json()["error"]["code"] == "dangling_tool_message"


def test_the_synthesized_ids_never_collide_inside_the_batch(
    fork_app: Any, fake_provider: FakeProvider
) -> None:
    """The property `duplicate_tool_call_id` would catch, if the backend got it wrong.

    Four independent expansions cannot agree on unique ids, which is why the
    backend mints them rather than forwarding the model's. A collision would
    make the assistant message the client stores invalid on its face — and this
    asserts through the validator rather than by comparing strings, so it is the
    same rule the next turn will be judged by.
    """
    app = fork_app()
    calls = _forked_batch(app, fake_provider)
    assert len({c["id"] for c in calls}) == 4
    results = [
        {"role": "tool", "tool_call_id": c["id"], "content": "spawned"} for c in calls
    ]
    fake_provider.complete_result = make_result(content="All four running.")
    response, _ = _post(
        app,
        _turn(messages=_continuation(calls, results), turn_id="t2", round=1),
    )
    assert response.status_code == 200, response.text


def test_a_missing_result_before_a_new_user_message_is_refused(
    fork_app: Any, fake_provider: FakeProvider
) -> None:
    # Three of four answered, then the conversation moves on. The batch is not
    # closed, and a model asked to continue would be reasoning about a spawn
    # whose outcome nobody reported.
    app = fork_app()
    calls = _forked_batch(app, fake_provider)
    results = [
        {"role": "tool", "tool_call_id": c["id"], "content": "spawned"} for c in calls[:3]
    ]
    messages = _continuation(calls, results) + [{"role": "user", "content": "and now?"}]
    response, _ = _post(app, _turn(messages=messages, turn_id="t2", round=1))
    assert response.status_code == 400
    assert response.json()["error"]["code"] == "unresolved_tool_calls"


def test_a_result_for_the_hidden_internal_call_is_refused(
    fork_app: Any, fake_provider: FakeProvider
) -> None:
    # The id the model's own fork call carried. A client that somehow learned it
    # and answered it is describing a call that never crossed the wire.
    app = fork_app()
    calls = _forked_batch(app, fake_provider)
    results = [
        {"role": "tool", "tool_call_id": c["id"], "content": "spawned"} for c in calls
    ]
    results.append(
        {"role": "tool", "tool_call_id": "call_internal_1", "content": "forked"}
    )
    response, _ = _post(
        app,
        _turn(messages=_continuation(calls, results), turn_id="t2", round=1),
    )
    assert response.status_code == 400
    assert response.json()["error"]["code"] == "dangling_tool_message"


def test_these_tests_run_against_the_container_production_builds(fork_app: Any) -> None:
    """The fixture asserts its own premise.

    If `main.py` stopped wiring the rewriter, every leak assertion above would
    still pass — a turn with no rewriter simply never rewrites — so the suite
    would go green while the feature was gone. This is the test that would not.
    """
    from fastapi.testclient import TestClient

    app = fork_app()
    with TestClient(app):
        container = app.state.container
        assert container.orchestrator.fork_rewriter is not None
        assert container.settings.forked_terminal_calls_mode == "enabled"
        assert container.assembler.forked_terminals is True


def test_an_unreadable_stream_is_recorded_as_unmeasured_not_as_clean(
    fork_app: Any, fake_provider: FakeProvider
) -> None:
    """A passthrough round must not read as a round that spawned nothing.

    A fragment with no integer index puts the buffer into passthrough, where it
    stops recording call names. Four whole spawn calls can then reach the client
    while `called_names` is empty — and a metric that just counted names would
    file that as zero, which is an unmeasured round reported as a clean one.
    """
    import structlog.testing

    calls = [
        {
            "id": f"call_{i}",
            "type": "function",
            "function": {
                "name": SPAWN_TOOL_WIRE_NAME,
                "arguments": json.dumps({"title": f"unit {i}", "taskPrompt": "by hand"}),
            },
        }
        for i in range(4)
    ]
    fake_provider.stream_chunks = [
        # No `index` anywhere: the shape that trips passthrough.
        {"choices": [{"delta": {"tool_calls": calls}}]},
        {"choices": [{"delta": {}, "finish_reason": "tool_calls"}]},
    ]
    with structlog.testing.capture_logs() as logs:
        response, _ = _post(fork_app(), _turn(generation={"stream": True}))
    assert response.status_code == 200
    batches = [e for e in logs if e.get("event") == "direct_spawn_batch"]
    assert len(batches) == 1
    assert batches[0]["measured"] is False
    assert "spawn_calls" not in batches[0]


# ---------------------------------------------------------------------------
# Eligibility, and the one composition that could silently disable the feature.
# ---------------------------------------------------------------------------


def _offered_tool_names(fake_provider: FakeProvider, *, streamed: bool = False) -> list[str]:
    requests = fake_provider.stream_requests if streamed else fake_provider.requests
    return [t["function"]["name"] for t in (requests[-1].tools or [])]


def test_the_tool_survives_enforced_tool_projection(
    fork_app: Any, fake_provider: FakeProvider
) -> None:
    """Projection filters, THEN the fork tool is injected — in that order.

    The two features are independent suites and each is green on its own, which
    is exactly how this could break unnoticed: a regression that filtered the
    spawn tool away, or injected the internal one before the inventory check,
    would disable forks under enforcement while both suites kept passing.
    `agentTask.spawnForEdits` is in the always-on core, so enforcement must keep
    it and the fork tool must arrive after the check that compares the assembled
    inventory against what the projection judged.
    """
    fake_provider.complete_result = _fork_main_then_expansions()
    response, _ = _post(
        fork_app(tool_projection_mode="enforced", tool_projection_rollout_percent=100),
        _turn(generation={"stream": False}),
    )
    assert response.status_code == 200
    _assert_four_spawns(response.json()["message"]["tool_calls"])
    # The main request is the FIRST one; the four after it are expansions.
    offered = [t["function"]["name"] for t in (fake_provider.requests[0].tools or [])]
    assert SPAWN_TOOL_WIRE_NAME in offered
    assert FORK_TOOL_WIRE_NAME in offered


def test_a_subagent_turn_is_never_offered_the_tool(
    fork_app: Any, fake_provider: FakeProvider
) -> None:
    # Read-only, and cannot spawn a visible terminal at all — so the tool would
    # be an instruction it has no legal move for. Asserted through HTTP because
    # the profile changes which assembly path runs.
    fake_provider.complete_result = make_result(content="Here is what I found.")
    payload = _turn(generation={"stream": False})
    payload["profile"] = "subagent"
    response, _ = _post(fork_app(), payload)
    assert response.status_code == 200
    assert FORK_TOOL_WIRE_NAME not in _offered_tool_names(fake_provider)


def test_a_client_without_the_spawn_tool_is_never_offered_the_fork(
    fork_app: Any, fake_provider: FakeProvider
) -> None:
    # The fork expands into exactly one tool and validates against that tool's
    # runtime schema. Offering it to a caller that does not hold the target
    # would produce four calls the client cannot execute — worse than not
    # offering it, because the model would believe it had spawned them.
    fake_provider.complete_result = make_result(content="I cannot spawn here.")
    response, _ = _post(
        fork_app(),
        _turn(
            generation={"stream": False},
            tools=[
                {
                    "type": "function",
                    "function": {
                        "name": "fs__read",
                        "parameters": {"type": "object", "properties": {}},
                    },
                }
            ],
        ),
    )
    assert response.status_code == 200
    assert FORK_TOOL_WIRE_NAME not in _offered_tool_names(fake_provider)
