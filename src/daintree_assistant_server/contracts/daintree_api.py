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
    Routing,
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
    # Optional endpoint-routing preference. Omitted by almost every request,
    # which keeps the server default (see providers/policy.py DEFAULT_POLICY).
    routing: Routing | None = None
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
    # What this turn's MAIN call charged the caller, in USD. The caller's own key
    # funds every upstream call, so this is their bill, not ours — which is the
    # whole reason it is surfaced rather than merely logged.
    #
    # It covers the main completion ONLY. The selector's cost is reported
    # separately under ``skills.selector.usage``; a client showing a turn total
    # adds the two. Kept split rather than summed because they answer different
    # questions — "what did the answer cost" and "what did routing cost" — and a
    # single number hides the one the prompt work is trying to move.
    #
    # ``None`` means the provider reported no cost, never "free".
    cost: float | None = None


class TurnTimings(BaseModel):
    """Wall-clock milliseconds by phase, measured server-side.

    WALL CLOCK, not the provider's accounting. Every figure is measured with
    ``time.perf_counter()`` around a real await on this server, so it includes
    queueing, network and upstream time exactly as the user experienced them.
    That is the point: a provider's own latency number cannot see the round trip
    it sits inside, and the question these answer is "where did the user's wait
    actually go".

    A phase that did not happen is ABSENT from the JSON, never ``0``. Both
    response paths serialize with ``exclude_none``, so an optional field here is
    simply missing rather than null — and that is the intended contract: a
    selector that did not run and a selector that answered instantly are
    different facts, and a zero would merge them. A client reads a missing key as
    "this phase did not occur on this turn", not as zero milliseconds.

    ``preparation_ms`` and ``total_ms`` are always present: every turn prepares,
    and every turn ends.

    The phases do NOT sum to ``total_ms``. They can overlap: an eagerly-started
    stream opens while the selector is still running, so ``selection_ms`` and
    ``upstream_open_ms`` can cover the same wall clock. Read them as "how long
    did this part take", never as a partition of the turn.
    """

    model_config = ConfigDict(extra="forbid")

    #: The skill-selector call. ``None`` when selection did not run — a
    #: tool-continuation round, or the no-task fast path. Includes a parse-repair
    #: round trip when one happened, which is why a surprising value should be
    #: read next to ``skills.selector`` rather than alone.
    selection_ms: int | None = None
    #: The documentation lookup, when the selector asked for one. ``None`` when
    #: no query was issued.
    docs_ms: int | None = None
    #: Request in until the upstream request is built and ready to open. Covers
    #: selection, any docs lookup, state verification and prompt assembly. This
    #: is the part of the wait this server is wholly responsible for.
    preparation_ms: int = 0
    #: Opening the main upstream call: connect, send, and wait for its first
    #: event. Mostly prefill. ``None`` if the turn never reached the model.
    #: A phase duration (open to first event), not a cumulative mark — it
    #: composes with ``preparation_ms``, which owns everything before the open.
    upstream_open_ms: int | None = None
    #: First upstream event until the first VISIBLE token: the model's
    #: chain-of-thought. ``None`` when thinking is off, which is the shipped
    #: posture for every interactive call (docs/PROVIDER_POLICY.md section 2), so
    #: a non-null value here on a normal turn means that posture changed.
    thinking_ms: int | None = None
    #: Request in until the first visible token reaches the client. The headline
    #: number, and the one a user calls "it started answering".
    first_output_ms: int | None = None
    #: First visible token until the turn completes. Pure generation.
    generation_ms: int | None = None
    #: Request in until the response is complete. The whole wait.
    total_ms: int = 0


class TurnCost(BaseModel):
    """What this whole request charged the caller, in USD.

    The caller's own key funds every upstream call a turn makes, so the number
    that matters to them is the TOTAL — not what any one call cost. Skill
    selection is local (in-process, unbilled), so an ordinary turn is ONE billed
    call — the main completion — and a re-rolled round additionally pays for a
    generation nobody ever saw; all of it lands on their card, so all of it is
    counted here.

    ``total`` is the headline. The breakdown is kept because it answers the
    follow-up question — "why was that turn expensive?". ``selector`` remains in
    the shape for the scripted test seam but is null on production turns: local
    classification costs nothing upstream.

    The whole object is ABSENT when the provider reported no cost at all. That is
    deliberate: a zero would read as "this turn was free", which is a claim about
    someone's bill that we would have no basis for. The authority on a bill is
    the OpenRouter dashboard; this field is for proportion and trend.
    """

    model_config = ConfigDict(extra="forbid")

    total: float
    main: float | None = None
    selector: float | None = None
    # False when a call this turn MADE did not report its cost, so ``total`` is
    # the sum of what we could observe and nothing more.
    #
    # This distinction is the whole contract. Without it, a selector whose cost
    # went unreported contributes 0.0 to a field labelled "total", and a partial
    # sum is presented as a complete one — the same "zero means free" lie the
    # absent-block rule exists to prevent, wearing a different hat. A client
    # showing a session total should render an incomplete turn as a lower bound.
    complete: bool = True


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
    # What this request cost the caller in total, across every upstream call it
    # made. Absent when the provider reported no cost. See TurnCost.
    cost: TurnCost | None = None
    # Where the wall clock went, by phase. See TurnTimings.
    timings: TurnTimings = Field(default_factory=TurnTimings)
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
    # The turn's total spend, same shape and same rule as the non-streaming
    # body's. Carried on the terminal event because that is the first moment the
    # main call's usage is known — OpenRouter reports it in the final chunk.
    cost: TurnCost | None = None
    # Where the wall clock went, by phase. On the TERMINAL event for the same
    # reason as cost: generation and total are not known until the stream ends.
    # `meta` cannot carry them — it is emitted before the model is even opened,
    # which is precisely what makes it useful.
    timings: TurnTimings = Field(default_factory=TurnTimings)
    # Output-contract findings for the text just streamed (``output_think_leak``,
    # ``output_oversized``, ``output_wide_table``). Advisory only — the bytes are
    # already on the wire, so this is the terminal event's chance to tell the CLI
    # that what it just rendered was malformed.
    #
    # Deliberately NOT an error and deliberately not a mid-stream termination:
    # the "hidden" content that can leak here is the server's own prompt and the
    # user's own local context, never a third party's secret, so a leak is ugly
    # rather than dangerous — and killing the stream would cost the user a reply
    # they are already reading in exchange for nothing.
    warnings: list[str] = Field(default_factory=list)


class StreamError(BaseModel):
    """Terminal streaming error.

    ``request_id`` carries the SAME correlation id as the response header and every
    log line for this turn. It is on the ERROR event specifically because a stream
    can fail BEFORE ``meta`` is ever emitted — a preparation failure is exactly
    that — and ``meta`` is otherwise the only event carrying the id. Without this
    field the one stream a tester reports is the one whose identity never reached
    them. Repeating it on an error that follows a ``meta`` costs a few bytes on a
    path that is already terminating, and saves the client from having to have
    retained the earlier event to report the failure.

    ``retry_after`` preserves the upstream's raw ``Retry-After`` header when a
    rate-limited open/prefetch exhausts its internal retries after SSE metadata
    has already committed the HTTP 200. It is absent for every other error.
    """

    error: ErrorDetail
    request_id: str | None = None
    retry_after: str | None = None
