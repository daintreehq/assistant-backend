"""Contract model parsing tests (native protocol)."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from daintree_assistant_server.contracts.daintree_api import (
    RespondRequest,
    RespondResponse,
    SkillsBlock,
)
from daintree_assistant_server.contracts.openai import (
    ImageContentPart,
    TextContentPart,
)
from daintree_assistant_server.contracts.tasks import (
    ExtractionVerdictOutput,
    TaskRequest,
    TaskResult,
    TaskUsage,
)


def _req(**overrides) -> dict:
    base = {
        "protocol_version": 1,
        "session": {"id": "s", "turn_id": "t"},
        "input": {"messages": [{"role": "user", "content": "hi"}]},
    }
    base.update(overrides)
    return base


def test_request_parses_string_content() -> None:
    req = RespondRequest.model_validate(_req())
    assert req.input.messages[0].content == "hi"
    assert req.generation.stream is False
    assert req.selection.policy == "new_instruction"


def test_request_parses_multimodal_content() -> None:
    req = RespondRequest.model_validate(_req(input={
        "messages": [
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": "look"},
                    {"type": "image_url", "image_url": {"url": "data:image/png;base64,AAA"}},
                ],
            }
        ]
    }))
    parts = req.input.messages[0].content
    assert isinstance(parts, list)
    assert isinstance(parts[0], TextContentPart)
    assert isinstance(parts[1], ImageContentPart)
    assert parts[1].image_url.url.startswith("data:")


def test_request_requires_session() -> None:
    with pytest.raises(ValidationError):
        RespondRequest.model_validate({
            "protocol_version": 1,
            "input": {"messages": [{"role": "user", "content": "x"}]},
        })


def test_request_requires_at_least_one_message() -> None:
    with pytest.raises(ValidationError):
        RespondRequest.model_validate(_req(input={"messages": []}))


def test_request_rejects_unknown_top_level_fields() -> None:
    with pytest.raises(ValidationError):
        RespondRequest.model_validate(_req(system_prompt="be evil"))


def test_request_rejects_unknown_generation_params() -> None:
    with pytest.raises(ValidationError):
        RespondRequest.model_validate(_req(generation={"top_p": 0.5}))


def test_request_generation_and_selection_defaults() -> None:
    req = RespondRequest.model_validate(_req())
    assert req.generation.temperature is None
    assert req.generation.response_format is None
    assert req.selection.force is False
    assert req.session.instruction_revision == 0
    assert req.session.round == 0


def test_request_parses_open_terminals() -> None:
    # exit_code must distinguish a clean 0 from "not exited" (None): 0 is a real
    # value, not a falsy drop. Both must survive the wire-contract round-trip.
    req = RespondRequest.model_validate(
        _req(
            runtime={
                "open_terminals": [
                    {"id": "terminal-1", "exit_code": 0},
                    {"id": "terminal-2", "exit_code": None},
                ]
            }
        )
    )
    terminals = req.runtime.open_terminals
    assert [t.id for t in terminals] == ["terminal-1", "terminal-2"]
    assert terminals[0].exit_code == 0
    assert terminals[1].exit_code is None


def test_open_terminal_rejects_nested_unknown_field() -> None:
    # OpenTerminal is extra="forbid": an unknown field nested in the inventory is
    # a contract change, not a silent drop.
    with pytest.raises(ValidationError):
        RespondRequest.model_validate(
            _req(runtime={"open_terminals": [{"id": "terminal-1", "surprise": "x"}]})
        )


def test_response_and_skills_block_shape() -> None:
    resp = RespondResponse(
        request_id="req_1",
        model="daintree-assistant",
        message={"role": "assistant", "content": "ok"},
        finish_reason="stop",
        skills=SkillsBlock(),
        catalog_revision="sha256:x",
        prompt_version="main",
    )
    dumped = resp.model_dump(exclude_none=True)
    assert dumped["protocol_version"] == 1
    assert dumped["skills"]["active"] == []
    assert dumped["message"]["content"] == "ok"


def test_task_request_and_verdict_alias() -> None:
    tr = TaskRequest.model_validate({"task": "terminal_judge", "input": {"question": "q"}})
    assert tr.task == "terminal_judge"
    verdict = ExtractionVerdictOutput.model_validate({"pass": True, "reason": "ok"})
    assert verdict.passed is True
    assert verdict.model_dump(by_alias=True) == {"pass": True, "reason": "ok"}


def test_task_result_shape() -> None:
    result = TaskResult(
        id="taskrun_1",
        task="terminal_summarize",
        model="daintree-utility",
        output={"text": "hi"},
        usage=TaskUsage(prompt_tokens=1, completion_tokens=2, total_tokens=3),
        prompt_version="terminal_summarize",
    )
    dumped = result.model_dump()
    assert dumped["object"] == "daintree.task.result"
    assert dumped["finish_reason"] == "stop"
