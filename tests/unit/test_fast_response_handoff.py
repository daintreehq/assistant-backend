"""The handoff into the executor, and the cache property it must not break.

Doc section 15.3. Every invariant here is one that fails silently: the request
still assembles, the model still answers, and the damage shows up as a cache miss,
a confused executor, or a client transcript that disagrees with what the model
believes it said.
"""

from __future__ import annotations

from typing import Any

import pytest

from daintree_assistant_server.config import EXECUTOR_MODEL, Settings
from daintree_assistant_server.contracts.daintree_api import RespondRequest
from daintree_assistant_server.prompts.loader import PromptLoader
from daintree_assistant_server.runbooks.catalog import default_runbooks_dir, load_catalog
from daintree_assistant_server.services.prompt_assembler import (
    FastResponseHandoff,
    PromptAssembler,
)

CATALOG = load_catalog(default_runbooks_dir())
FOUNDATION = CATALOG.get("daintree.foundation")
assert FOUNDATION is not None

PREAMBLE = "I'll trace the failing build and make the smallest change that fixes it."
CONTINUATION = "[Daintree continuation — server-owned instruction]\nContinue the task."


def _assembler() -> PromptAssembler:
    return PromptAssembler(
        PromptLoader(), Settings(STATE_SIGNING_KEY_CURRENT="x" * 32, TOGETHER_API_KEY="k")
    )


def _request(**overrides: Any) -> RespondRequest:
    body: dict[str, Any] = {
        "protocol_version": 3,
        "session": {"id": "s", "turn_id": "t"},
        "startup": {"project": {"id": "p", "name": "N", "path": "/p"}},
        "input": {"messages": [{"role": "user", "content": "fix the build"}]},
    }
    body.update(overrides)
    return RespondRequest.model_validate(body)


def _build(handoff: FastResponseHandoff | None) -> tuple[Any, list[Any]]:
    return _assembler().build_main_request_with_segments(
        request=_request(),
        runbooks=[FOUNDATION],
        upstream_model=EXECUTOR_MODEL,
        handoff=handoff,
    )


def _handoff() -> FastResponseHandoff:
    return FastResponseHandoff(content=PREAMBLE, continuation_message=CONTINUATION)


# ---------------------------------------------------------------------------
# The cache property. This is the one that pays for the whole layout.
# ---------------------------------------------------------------------------


def test_the_system_and_tools_prefix_is_byte_identical_with_and_without_a_handoff() -> None:
    """The entire argument for appending after the tail.

    The chat template serialises [all system rows] -> [tools] -> [conversation],
    so anything inserted above the tail re-encodes ~21.5k tokens of tool schemas.
    Appending at the very end means a preamble costs its own bytes and nothing
    else — which is also what makes `FAST_RESPONSE_MODE=off` a genuine no-op on
    the wire rather than a different-but-similar request.
    """
    plain, _ = _build(None)
    with_handoff, _ = _build(_handoff())

    system_plain = [m for m in plain.messages if m.get("role") == "system"]
    system_with = [m for m in with_handoff.messages if m.get("role") == "system"]
    assert system_plain == system_with
    assert plain.tools == with_handoff.tools


def test_the_handoff_is_purely_additive_to_the_message_array() -> None:
    """Not merely "the prefix matches" — EVERY prior row is untouched and in
    order, so nothing below the system region shifts either."""
    plain, _ = _build(None)
    with_handoff, _ = _build(_handoff())

    assert with_handoff.messages[: len(plain.messages)] == plain.messages
    assert len(with_handoff.messages) == len(plain.messages) + 2


def test_the_two_rows_are_appended_after_the_volatile_tail() -> None:
    with_handoff, segments = _build(_handoff())

    assert [s.id for s in segments][-2:] == [
        "fast_response_handoff",
        "continuation_control",
    ]
    assert with_handoff.messages[-2]["role"] == "assistant"
    assert with_handoff.messages[-1]["role"] == "user"


def test_no_new_system_message_is_ever_added() -> None:
    """A system row anywhere in the array lands ahead of the tool schemas, which
    is the exact mistake the layout exists to prevent."""
    plain, _ = _build(None)
    with_handoff, _ = _build(_handoff())

    def system_count(request: Any) -> int:
        return sum(1 for m in request.messages if m.get("role") == "system")

    assert system_count(with_handoff) == system_count(plain)


# ---------------------------------------------------------------------------
# Exactness and ordering.
# ---------------------------------------------------------------------------


def test_the_assistant_row_is_the_visible_preamble_byte_for_byte() -> None:
    """If these diverge, the client's committed history and the model's view of
    its own last turn disagree, and every later round reasons about a
    conversation that did not happen."""
    with_handoff, _ = _build(_handoff())
    assert with_handoff.messages[-2]["content"].encode("utf-8") == PREAMBLE.encode("utf-8")


def test_the_synthetic_continuation_is_the_final_message() -> None:
    """Load-bearing, not decoration: without it the executor reads the assistant
    row above as evidence that the work already happened."""
    with_handoff, _ = _build(_handoff())
    assert with_handoff.messages[-1]["content"] == CONTINUATION


def test_the_client_transcript_is_not_modified() -> None:
    """Server-generated control material stays structurally separate from what
    the client actually sent, so nothing downstream has to guess which is which."""
    request = _request()
    original = [m.model_dump() for m in request.input.messages]
    _assembler().build_main_request_with_segments(
        request=request,
        runbooks=[FOUNDATION],
        upstream_model=EXECUTOR_MODEL,
        handoff=_handoff(),
    )
    assert [m.model_dump() for m in request.input.messages] == original


def test_the_user_turn_still_appears_before_the_handoff() -> None:
    with_handoff, _ = _build(_handoff())
    rendered = [str(m.get("content")) for m in with_handoff.messages]
    user_at = next(i for i, c in enumerate(rendered) if "fix the build" in c)
    assert user_at < len(with_handoff.messages) - 2


def test_no_handoff_means_no_extra_rows_at_all() -> None:
    """The `off` path must be indistinguishable from the feature not existing."""
    plain, segments = _build(None)
    ids = [s.id for s in segments]
    assert "fast_response_handoff" not in ids
    assert "continuation_control" not in ids
    assert plain.messages[-1]["role"] != "assistant"


# ---------------------------------------------------------------------------
# The runbooks both stages see.
# ---------------------------------------------------------------------------


def test_the_executor_sees_the_same_runbook_bodies_the_fast_model_did() -> None:
    """The invariant the preview's accuracy rests on. If the two stages read
    different procedures, the preamble describes work the executor will not do."""
    from daintree_assistant_server.services.fast_response import FastResponseService

    service = FastResponseService(
        settings=Settings(STATE_SIGNING_KEY_CURRENT="x" * 32, TOGETHER_API_KEY="k"),
        prompt_loader=PromptLoader(),
        provider=None,
    )
    fast_messages = service.build_messages(_request(), active_runbooks=[FOUNDATION])
    assert fast_messages is not None
    fast_text = "\n".join(str(m["content"]) for m in fast_messages)

    executor, _ = _build(_handoff())
    executor_text = "\n".join(str(m.get("content")) for m in executor.messages)

    assert FOUNDATION.body in fast_text
    assert FOUNDATION.body in executor_text


@pytest.mark.parametrize("content", ["", "   ", "\n"])
def test_an_empty_preamble_never_becomes_a_handoff(content: str) -> None:
    """Guarded upstream, but asserted here too: an empty assistant row would give
    the executor a blank prior turn to continue from."""
    from daintree_assistant_server.services.fast_response import FastResponseResult

    result = FastResponseResult(content=content or None, source="model")
    assert result.has_content is False


def test_a_subagent_can_never_receive_a_handoff() -> None:
    """Eligibility already refuses the profile; this is the structural guard.

    Silently dropping the pair would be worse than raising: the executor's view of
    its own last turn would then differ from what was shown, which is exactly the
    divergence the handoff exists to prevent.
    """
    with pytest.raises(ValueError, match="sub-agent"):
        _assembler().build_main_request_with_segments(
            request=_request(profile="subagent"),
            runbooks=[FOUNDATION],
            upstream_model=EXECUTOR_MODEL,
            handoff=_handoff(),
        )


def test_a_bare_intent_re_roll_keeps_the_preamble_and_adds_only_the_nudge() -> None:
    """A re-roll is an executor retry INSIDE one backend request.

    It must not call the fast model again — the preamble is already visible and
    already part of the executor's context — and it must not disturb the handoff
    pair, or the model would be re-rolling against a different prior turn than the
    one the user saw.
    """
    from daintree_assistant_server.services.chat_orchestrator import ChatOrchestrator

    original, _ = _build(_handoff())
    reroll = ChatOrchestrator._bare_intent_repair_request(original)

    # Everything the first attempt sent is still there, in order...
    assert reroll.messages[: len(original.messages)] == original.messages
    # ...the preamble and continuation among it...
    assert reroll.messages[len(original.messages) - 2]["content"] == PREAMBLE
    assert reroll.messages[len(original.messages) - 1]["content"] == CONTINUATION
    # ...and the nudge is the only addition, as the final user message.
    assert len(reroll.messages) == len(original.messages) + 1
    assert reroll.messages[-1]["role"] == "user"
    assert "discarded" in reroll.messages[-1]["content"]


@pytest.mark.asyncio
async def test_a_skipped_fast_response_carries_its_specific_reason() -> None:
    """`_collect_fast_response` must not collapse every skip to one generic
    reason: `should_generate_fast_response` already computes a specific one per
    case (`no_active_runbooks`, `continuation_round`, ...), and nothing else in
    the turn observes it — this is the only place it can survive to."""
    from daintree_assistant_server.services.chat_orchestrator import ChatOrchestrator

    result = await ChatOrchestrator._collect_fast_response(
        None, request_id="r1", skip_reason="no_active_runbooks"
    )
    assert result.source == "none"
    assert result.reason == "no_active_runbooks"

    # The default is preserved for a caller that omits the reason entirely.
    default = await ChatOrchestrator._collect_fast_response(None, request_id="r1")
    assert default.reason == "not_eligible"


# ---------------------------------------------------------------------------
# The hidden plan. It rides the CONTINUATION row and nothing else.
# ---------------------------------------------------------------------------

PLAN_SENTINEL = "SENTINEL_PLAN_terminal.list -> terminal.end x5"


def _handoff_with_plan() -> FastResponseHandoff:
    return FastResponseHandoff(
        content=PREAMBLE, continuation_message=CONTINUATION, plan=PLAN_SENTINEL
    )


def test_the_assistant_row_stays_byte_identical_even_when_a_plan_exists() -> None:
    """The load-bearing one. The assistant row mirrors what the user was SHOWN, and
    the plan is the half they were not. Appending it there would put text in the
    client's committed history that never appeared on screen, and every later
    round would reason about a conversation that did not happen."""
    _, segments = _build(_handoff_with_plan())
    rows = [s for s in segments if s.id == "fast_response_handoff"]
    assert len(rows) == 1
    messages, _ = _build(_handoff_with_plan())
    assistant = [m for m in messages.messages if m["role"] == "assistant"]
    assert assistant[-1]["content"] == PREAMBLE
    assert PLAN_SENTINEL not in assistant[-1]["content"]
    assert "planned_route" not in assistant[-1]["content"]


def test_the_plan_rides_the_continuation_row_exactly_once() -> None:
    """Framed and delimited rather than concatenated: the block is model-authored
    text derived from a conversation containing user input, so the executor has to
    be told at the point of use that it is a route and not a record."""
    messages, _ = _build(_handoff_with_plan())
    final = messages.messages[-1]
    assert final["role"] == "user"
    assert final["content"].count(PLAN_SENTINEL) == 1
    assert "<planned_route>" in final["content"]
    # Both limits are stated next to the content, which is where framing works.
    assert "never a record" in final["content"]
    assert "yours\nare authoritative" in final["content"] or "authoritative" in final["content"]


def test_a_handoff_without_a_plan_adds_no_route_framing_at_all() -> None:
    """The overwhelmingly common shape: the model wrote prose and no block. The
    continuation row must then be exactly the server-owned message, or every
    plan-less turn pays bytes for a wrapper around nothing."""
    messages, _ = _build(_handoff())
    assert messages.messages[-1]["content"] == CONTINUATION
    assert "planned_route" not in messages.messages[-1]["content"]


def test_the_plan_adds_no_extra_message_rows() -> None:
    """It rides an EXISTING row. A third row would be a new position in the array
    for every downstream reader to account for, and the manifest names rows by
    layer — an unnamed one would send a reader chasing a segment that is not there."""
    with_plan, seg_with = _build(_handoff_with_plan())
    without, seg_without = _build(_handoff())
    assert len(with_plan.messages) == len(without.messages)
    assert [s.id for s in seg_with] == [s.id for s in seg_without]
