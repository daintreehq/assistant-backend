"""The native Daintree Assistant API wire contract (``/v1/daintree/respond``).

This is a Daintree-owned protocol — not OpenAI-compatible. The skill/session
envelope is first-class (not a bolted-on extension), the request is strict
(``extra="forbid"`` everywhere), and skills are surfaced as their own top-level
block in the response because dynamic skill loading is the system's core value.

Conversation messages and tool definitions reuse the internal canonical models
(:mod:`.openai`) so the selector, assembler, and DeepSeek provider — which speak
the OpenAI wire shape *upstream* — need no translation. Only the public envelope
and the response/stream serialization are Daintree-native.
"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from .errors import ErrorDetail
from .extensions import (
    ClientInfo,
    Prelude,
    RuntimeContext,
    Selection,
    SelectorMeta,
    SkillRef,
    StartupContext,
    TurnContext,
)
from .openai import ChatMessage, ToolCall, ToolDef

# --------------------------------------------------------------------------
# Request.
# --------------------------------------------------------------------------


class RespondSession(BaseModel):
    """Identifies the conversation and turn so skill state and selector cadence
    have a stable key. Required whenever a request is made — the native API has
    no anonymous/stateless mode."""

    model_config = ConfigDict(extra="forbid")

    id: str = Field(min_length=1, max_length=256)
    turn_id: str = Field(min_length=1, max_length=256)
    instruction_revision: int = Field(default=0, ge=0)
    round: int = Field(default=0, ge=0)


class RespondToolChoice(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str = Field(min_length=1, max_length=64)


# "auto"/"none"/"required" or a specific tool by name.
RespondToolChoiceField = Literal["none", "auto", "required"] | RespondToolChoice


class RespondInput(BaseModel):
    """The conversation plus the client's current tool inventory."""

    model_config = ConfigDict(extra="forbid")

    messages: list[ChatMessage] = Field(min_length=1)
    tools: list[ToolDef] | None = None
    tool_choice: RespondToolChoiceField | None = None


class Generation(BaseModel):
    """Only the generation parameters Daintree supports. Anything else is
    rejected by the schema rather than silently ignored."""

    model_config = ConfigDict(extra="forbid")

    temperature: float | None = Field(default=None, ge=0, le=2)
    max_tokens: int | None = Field(default=None, ge=1)
    stop: str | list[str] | None = None
    response_format: Literal["text", "json_object"] | None = None
    stream: bool = False


class RespondRequest(BaseModel):
    """The single request body for ``POST /v1/daintree/respond``."""

    model_config = ConfigDict(extra="forbid")

    protocol_version: int
    session: RespondSession
    state: str | None = Field(default=None, max_length=16384)
    input: RespondInput
    # Required even when empty. Keeping stable local context outside
    # ``input.messages`` makes the conversation semantically pure and lets the
    # assembler place this data at its cache-friendly boundary.
    startup: StartupContext
    runtime: RuntimeContext | None = None
    turn: TurnContext | None = None
    selection: Selection = Field(default_factory=Selection)
    generation: Generation = Field(default_factory=Generation)
    client: ClientInfo | None = None


# --------------------------------------------------------------------------
# Response (non-streaming) and the skills block.
# --------------------------------------------------------------------------


class SkillsBlock(BaseModel):
    """The dynamic-skill outcome for this turn — the heart of the protocol.

    ``prelude`` is the synthetic skill-load the client renders *before* the
    assistant's reply, so the user sees the skill being loaded as if it happened
    first.
    """

    active: list[SkillRef] = Field(default_factory=list)
    newly_loaded: list[SkillRef] = Field(default_factory=list)
    prelude: Prelude = Field(default_factory=Prelude)
    selector: SelectorMeta = Field(default_factory=SelectorMeta)


class RespondUsage(BaseModel):
    prompt_tokens: int = 0
    completion_tokens: int = 0
    total_tokens: int = 0
    cached_tokens: int = 0


class RespondMessage(BaseModel):
    role: str = "assistant"
    content: str | None = None
    # DeepSeek thinking-mode chain-of-thought (present only when thinking is on).
    # The client must store it on the assistant turn and replay it next request
    # when the turn performed tool calls — see ChatMessage.reasoning_content.
    reasoning_content: str | None = None
    tool_calls: list[ToolCall] | None = None


class RespondResponse(BaseModel):
    """Non-streaming response body."""

    protocol_version: int = 2
    request_id: str
    model: str
    message: RespondMessage
    finish_reason: str | None = None
    usage: RespondUsage = Field(default_factory=RespondUsage)
    # First-class skill outcome.
    skills: SkillsBlock = Field(default_factory=SkillsBlock)
    state: str | None = None
    catalog_revision: str
    prompt_version: str
    warnings: list[str] = Field(default_factory=list)


# --------------------------------------------------------------------------
# Streaming event payloads (named SSE events: meta / status / delta / done / error).
# --------------------------------------------------------------------------


class StreamMeta(BaseModel):
    """Immediate first streamed event, emitted before the upstream model opens.

    It carries everything the client needs to surface selection progress before
    the first model token: the skill prelude/active set, refreshed state token,
    and selector outcome.
    """

    protocol_version: int = 2
    request_id: str
    model: str
    skills: SkillsBlock = Field(default_factory=SkillsBlock)
    state: str | None = None
    catalog_revision: str
    prompt_version: str
    warnings: list[str] = Field(default_factory=list)


class StreamDelta(BaseModel):
    content: str | None = None
    # Thinking-mode chain-of-thought fragments, streamed before content (present
    # only when thinking is on). The client concatenates these into the assistant
    # turn's reasoning_content for replay — see ChatMessage.reasoning_content.
    reasoning_content: str | None = None
    # Standard streaming tool-call delta fragments, passed through verbatim.
    tool_calls: list[dict[str, Any]] | None = None


class StreamStatus(BaseModel):
    """A generation-phase marker streamed after ``meta``. Currently emitted once
    with ``phase="thinking"`` the moment the model begins its thinking-mode
    chain-of-thought — the ``reasoning_content`` deltas that follow are the CoT to
    display. Absent entirely when thinking is off."""

    phase: str


class StreamDone(BaseModel):
    finish_reason: str | None = None
    usage: RespondUsage = Field(default_factory=RespondUsage)


class StreamError(BaseModel):
    """Terminal streaming error.

    ``retry_after`` preserves the upstream's raw ``Retry-After`` header when a
    rate-limited open/prefetch exhausts its internal retries after SSE metadata
    has already committed the HTTP 200. It is absent for every other error.
    """

    error: ErrorDetail
    retry_after: str | None = None
