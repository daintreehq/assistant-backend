"""What the client actually receives when the model forks a spawn.

The unit tests prove the rewriter replaces the call. These drive the real
orchestrator and assert on the SSE bytes, because that is the only place the
guarantee is testable end to end: no internal fragment in any event, `meta`
still first, `done` still terminal, and the four calls arriving as one delta the
existing client accumulator already knows how to read.
"""

from __future__ import annotations

import json
from typing import Any

import pytest

from daintree_assistant_server.config import Settings
from daintree_assistant_server.contracts.daintree_api import RespondRequest
from daintree_assistant_server.services.chat_orchestrator import ChatOrchestrator
from daintree_assistant_server.services.fork_prompt_expansion import (
    ForkPromptExpansionService,
)
from daintree_assistant_server.services.forked_terminal_calls import (
    FORK_TOOL_WIRE_NAME,
    SPAWN_TOOL_WIRE_NAME,
)
from daintree_assistant_server.services.forked_tool_call_rewriter import (
    ForkedToolCallRewriter,
)
from daintree_assistant_server.services.prompt_assembler import PromptAssembler
from tests.fakes import FakeProvider, make_result

#: `make_result`'s fixed per-call usage, so a total can be asserted exactly.
MAIN_PROMPT_TOKENS = 10
MAIN_COMPLETION_TOKENS = 5

PLAN = {
    "sharedGoal": "Prepare the application for release.",
    "sharedConstraints": ["Run the relevant tests."],
    "defaults": {"agentId": "claude", "mode": "edit", "watch": True},
    "branches": [
        {
            "id": bid,
            "worktreeId": f"/repo/{bid}",
            "brief": f"Own the {bid} scope; touch nothing else; tests must pass.",
        }
        for bid in ("backend", "frontend", "tests", "performance")
    ],
}

SPAWN_TOOL: dict[str, Any] = {
    "type": "function",
    "function": {
        "name": SPAWN_TOOL_WIRE_NAME,
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


def _fork_chunks(
    plan: dict[str, Any] | str | None = None,
    *,
    index: int | None = 0,
    split_name: bool = False,
    extra_call: dict[str, Any] | None = None,
) -> list[dict[str, Any]]:
    """An upstream stream whose whole reply is one internal fork call.

    `index=None` reproduces the fragment shape that puts the buffer into
    PASSTHROUGH — the one route by which a raw internal fragment could reach a
    client. `split_name` splits the FUNCTION NAME across fragments, which the
    delta protocol permits and the buffer concatenates.
    """
    arguments = plan if isinstance(plan, str) else json.dumps(plan or PLAN)
    first: dict[str, Any] = {
        "id": "call_internal_1",
        "type": "function",
        "function": {
            "name": FORK_TOOL_WIRE_NAME[:10] if split_name else FORK_TOOL_WIRE_NAME,
            "arguments": "",
        },
    }
    if index is not None:
        first["index"] = index
    fragments: list[dict[str, Any]] = [first]
    if split_name:
        rest: dict[str, Any] = {"function": {"name": FORK_TOOL_WIRE_NAME[10:]}}
        if index is not None:
            rest["index"] = index
        fragments.append(rest)
    head = {"choices": [{"delta": {"tool_calls": fragments}}]}
    pieces = [arguments[i : i + 64] for i in range(0, len(arguments), 64)] or [""]
    body = []
    for piece in pieces:
        fragment: dict[str, Any] = {"function": {"arguments": piece}}
        if index is not None:
            fragment["index"] = index
        body.append({"choices": [{"delta": {"tool_calls": [fragment]}}]})
    if extra_call is not None:
        body.append({"choices": [{"delta": {"tool_calls": [extra_call]}}]})
    tail = {"choices": [{"delta": {}, "finish_reason": "tool_calls"}]}
    return [head, *body, tail]


def _spawn_answer(request: Any) -> Any:
    """One branch expansion's answer: a valid spawn call."""
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
                            "acceptanceCriteria": "Tests pass; nothing outside scope changed.",
                        }
                    ),
                },
            }
        ],
        finish_reason="tool_calls",
    )


@pytest.fixture
def fork_settings(settings: Settings) -> Settings:
    return settings.model_copy(update={"forked_terminal_calls_mode": "enabled"})


@pytest.fixture
def fork_assembler(prompt_loader: Any, fork_settings: Settings) -> PromptAssembler:
    # Built from the ENABLED settings. The shared `assembler` fixture carries the
    # default (disabled) ones, so reusing it would run every test below against a
    # base prompt with no forked-spawn rule in it — the tool offered and the rule
    # explaining it absent, which is the one combination production cannot
    # produce.
    return PromptAssembler(prompt_loader, fork_settings)


@pytest.fixture
def fork_orchestrator(
    fork_settings: Settings,
    catalog_manager: Any,
    selector: Any,
    state_codec: Any,
    fork_assembler: PromptAssembler,
    fake_provider: FakeProvider,
) -> ChatOrchestrator:
    fake_provider.complete_result = _spawn_answer
    return ChatOrchestrator(
        settings=fork_settings,
        catalog_manager=catalog_manager,
        selector=selector,
        state_codec=state_codec,
        assembler=fork_assembler,
        provider=fake_provider,
        fork_rewriter=ForkedToolCallRewriter(
            expansion=ForkPromptExpansionService(
                settings=fork_settings, provider=fake_provider
            )
        ),
    )


def _request(**overrides: Any) -> RespondRequest:
    body: dict[str, Any] = {
        "protocol_version": 2,
        "session": {"id": "s1", "turn_id": "t1"},
        "input": {
            "messages": [{"role": "user", "content": "split the release work four ways"}],
            "tools": [SPAWN_TOOL],
        },
        "startup": {},
        "generation": {"stream": True},
    }
    body.update(overrides)
    return RespondRequest.model_validate(body)


async def _collect(orchestrator: ChatOrchestrator, request: RespondRequest) -> list[str]:
    outcome = await orchestrator.complete(request, request_id="req-1")
    assert outcome.streaming
    return [event async for event in outcome.stream]


def _events(raw: list[str]) -> list[tuple[str, dict[str, Any]]]:
    out: list[tuple[str, dict[str, Any]]] = []
    for chunk in raw:
        name = ""
        for line in chunk.splitlines():
            if line.startswith("event: "):
                name = line[len("event: ") :]
            elif line.startswith("data: ") and name:
                out.append((name, json.loads(line[len("data: ") :])))
    return out


async def test_the_client_sees_four_spawn_calls_and_no_internal_fragment(
    fork_orchestrator: ChatOrchestrator, fake_provider: FakeProvider
) -> None:
    fake_provider.stream_chunks = _fork_chunks()
    raw = await _collect(fork_orchestrator, _request())

    # The strongest statement available: the reserved name appears nowhere in
    # ANY byte the client received. A leaked fragment would come back next round
    # as a 400 from our own request boundary.
    assert FORK_TOOL_WIRE_NAME not in "".join(raw)

    events = _events(raw)
    calls = [
        call
        for name, payload in events
        if name == "delta"
        for call in (payload.get("tool_calls") or [])
    ]
    assert [c["function"]["name"] for c in calls] == [SPAWN_TOOL_WIRE_NAME] * 4


async def test_meta_is_still_first_and_done_still_terminal(
    fork_orchestrator: ChatOrchestrator, fake_provider: FakeProvider
) -> None:
    fake_provider.stream_chunks = _fork_chunks()
    events = _events(await _collect(fork_orchestrator, _request()))
    assert events[0][0] == "meta"
    assert events[-1][0] == "done"
    assert [name for name, _ in events].count("done") == 1


async def test_the_four_calls_arrive_as_one_consolidated_delta(
    fork_orchestrator: ChatOrchestrator, fake_provider: FakeProvider
) -> None:
    # One fragment carrying the whole call is a valid chunking of the same wire
    # shape, so the existing client accumulator needs no change.
    fake_provider.stream_chunks = _fork_chunks()
    events = _events(await _collect(fork_orchestrator, _request()))
    tool_deltas = [
        payload for name, payload in events if name == "delta" and payload.get("tool_calls")
    ]
    assert len(tool_deltas) == 1
    assert len(tool_deltas[0]["tool_calls"]) == 4


def test_each_streamed_call_carries_its_own_index() -> None:
    # THE detail that decides whether the user gets four terminals or one broken
    # spawn. The client accumulates streamed fragments into a map keyed by
    # index and DEFAULTS A MISSING ONE TO ZERO (cli/internal/backend/sse.go), so
    # four index-less calls land in one slot: the ids overwrite each other and
    # the four argument strings concatenate into unparseable JSON.
    from daintree_assistant_server.contracts.daintree_api import StreamDelta

    calls = [
        {"id": f"call_{i}", "type": "function", "function": {"name": "x", "arguments": "{}"}}
        for i in range(4)
    ]
    indexed = [{**call, "index": position} for position, call in enumerate(calls)]
    delta = StreamDelta(tool_calls=indexed)
    assert [c["index"] for c in (delta.tool_calls or [])] == [0, 1, 2, 3]


async def test_the_spawn_calls_carry_the_plans_worktrees(
    fork_orchestrator: ChatOrchestrator, fake_provider: FakeProvider
) -> None:
    fake_provider.stream_chunks = _fork_chunks()
    events = _events(await _collect(fork_orchestrator, _request()))
    calls = [
        call
        for name, payload in events
        if name == "delta"
        for call in (payload.get("tool_calls") or [])
    ]
    worktrees = [json.loads(c["function"]["arguments"])["worktreeId"] for c in calls]
    assert worktrees == [branch["worktreeId"] for branch in PLAN["branches"]]


async def test_an_invalid_plan_ends_the_stream_with_an_error_not_a_done(
    fork_orchestrator: ChatOrchestrator, fake_provider: FakeProvider
) -> None:
    # `done` claims the turn succeeded. A fork that could not be expanded did
    # not, and fabricating one would present four terminals that do not exist as
    # a finished job.
    fake_provider.stream_chunks = _fork_chunks("{not json")
    events = _events(await _collect(fork_orchestrator, _request()))
    assert events[-1][0] == "error"
    assert events[-1][1]["error"]["code"] == "fork_plan_invalid"
    assert "done" not in [name for name, _ in events]


async def test_a_disabled_deployment_never_offers_the_tool(
    orchestrator: ChatOrchestrator, fake_provider: FakeProvider
) -> None:
    # The default fixture settings leave the mode at `disabled`, and the
    # orchestrator there has no rewriter at all — the pre-feature path.
    fake_provider.stream_chunks = None
    await _collect(orchestrator, _request())
    sent = fake_provider.stream_requests[-1]
    names = [t["function"]["name"] for t in (sent.tools or [])]
    assert FORK_TOOL_WIRE_NAME not in names


async def test_an_enabled_turn_offers_the_tool_last_in_the_inventory(
    fork_orchestrator: ChatOrchestrator, fake_provider: FakeProvider
) -> None:
    fake_provider.stream_chunks = _fork_chunks()
    await _collect(fork_orchestrator, _request())
    sent = fake_provider.stream_requests[-1]
    names = [t["function"]["name"] for t in (sent.tools or [])]
    assert names[-1] == FORK_TOOL_WIRE_NAME
    # And never into the client's own declared inventory, which is replayed back
    # to us next round where the namespace is refused.
    assert names[:-1] == [SPAWN_TOOL_WIRE_NAME]


async def test_the_expansions_reuse_the_main_requests_prefix(
    fork_orchestrator: ChatOrchestrator, fake_provider: FakeProvider
) -> None:
    # The latency and cost argument, observed through the real orchestrator
    # rather than by calling the builder directly.
    fake_provider.stream_chunks = _fork_chunks()
    await _collect(fork_orchestrator, _request())
    main = fake_provider.stream_requests[-1]
    expansions = fake_provider.requests
    assert len(expansions) == 4
    for expansion in expansions:
        assert expansion.messages[: len(main.messages)] == main.messages
        assert expansion.tools == main.tools
        # Only the manifest and the branch assignment are new.
        assert len(expansion.messages) == len(main.messages) + 2


# ---------------------------------------------------------------------------
# The non-streaming path.
# ---------------------------------------------------------------------------


async def test_the_non_streaming_body_carries_the_four_calls_and_no_internal_one(
    fork_orchestrator: ChatOrchestrator, fake_provider: FakeProvider
) -> None:
    # The rewrite runs before `_build_nonstream_body`, so the body a client
    # parses can only ever contain the four ordinary calls.
    fake_provider.complete_result = _nonstream_answers()
    outcome = await fork_orchestrator.complete(
        _request(generation={"stream": False}), request_id="req-2"
    )
    assert not outcome.streaming
    body = json.dumps(outcome.body)
    assert FORK_TOOL_WIRE_NAME not in body
    calls = outcome.body["message"]["tool_calls"]
    assert [c["function"]["name"] for c in calls] == [SPAWN_TOOL_WIRE_NAME] * 4


async def test_the_non_streaming_turn_bills_the_expansion_calls(
    fork_orchestrator: ChatOrchestrator, fake_provider: FakeProvider
) -> None:
    # Four hidden completions are part of what the turn cost. Asserted as an
    # EXACT total rather than "greater than zero": the main call already reports
    # usage on its own, so a positive number would still be positive if every
    # expansion were dropped from the sum.
    fake_provider.complete_result = _nonstream_answers()
    outcome = await fork_orchestrator.complete(
        _request(generation={"stream": False}), request_id="req-3"
    )
    usage = outcome.body["usage"]
    # One main call plus four expansions, all from `make_result`'s fixed usage.
    assert usage["prompt_tokens"] == 5 * MAIN_PROMPT_TOKENS
    assert usage["completion_tokens"] == 5 * MAIN_COMPLETION_TOKENS


def _nonstream_answers() -> Any:
    """The main call returns the fork call; every later call is an expansion."""
    state = {"first": True}

    def answer(request: Any) -> Any:
        if state["first"]:
            state["first"] = False
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
        return _spawn_answer(request)

    return answer


# ---------------------------------------------------------------------------
# The escape paths.
#
# The invariant is "the internal call never reaches the client". These are the
# routes by which it could, each of which fails closed rather than forwarding.
# ---------------------------------------------------------------------------


async def test_a_passthrough_stream_carrying_the_internal_name_fails_closed(
    fork_orchestrator: ChatOrchestrator, fake_provider: FakeProvider
) -> None:
    # A fragment with no usable integer `index` puts the buffer into PASSTHROUGH,
    # where it forwards raw fragments verbatim and stops recording names. That
    # is the ONE route by which an internal fragment could reach a client, so it
    # ends the round rather than emitting.
    fake_provider.stream_chunks = _fork_chunks(index=None)
    raw = await _collect(fork_orchestrator, _request())
    assert FORK_TOOL_WIRE_NAME not in "".join(raw)
    events = _events(raw)
    assert events[-1][0] == "error"
    assert "done" not in [name for name, _ in events]
    # And nothing was expanded — there was no whole call to expand.
    assert fake_provider.requests == []


async def test_a_split_function_name_is_still_recognised_and_rewritten(
    fork_orchestrator: ChatOrchestrator, fake_provider: FakeProvider
) -> None:
    # The delta protocol permits splitting the function NAME across fragments,
    # and the buffer concatenates them. A rewrite keyed on the first fragment
    # alone would miss this and forward the internal call.
    fake_provider.stream_chunks = _fork_chunks(split_name=True)
    raw = await _collect(fork_orchestrator, _request())
    assert FORK_TOOL_WIRE_NAME not in "".join(raw)
    calls = [
        call
        for name, payload in _events(raw)
        if name == "delta"
        for call in (payload.get("tool_calls") or [])
    ]
    assert [c["function"]["name"] for c in calls] == [SPAWN_TOOL_WIRE_NAME] * 4


async def test_a_hallucinated_internal_call_on_a_disabled_turn_is_refused(
    orchestrator: ChatOrchestrator, fake_provider: FakeProvider
) -> None:
    # The tool was never offered — the fixture orchestrator has no rewriter and
    # the default mode is disabled — but model output is not a security
    # boundary, and a user can quote the name in prose. Relaying it would put a
    # reserved name in the transcript, which our own request boundary refuses on
    # the next turn.
    fake_provider.stream_chunks = _fork_chunks()
    raw = await _collect(orchestrator, _request())
    assert FORK_TOOL_WIRE_NAME not in "".join(raw)
    events = _events(raw)
    assert events[-1][0] == "error"
    assert events[-1][1]["error"]["code"] == "fork_call_not_offered"


async def test_a_mixed_batch_over_the_stream_emits_no_calls_at_all(
    fork_orchestrator: ChatOrchestrator, fake_provider: FakeProvider
) -> None:
    # Refused whole. Exposing the sibling while dropping the fork would leave
    # the model believing it had spawned four agents.
    fake_provider.stream_chunks = _fork_chunks(
        extra_call={
            "index": 1,
            "id": "call_sibling",
            "type": "function",
            "function": {"name": SPAWN_TOOL_WIRE_NAME, "arguments": "{}"},
        }
    )
    raw = await _collect(fork_orchestrator, _request())
    assert FORK_TOOL_WIRE_NAME not in "".join(raw)
    events = _events(raw)
    assert events[-1][0] == "error"
    assert events[-1][1]["error"]["code"] == "fork_call_not_alone"
    assert not [
        payload for name, payload in events if name == "delta" and payload.get("tool_calls")
    ]


async def test_the_streamed_batch_carries_indices_zero_through_three(
    fork_orchestrator: ChatOrchestrator, fake_provider: FakeProvider
) -> None:
    # Through the real orchestrator, not a hand-built delta: this is what the
    # client's index-keyed accumulator actually receives.
    fake_provider.stream_chunks = _fork_chunks()
    calls = [
        call
        for name, payload in _events(await _collect(fork_orchestrator, _request()))
        if name == "delta"
        for call in (payload.get("tool_calls") or [])
    ]
    assert [c["index"] for c in calls] == [0, 1, 2, 3]
    assert len({c["id"] for c in calls}) == 4


async def test_the_streaming_turn_reports_the_expansion_usage_it_billed(
    fork_orchestrator: ChatOrchestrator, fake_provider: FakeProvider
) -> None:
    # The SSE total and the durable turn-usage log must agree: four hidden
    # completions were billed either way, and a record that omitted them would
    # disagree with the response the user was handed.
    fake_provider.stream_chunks = _fork_chunks()
    events = _events(await _collect(fork_orchestrator, _request()))
    done = [payload for name, payload in events if name == "done"][0]
    assert done["usage"]["prompt_tokens"] == 4 * MAIN_PROMPT_TOKENS
    assert done["usage"]["completion_tokens"] == 4 * MAIN_COMPLETION_TOKENS


async def test_the_rule_and_the_tool_ship_together(
    fork_orchestrator: ChatOrchestrator, fake_provider: FakeProvider
) -> None:
    # The one combination production cannot produce is a tool with no rule
    # explaining it, or a rule naming a tool the turn was not offered. Both come
    # from the same setting, and this is where that would break.
    fake_provider.stream_chunks = _fork_chunks()
    await _collect(fork_orchestrator, _request())
    sent = fake_provider.stream_requests[-1]
    system = "\n".join(
        str(m.get("content", "")) for m in sent.messages if m.get("role") == "system"
    )
    assert "Branch expansion mode:" in system
    assert FORK_TOOL_WIRE_NAME in system
    assert FORK_TOOL_WIRE_NAME in [t["function"]["name"] for t in (sent.tools or [])]


async def test_a_disabled_deployment_ships_neither(
    orchestrator: ChatOrchestrator, fake_provider: FakeProvider
) -> None:
    fake_provider.stream_chunks = None
    await _collect(orchestrator, _request())
    sent = fake_provider.stream_requests[-1]
    system = "\n".join(
        str(m.get("content", "")) for m in sent.messages if m.get("role") == "system"
    )
    assert "Branch expansion mode:" not in system
    assert FORK_TOOL_WIRE_NAME not in [t["function"]["name"] for t in (sent.tools or [])]
