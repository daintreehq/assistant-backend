"""Wire models for the named utility-task API (section 11).

Clients send task *data*; the server owns the prompt, model, schema, and
output mode. Every task has a typed INPUT model. Most have a typed output model
too, but the text / memory-distill / extract-JSON tasks are normalized by hand-
written parsers in ``services/task_runner.py`` instead.
"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

from .extensions import Routing

# --------------------------------------------------------------------------
# Envelope.
# --------------------------------------------------------------------------


class TaskRequest(BaseModel):
    # ``extra="forbid"`` so a task request can never smuggle in ``messages``,
    # ``system``, ``developer``, or ``system_prompt`` — task prompts are
    # server-owned.
    model_config = ConfigDict(extra="forbid")

    task: str = Field(min_length=1, max_length=128)
    # Echoed into a response HEADER, so constrain it to header-safe characters here.
    # A non-latin-1 value would otherwise raise UnicodeEncodeError inside JSONResponse
    # AFTER the upstream call has run and been billed — a 500 for a request that should
    # have been rejected (as a 400) before any spend.
    request_id: str | None = Field(default=None, max_length=256, pattern=r"^[A-Za-z0-9._:-]+$")
    input: dict[str, Any] = Field(default_factory=dict)
    # Only consumed by terminal_extract_json.
    result_schema: dict[str, Any] | None = None
    # Optional endpoint-routing preference, identical in shape and meaning to the
    # one on /respond. A task sends the caller's content upstream just as a turn
    # does — terminal tails, transcripts, memories — so a privacy choice that
    # applied only to /respond would be honoured exactly where the user could see
    # it and dropped everywhere else.
    routing: Routing | None = None


class TaskUsage(BaseModel):
    prompt_tokens: int = 0
    completion_tokens: int = 0
    total_tokens: int = 0
    cached_tokens: int = 0
    # USD this task charged the caller, as reported by the provider. A task is a
    # single upstream call, so this IS its total. Same rule as a turn's: absent
    # means the provider reported nothing, never that the call was free.
    #
    # Surfaced for the same reason /respond surfaces it — the caller's key funds
    # it. A CLI that only counted turns would under-report a session that ran
    # dozens of summarize/extract calls behind the scenes.
    cost: float | None = None


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


class CheckpointOutput(BaseModel):
    model_config = ConfigDict(extra="ignore")

    goal: str = ""
    # Standing user instructions/constraints stated mid-conversation ("never close
    # terminals", "always branch first") — the highest-value content a compaction can
    # lose, preserved near-verbatim so the orchestrator keeps honouring them.
    user_directives: list[str] = Field(default_factory=list)
    active_terminals: list[str] = Field(default_factory=list)
    active_watchers: list[str] = Field(default_factory=list)
    workflow_run_ids: list[str] = Field(default_factory=list)
    decisions: list[str] = Field(default_factory=list)
    # The loop-prevention register: approaches tried and failed (with why), so the
    # post-compaction assistant does not repeat them. Industry-standard checkpoint
    # section (Claude Code "Errors and Fixes"; the documented failure mode without it
    # is the agent re-running the same failing tool call after history is discarded).
    failed_approaches: list[str] = Field(default_factory=list)
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


class ExtractionVerdictOutput(BaseModel):
    model_config = ConfigDict(extra="ignore")

    # ``pass`` is a Python keyword; expose it via an alias.
    passed: bool = Field(alias="pass")
    reason: str = ""


# --------------------------------------------------------------------------
# Workflow intelligence tasks (workflow_plan / workflow_reconcile /
# workflow_resume_digest).
#
# The execution graph is CLIENT-OWNED durable state: the CLI stores it locally
# and sends bounded snapshots here. These tasks are pure functions of their
# input — the server never persists workflow state. Outputs are typed and
# hard-capped so a hallucinating small model cannot flood the client with an
# unbounded "plan"; the client additionally validates every plan/patch against
# its own invariants before committing anything.
# --------------------------------------------------------------------------

# Hard output caps (also cited in the task prompts).
MAX_PLAN_NODES = 20
MAX_PLAN_EDGES = 40
MAX_PATCH_RESOURCES = 30
MAX_NODE_TITLE = 120
MAX_NODE_EVIDENCE = 10
MAX_NODE_EVIDENCE_CHARS = 200

WorkflowStatus = Literal["planned", "active", "blocked", "done", "cancelled", "failed"]
NodeKind = Literal[
    "orient",
    "delegate",
    "wait",
    "verify",
    "synthesize",
    "ask_user",
    "approval",
    "cleanup",
    "report",
]
NodeStatus = Literal[
    "pending",
    "ready",
    "running",
    "waiting",
    "blocked",
    "done",
    "skipped",
    "failed",
    "cancelled",
]
AsyncPolicy = Literal["blocking", "async_ok", "async_required", "wake_only"]

# Node statuses the client may still move; done/skipped/cancelled are final.
TERMINAL_NODE_STATUSES: frozenset[str] = frozenset({"done", "skipped", "cancelled"})


def _truncate_items(items: list[str], *, max_items: int, max_chars: int) -> list[str]:
    """Tolerantly bound a model-produced string list (truncate, never reject)."""
    return [item[:max_chars] for item in items[:max_items]]


class RecommendedActionOut(BaseModel):
    """A recommendation, never execution authority: the client dispatches tools
    through its own safety policy and treats this as advisory text + hints."""

    model_config = ConfigDict(extra="ignore")

    label: str = Field(min_length=1, max_length=200)
    tool_name: str | None = Field(default=None, max_length=128)
    args: dict[str, Any] = Field(default_factory=dict)
    risk: str | None = Field(default=None, max_length=64)
    requires_confirmation: bool = False
    node_id: str | None = Field(default=None, max_length=128)


class MultipleChoiceQuestionOut(BaseModel):
    model_config = ConfigDict(extra="ignore")

    question: str = Field(min_length=1, max_length=512)
    options: list[str] = Field(min_length=2, max_length=6)
    allow_other: bool = False

    @field_validator("options")
    @classmethod
    def _bound_options(cls, v: list[str]) -> list[str]:
        return [opt[:200] for opt in v]


class WorkflowNodeOut(BaseModel):
    model_config = ConfigDict(extra="ignore")

    id: str = Field(min_length=1, max_length=128)
    title: str = Field(min_length=1, max_length=MAX_NODE_TITLE)
    kind: NodeKind
    status: NodeStatus = "pending"
    depends_on: list[str] = Field(default_factory=list, max_length=MAX_PLAN_NODES)
    tool_name: str | None = Field(default=None, max_length=128)
    tool_args: dict[str, Any] = Field(default_factory=dict)
    risk: str | None = Field(default=None, max_length=64)
    requires_confirm: bool = False
    expected_evidence: list[str] = Field(default_factory=list)
    async_policy: AsyncPolicy = "blocking"
    owner: str = Field(default="assistant", max_length=128)

    @field_validator("expected_evidence")
    @classmethod
    def _bound_evidence(cls, v: list[str]) -> list[str]:
        return _truncate_items(v, max_items=MAX_NODE_EVIDENCE, max_chars=MAX_NODE_EVIDENCE_CHARS)


class WorkflowEdgeOut(BaseModel):
    model_config = ConfigDict(extra="ignore")

    source: str = Field(min_length=1, max_length=128)
    target: str = Field(min_length=1, max_length=128)


class NodePatchOut(BaseModel):
    model_config = ConfigDict(extra="ignore")

    node_id: str = Field(min_length=1, max_length=128)
    status: NodeStatus | None = None
    title: str | None = Field(default=None, max_length=MAX_NODE_TITLE)
    last_error: str | None = Field(default=None, max_length=512)
    note: str | None = Field(default=None, max_length=512)


class ResourcePatchOut(BaseModel):
    model_config = ConfigDict(extra="ignore")

    resource_id: str = Field(min_length=1, max_length=256)
    status: str | None = Field(default=None, max_length=64)
    node_id: str | None = Field(default=None, max_length=128)
    label: str | None = Field(default=None, max_length=200)


class WorkflowBlockerOut(BaseModel):
    model_config = ConfigDict(extra="ignore")

    id: str | None = Field(default=None, max_length=128)
    summary: str = Field(min_length=1, max_length=512)
    node_id: str | None = Field(default=None, max_length=128)
    kind: str = Field(default="other", max_length=64)


class WorkflowPatchOut(BaseModel):
    """A graph patch, applied client-side with optimistic revision checks.

    It updates graph STATE only — it can never execute tools, close terminals,
    or bypass confirmation/grant policy on the client.
    """

    model_config = ConfigDict(extra="ignore")

    base_revision: int | None = None
    new_status: WorkflowStatus | None = None
    node_updates: list[NodePatchOut] = Field(default_factory=list, max_length=MAX_PLAN_EDGES)
    add_nodes: list[WorkflowNodeOut] = Field(default_factory=list, max_length=MAX_PLAN_NODES)
    add_edges: list[WorkflowEdgeOut] = Field(default_factory=list, max_length=MAX_PLAN_EDGES)
    resource_updates: list[ResourcePatchOut] = Field(
        default_factory=list, max_length=MAX_PATCH_RESOURCES
    )
    add_blockers: list[WorkflowBlockerOut] = Field(default_factory=list, max_length=10)
    resolve_blockers: list[str] = Field(default_factory=list, max_length=20)
    rationale: str = Field(default="", max_length=1000)


class WorkflowResumeItem(BaseModel):
    model_config = ConfigDict(extra="ignore")

    workflow_id: str = Field(min_length=1, max_length=128)
    rank: int = Field(ge=1)
    headline: str = Field(min_length=1, max_length=200)
    status: str = Field(default="", max_length=64)
    summary: str = Field(default="", max_length=1000)
    blockers: list[str] = Field(default_factory=list)
    recommended_action: RecommendedActionOut | None = None

    @field_validator("blockers")
    @classmethod
    def _bound_blockers(cls, v: list[str]) -> list[str]:
        return _truncate_items(v, max_items=10, max_chars=300)


# ---- workflow task inputs -------------------------------------------------


class WorkflowPlanInput(BaseModel):
    model_config = ConfigDict(extra="ignore")

    goal: str = Field(min_length=1, max_length=8192)
    scope: str | None = Field(default=None, max_length=4096)
    runtime_summary: dict[str, Any] = Field(default_factory=dict)
    tool_inventory: list[dict[str, Any]] = Field(default_factory=list, max_length=256)
    active_skill_ids: list[str] = Field(default_factory=list, max_length=16)
    existing_workflow: dict[str, Any] | None = None
    relevant_memories: list[str] = Field(default_factory=list, max_length=50)
    open_resources: list[dict[str, Any]] = Field(default_factory=list, max_length=100)
    constraints: list[str] = Field(default_factory=list, max_length=25)


class WorkflowReconcileInput(BaseModel):
    model_config = ConfigDict(extra="ignore")

    workflow: dict[str, Any]
    recent_events: list[dict[str, Any]] = Field(default_factory=list, max_length=100)
    runtime_summary: dict[str, Any] = Field(default_factory=dict)
    queue_events: list[dict[str, Any]] = Field(default_factory=list, max_length=50)
    latest_user_message: str | None = Field(default=None, max_length=8192)
    reason: Literal[
        "tool_result",
        "queue_event",
        "wake",
        "manual",
        "resume",
        "failure",
        "user_interjection",
    ]
    tool_inventory: list[dict[str, Any]] = Field(default_factory=list, max_length=256)


class WorkflowResumeDigestInput(BaseModel):
    model_config = ConfigDict(extra="ignore")

    workflows: list[dict[str, Any]] = Field(default_factory=list, max_length=20)
    runtime_summary: dict[str, Any] = Field(default_factory=dict)
    queue_digest: list[dict[str, Any]] = Field(default_factory=list, max_length=50)
    user_message: str | None = Field(default=None, max_length=8192)


# ---- workflow task outputs ------------------------------------------------


class WorkflowPlanOutput(BaseModel):
    model_config = ConfigDict(extra="ignore")

    goal: str = Field(min_length=1, max_length=8192)
    status: Literal["planned", "active", "blocked"] = "active"
    success_criteria: list[str] = Field(default_factory=list)
    assumptions: list[str] = Field(default_factory=list)
    nodes: list[WorkflowNodeOut] = Field(min_length=1, max_length=MAX_PLAN_NODES)
    edges: list[WorkflowEdgeOut] = Field(default_factory=list, max_length=MAX_PLAN_EDGES)
    next_action: RecommendedActionOut | None = None
    user_question: MultipleChoiceQuestionOut | None = None
    warnings: list[str] = Field(default_factory=list)
    confidence: float = Field(ge=0, le=1, default=0.7)

    @field_validator("success_criteria", "assumptions", "warnings")
    @classmethod
    def _bound_lists(cls, v: list[str]) -> list[str]:
        return _truncate_items(v, max_items=10, max_chars=300)


class WorkflowReconcileOutput(BaseModel):
    model_config = ConfigDict(extra="ignore")

    patch: WorkflowPatchOut
    next_action: RecommendedActionOut | None = None
    user_question: MultipleChoiceQuestionOut | None = None
    summary: str = Field(default="", max_length=1000)
    warnings: list[str] = Field(default_factory=list)
    confidence: float = Field(ge=0, le=1, default=0.7)

    @field_validator("warnings")
    @classmethod
    def _bound_warnings(cls, v: list[str]) -> list[str]:
        return _truncate_items(v, max_items=10, max_chars=300)


class WorkflowResumeDigestOutput(BaseModel):
    model_config = ConfigDict(extra="ignore")

    ranked: list[WorkflowResumeItem] = Field(default_factory=list, max_length=20)
    suggested_focus_workflow_id: str | None = Field(default=None, max_length=128)
    assistant_context_lines: list[str] = Field(default_factory=list)

    @field_validator("assistant_context_lines")
    @classmethod
    def _bound_lines(cls, v: list[str]) -> list[str]:
        return _truncate_items(v, max_items=10, max_chars=300)
