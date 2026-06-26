"""The negotiated Daintree extension: request fields and response metadata.

These travel alongside the OpenAI-compatible payload. A generic OpenAI client
may omit the request extension entirely (stateless compatibility mode); the
response metadata is delivered in the ``X-Daintree-Meta`` header and, for
non-streaming responses, also in a top-level ``daintree`` field.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

# --------------------------------------------------------------------------
# Request extension (section 6.4).
# --------------------------------------------------------------------------


class ClientInfo(BaseModel):
    model_config = ConfigDict(extra="ignore")

    name: str | None = Field(default=None, max_length=128)
    version: str | None = Field(default=None, max_length=64)
    platform: str | None = Field(default=None, max_length=64)


class McpInfo(BaseModel):
    model_config = ConfigDict(extra="ignore")

    connected: bool = False
    transport: str | None = Field(default=None, max_length=64)
    tool_count: int | None = Field(default=None, ge=0)
    status: str | None = Field(default=None, max_length=64)


class RuntimeContext(BaseModel):
    model_config = ConfigDict(extra="ignore")

    permission_tier: str | None = Field(default=None, max_length=64)
    project_path: str | None = Field(default=None, max_length=4096)
    project_id: str | None = Field(default=None, max_length=256)
    mcp: McpInfo | None = None
    configured_agent_ids: list[str] = Field(default_factory=list)
    scheduler_active: bool = True
    active_worktree: str | None = Field(default=None, max_length=512)
    project_instructions: str | None = None


class Memories(BaseModel):
    model_config = ConfigDict(extra="ignore")

    pinned: list[str] = Field(default_factory=list)
    relevant: list[str] = Field(default_factory=list)


class TurnContext(BaseModel):
    model_config = ConfigDict(extra="ignore")

    goal: str | None = None
    is_wake: bool = False
    workflow_runs: list[str] = Field(default_factory=list)
    memories: Memories = Field(default_factory=Memories)
    session_ended_watchers: list[str] = Field(default_factory=list)


class Selection(BaseModel):
    model_config = ConfigDict(extra="ignore")

    policy: Literal["new_instruction", "always"] = "new_instruction"
    force: bool = False


class DaintreeExtension(BaseModel):
    model_config = ConfigDict(extra="ignore")

    protocol_version: int | None = None
    session_id: str | None = Field(default=None, max_length=256)
    turn_id: str | None = Field(default=None, max_length=256)
    instruction_revision: int = Field(default=0, ge=0)
    round: int = Field(default=0, ge=0)
    state: str | None = Field(default=None, max_length=16384)
    client: ClientInfo | None = None
    runtime_context: RuntimeContext | None = None
    turn_context: TurnContext | None = None
    selection: Selection = Field(default_factory=Selection)


# --------------------------------------------------------------------------
# Response metadata (section 6.6).
# --------------------------------------------------------------------------


class SkillRef(BaseModel):
    id: str
    version: str
    title: str


class PreludeToolCallFunction(BaseModel):
    name: str
    arguments: str


class PreludeToolCall(BaseModel):
    id: str
    type: Literal["function"] = "function"
    function: PreludeToolCallFunction


class PreludeToolResult(BaseModel):
    role: Literal["tool"] = "tool"
    tool_call_id: str
    content: str


class PreludeExecution(BaseModel):
    call: PreludeToolCall
    result: PreludeToolResult
    display_name: str


class Prelude(BaseModel):
    tool_executions: list[PreludeExecution] = Field(default_factory=list)


class SelectorUsage(BaseModel):
    prompt_tokens: int = 0
    completion_tokens: int = 0
    total_tokens: int = 0
    cached_tokens: int = 0


class SelectorMeta(BaseModel):
    ran: bool = False
    degraded: bool = False
    task_type: str | None = None
    confidence: float | None = None
    usage: SelectorUsage | None = None
    # Free-text reason is omitted by default; only present under a debug flag.
    reason: str | None = None


class ResponseMeta(BaseModel):
    v: int = 1
    request_id: str
    state: str | None = None
    catalog_revision: str
    prompt_version: str
    active_skills: list[SkillRef] = Field(default_factory=list)
    newly_loaded_skills: list[SkillRef] = Field(default_factory=list)
    prelude: Prelude = Field(default_factory=Prelude)
    selector: SelectorMeta = Field(default_factory=SelectorMeta)
    warnings: list[str] = Field(default_factory=list)
