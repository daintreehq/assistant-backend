"""Wire models for the named utility-task API (section 11).

Clients send task *data*; the server owns the prompt, model, schema, and
output mode. Each task has a typed input model and a typed output model so
both directions are validated.
"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

# --------------------------------------------------------------------------
# Envelope.
# --------------------------------------------------------------------------


class TaskRequest(BaseModel):
    # ``extra="forbid"`` so a task request can never smuggle in ``messages``,
    # ``system``, ``developer``, or ``system_prompt`` — task prompts are
    # server-owned.
    model_config = ConfigDict(extra="forbid")

    task: str = Field(min_length=1, max_length=128)
    request_id: str | None = Field(default=None, max_length=256)
    input: dict[str, Any] = Field(default_factory=dict)
    # Only consumed by terminal_extract_json.v1.
    result_schema: dict[str, Any] | None = None


class TaskUsage(BaseModel):
    prompt_tokens: int = 0
    completion_tokens: int = 0
    total_tokens: int = 0
    cached_tokens: int = 0


class TaskResult(BaseModel):
    id: str
    object: Literal["daintree.task.result"] = "daintree.task.result"
    task: str
    model: str
    output: dict[str, Any]
    finish_reason: str = "stop"
    usage: TaskUsage
    prompt_version: str


# --------------------------------------------------------------------------
# Per-task input models.
# --------------------------------------------------------------------------


class CheckpointInput(BaseModel):
    model_config = ConfigDict(extra="ignore")

    transcript: str = Field(max_length=400_000)


class MemoryDistillInput(BaseModel):
    model_config = ConfigDict(extra="ignore")

    transcript: str = Field(max_length=400_000)


class TerminalState(BaseModel):
    model_config = ConfigDict(extra="ignore")

    agent_state: str | None = None
    runtime_status: str | None = None
    waiting_reason: str | None = None
    last_output_at: str | None = None


class WatcherClassifyInput(BaseModel):
    model_config = ConfigDict(extra="ignore")

    goal: str = Field(default="", max_length=8192)
    terminal_state: TerminalState = Field(default_factory=TerminalState)
    previous_classification: str | None = Field(default=None, max_length=256)
    tail: str = Field(default="", max_length=120_000)


class TerminalJudgeInput(BaseModel):
    model_config = ConfigDict(extra="ignore")

    goal: str = Field(default="", max_length=8192)
    question: str = Field(min_length=1, max_length=4096)
    terminal_state: TerminalState = Field(default_factory=TerminalState)
    tail: str = Field(default="", max_length=120_000)


class TerminalSummarizeInput(BaseModel):
    model_config = ConfigDict(extra="ignore")

    purpose: str = Field(default="", max_length=4096)
    tail: str = Field(default="", max_length=120_000)


class TerminalExtractTextInput(BaseModel):
    model_config = ConfigDict(extra="ignore")

    terminal_ids: list[str] = Field(default_factory=list)
    instruction: str = Field(min_length=1, max_length=8192)
    tail: str = Field(default="", max_length=120_000)


class TerminalExtractJsonInput(BaseModel):
    model_config = ConfigDict(extra="ignore")

    terminal_ids: list[str] = Field(default_factory=list)
    instruction: str = Field(min_length=1, max_length=8192)
    tail: str = Field(default="", max_length=120_000)


class ExtractionVerdictInput(BaseModel):
    model_config = ConfigDict(extra="ignore")

    result: str = Field(max_length=120_000)
    condition: str = Field(min_length=1, max_length=8192)


class SkillStepConsistencyInput(BaseModel):
    model_config = ConfigDict(extra="ignore")

    skill_id: str = Field(min_length=1, max_length=256)
    completed_step: int = Field(ge=0)
    status: Literal["done", "skipped"] = "done"
    requested_next: str | None = Field(default=None, max_length=256)
    current_step_before: int = Field(ge=0)
    current_step_after: int = Field(ge=0)
    run_status_before: str = Field(default="active", max_length=64)
    run_status_after: str = Field(default="active", max_length=64)
    notes: str | None = Field(default=None, max_length=4096)
    progress_before: list[dict[str, Any]] = Field(default_factory=list)
    progress_after: list[dict[str, Any]] = Field(default_factory=list)


# --------------------------------------------------------------------------
# Per-task output models.
# --------------------------------------------------------------------------


class DistilledFact(BaseModel):
    fact: str = Field(max_length=400)
    kind: Literal["semantic", "episodic"] = "semantic"


class MemoryDistillOutput(BaseModel):
    facts: list[DistilledFact] = Field(default_factory=list)


class CheckpointOutput(BaseModel):
    model_config = ConfigDict(extra="ignore")

    goal: str = ""
    active_terminals: list[str] = Field(default_factory=list)
    active_watchers: list[str] = Field(default_factory=list)
    workflow_run_ids: list[str] = Field(default_factory=list)
    decisions: list[str] = Field(default_factory=list)
    pending_tool_state: list[str] = Field(default_factory=list)
    next_actions: list[str] = Field(default_factory=list)
    open_questions: list[str] = Field(default_factory=list)
    approvals_grants: list[str] = Field(default_factory=list)
    preserved_ids: list[str] = Field(default_factory=list)


class WatcherClassifyOutput(BaseModel):
    model_config = ConfigDict(extra="ignore")

    classification: str
    confidence: float = Field(ge=0, le=1)
    summary: str = ""
    evidence: list[str] = Field(default_factory=list)
    recommendedAction: str = "none"


class JudgeOutput(BaseModel):
    model_config = ConfigDict(extra="ignore")

    reason: str = ""
    confidence: float = Field(ge=0, le=1)
    matched: bool = False


class TextOutput(BaseModel):
    text: str = ""


class ExtractJsonOutput(BaseModel):
    result: Any = None


class ExtractionVerdictOutput(BaseModel):
    model_config = ConfigDict(extra="ignore")

    # ``pass`` is a Python keyword; expose it via an alias.
    passed: bool = Field(alias="pass")
    reason: str = ""
