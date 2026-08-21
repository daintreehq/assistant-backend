"""Which span of history a turn may compact, and the seam that compacts it.

The span rules carry the append-only guarantee. Everything else about compaction
is best-effort and recoverable; feeding a frozen block back through a compactor is
not, because the loss compounds silently and nothing in the output says it
happened. So the boundaries are pinned here rather than left to the producer.
"""

from __future__ import annotations

from daintree_assistant_server.contracts.daintree_api import (
    COMPACTION_BLOCK_NAME,
    CompactionBlock,
    CompactionSpan,
    StreamCompaction,
)
from daintree_assistant_server.contracts.openai import ChatMessage
from daintree_assistant_server.services.compaction import (
    CompactionRequest,
    Compactor,
    NullCompactor,
    select_source,
)


def _user(text: str) -> ChatMessage:
    return ChatMessage(role="user", content=text)


def _assistant(text: str) -> ChatMessage:
    return ChatMessage(role="assistant", content=text)


def _block(text: str = "state") -> ChatMessage:
    return ChatMessage(role="user", name=COMPACTION_BLOCK_NAME, content=text)


# ---- span selection ----------------------------------------------------


def test_the_live_instruction_and_everything_after_it_stay_raw() -> None:
    """A task still running has no ending yet; reconciling it would invent one."""
    messages = [
        _user("start the run"),
        _assistant("started"),
        _user("what is the status?"),
        _assistant("checking"),
    ]
    source = select_source(messages)
    assert source is not None
    assert (source.start, source.end) == (0, 2)
    assert source.messages == tuple(messages[0:2])


def test_a_span_resumes_after_the_last_frozen_block() -> None:
    messages = [_block(), _user("a"), _assistant("b"), _user("live")]
    source = select_source(messages)
    assert source is not None
    assert (source.start, source.end) == (1, 3)
    assert all(m.name != COMPACTION_BLOCK_NAME for m in source.messages)


def test_only_the_last_block_moves_the_boundary() -> None:
    """Append-only: several blocks accumulate, and the newest one wins."""
    messages = [_block("older"), _user("a"), _block("newer"), _user("b"), _user("live")]
    source = select_source(messages)
    assert source is not None
    assert source.start == 3
    assert source.messages == (messages[3],)


def test_nothing_to_compact_returns_none() -> None:
    assert select_source([_user("hello")]) is None
    assert select_source([]) is None


def test_a_turn_straight_after_a_block_has_nothing_left() -> None:
    assert select_source([_block(), _user("live")]) is None


def test_a_conversation_with_no_user_message_yields_nothing() -> None:
    assert select_source([_assistant("unprompted")]) is None


def test_a_tool_round_is_never_split_from_its_call() -> None:
    """The span always ends on a user message, so a call keeps its results."""
    messages = [
        _user("do it"),
        ChatMessage(
            role="assistant",
            tool_calls=[
                {
                    "id": "call_1",
                    "type": "function",
                    "function": {"name": "terminal__read", "arguments": "{}"},
                }
            ],
        ),
        ChatMessage(role="tool", tool_call_id="call_1", content="output"),
        _assistant("done"),
        _user("and now this"),
    ]
    source = select_source(messages)
    assert source is not None
    assert (source.start, source.end) == (0, 4)
    roles = [m.role for m in source.messages]
    assert roles == ["user", "assistant", "tool", "assistant"]


# ---- the seam ----------------------------------------------------------


async def test_the_null_compactor_is_unavailable_and_produces_nothing() -> None:
    compactor = NullCompactor()
    assert isinstance(compactor, Compactor)
    assert compactor.available is False
    result = await compactor.compact(
        CompactionRequest(
            request_id="req",
            turn_id="turn",
            messages=(_user("hi"),),
            max_output_bytes=1024,
        )
    )
    assert result is None


# ---- wire shape --------------------------------------------------------


def test_the_block_carries_the_reserved_name_so_the_next_turn_sees_it() -> None:
    block = CompactionBlock(content="agent-1 at /w/a, blocked on review")
    assert block.role == "user"
    assert block.name == COMPACTION_BLOCK_NAME


def test_the_event_splices_without_guessing() -> None:
    messages = ["m0", "m1", "m2", "m3"]
    event = StreamCompaction(
        turn_id="turn-7",
        replaces=CompactionSpan(start_index=1, end_index=3),
        block=CompactionBlock(content="state"),
    )
    spliced = (
        messages[: event.replaces.start_index]
        + [event.block.content]
        + messages[event.replaces.end_index :]
    )
    assert spliced == ["m0", "state", "m3"]


def test_the_event_is_strict_about_its_own_shape() -> None:
    import pytest
    from pydantic import ValidationError

    with pytest.raises(ValidationError):
        CompactionBlock(content="")
    with pytest.raises(ValidationError):
        CompactionSpan(start_index=0, end_index=0)
    with pytest.raises(ValidationError):
        StreamCompaction(
            turn_id="t",
            replaces=CompactionSpan(start_index=0, end_index=1),
            block=CompactionBlock(content="x"),
            extra="nope",  # type: ignore[call-arg]
        )
