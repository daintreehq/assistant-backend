"""Wire contract for the v2 durable run protocol (``/v2/runs`` and friends).

Strict (``extra="forbid"``) like the v1 native contract. The ``/v2/`` URL path
is the protocol version signal — there is no per-request protocol_version
field. Conversation messages and tool definitions reuse the canonical
:mod:`.openai` models; request context reuses :mod:`.extensions`.
See ``docs/RUN_SUPERVISOR.md``.
"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from .extensions import ClientInfo, RuntimeContext, Selection, TurnContext
from .openai import ChatMessage, ToolDef

# --------------------------------------------------------------------------
# Turn submission.
# --------------------------------------------------------------------------


class RunGeneration(BaseModel):
    """Per-run generation knobs. Streaming/response_format are server-owned in
    v2 (output always flows through the run event stream)."""

    model_config = ConfigDict(extra="forbid")

    temperature: float | None = Field(default=None, ge=0, le=2)
    max_tokens: int | None = Field(default=None, ge=1)
    stop: str | list[str] | None = None


class TurnSubmitRequest(BaseModel):
    """One user turn. Only ``user``-role messages are accepted — assistant and
    tool messages are server-recorded facts in v2, never client-supplied."""

    model_config = ConfigDict(extra="forbid")

    turn_id: str = Field(min_length=1, max_length=256)
    messages: list[ChatMessage] = Field(min_length=1, max_length=64)
    runtime: RuntimeContext | None = None
    turn: TurnContext | None = None
    tools: list[ToolDef] | None = None
    generation: RunGeneration | None = None
    selection: Selection = Field(default_factory=Selection)


class RunCreateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    session_id: str | None = Field(default=None, max_length=256)
    project_id: str | None = Field(default=None, max_length=256)
    title: str | None = Field(default=None, max_length=512)
    client: ClientInfo | None = None
    turn: TurnSubmitRequest


class CancelRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    reason: str | None = Field(default=None, max_length=512)


# --------------------------------------------------------------------------
# Views.
# --------------------------------------------------------------------------


class RunView(BaseModel):
    run_id: str
    status: str
    session_id: str | None = None
    project_id: str | None = None
    title: str | None = None
    active_skills: list[str] = Field(default_factory=list)
    current_round: int = 0
    current_turn_id: str | None = None
    last_seq: int = 0
    executor_present: bool = False
    last_error: str | None = None
    created_at: float = 0
    updated_at: float = 0


class TurnAccepted(BaseModel):
    run_id: str
    turn_id: str
    status: str
    seq: int | None = None
    duplicate: bool = False


class LeaseView(BaseModel):
    lease_id: str
    run_id: str
    round: int
    tool_call_id: str
    tool_name: str
    arguments: str
    risk: str | None = None
    status: str
    requires_approval: bool = False
    approval_id: str | None = None
    executor_id: str | None = None
    attempt: int = 0
    claim_expires_at: float | None = None
    deadline: float
    idempotency_key: str
    result_status: str | None = None
    created_at: float
    updated_at: float


class WakeupView(BaseModel):
    wakeup_id: str
    run_id: str
    fire_at: float
    note: str | None = None
    status: str
    created_at: float
    fired_at: float | None = None


# --------------------------------------------------------------------------
# Leases.
# --------------------------------------------------------------------------


class LeaseClaimRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    executor_id: str = Field(min_length=1, max_length=256)


class LeaseClaimResponse(BaseModel):
    outcome: str  # claimed | extended
    lease: LeaseView


class LeaseResultRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    executor_id: str = Field(min_length=1, max_length=256)
    status: Literal["ok", "error", "declined"]
    content: str = Field(default="", max_length=1_048_576)


class LeaseResultResponse(BaseModel):
    outcome: str  # recorded | duplicate
    lease: LeaseView


# --------------------------------------------------------------------------
# Approvals.
# --------------------------------------------------------------------------


class ApprovalResolveRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    approve: bool
    note: str | None = Field(default=None, max_length=1024)


class ApprovalView(BaseModel):
    approval_id: str
    run_id: str
    lease_id: str
    status: str
    reason: str | None = None
    note: str | None = None
    requested_at: float
    resolved_at: float | None = None


# --------------------------------------------------------------------------
# Executors.
# --------------------------------------------------------------------------


class ExecutorTool(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str = Field(min_length=1, max_length=64)
    risk: str | None = Field(default=None, max_length=32)
    description: str | None = Field(default=None, max_length=256)


class HeartbeatRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    executor_id: str = Field(min_length=1, max_length=256)
    project_id: str | None = Field(default=None, max_length=256)
    client: ClientInfo | None = None
    tools: list[ExecutorTool] = Field(default_factory=list, max_length=256)
    capabilities: dict[str, Any] = Field(default_factory=dict)
    ttl_seconds: float = Field(default=90.0, ge=10.0, le=3600.0)


class HeartbeatResponse(BaseModel):
    executor_id: str
    expires_at: float
    pending_leases: list[LeaseView] = Field(default_factory=list)


# --------------------------------------------------------------------------
# Wakeups.
# --------------------------------------------------------------------------


class WakeupCreateRequest(BaseModel):
    """Exactly one of ``delay_seconds`` / ``fire_at`` is required."""

    model_config = ConfigDict(extra="forbid")

    delay_seconds: float | None = Field(default=None, ge=0, le=30 * 86400)
    fire_at: float | None = Field(default=None, ge=0)
    note: str | None = Field(default=None, max_length=512)
