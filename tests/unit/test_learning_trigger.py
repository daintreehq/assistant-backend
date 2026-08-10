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
import json
from typing import Any

import pytest
import structlog.testing
from conftest import respond_body
from fakes import FakeProvider
from pydantic import SecretStr

from daintree_assistant_server.contracts.daintree_api import RespondRequest
from daintree_assistant_server.contracts.openai import ChatMessage
from daintree_assistant_server.services import chat_orchestrator as chat_orchestrator_module
from daintree_assistant_server.services.chat_orchestrator import ChatOrchestrator
from daintree_assistant_server.services.skill_learning import LearningOutcome

# Verbatim from session ses_659b0fc9 (issue #26): finish_reason "stop", zero tool
# calls, 20/43/32 completion tokens. Each ANNOUNCED a read and emitted no call, so
# the round accomplished nothing and there is no workflow to learn from. The last
# one is scripted by our own prompt (main/base/10-hard-rules.md), which makes it
# the likeliest clothing for a dead round in this system.
#
# What is a dead round is decided by ``services.bare_intent.is_bare_tool_intent``
# and tested exhaustively in ``tests/unit/test_bare_intent.py``; these are here to
# prove the LEARNING TRIGGER consults it, not to re-test the predicate.
DEAD_ROUNDS = [
    "All five votes are in. Let me read them, one read per terminal, batched.",
    (
        "You're right, my previous reply ended with a tool call intent but I only "
        "emitted the reads in the next sentence without actually calling them. Let "
        "me read all five votes now, in one batched reply."
    ),
    (
        "I have the live roster, all five agents are waiting at a prompt, meaning "
        "they finished their votes. Let me read each vote now in one batch."
    ),
    "This needs file changes, so I'll spawn a visible agent to do them.",
]

# Genuine completions that must NEVER be mistaken for a dead round — the whole
# point of the gate is that a short answer, a checkpoint, or a question back to the
# user still gets learned from.
LIVE_ANSWERS = [
    "Done.",
    "Sounds good, PR is open.",
    "Let me know if you need anything else.",
    "I'll be here if you need me.",
    "Let me know which one you prefer and I'll open it.",
    "Which approach do you want — A or B?",
    "Let me be clear: all checks passed.",
    "Let me explain: the cache was stale, so the second read saw the old value.",
    "Available choices:",
    # Content actually DELIVERED after a colon / em dash is an answer, not a promise.
    "I'll run through the options: A does X, while B does Y.",
    "Let's review the finding — the cache key is stale.",
    "The migration is finished and every check is green.\n\nLet me know how it goes.",
    # Polysemy: "call" is an action verb, but not in this idiom.
    "Everything is green. Let's call this done.",
    # Deferred on the user — a checkpoint, not a dropped call.
    "I'll review another trace if you send one.",
    "What constraints should the API enforce? I'll update the design once you clarify.",
    # A participle clause with a finite verb DESCRIBES, it does not promise.
    "Running the suite takes 4 minutes.",
    # A bullet is an item in a plan being presented, not a dangling promise.
    "Here's the plan.\n- I'll inspect the current implementation.",
]


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
        # The shape a bare-intent round actually arrives in: a clean ``stop`` on a
        # request that DID offer tools. Anything else and the predicate short-circuits
        # to False, which would make the gate tests pass for the wrong reason.
        "finish_reason": "stop",
        "tools_callable": True,
        "request_id": "req-test",
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
    # The caller's credential was captured BY VALUE into the detached task — the
    # request/Principal is out of scope by the time learning runs.
    assert call["api_key"].get_secret_value() == "test-caller-key"


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
@pytest.mark.parametrize("text", DEAD_ROUNDS)
async def test_skips_a_round_that_only_announced_an_action(
    learning_orchestrator, respond_request, text: str
) -> None:
    # Structurally this IS a final answer (no tool calls, "stop", non-empty text), so
    # the guards above let it through — but nothing happened, and learning from it
    # would write a lesson about a workflow that never ran. The response path re-rolls
    # such a round once; this is the trigger's own guard for the ones it did not fix.
    learner = RecordingLearner()
    orch = learning_orchestrator(learner)
    _fire(orch, respond_request, assistant_text=text)
    await _settle(orch)
    assert learner.calls == []


@pytest.mark.asyncio
@pytest.mark.parametrize("text", LIVE_ANSWERS)
async def test_still_learns_from_a_genuine_short_answer(
    learning_orchestrator, respond_request, text: str
) -> None:
    # The hard constraint from the issue: a legitimate short answer, checkpoint, or
    # open-ended question must never be blocked by the dead-round gate.
    learner = RecordingLearner()
    orch = learning_orchestrator(learner)
    _fire(orch, respond_request, assistant_text=text)
    await _settle(orch)
    assert len(learner.calls) == 1
    assert learner.calls[0]["trailing"][0].content == text


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("kwargs", "why"),
    [
        # A round with nothing callable cannot have DROPPED a call, so the same
        # sentence is at worst awkward prose and the workflow is genuinely finished.
        ({"tools_callable": False}, "no tool was callable this round"),
        # ``length`` is truncation — a different failure, and not this gate's business.
        ({"finish_reason": "length"}, "the reply was cut off, not abandoned"),
    ],
)
async def test_structural_signals_are_passed_through_to_the_predicate(
    learning_orchestrator, respond_request, kwargs: dict[str, Any], why: str
) -> None:
    # Proves the gate is wired with the REAL per-round signals rather than assumed
    # constants: flip one and the same dead-round text stops blocking learning.
    learner = RecordingLearner()
    orch = learning_orchestrator(learner)
    _fire(orch, respond_request, assistant_text=DEAD_ROUNDS[0], **kwargs)
    await _settle(orch)
    assert len(learner.calls) == 1, why


@pytest.mark.asyncio
async def test_bare_intent_skip_is_logged_without_assistant_content(
    learning_orchestrator, respond_request
) -> None:
    # The skip rate is worth watching, but the reply itself must never reach a log.
    learner = RecordingLearner()
    orch = learning_orchestrator(learner)
    with structlog.testing.capture_logs() as logs:
        _fire(orch, respond_request, assistant_text=DEAD_ROUNDS[0])
    await _settle(orch)

    events = [entry for entry in logs if entry["event"] == "skill_learning_skipped_bare_intent"]
    assert len(events) == 1
    assert DEAD_ROUNDS[0] not in json.dumps(events[0])


@pytest.mark.asyncio
async def test_a_tool_continuation_round_is_not_reported_as_a_bare_intent_skip(
    learning_orchestrator, respond_request
) -> None:
    # Guard ORDER: a round that ends in a tool call is skipped structurally, even
    # when its prose also happens to announce the very call it emitted. Reporting
    # that as a bare-intent skip would misattribute the reason and poison the signal.
    learner = RecordingLearner()
    orch = learning_orchestrator(learner)
    with structlog.testing.capture_logs() as logs:
        _fire(orch, respond_request, assistant_text=DEAD_ROUNDS[0], had_tool_calls=True)
    await _settle(orch)

    assert learner.calls == []
    assert not [e for e in logs if e["event"] == "skill_learning_skipped_bare_intent"]


@pytest.mark.asyncio
async def test_a_raising_detector_neither_breaks_the_turn_nor_learns(
    learning_orchestrator, respond_request, monkeypatch
) -> None:
    """A detector bug must not propagate — skill learning may never break a turn.

    The gate runs synchronously, before ``complete()`` returns its outcome, so an
    escaping exception here would fail ``/respond``. It also fails toward NOT
    learning: a skipped pass costs nothing, while learning from a round that may
    have accomplished nothing can write a real skill file out of a non-answer.
    """
    def boom(*args: Any, **kwargs: Any) -> bool:
        raise ValueError("detector exploded")

    monkeypatch.setattr(chat_orchestrator_module, "is_bare_tool_intent", boom)
    learner = RecordingLearner()
    orch = learning_orchestrator(learner)
    with structlog.testing.capture_logs() as logs:
        _fire(orch, respond_request)  # must not raise
    await _settle(orch)

    assert learner.calls == []
    assert [e for e in logs if e["event"] == "skill_learning_bare_intent_check_failed"]


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
