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
    model_config = ConfigDict(extra="forbid")

    name: str | None = Field(default=None, max_length=128)
    version: str | None = Field(default=None, max_length=64)
    platform: str | None = Field(default=None, max_length=64)


class McpInfo(BaseModel):
    model_config = ConfigDict(extra="forbid")

    connected: bool = False
    transport: str | None = Field(default=None, max_length=64)
    tool_count: int | None = Field(default=None, ge=0)
    status: str | None = Field(default=None, max_length=64)


class McpServer(BaseModel):
    """One MCP server the CLI is connected to, as the CLI reports it.

    Server-rendered into the main system prompt as inert data. The request is
    strict (``extra="forbid"``): new MCP fields require a contract/protocol bump,
    not a silent drop.
    """

    model_config = ConfigDict(extra="forbid")

    name: str = Field(max_length=128)
    transport: str | None = Field(default=None, max_length=64)
    status: str | None = Field(default=None, max_length=64)
    tool_count: int | None = Field(default=None, ge=0)
    description: str | None = Field(default=None, max_length=4096)
    # Free-form operating notes for this server (e.g. quick paths, gotchas).
    instructions: str | None = Field(default=None, max_length=16384)


class OpenTerminal(BaseModel):
    """One live Daintree terminal in the per-request open-terminal inventory.

    The CLI attaches a fresh, metadata-only snapshot (no terminal output) of the
    open terminals to every request so the model always sees the live roster as
    inert data instead of tool-calling ``terminal.list`` mid-turn to discover it.
    Strict (``extra="forbid"``): a new terminal field is a contract change, not a
    silent drop. ``exit_code`` is nullable because 0 is a meaningful clean exit.
    """

    model_config = ConfigDict(extra="forbid")

    id: str = Field(max_length=256)
    kind: str | None = Field(default=None, max_length=64)
    worktree_id: str | None = Field(default=None, max_length=4096)
    title: str | None = Field(default=None, max_length=512)
    agent_id: str | None = Field(default=None, max_length=256)
    agent_state: str | None = Field(default=None, max_length=64)
    waiting_reason: str | None = Field(default=None, max_length=512)
    exit_code: int | None = None


class RuntimeContext(BaseModel):
    model_config = ConfigDict(extra="forbid")

    permission_tier: str | None = Field(default=None, max_length=64)
    project_path: str | None = Field(default=None, max_length=4096)
    project_id: str | None = Field(default=None, max_length=256)
    mcp: McpInfo | None = None
    # The CLI's actual integration surface, sent per request so the server need
    # not hardcode (and drift from) the tool reference. Rendered as inert data.
    mcp_servers: list[McpServer] = Field(default_factory=list)
    configured_agent_ids: list[str] = Field(default_factory=list)
    scheduler_active: bool = True
    active_worktree: str | None = Field(default=None, max_length=512)
    project_instructions: str | None = None
    # Fresh per-request snapshot of the open terminals (issue #286): the live
    # roster the CLI fetched this turn, rendered as inert data so the model can
    # see what is open without a mid-turn terminal.list discovery call.
    open_terminals: list[OpenTerminal] = Field(default_factory=list)


class Memories(BaseModel):
    model_config = ConfigDict(extra="forbid")

    pinned: list[str] = Field(default_factory=list)
    relevant: list[str] = Field(default_factory=list)


class TurnContext(BaseModel):
    model_config = ConfigDict(extra="forbid")

    goal: str | None = None
    is_wake: bool = False
    workflow_runs: list[str] = Field(default_factory=list)
    memories: Memories = Field(default_factory=Memories)
    session_ended_watchers: list[str] = Field(default_factory=list)


class Selection(BaseModel):
    model_config = ConfigDict(extra="forbid")

    policy: Literal["new_instruction", "always"] = "new_instruction"
    force: bool = False


# --------------------------------------------------------------------------
# Shared response-metadata building blocks (reused by the native API's
# first-class skills block in :mod:`.daintree_api`).
# --------------------------------------------------------------------------


class SkillRef(BaseModel):
    id: str
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
