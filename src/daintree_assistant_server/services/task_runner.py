"""The named utility-task runner (specification section 11).

Each task is a server-owned profile: it picks the model, system prompt, output
mode, schema, temperature, and token cap. Clients send only task *data*; no task
request contains ``messages``, ``system``, ``developer``, or ``system_prompt``.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from pydantic import BaseModel, ValidationError

from ..config import DEEPSEEK_MODEL, Settings
from ..contracts.tasks import (
    CheckpointInput,
    CheckpointOutput,
    ExtractionVerdictInput,
    ExtractionVerdictOutput,
    JudgeOutput,
    MemoryDistillInput,
    SkillStepConsistencyInput,
    TaskRequest,
    TaskResult,
    TaskUsage,
    TerminalExtractJsonInput,
    TerminalExtractTextInput,
    TerminalJudgeInput,
    TerminalState,
    TerminalSummarizeInput,
    WatcherClassifyInput,
    WatcherClassifyOutput,
)
from ..errors import DaintreeError, invalid_request, unknown_task
from ..observability import get_logger, new_task_run_id
from ..prompts.loader import PromptLoader
from ..providers.base import ChatProvider, ProviderChatRequest, ProviderUsage
from ..providers.retry import retry_upstream
from .result_schema import conforms, validate_result_schema

_SCHEMA_TASK = "terminal_extract_json.v1"

logger = get_logger("daintree.tasks")

MAX_DISTILLED_FACTS = 10


class TaskParseError(Exception):
    """Raised when a task's model output cannot be parsed into its schema."""


def _task_output_invalid(task: str) -> DaintreeError:
    return DaintreeError(
        status_code=502,
        message=f"Task {task!r} produced output that could not be parsed.",
        type="api_error",
        code="task_output_invalid",
    )


def _loads_lenient(raw: str) -> Any:
    """Parse JSON tolerantly: strip code fences, fall back to first object/array."""
    s = raw.strip()
    if s.startswith("```"):
        s = s.strip("`").strip()
        if s.lower().startswith("json"):
            s = s[4:].strip()
    try:
        return json.loads(s)
    except json.JSONDecodeError:
        pass
    for open_ch, close_ch in (("{", "}"), ("[", "]")):
        i, j = s.find(open_ch), s.rfind(close_ch)
        if i != -1 and j > i:
            try:
                return json.loads(s[i : j + 1])
            except json.JSONDecodeError:
                continue
    raise TaskParseError("output is not valid JSON")


def _clamp_confidence(data: dict[str, Any]) -> dict[str, Any]:
    if "confidence" in data:
        try:
            data["confidence"] = min(1.0, max(0.0, float(data["confidence"])))
        except (TypeError, ValueError):
            data["confidence"] = 0.0
    return data


def _state_line(state: TerminalState, *, include_waiting: bool = False) -> str:
    parts = [
        f"agentState={state.agent_state or 'unknown'}",
        f"runtimeStatus={state.runtime_status or 'unknown'}",
    ]
    if include_waiting:
        parts.append(f"waitingReason={state.waiting_reason or 'none'}")
    parts.append(f"lastOutputAt={state.last_output_at or 'unknown'}")
    return ", ".join(parts)


def _tail_block(tail: str) -> str:
    return tail if tail.strip() else "(no output captured)"


# --------------------------------------------------------------------------
# Per-task user-message builders.
# --------------------------------------------------------------------------


def _u_checkpoint(inp: CheckpointInput, _req: TaskRequest) -> str:
    return (
        "Conversation to compact:\n"
        f'"""\n{inp.transcript}\n"""\n\n'
        "Produce the checkpoint JSON now."
    )


def _u_memory_distill(inp: MemoryDistillInput, _req: TaskRequest) -> str:
    return (
        "Conversation to distill:\n"
        f'"""\n{inp.transcript}\n"""\n\n'
        "Return the JSON array of facts now."
    )


def _u_watcher(inp: WatcherClassifyInput, _req: TaskRequest) -> str:
    return (
        f"Goal: {inp.goal}\n"
        f"Known terminal state: {_state_line(inp.terminal_state)}\n"
        f"Previous classification: {inp.previous_classification or 'none'}\n\n"
        "Terminal tail (most recent output, bounded):\n"
        f'"""\n{_tail_block(inp.tail)}\n"""\n\n'
        "Classify now. Return only the JSON object."
    )


def _u_judge(inp: TerminalJudgeInput, _req: TaskRequest) -> str:
    return (
        f"Watcher goal (context): {inp.goal}\n"
        f"Question to answer (yes/no): {inp.question}\n"
        f"Known terminal state: {_state_line(inp.terminal_state, include_waiting=True)}\n\n"
        "Terminal tail (most recent output, bounded):\n"
        f'"""\n{_tail_block(inp.tail)}\n"""\n\n'
        "Answer the question now. Return only the JSON object."
    )


def _u_summarize(inp: TerminalSummarizeInput, _req: TaskRequest) -> str:
    return (
        f"Purpose of this summary: {inp.purpose}\n\n"
        "Terminal output:\n"
        f'"""\n{_tail_block(inp.tail)}\n"""\n\n'
        "Summarize."
    )


def _u_extract_text(inp: TerminalExtractTextInput, _req: TaskRequest) -> str:
    return (
        f"Source terminal(s): {', '.join(inp.terminal_ids) or '(unspecified)'}\n"
        f"Extraction instruction: {inp.instruction}\n\n"
        "Terminal output (most recent, bounded):\n"
        f'"""\n{_tail_block(inp.tail)}\n"""\n\n'
        "Extract now."
    )


def _u_extract_json(inp: TerminalExtractJsonInput, req: TaskRequest) -> str:
    schema = req.result_schema
    schema_text = (
        json.dumps(schema, indent=2, sort_keys=True) if schema else "any JSON value"
    )
    return (
        f"Source terminal(s): {', '.join(inp.terminal_ids) or '(unspecified)'}\n"
        f"Extraction instruction: {inp.instruction}\n\n"
        'Return a JSON object { "result": <value> } where <value> conforms to this schema:\n'
        f'"""\n{schema_text}\n"""\n\n'
        "Terminal output (most recent, bounded):\n"
        f'"""\n{_tail_block(inp.tail)}\n"""\n\n'
        "Extract now."
    )


def _u_extraction_verdict(inp: ExtractionVerdictInput, _req: TaskRequest) -> str:
    return (
        f"Condition to satisfy: {inp.condition}\n\n"
        "Extracted result:\n"
        f'"""\n{inp.result}\n"""\n\n'
        "Judge now. Return only the JSON object."
    )


def _u_skill_consistency(inp: SkillStepConsistencyInput, _req: TaskRequest) -> str:
    next_step = inp.requested_next or "finish (run marked complete)"
    before = json.dumps(inp.progress_before, indent=2) if inp.progress_before else "[]"
    after = json.dumps(inp.progress_after, indent=2) if inp.progress_after else "[]"
    return (
        f"Skill: {inp.skill_id}\n"
        f"Recorded advance: completedStep={inp.completed_step}, status={inp.status}, "
        f"requestedNext={next_step}\n"
        f"currentStep: {inp.current_step_before} -> {inp.current_step_after}\n"
        f"runStatus: {inp.run_status_before} -> {inp.run_status_after}\n"
        f"notes: {inp.notes or 'none'}\n\n"
        "Progress BEFORE this advance (JSON SkillStepProgress[]):\n"
        f'"""\n{before}\n"""\n\n'
        "Progress AFTER this advance (JSON SkillStepProgress[]):\n"
        f'"""\n{after}\n"""\n\n'
        "Judge this advance now. Return only the JSON object."
    )


# --------------------------------------------------------------------------
# Per-task output parsers.
# --------------------------------------------------------------------------


def _p_text(raw: str, _req: TaskRequest) -> dict[str, Any]:
    return {"text": raw.strip()}


def _p_checkpoint(raw: str, _req: TaskRequest) -> dict[str, Any]:
    data = _loads_lenient(raw)
    if not isinstance(data, dict):
        raise TaskParseError("checkpoint output is not a JSON object")
    return CheckpointOutput.model_validate(data).model_dump()


def _p_memory_distill(raw: str, _req: TaskRequest) -> dict[str, Any]:
    data = _loads_lenient(raw)
    facts: list[dict[str, str]] = []
    seen: set[str] = set()
    if isinstance(data, list):
        for item in data:
            if isinstance(item, str):
                fact, kind = item.strip(), "semantic"
            elif isinstance(item, dict):
                fact = str(item.get("fact", "")).strip()
                kind = item.get("kind", "semantic")
                if kind not in ("semantic", "episodic"):
                    kind = "semantic"
            else:
                continue
            if not fact:
                continue
            key = fact.lower()
            if key in seen:
                continue
            seen.add(key)
            facts.append({"fact": fact[:400], "kind": kind})
            if len(facts) >= MAX_DISTILLED_FACTS:
                break
    return {"facts": facts}


def _p_watcher(raw: str, _req: TaskRequest) -> dict[str, Any]:
    data = _loads_lenient(raw)
    if not isinstance(data, dict):
        raise TaskParseError("watcher output is not a JSON object")
    return WatcherClassifyOutput.model_validate(_clamp_confidence(data)).model_dump()


def _p_judge(raw: str, _req: TaskRequest) -> dict[str, Any]:
    data = _loads_lenient(raw)
    if not isinstance(data, dict):
        raise TaskParseError("judge output is not a JSON object")
    return JudgeOutput.model_validate(_clamp_confidence(data)).model_dump()


def _p_extract_json(raw: str, req: TaskRequest) -> dict[str, Any]:
    data = _loads_lenient(raw)
    value = data["result"] if isinstance(data, dict) and "result" in data else data
    # When the caller supplied a result_schema, the extracted value must conform.
    # A non-conforming value is treated like a parse failure so the repair path runs.
    if req.result_schema is not None and not conforms(value, req.result_schema):
        raise TaskParseError("result does not conform to result_schema")
    return {"result": value}


def _p_extraction_verdict(raw: str, _req: TaskRequest) -> dict[str, Any]:
    data = _loads_lenient(raw)
    if not isinstance(data, dict):
        raise TaskParseError("verdict output is not a JSON object")
    return ExtractionVerdictOutput.model_validate(data).model_dump(by_alias=True)


# --------------------------------------------------------------------------
# Profile registry.
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class TaskProfile:
    id: str
    input_model: type[BaseModel]
    output_mode: str  # "json" | "text"
    build_user: Callable[[Any, TaskRequest], str]
    parse_output: Callable[[str, TaskRequest], dict[str, Any]]
    temperature: float = 0.0
    max_output_tokens: int = 500
    timeout_seconds: float | None = None
    sensitive_fields: frozenset[str] = field(default_factory=frozenset)
    # Appended to the generic repair instruction so the retry re-anchors on this
    # task's exact output shape (the selector does the same for select_skills).
    # Only used on the rare second attempt — no happy-path cost.
    repair_hint: str | None = None


def build_profiles() -> dict[str, TaskProfile]:
    return {
        p.id: p
        for p in [
            TaskProfile(
                "checkpoint.v1", CheckpointInput, "json", _u_checkpoint, _p_checkpoint,
                max_output_tokens=1200, sensitive_fields=frozenset({"transcript"}),
                repair_hint="Return a single JSON object with the checkpoint fields described "
                "above (goal, decisions, next_actions, open_questions, preserved_ids, …).",
            ),
            TaskProfile(
                "memory_distill.v1", MemoryDistillInput, "json", _u_memory_distill,
                _p_memory_distill, max_output_tokens=1200,
                sensitive_fields=frozenset({"transcript"}),
                repair_hint="Return a JSON array of fact objects, each with a \"fact\" string "
                "and a \"kind\" of \"semantic\" or \"episodic\".",
            ),
            TaskProfile(
                "watcher_classify.v1", WatcherClassifyInput, "json", _u_watcher, _p_watcher,
                max_output_tokens=400, sensitive_fields=frozenset({"tail"}),
                repair_hint="Return a single JSON object with keys classification, confidence, "
                "summary, evidence, recommendedAction.",
            ),
            TaskProfile(
                "terminal_judge.v1", TerminalJudgeInput, "json", _u_judge, _p_judge,
                max_output_tokens=300, sensitive_fields=frozenset({"tail"}),
                repair_hint="Return a single JSON object with keys matched (boolean), "
                "confidence (0-1), reason.",
            ),
            TaskProfile(
                "terminal_summarize.v1", TerminalSummarizeInput, "text", _u_summarize, _p_text,
                temperature=0.2, max_output_tokens=400, sensitive_fields=frozenset({"tail"}),
            ),
            TaskProfile(
                "terminal_extract_text.v1", TerminalExtractTextInput, "text", _u_extract_text,
                _p_text, max_output_tokens=400, sensitive_fields=frozenset({"tail"}),
            ),
            TaskProfile(
                "terminal_extract_json.v1", TerminalExtractJsonInput, "json", _u_extract_json,
                _p_extract_json, max_output_tokens=600, sensitive_fields=frozenset({"tail"}),
                repair_hint="Return a single JSON object of the form {\"result\": <value>} where "
                "<value> conforms to the requested schema.",
            ),
            TaskProfile(
                "extraction_verdict.v1", ExtractionVerdictInput, "json", _u_extraction_verdict,
                _p_extraction_verdict, max_output_tokens=300,
                sensitive_fields=frozenset({"result"}),
                repair_hint="Return a single JSON object with keys pass (boolean) and reason.",
            ),
            TaskProfile(
                "skill_step_consistency.v1", SkillStepConsistencyInput, "json",
                _u_skill_consistency, _p_judge, max_output_tokens=300,
                repair_hint="Return a single JSON object with keys matched (boolean), "
                "confidence (0-1), reason.",
            ),
        ]
    }


def _sum_usage(a: ProviderUsage, b: ProviderUsage) -> ProviderUsage:
    def add(x: int | None, y: int | None) -> int:
        return (x or 0) + (y or 0)

    return ProviderUsage(
        prompt_tokens=add(a.prompt_tokens, b.prompt_tokens),
        completion_tokens=add(a.completion_tokens, b.completion_tokens),
        total_tokens=add(a.total_tokens, b.total_tokens),
        cached_tokens=add(a.cached_tokens, b.cached_tokens),
    )


_REPAIR = (
    "Your previous response did not match the required output. Respond again with "
    "ONLY the valid JSON described, no prose and no code fences."
)


class TaskRunner:
    def __init__(
        self,
        *,
        settings: Settings,
        provider: ChatProvider,
        prompt_loader: PromptLoader,
        profiles: dict[str, TaskProfile] | None = None,
    ) -> None:
        self.settings = settings
        self.provider = provider
        self.prompts = prompt_loader
        self.profiles = profiles if profiles is not None else build_profiles()

    def task_ids(self) -> list[str]:
        return sorted(self.profiles)

    async def _complete_with_retry(self, request: ProviderChatRequest) -> Any:
        return await retry_upstream(
            lambda: self.provider.complete(request),
            retries=self.settings.max_upstream_retries,
            backoff_base_seconds=self.settings.retry_backoff_base_seconds,
        )

    async def run(self, request: TaskRequest) -> TaskResult:
        profile = self.profiles.get(request.task)
        if profile is None:
            raise unknown_task(request.task)

        # result_schema is consumed only by terminal_extract_json.v1; reject it
        # elsewhere, and validate (size/depth/keys) it before it reaches the prompt.
        if request.result_schema is not None:
            if profile.id != _SCHEMA_TASK:
                raise invalid_request(
                    f"result_schema is only accepted by the {_SCHEMA_TASK} task.",
                    param="result_schema",
                    code="result_schema_not_supported",
                )
            validate_result_schema(request.result_schema, self.settings)

        try:
            input_obj = profile.input_model.model_validate(request.input)
        except ValidationError as exc:
            raise invalid_request(
                f"Invalid input for task {request.task!r}: {exc.errors()[0].get('msg', '')}",
                param="input",
                code="invalid_task_input",
            ) from exc

        system = self.prompts.task_system_prompt(profile.id)
        user = profile.build_user(input_obj, request)
        model = DEEPSEEK_MODEL
        timeout = profile.timeout_seconds or self.settings.utility_timeout_seconds

        messages = [
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ]
        response_format = {"type": "json_object"} if profile.output_mode == "json" else None
        provider_request = ProviderChatRequest(
            model=model,
            messages=messages,
            temperature=profile.temperature,
            max_tokens=profile.max_output_tokens,
            response_format=response_format,
            thinking_disabled=True,
            timeout=timeout,
        )

        result = await self._complete_with_retry(provider_request)
        usage = result.usage
        finish = result.finish_reason or "stop"

        try:
            output = profile.parse_output(result.text, request)
        except (TaskParseError, ValidationError) as first_error:
            if profile.output_mode != "json":
                raise _task_output_invalid(profile.id) from first_error
            repair_text = f"{_REPAIR} {profile.repair_hint}" if profile.repair_hint else _REPAIR
            repair_request = ProviderChatRequest(
                model=model,
                messages=[*messages, {"role": "user", "content": repair_text}],
                temperature=profile.temperature,
                max_tokens=profile.max_output_tokens,
                response_format=response_format,
                thinking_disabled=True,
                timeout=timeout,
            )
            repair = await self._complete_with_retry(repair_request)
            usage = _sum_usage(usage, repair.usage)
            finish = repair.finish_reason or "stop"
            try:
                output = profile.parse_output(repair.text, request)
            except (TaskParseError, ValidationError) as exc:
                raise _task_output_invalid(profile.id) from exc

        logger.info("task_run", task=profile.id, finish_reason=finish)

        return TaskResult(
            id=new_task_run_id(),
            task=profile.id,
            model=model,
            output=output,
            finish_reason=finish,
            usage=TaskUsage(
                prompt_tokens=usage.prompt_tokens or 0,
                completion_tokens=usage.completion_tokens or 0,
                total_tokens=usage.total_tokens or 0,
                cached_tokens=usage.cached_tokens or 0,
            ),
            prompt_version=profile.id,
        )
