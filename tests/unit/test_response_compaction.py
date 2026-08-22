"""The declaration on the wire: stripped from the stream, and what it starts.

Three properties, in the order they can fail. The marker must never reach a
`delta` — that is the visible bug. The call it triggers must start while the
model is still talking — that is the entire reason the declaration goes first
rather than last. And the block must only be DELIVERED when the reply turned out
to be what it claimed, because a wrongly frozen segment does not unfreeze.
"""

from __future__ import annotations

import asyncio
import json
from typing import Any

import pytest
from fakes import FakeCompactor, FakeProvider, FakeStream, make_result

from daintree_assistant_server.contracts.daintree_api import (
    COMPACTION_BLOCK_NAME,
    RespondRequest,
)
from daintree_assistant_server.services.chat_orchestrator import ChatOrchestrator
from daintree_assistant_server.services.compaction import NullCompactor
from daintree_assistant_server.services.response_declaration import (
    FINAL_MARKER,
    TOOLS_MARKER,
)

TEST_API_KEY = "sk-test"

_TOOL = {
    "type": "function",
    "function": {
        "name": "terminal__read",
        "parameters": {
            "type": "object",
            "additionalProperties": False,
            "properties": {"terminalId": {"type": "string"}},
            "required": ["terminalId"],
        },
    },
}


def _request(
    session_id: str = "compaction",
    *,
    tools: bool = True,
    stream: bool = True,
    profile: str | None = None,
) -> RespondRequest:
    """A streaming turn with enough settled history to have a span."""
    payload: dict[str, Any] = {
        "protocol_version": 2,
        "session": {"id": session_id, "turn_id": "turn-1"},
        "input": {
            "messages": [
                {"role": "user", "content": "spawn three agents on the parser"},
                {"role": "assistant", "content": "agent-1, agent-2 and agent-3 are up"},
                {"role": "user", "content": "where are we?"},
            ],
        },
        "startup": {},
        "generation": {"stream": stream},
    }
    if tools:
        payload["input"]["tools"] = [_TOOL]
    if profile is not None:
        payload["profile"] = profile
    return RespondRequest.model_validate(payload)


def _chunks(*pieces: str, finish_reason: str = "stop") -> list[dict[str, Any]]:
    """Content chunks carrying EXACTLY the given strings, then a finish chunk."""
    out: list[dict[str, Any]] = [
        {
            "id": "chatcmpl-fake",
            "object": "chat.completion.chunk",
            "created": 1700000000,
            "model": "deepseek-v4-flash",
            "choices": [{"index": 0, "delta": {"role": "assistant"}, "finish_reason": None}],
        }
    ]
    out.extend(
        {
            "id": "chatcmpl-fake",
            "object": "chat.completion.chunk",
            "created": 1700000000,
            "model": "deepseek-v4-flash",
            "choices": [{"index": 0, "delta": {"content": piece}, "finish_reason": None}],
        }
        for piece in pieces
    )
    out.append(
        {
            "id": "chatcmpl-fake",
            "object": "chat.completion.chunk",
            "created": 1700000000,
            "model": "deepseek-v4-flash",
            "choices": [{"index": 0, "delta": {}, "finish_reason": finish_reason}],
        }
    )
    return out


def _tool_call_chunk() -> dict[str, Any]:
    return {
        "id": "chatcmpl-fake",
        "object": "chat.completion.chunk",
        "created": 1700000000,
        "model": "deepseek-v4-flash",
        "choices": [
            {
                "index": 0,
                "delta": {
                    "tool_calls": [
                        {
                            "index": 0,
                            "id": "call_1",
                            "type": "function",
                            "function": {
                                "name": "terminal__read",
                                "arguments": '{"terminalId":"t1"}',
                            },
                        }
                    ]
                },
                "finish_reason": None,
            }
        ],
    }


class _YieldingStream(FakeStream):
    """A fake upstream that hands control back to the event loop between chunks.

    `FakeStream` awaits nothing, so an in-memory stream is consumed end to end
    without the loop ever scheduling anything else — which would make any
    concurrency assertion vacuously false whatever the code under test did. A
    real upstream awaits its socket at every chunk. This models that, and nothing
    else about it differs.
    """

    async def _advance(self) -> dict[str, Any] | None:
        await asyncio.sleep(0)
        return await super()._advance()


async def _drain(stream: object) -> list[str]:
    return [event async for event in stream]  # type: ignore[attr-defined]


def _kinds(events: list[str]) -> list[str]:
    return [e.split("\n", 1)[0].removeprefix("event: ") for e in events if e.startswith("event: ")]


def _of(events: list[str], kind: str) -> list[dict[str, Any]]:
    return [
        json.loads(e.split("data: ", 1)[1])
        for e in events
        if e.startswith(f"event: {kind}\n")
    ]


def _content(events: list[str]) -> str:
    return "".join(d.get("content", "") or "" for d in _of(events, "delta"))


def _with(orchestrator: ChatOrchestrator, compactor: Any) -> ChatOrchestrator:
    orchestrator.compactor = compactor
    return orchestrator


async def _run(
    orchestrator: ChatOrchestrator, request: RespondRequest
) -> list[str]:
    outcome = await orchestrator.complete(
        request, api_key=TEST_API_KEY
    )
    assert outcome.stream is not None
    return await _drain(outcome.stream)


# ---- the marker never reaches the client -------------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize("marker", [FINAL_MARKER, TOOLS_MARKER])
async def test_the_control_line_never_reaches_a_delta(
    orchestrator: ChatOrchestrator, fake_provider: FakeProvider, marker: str
) -> None:
    fake_provider.stream_chunks = _chunks(f"{marker}\n", "All three agents are green.")
    events = await _run(orchestrator, _request())
    assert _content(events) == "All three agents are green."
    assert "DAINTREE" not in "".join(events)


@pytest.mark.asyncio
async def test_a_marker_split_across_chunks_still_never_leaks(
    orchestrator: ChatOrchestrator, fake_provider: FakeProvider
) -> None:
    """The straddled-chunk case is the one a naive per-chunk strip gets wrong."""
    fake_provider.stream_chunks = _chunks("[[DAIN", "TREE:FI", "NAL]]", "\nDone.")
    events = await _run(orchestrator, _request())
    assert _content(events) == "Done."


@pytest.mark.asyncio
async def test_a_reply_without_a_marker_is_unchanged(
    orchestrator: ChatOrchestrator, fake_provider: FakeProvider
) -> None:
    fake_provider.stream_chunks = _chunks("The three agents ", "are green.")
    events = await _run(orchestrator, _request())
    assert _content(events) == "The three agents are green."


@pytest.mark.asyncio
async def test_a_reply_that_is_only_a_marker_and_tool_calls_emits_no_empty_delta(
    orchestrator: ChatOrchestrator, fake_provider: FakeProvider
) -> None:
    """A continuation round is usually exactly this shape."""
    fake_provider.stream_chunks = [
        *_chunks(f"{TOOLS_MARKER}\n", finish_reason="tool_calls")[:2],
        _tool_call_chunk(),
        _chunks("x", finish_reason="tool_calls")[-1],
    ]
    events = await _run(orchestrator, _request())
    deltas = _of(events, "delta")
    # EXACTLY one delta, carrying only the consolidated tool call. An extra empty
    # `{}` delta would satisfy a "no content anywhere" assertion while still being
    # the bug — a marker-only fragment forwarded as an empty event.
    assert len(deltas) == 1
    assert set(deltas[0]) == {"tool_calls"}


# ---- what the declaration starts ---------------------------------------


@pytest.mark.asyncio
async def test_compaction_starts_before_the_reply_has_finished(
    orchestrator: ChatOrchestrator, fake_provider: FakeProvider
) -> None:
    """The whole point of putting the declaration first.

    The probe reads how far the upstream stream had been consumed at the moment
    the compactor was entered. Anything short of the full chunk list proves the
    call overlapped generation rather than waiting it out.
    """
    compactor = FakeCompactor(
        observer=lambda: fake_provider.last_stream._index  # noqa: SLF001
        if fake_provider.last_stream
        else -1
    )
    chunks = _chunks(f"{FINAL_MARKER}\n", "one ", "two ", "three ", "four")
    fake_provider.stream_factory = lambda _request: _YieldingStream(chunks)
    await _run(_with(orchestrator, compactor), _request())
    # EXACT, not merely "before the end": the role chunk and the marker chunk are
    # the only two consumed when the declaration resolves, so anything above 2
    # means the call waited for generation it was supposed to run beside. `< len`
    # would still pass if it started at the very last chunk.
    assert compactor.observed == [2]


@pytest.mark.asyncio
async def test_the_compactor_sees_the_settled_prefix_only(
    orchestrator: ChatOrchestrator, fake_provider: FakeProvider
) -> None:
    compactor = FakeCompactor()
    fake_provider.stream_chunks = _chunks(f"{FINAL_MARKER}\n", "green")
    await _run(_with(orchestrator, compactor), _request())
    assert len(compactor.calls) == 1
    call = compactor.calls[0]
    assert call.turn_id == "turn-1"
    # The live instruction ("where are we?") stays raw.
    assert [m.content for m in call.messages] == [
        "spawn three agents on the parser",
        "agent-1, agent-2 and agent-3 are up",
    ]


@pytest.mark.asyncio
async def test_a_tools_declaration_starts_nothing(
    orchestrator: ChatOrchestrator, fake_provider: FakeProvider
) -> None:
    compactor = FakeCompactor()
    fake_provider.stream_chunks = _chunks(f"{TOOLS_MARKER}\n", "Reading terminal t1.")
    await _run(_with(orchestrator, compactor), _request())
    assert compactor.calls == []


@pytest.mark.asyncio
async def test_a_missing_declaration_starts_nothing(
    orchestrator: ChatOrchestrator, fake_provider: FakeProvider
) -> None:
    """A miss costs one turn's savings; a wrong FINAL freezes history mid-task."""
    compactor = FakeCompactor()
    fake_provider.stream_chunks = _chunks("All three agents are green.")
    await _run(_with(orchestrator, compactor), _request())
    assert compactor.calls == []


@pytest.mark.asyncio
async def test_an_unavailable_compactor_is_never_called(
    orchestrator: ChatOrchestrator, fake_provider: FakeProvider
) -> None:
    compactor = FakeCompactor(available=False)
    fake_provider.stream_chunks = _chunks(f"{FINAL_MARKER}\n", "green")
    events = await _run(_with(orchestrator, compactor), _request())
    assert compactor.calls == []
    assert "compaction" not in _kinds(events)


@pytest.mark.asyncio
async def test_the_null_compactor_strips_the_line_and_emits_no_event(
    orchestrator: ChatOrchestrator, fake_provider: FakeProvider
) -> None:
    """Today's only shipped state.

    The rule is not even shipped in this state — the container omits the partial
    when `compactor.available` is False — so a real reply would carry no marker at
    all. What this pins is what happens if one arrives anyway (a stale client
    prompt, a model volunteering it): the observable stream is still clean. The
    line is gone, no task runs, no event is emitted, and `done` is still last.
    """
    fake_provider.stream_chunks = _chunks(f"{FINAL_MARKER}\n", "green")
    events = await _run(_with(orchestrator, NullCompactor()), _request())
    assert _content(events) == "green"
    assert _kinds(events)[-1] == "done"
    assert "compaction" not in _kinds(events)


@pytest.mark.asyncio
async def test_a_short_conversation_has_nothing_to_compact(
    orchestrator: ChatOrchestrator, fake_provider: FakeProvider
) -> None:
    compactor = FakeCompactor()
    request = RespondRequest.model_validate(
        {
            "protocol_version": 2,
            "session": {"id": "short", "turn_id": "turn-1"},
            "input": {"messages": [{"role": "user", "content": "hi"}]},
            "startup": {},
            "generation": {"stream": True},
        }
    )
    fake_provider.stream_chunks = _chunks(f"{FINAL_MARKER}\n", "hello")
    await _run(_with(orchestrator, compactor), request)
    assert compactor.calls == []


# ---- delivery ----------------------------------------------------------


@pytest.mark.asyncio
async def test_the_block_arrives_immediately_before_done(
    orchestrator: ChatOrchestrator, fake_provider: FakeProvider
) -> None:
    """`done` stays terminal: a reader that stops on it loses nothing."""
    compactor = FakeCompactor(content="agent-1 at /w/a; blocked on review")
    fake_provider.stream_chunks = _chunks(f"{FINAL_MARKER}\n", "All green.")
    events = await _run(_with(orchestrator, compactor), _request())
    kinds = _kinds(events)
    assert kinds[-2:] == ["compaction", "done"]
    [block] = _of(events, "compaction")
    assert block["turn_id"] == "turn-1"
    assert block["replaces"] == {"start_index": 0, "end_index": 2}
    assert block["block"] == {
        "role": "user",
        "name": COMPACTION_BLOCK_NAME,
        "content": "agent-1 at /w/a; blocked on review",
    }


@pytest.mark.asyncio
async def test_the_span_splices_the_client_s_own_message_array(
    orchestrator: ChatOrchestrator, fake_provider: FakeProvider
) -> None:
    request = _request()
    fake_provider.stream_chunks = _chunks(f"{FINAL_MARKER}\n", "All green.")
    events = await _run(_with(orchestrator, FakeCompactor()), request)
    [block] = _of(events, "compaction")
    sent = [m.content for m in request.input.messages]
    spliced = (
        sent[: block["replaces"]["start_index"]]
        + [block["block"]["content"]]
        + sent[block["replaces"]["end_index"] :]
    )
    assert spliced == [block["block"]["content"], "where are we?"]


@pytest.mark.asyncio
async def test_a_final_declaration_followed_by_a_tool_call_delivers_nothing(
    orchestrator: ChatOrchestrator, fake_provider: FakeProvider
) -> None:
    """The model contradicting itself is the case the whole gate exists for."""
    compactor = FakeCompactor()
    fake_provider.stream_chunks = [
        *_chunks(f"{FINAL_MARKER}\n", "Let me look.", finish_reason="tool_calls")[:3],
        _tool_call_chunk(),
        _chunks("x", finish_reason="tool_calls")[-1],
    ]
    events = await _run(_with(orchestrator, compactor), _request())
    assert compactor.calls  # it was speculated on...
    assert "compaction" not in _kinds(events)  # ...and then thrown away


@pytest.mark.asyncio
async def test_a_truncated_reply_delivers_nothing(
    orchestrator: ChatOrchestrator, fake_provider: FakeProvider
) -> None:
    """`length` is a turn cut short, not a turn completed."""
    compactor = FakeCompactor()
    fake_provider.stream_chunks = _chunks(
        f"{FINAL_MARKER}\n", "All gre", finish_reason="length"
    )
    events = await _run(_with(orchestrator, compactor), _request())
    # It must have been SPECULATED on, or this test would pass with declaration
    # handling entirely broken.
    assert compactor.calls
    assert "compaction" not in _kinds(events)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "compactor",
    [
        FakeCompactor(content=None),
        FakeCompactor(content="   "),
        FakeCompactor(error=RuntimeError("upstream exploded")),
    ],
    ids=["nothing", "blank", "raised"],
)
async def test_a_failed_compaction_still_completes_the_turn(
    orchestrator: ChatOrchestrator, fake_provider: FakeProvider, compactor: Any
) -> None:
    fake_provider.stream_chunks = _chunks(f"{FINAL_MARKER}\n", "All green.")
    events = await _run(_with(orchestrator, compactor), _request())
    assert "compaction" not in _kinds(events)
    assert _kinds(events)[-1] == "done"
    assert _content(events) == "All green."


@pytest.mark.asyncio
async def test_an_oversized_block_is_discarded_whole(
    orchestrator: ChatOrchestrator, fake_provider: FakeProvider
) -> None:
    """Truncating would cut exactly the identifiers the block exists to keep."""
    orchestrator.settings = orchestrator.settings.model_copy(
        update={"cap_compaction_block_bytes": 16}
    )
    fake_provider.stream_chunks = _chunks(f"{FINAL_MARKER}\n", "All green.")
    events = await _run(_with(orchestrator, FakeCompactor(content="x" * 64)), _request())
    assert "compaction" not in _kinds(events)
    assert _kinds(events)[-1] == "done"


@pytest.mark.asyncio
async def test_a_block_the_wire_model_rejects_is_not_an_exception(
    orchestrator: ChatOrchestrator, fake_provider: FakeProvider
) -> None:
    """`_run_compaction` must be total: nothing escapes onto a detached task.

    The byte cap is a setting, so raising it far enough lets a block clear it and
    still fail `CompactionBlock`'s own code-point limit. An exception escaping
    there would sit unretrieved on a task the terminal path may have cancelled
    rather than awaited.
    """
    orchestrator.settings = orchestrator.settings.model_copy(
        update={"cap_compaction_block_bytes": 4 * 1024 * 1024}
    )
    fake_provider.stream_chunks = _chunks(f"{FINAL_MARKER}\n", "All green.")
    events = await _run(
        _with(orchestrator, FakeCompactor(content="x" * 300_000)), _request()
    )
    assert "compaction" not in _kinds(events)
    assert _kinds(events)[-1] == "done"
    assert _content(events) == "All green."


@pytest.mark.asyncio
async def test_a_slow_compaction_does_not_hold_the_terminal_event(
    orchestrator: ChatOrchestrator, fake_provider: FakeProvider
) -> None:
    """The block is an optimisation for the NEXT turn, not a debt on this one."""
    monkeypatched = FakeCompactor(delay=30.0)
    fake_provider.stream_chunks = _chunks(f"{FINAL_MARKER}\n", "All green.")
    import daintree_assistant_server.services.chat_orchestrator as module

    original = module._COMPACTION_GRACE_SECONDS
    module._COMPACTION_GRACE_SECONDS = 0.01
    try:
        events = await asyncio.wait_for(
            _run(_with(orchestrator, monkeypatched), _request()), timeout=5
        )
    finally:
        module._COMPACTION_GRACE_SECONDS = original
    assert "compaction" not in _kinds(events)
    assert _kinds(events)[-1] == "done"
    assert monkeypatched.cancellations == 1


@pytest.mark.asyncio
async def test_a_sub_agent_never_compacts(
    orchestrator: ChatOrchestrator, fake_provider: FakeProvider
) -> None:
    """Its conversation is bounded and thrown away; there is no next turn."""
    compactor = FakeCompactor()
    fake_provider.stream_chunks = _chunks(f"{FINAL_MARKER}\n", "green")
    await _run(
        _with(orchestrator, compactor), _request(tools=False, profile="subagent")
    )
    assert compactor.calls == []


@pytest.mark.asyncio
async def test_a_bare_intent_re_roll_cancels_the_speculative_call(
    orchestrator: ChatOrchestrator, fake_provider: FakeProvider
) -> None:
    """Attempt 0 declared FINAL about a draft that is being thrown away.

    The re-roll deliberately starts no replacement: a round whose output shape was
    already wrong once is not the one to trust with freezing history.
    """
    compactor = FakeCompactor(delay=30.0)
    dead = "All five votes are in. Let me read them, one read per terminal, batched."
    # A YIELDING stream on both attempts: the speculative task has to actually be
    # SCHEDULED for its cancellation to be observable at all, and `FakeStream`
    # awaits nothing.
    fake_provider.stream_factory = lambda request: _YieldingStream(
        _chunks(f"{TOOLS_MARKER}\n", "Tallied.")
        if any(
            "previous draft was discarded" in (m.get("content") or "")
            for m in request.messages
        )
        else _chunks(f"{FINAL_MARKER}\n", dead)
    )
    events = await _run(_with(orchestrator, compactor), _request())
    assert len(fake_provider.stream_requests) == 2, "the re-roll must have happened"
    assert len(compactor.calls) == 1, "and no second call was started"
    assert compactor.cancellations == 1
    assert "compaction" not in _kinds(events)
    assert _content(events) == "Tallied."


@pytest.mark.asyncio
async def test_a_marker_sharing_a_delta_with_content_and_tool_calls(
    orchestrator: ChatOrchestrator, fake_provider: FakeProvider
) -> None:
    """One chunk carrying all three is the case a naive early `continue` breaks."""
    mixed = _tool_call_chunk()
    mixed["choices"][0]["delta"]["content"] = f"{FINAL_MARKER}\nLooking now."
    fake_provider.stream_chunks = [
        _chunks("x")[0],
        mixed,
        _chunks("x", finish_reason="tool_calls")[-1],
    ]
    events = await _run(_with(orchestrator, FakeCompactor()), _request())
    assert "DAINTREE" not in "".join(events)
    assert _content(events) == "Looking now."
    assert any(d.get("tool_calls") for d in _of(events, "delta"))
    # Declared FINAL, then called a tool: speculated on, never delivered.
    assert "compaction" not in _kinds(events)


@pytest.mark.asyncio
async def test_a_reply_that_ends_mid_marker_still_reaches_the_client(
    orchestrator: ChatOrchestrator, fake_provider: FakeProvider
) -> None:
    """`scanner.finish()` owes the client every character it was holding."""
    fake_provider.stream_chunks = _chunks("[[DAINT")
    events = await _run(orchestrator, _request())
    assert _content(events) == "[[DAINT"
    assert _kinds(events)[-1] == "done"


@pytest.mark.asyncio
async def test_a_mid_stream_failure_abandons_the_speculative_call(
    orchestrator: ChatOrchestrator, fake_provider: FakeProvider
) -> None:
    """An errored stream can never deliver, so the call it carried is pure waste."""
    from daintree_assistant_server.errors import upstream_unavailable

    compactor = FakeCompactor(delay=30.0)
    chunks = _chunks(f"{FINAL_MARKER}\n", "one ", "two")
    fake_provider.stream_factory = lambda _request: FakeStream(
        chunks, error_at=3, error=upstream_unavailable("gone")
    )
    events = await _run(_with(orchestrator, compactor), _request())
    assert _kinds(events)[-1] == "error"
    assert "compaction" not in _kinds(events)
    assert compactor.cancellations == 1


@pytest.mark.asyncio
async def test_the_block_limit_is_measured_in_bytes_not_characters(
    orchestrator: ChatOrchestrator, fake_provider: FakeProvider
) -> None:
    """A multibyte block that fits by character count can still bust the budget."""
    orchestrator.settings = orchestrator.settings.model_copy(
        update={"cap_compaction_block_bytes": 64}
    )
    fake_provider.stream_chunks = _chunks(f"{FINAL_MARKER}\n", "All green.")
    # 40 code points, 120 UTF-8 bytes.
    events = await _run(_with(orchestrator, FakeCompactor(content="\u4f60" * 40)), _request())
    assert "compaction" not in _kinds(events)
    assert _kinds(events)[-1] == "done"


# ---- the non-streaming path --------------------------------------------


@pytest.mark.asyncio
async def test_a_compactor_that_raises_on_availability_does_not_fail_the_turn(
    orchestrator: ChatOrchestrator, fake_provider: FakeProvider
) -> None:
    """This runs INSIDE the delta loop, mid-reply.

    `available` is a property a real producer computes — a key, a health check.
    An exception escaping there would turn a reply the user is already reading
    into a terminal error, for an optimisation they never asked for.
    """

    class _Exploding:
        @property
        def available(self) -> bool:
            raise RuntimeError("config refresh failed")

        async def compact(self, request: Any) -> None:  # pragma: no cover
            return None

    fake_provider.stream_chunks = _chunks(f"{FINAL_MARKER}\n", "All green.")
    events = await _run(_with(orchestrator, _Exploding()), _request())
    assert _content(events) == "All green."
    assert _kinds(events)[-1] == "done"
    assert "error" not in _kinds(events)


@pytest.mark.asyncio
async def test_the_non_streaming_body_carries_no_control_line(
    orchestrator: ChatOrchestrator, fake_provider: FakeProvider
) -> None:
    compactor = FakeCompactor()
    fake_provider.complete_result = make_result(
        content=f"{FINAL_MARKER}\nAll three agents are green."
    )
    outcome = await _with(orchestrator, compactor).complete(
        _request(stream=False),

        api_key=TEST_API_KEY,
    )
    assert outcome.body is not None
    assert outcome.body["message"]["content"] == "All three agents are green."
    # No SSE channel to deliver on, and nothing left to overlap with.
    assert compactor.calls == []


@pytest.mark.asyncio
async def test_the_non_streaming_path_strips_exactly_one_control_line(
    orchestrator: ChatOrchestrator, fake_provider: FakeProvider
) -> None:
    """Stripping is not idempotent, and the path runs it around the repair.

    A reply that legitimately QUOTES the marker on its second line would lose
    that line to a second pass — and the two response modes would then disagree
    about what the user is owed, since streaming removes only the first.
    """
    fake_provider.complete_result = make_result(
        content=f"{FINAL_MARKER}\n{TOOLS_MARKER}\nThat is the marker you asked about."
    )
    outcome = await orchestrator.complete(
        _request(stream=False, tools=False),

        api_key=TEST_API_KEY,
    )
    assert outcome.body is not None
    assert outcome.body["message"]["content"] == (
        f"{TOOLS_MARKER}\nThat is the marker you asked about."
    )
