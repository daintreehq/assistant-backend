"""A draft review must not confuse limited model output with missing source text."""

from __future__ import annotations

import pytest
from fakes import FakeProvider, make_result

from daintree_assistant_server.config import EXECUTOR_REASONING_ALLOWANCE_TOKENS
from daintree_assistant_server.contracts.tasks import MAX_TASK_OUTPUT_TOKENS, TaskRequest
from daintree_assistant_server.errors import DaintreeError
from daintree_assistant_server.services.task_runner import TaskRunner


@pytest.mark.asyncio
@pytest.mark.parametrize("task", ["terminal_extract_text", "terminal_extract_json"])
@pytest.mark.parametrize("budget", [1, 1024, 2000])
async def test_requested_budget_reaches_provider(
    task_runner: TaskRunner, fake_provider: FakeProvider, task: str, budget: int
) -> None:
    fake_provider.complete_result = make_result(content='{"result": "owner commits"}')
    await task_runner.run(
        TaskRequest(
            task=task,
            input={
                "instruction": "ownership",
                "tail": "I'll handle it.",
                "max_output_tokens": budget,
            },
        )
    )
    assert fake_provider.requests[-1].max_tokens == budget + EXECUTOR_REASONING_ALLOWANCE_TOKENS


@pytest.mark.asyncio
@pytest.mark.parametrize("task", ["terminal_extract_text", "terminal_extract_json"])
@pytest.mark.parametrize("budget", [0, -1, 2001, True, "2000", 1.5])
async def test_invalid_budget_rejected_before_spending(
    task_runner: TaskRunner, fake_provider: FakeProvider, task: str, budget: object
) -> None:
    with pytest.raises(DaintreeError) as exc:
        await task_runner.run(
            TaskRequest(
                task=task,
                input={"instruction": "ownership", "max_output_tokens": budget},
            )
        )
    assert exc.value.code == "invalid_task_input"
    assert not fake_provider.requests


@pytest.mark.asyncio
@pytest.mark.parametrize("content", ['{"result": [{"draft": "What I', '{"result": []}'])
async def test_length_finish_is_not_repaired_or_accepted_as_complete(
    task_runner: TaskRunner, fake_provider: FakeProvider, content: str
) -> None:
    fake_provider.complete_result = make_result(content=content, finish_reason="length")
    with pytest.raises(DaintreeError) as exc:
        await task_runner.run(
            TaskRequest(
                task="terminal_extract_json",
                input={"instruction": "all six drafts", "max_output_tokens": 2000},
            )
        )
    assert exc.value.code == "task_output_truncated"
    assert "not missing source content" in exc.value.message
    assert "terminal.read" in exc.value.message
    assert len(fake_provider.requests) == 1


@pytest.mark.asyncio
async def test_repair_truncation_has_specific_error(
    task_runner: TaskRunner,
    fake_provider: FakeProvider,
) -> None:
    fake_provider.complete_result = [
        make_result(content="invalid JSON"),
        make_result(content='{"result": "cut', finish_reason="length"),
    ]
    with pytest.raises(DaintreeError) as exc:
        await task_runner.run(
            TaskRequest(
                task="terminal_extract_json",
                input={"instruction": "draft", "max_output_tokens": 1800},
            )
        )
    assert exc.value.code == "task_output_truncated"
    assert len(fake_provider.requests) == 2
    assert all(
        r.max_tokens == 1800 + EXECUTOR_REASONING_ALLOWANCE_TOKENS for r in fake_provider.requests
    )


@pytest.mark.asyncio
async def test_parse_failure_does_not_invent_a_crashed_agent(
    task_runner: TaskRunner,
    fake_provider: FakeProvider,
) -> None:
    fake_provider.complete_result = make_result(content="invalid JSON")
    with pytest.raises(DaintreeError) as exc:
        await task_runner.run(
            TaskRequest(task="terminal_extract_json", input={"instruction": "draft"})
        )
    assert exc.value.code == "task_output_invalid"
    assert "does not establish" in exc.value.message
    assert "crashed" not in exc.value.message


@pytest.mark.asyncio
@pytest.mark.parametrize("task", ["terminal_summarize", "terminal_extract_text"])
async def test_text_preserves_length_finish_for_cli(
    task_runner: TaskRunner,
    fake_provider: FakeProvider,
    task: str,
) -> None:
    fake_provider.complete_result = make_result(content="What I'll ship is", finish_reason="length")
    result = await task_runner.run(TaskRequest(task=task, input={"instruction": "draft"}))
    assert result.finish_reason == "length"
    assert result.output["text"] == "What I'll ship is"


@pytest.mark.asyncio
@pytest.mark.parametrize("budget", [1, 300, 1024, 1999])
async def test_truncation_quotes_the_budget_the_caller_sent(
    task_runner: TaskRunner, fake_provider: FakeProvider, budget: int
) -> None:
    """The number in the message must be one the caller can reproduce.

    ``max_tokens`` carries the server-owned reasoning allowance, so quoting it
    would report 1088 for a maxTokens of 1024 — a value no client can set.
    """
    fake_provider.complete_result = make_result(content='{"result": "cut', finish_reason="length")
    with pytest.raises(DaintreeError) as exc:
        await task_runner.run(
            TaskRequest(
                task="terminal_extract_json",
                input={"instruction": "drafts", "max_output_tokens": budget},
            )
        )
    assert exc.value.code == "task_output_truncated"
    assert f"({budget})" in exc.value.message
    inflated = budget + EXECUTOR_REASONING_ALLOWANCE_TOKENS
    assert str(inflated) not in exc.value.message


@pytest.mark.asyncio
async def test_truncation_at_the_ceiling_does_not_advise_raising_maxtokens(
    task_runner: TaskRunner, fake_provider: FakeProvider
) -> None:
    """At the cap that advice is impossible to act on and invites a futile retry."""
    fake_provider.complete_result = make_result(content='{"result": "cut', finish_reason="length")
    with pytest.raises(DaintreeError) as exc:
        await task_runner.run(
            TaskRequest(
                task="terminal_extract_json",
                input={
                    "instruction": "drafts",
                    "max_output_tokens": MAX_TASK_OUTPUT_TOKENS,
                },
            )
        )
    assert exc.value.code == "task_output_truncated"
    assert "maxTokens" not in exc.value.message
    # The remedies that ARE actionable at the ceiling must survive.
    assert "request fewer fields" in exc.value.message
    assert "one terminal per call" in exc.value.message
    assert "terminal.read" in exc.value.message


@pytest.mark.asyncio
async def test_truncation_below_the_ceiling_still_advises_raising_maxtokens(
    task_runner: TaskRunner, fake_provider: FakeProvider
) -> None:
    fake_provider.complete_result = make_result(content='{"result": "cut', finish_reason="length")
    with pytest.raises(DaintreeError) as exc:
        await task_runner.run(
            TaskRequest(
                task="terminal_extract_json",
                input={
                    "instruction": "drafts",
                    "max_output_tokens": MAX_TASK_OUTPUT_TOKENS - 1,
                },
            )
        )
    assert "raise maxTokens within its limit" in exc.value.message


@pytest.mark.asyncio
async def test_repair_truncation_also_quotes_the_caller_budget(
    task_runner: TaskRunner, fake_provider: FakeProvider
) -> None:
    fake_provider.complete_result = [
        make_result(content="invalid JSON"),
        make_result(content='{"result": "cut', finish_reason="length"),
    ]
    with pytest.raises(DaintreeError) as exc:
        await task_runner.run(
            TaskRequest(
                task="terminal_extract_json",
                input={"instruction": "draft", "max_output_tokens": 1800},
            )
        )
    assert exc.value.code == "task_output_truncated"
    assert "(1800)" in exc.value.message
    assert str(1800 + EXECUTOR_REASONING_ALLOWANCE_TOKENS) not in exc.value.message


@pytest.mark.asyncio
async def test_truncation_without_a_client_budget_quotes_the_profile_default(
    task_runner: TaskRunner, fake_provider: FakeProvider
) -> None:
    """An omitted budget keeps the profile's own cap, and that is what is reported."""
    fake_provider.complete_result = make_result(content='{"result": "cut', finish_reason="length")
    with pytest.raises(DaintreeError) as exc:
        await task_runner.run(
            TaskRequest(task="terminal_extract_json", input={"instruction": "drafts"})
        )
    assert exc.value.code == "task_output_truncated"
    profile_default = fake_provider.requests[-1].max_tokens - EXECUTOR_REASONING_ALLOWANCE_TOKENS
    assert f"({profile_default})" in exc.value.message
