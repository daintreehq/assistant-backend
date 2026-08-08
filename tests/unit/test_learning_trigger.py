"""Guard-matrix coverage for the post-turn skill-learning trigger.

``tests/unit/test_skill_learning.py`` already drives both real call sites end to
end through ``complete()``. What it does NOT isolate is the guard matrix itself:
those tests set every "don't learn" signal at once, so a regression that dropped
one half of a condition would still pass. These exercise
``_maybe_learn_after_turn`` directly, one guard at a time, plus the task
bookkeeping (strong reference held, discarded on completion) that the end-to-end
tests never inspect.
"""

from __future__ import annotations

import asyncio
from typing import Any

import pytest
from conftest import respond_body
from fakes import FakeProvider
from pydantic import SecretStr

from daintree_assistant_server.contracts.daintree_api import RespondRequest
from daintree_assistant_server.contracts.openai import ChatMessage
from daintree_assistant_server.services.chat_orchestrator import ChatOrchestrator
from daintree_assistant_server.services.skill_learning import LearningOutcome


class RecordingLearner:
    """Stands in for ``SkillLearner``: records calls, never touches disk."""

    def __init__(self, *, enabled: bool = True, boom: bool = False) -> None:
        self.enabled = enabled
        self._boom = boom
        self.calls: list[dict[str, Any]] = []
        self.started = asyncio.Event()

    async def run(
        self,
        *,
        request: Any,
        active_ids: list[str],
        api_key: SecretStr,
        trailing_messages: tuple[ChatMessage, ...] = (),
    ) -> LearningOutcome:
        self.calls.append(
            {
                "request": request,
                "active_ids": active_ids,
                "api_key": api_key,
                "trailing": list(trailing_messages),
            }
        )
        self.started.set()
        if self._boom:
            raise RuntimeError("learner exploded")
        return LearningOutcome(changed=False)


@pytest.fixture
def learning_orchestrator(
    settings: Any,
    catalog_manager: Any,
    selector: Any,
    state_codec: Any,
    assembler: Any,
    fake_provider: FakeProvider,
) -> Any:
    def _build(learner: RecordingLearner | None) -> ChatOrchestrator:
        return ChatOrchestrator(
            settings=settings,
            catalog_manager=catalog_manager,
            selector=selector,
            state_codec=state_codec,
            assembler=assembler,
            provider=fake_provider,
            learner=learner,
        )

    return _build


@pytest.fixture
def respond_request() -> RespondRequest:
    return RespondRequest.model_validate(
        respond_body([{"role": "user", "content": "do the thing"}])
    )


async def _settle(orch: ChatOrchestrator) -> None:
    """Let the detached task run, then drain it deterministically."""
    await orch.drain_background_tasks(timeout=5.0)


def _fire(orch: ChatOrchestrator, request: Any, **kw: Any) -> None:
    defaults: dict[str, Any] = {
        "request": request,
        "active_ids": ["daintree.test.alpha"],
        "assistant_text": "here is the answer",
        "had_tool_calls": False,
        "api_key": SecretStr("test-caller-key"),
    }
    defaults.update(kw)
    orch._maybe_learn_after_turn(**defaults)


@pytest.mark.asyncio
async def test_fires_on_a_completed_turn(learning_orchestrator, respond_request) -> None:
    learner = RecordingLearner()
    orch = learning_orchestrator(learner)
    _fire(orch, respond_request)
    await _settle(orch)

    assert len(learner.calls) == 1
    call = learner.calls[0]
    assert call["active_ids"] == ["daintree.test.alpha"]
    # The just-generated answer is appended so learning sees the FINISHED workflow;
    # the request's own message list stops one turn short of it.
    assert [m.role for m in call["trailing"]] == ["assistant"]
    assert call["trailing"][0].content == "here is the answer"


@pytest.mark.asyncio
async def test_skips_a_tool_continuation_round(learning_orchestrator, respond_request) -> None:
    # A turn ending in a tool call is a half-finished workflow — learning must wait
    # for the round that actually closes it.
    learner = RecordingLearner()
    orch = learning_orchestrator(learner)
    _fire(orch, respond_request, had_tool_calls=True)
    await _settle(orch)
    assert learner.calls == []


@pytest.mark.asyncio
@pytest.mark.parametrize("text", ["", "   ", "\n\t "])
async def test_skips_a_blank_answer(learning_orchestrator, respond_request, text: str) -> None:
    learner = RecordingLearner()
    orch = learning_orchestrator(learner)
    _fire(orch, respond_request, assistant_text=text)
    await _settle(orch)
    assert learner.calls == []


@pytest.mark.asyncio
async def test_skips_when_learning_is_disabled(learning_orchestrator, respond_request) -> None:
    learner = RecordingLearner(enabled=False)
    orch = learning_orchestrator(learner)
    _fire(orch, respond_request)
    await _settle(orch)
    assert learner.calls == []


@pytest.mark.asyncio
async def test_no_learner_is_a_no_op(learning_orchestrator, respond_request) -> None:
    orch = learning_orchestrator(None)
    _fire(orch, respond_request)  # must not raise
    await _settle(orch)


@pytest.mark.asyncio
async def test_task_is_registered_so_it_is_not_garbage_collected(
    learning_orchestrator, respond_request
) -> None:
    # asyncio only holds a WEAK reference to a bare create_task result, so an
    # unregistered fire-and-forget task can be collected mid-flight.
    learner = RecordingLearner()
    orch = learning_orchestrator(learner)
    _fire(orch, respond_request)
    assert len(orch._background_tasks) == 1
    await _settle(orch)
    # ...and the done-callback discards it again, so the set cannot grow unbounded.
    assert orch._background_tasks == set()


@pytest.mark.asyncio
async def test_a_raising_learner_never_escapes(learning_orchestrator, respond_request) -> None:
    """Learning must never break a turn — the response is already sent by now.

    Awaited DIRECTLY rather than through ``drain_background_tasks``: that drains
    with ``asyncio.wait``, which never re-raises a completed task's stored
    exception, so draining would pass even with the guard removed.
    """
    learner = RecordingLearner(boom=True)
    orch = learning_orchestrator(learner)
    await orch._run_learner_background(
        respond_request, ["daintree.test.alpha"], [], SecretStr("test-caller-key")
    )
    assert len(learner.calls) == 1


@pytest.mark.asyncio
async def test_a_raising_learner_is_not_left_unretrieved(
    learning_orchestrator, respond_request
) -> None:
    """The spawned task must complete without an unretrieved exception."""
    learner = RecordingLearner(boom=True)
    orch = learning_orchestrator(learner)
    _fire(orch, respond_request)
    task = next(iter(orch._background_tasks))
    await task
    assert task.exception() is None  # swallowed by the guarded wrapper, not stored
    assert orch._background_tasks == set()
