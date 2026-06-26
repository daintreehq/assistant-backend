"""Task runner tests for every named profile."""

from __future__ import annotations

import json

import pytest
from fakes import FakeProvider, make_result

from daintree_assistant_server.contracts.tasks import TaskRequest
from daintree_assistant_server.errors import DaintreeError
from daintree_assistant_server.services.task_runner import TaskRunner


async def _run(runner: TaskRunner, task: str, inp: dict, **extra: object) -> dict:
    result = await runner.run(TaskRequest(task=task, input=inp, **extra))
    return result.output


@pytest.mark.asyncio
async def test_terminal_judge(task_runner: TaskRunner, fake_provider: FakeProvider) -> None:
    fake_provider.complete_result = make_result(
        content=json.dumps({"reason": "passed", "confidence": 0.9, "matched": True})
    )
    out = await _run(task_runner, "terminal_judge.v1", {"question": "ok?", "tail": "OK"})
    assert out == {"reason": "passed", "confidence": 0.9, "matched": True}


@pytest.mark.asyncio
async def test_terminal_summarize_text(task_runner: TaskRunner, fake_provider: FakeProvider) -> None:
    fake_provider.complete_result = make_result(content="Tests are green.")
    out = await _run(task_runner, "terminal_summarize.v1", {"purpose": "p", "tail": "x"})
    assert out == {"text": "Tests are green."}


@pytest.mark.asyncio
async def test_checkpoint(task_runner: TaskRunner, fake_provider: FakeProvider) -> None:
    fake_provider.complete_result = make_result(
        content=json.dumps({"goal": "ship", "next_actions": ["do x"]})
    )
    out = await _run(task_runner, "checkpoint.v1", {"transcript": "..."})
    assert out["goal"] == "ship"
    assert out["next_actions"] == ["do x"]
    assert out["decisions"] == []  # defaulted


@pytest.mark.asyncio
async def test_memory_distill_array_and_dedupe(task_runner: TaskRunner, fake_provider: FakeProvider) -> None:
    payload = [
        {"fact": "Project uses Python", "kind": "semantic"},
        "tried X it failed",
        {"fact": "Project uses Python", "kind": "semantic"},  # duplicate
        {"fact": "bad kind", "kind": "weird"},
    ]
    fake_provider.complete_result = make_result(content=json.dumps(payload))
    out = await _run(task_runner, "memory_distill.v1", {"transcript": "..."})
    facts = out["facts"]
    assert len(facts) == 3
    assert facts[1] == {"fact": "tried X it failed", "kind": "semantic"}
    assert facts[2]["kind"] == "semantic"  # invalid kind coerced


@pytest.mark.asyncio
async def test_extract_json_wraps_bare_value(task_runner: TaskRunner, fake_provider: FakeProvider) -> None:
    fake_provider.complete_result = make_result(content=json.dumps({"result": {"count": 5}}))
    out = await _run(task_runner, "terminal_extract_json.v1", {"instruction": "count", "tail": "5"})
    assert out == {"result": {"count": 5}}


@pytest.mark.asyncio
async def test_extraction_verdict_alias(task_runner: TaskRunner, fake_provider: FakeProvider) -> None:
    fake_provider.complete_result = make_result(content=json.dumps({"pass": True, "reason": "ok"}))
    out = await _run(task_runner, "extraction_verdict.v1", {"result": "x", "condition": "non-empty"})
    assert out == {"pass": True, "reason": "ok"}


@pytest.mark.asyncio
async def test_watcher_classify_clamps_confidence(task_runner: TaskRunner, fake_provider: FakeProvider) -> None:
    fake_provider.complete_result = make_result(
        content=json.dumps({"classification": "tests_passed", "confidence": 5, "summary": "ok"})
    )
    out = await _run(task_runner, "watcher_classify.v1", {"goal": "g", "tail": "OK"})
    assert out["confidence"] == 1.0


@pytest.mark.asyncio
async def test_skill_step_consistency(task_runner: TaskRunner, fake_provider: FakeProvider) -> None:
    fake_provider.complete_result = make_result(
        content=json.dumps({"reason": "normal", "confidence": 0.2, "matched": False})
    )
    out = await _run(
        task_runner,
        "skill_step_consistency.v1",
        {"skill_id": "x", "completed_step": 1, "current_step_before": 1, "current_step_after": 2},
    )
    assert out["matched"] is False


@pytest.mark.asyncio
async def test_lenient_json_with_code_fence(task_runner: TaskRunner, fake_provider: FakeProvider) -> None:
    fenced = "```json\n" + json.dumps({"reason": "r", "confidence": 0.5, "matched": True}) + "\n```"
    fake_provider.complete_result = make_result(content=fenced)
    out = await _run(task_runner, "terminal_judge.v1", {"question": "q", "tail": "t"})
    assert out["matched"] is True


@pytest.mark.asyncio
async def test_json_repair_path(task_runner: TaskRunner, fake_provider: FakeProvider) -> None:
    results = iter(
        [
            make_result(content="not json at all"),
            make_result(content=json.dumps({"reason": "r", "confidence": 0.5, "matched": True})),
        ]
    )
    fake_provider.complete_result = lambda _req: next(results)
    result = await task_runner.run(TaskRequest(task="terminal_judge.v1", input={"question": "q"}))
    assert result.output["matched"] is True
    assert len(fake_provider.requests) == 2  # repaired


@pytest.mark.asyncio
async def test_json_repair_exhausted_raises(task_runner: TaskRunner, fake_provider: FakeProvider) -> None:
    fake_provider.complete_result = make_result(content="never valid json")
    with pytest.raises(DaintreeError) as exc:
        await task_runner.run(TaskRequest(task="terminal_judge.v1", input={"question": "q"}))
    assert exc.value.code == "task_output_invalid"


@pytest.mark.asyncio
async def test_unknown_task_404(task_runner: TaskRunner) -> None:
    with pytest.raises(DaintreeError) as exc:
        await task_runner.run(TaskRequest(task="nope.v1", input={}))
    assert exc.value.status_code == 404


@pytest.mark.asyncio
async def test_invalid_input_400(task_runner: TaskRunner) -> None:
    with pytest.raises(DaintreeError) as exc:
        await task_runner.run(TaskRequest(task="terminal_judge.v1", input={}))  # missing question
    assert exc.value.status_code == 400


@pytest.mark.asyncio
async def test_prompt_version_and_usage(task_runner: TaskRunner, fake_provider: FakeProvider) -> None:
    fake_provider.complete_result = make_result(content="summary")
    result = await task_runner.run(TaskRequest(task="terminal_summarize.v1", input={"tail": "x"}))
    assert result.prompt_version == "terminal_summarize.v1"
    assert result.usage.total_tokens >= 0
    assert result.model == "deepseek-v4-flash"


def test_all_nine_profiles_registered(task_runner: TaskRunner) -> None:
    assert len(task_runner.task_ids()) == 9
