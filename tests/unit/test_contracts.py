"""Contract model parsing tests."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from daintree_assistant_server.contracts.extensions import DaintreeExtension, ResponseMeta
from daintree_assistant_server.contracts.openai import (
    ChatCompletionRequest,
    ImageContentPart,
    TextContentPart,
)
from daintree_assistant_server.contracts.tasks import (
    ExtractionVerdictOutput,
    TaskRequest,
    TaskResult,
    TaskUsage,
)


def test_request_parses_string_content() -> None:
    req = ChatCompletionRequest.model_validate(
        {"model": "daintree-assistant", "messages": [{"role": "user", "content": "hi"}]}
    )
    assert req.messages[0].content == "hi"
    assert req.stream is False


def test_request_parses_multimodal_content() -> None:
    req = ChatCompletionRequest.model_validate(
        {
            "model": "daintree-assistant",
            "messages": [
                {
                    "role": "user",
                    "content": [
                        {"type": "text", "text": "look"},
                        {"type": "image_url", "image_url": {"url": "data:image/png;base64,AAA"}},
                    ],
                }
            ],
        }
    )
    parts = req.messages[0].content
    assert isinstance(parts, list)
    assert isinstance(parts[0], TextContentPart)
    assert isinstance(parts[1], ImageContentPart)
    assert parts[1].image_url.url.startswith("data:")


def test_request_captures_daintree_extension_raw() -> None:
    req = ChatCompletionRequest.model_validate(
        {
            "model": "daintree-assistant",
            "messages": [{"role": "user", "content": "hi"}],
            "daintree": {"protocol_version": 1, "session_id": "s"},
        }
    )
    assert req.daintree == {"protocol_version": 1, "session_id": "s"}


def test_request_allows_extra_vendor_fields() -> None:
    req = ChatCompletionRequest.model_validate(
        {"model": "m", "messages": [{"role": "user", "content": "x"}], "metadata": {"k": "v"}}
    )
    assert req.model_extra is not None
    assert "metadata" in req.model_extra


def test_resolved_max_tokens_prefers_completion() -> None:
    req = ChatCompletionRequest.model_validate(
        {"model": "m", "messages": [{"role": "user", "content": "x"}], "max_completion_tokens": 50}
    )
    assert req.resolved_max_tokens() == 50


def test_request_requires_at_least_one_message() -> None:
    with pytest.raises(ValidationError):
        ChatCompletionRequest.model_validate({"model": "m", "messages": []})


def test_extension_defaults() -> None:
    ext = DaintreeExtension.model_validate({"protocol_version": 1})
    assert ext.instruction_revision == 0
    assert ext.round == 0
    assert ext.selection.policy == "new_instruction"
    assert ext.selection.force is False
    assert ext.turn_context is None


def test_task_request_and_verdict_alias() -> None:
    tr = TaskRequest.model_validate({"task": "terminal_judge.v1", "input": {"question": "q"}})
    assert tr.task == "terminal_judge.v1"
    verdict = ExtractionVerdictOutput.model_validate({"pass": True, "reason": "ok"})
    assert verdict.passed is True
    assert verdict.model_dump(by_alias=True) == {"pass": True, "reason": "ok"}


def test_task_result_shape() -> None:
    result = TaskResult(
        id="taskrun_1",
        task="terminal_summarize.v1",
        model="daintree-utility",
        output={"text": "hi"},
        usage=TaskUsage(prompt_tokens=1, completion_tokens=2, total_tokens=3),
        prompt_version="terminal_summarize.v1",
    )
    dumped = result.model_dump()
    assert dumped["object"] == "daintree.task.result"
    assert dumped["finish_reason"] == "stop"


def test_response_meta_minimal() -> None:
    meta = ResponseMeta(
        request_id="req_1", catalog_revision="sha256:x", prompt_version="main-2026-06-26.1"
    )
    dumped = meta.model_dump(exclude_none=True)
    assert dumped["v"] == 1
    assert dumped["active_skills"] == []
    assert "reason" not in dumped["selector"]
