"""The respond orchestrator: selection cadence, prompt assembly, skill metadata,
and immediate metadata streaming.

It coordinates state verification, the skill selector, the prompt assembler, and
the provider, producing a native Daintree response (non-streaming body, or a
named-event SSE stream) with skills as a first-class block. The protocol is
Daintree-owned — not OpenAI-compatible — though DeepSeek is still spoken in the
OpenAI wire shape *upstream*, behind the provider abstraction.
"""

from __future__ import annotations

import asyncio
import json
import time
from collections.abc import AsyncGenerator, AsyncIterator, Sequence
from dataclasses import dataclass, field, replace
from typing import Any

from pydantic import SecretStr

from .. import PROTOCOL_VERSION
from ..config import DOCS_SKILL_ID, PUBLIC_MODEL_ID, Settings
from ..contracts.daintree_api import (
    RespondMessage,
    RespondRequest,
    RespondResponse,
    SkillsBlock,
    StreamDelta,
    StreamDone,
    StreamError,
    StreamMeta,
    StreamStatus,
    TurnCost,
    TurnTimings,
)
from ..contracts.extensions import (
    Prelude,
    SelectorMeta,
    SelectorUsage,
    SkillRef,
)
from ..contracts.openai import ChatMessage
from ..errors import (
    DaintreeError,
    internal_error,
    turn_deadline_exceeded,
    upstream_runaway_output,
    upstream_timeout,
)
from ..observability import get_logger, new_call_id, new_request_id
from ..prompts.loader import MAIN_PROMPT_VERSION
from ..providers.base import ChatProvider, ProviderChatRequest, ProviderStream, ProviderUsage
from ..providers.retry import retry_upstream
from ..skills.active_set import (
    merge_active_skills,
    resolve_active_skills,
)
from ..skills.catalog import Catalog, CatalogManager
from ..skills.fast_path import can_skip_selection
from ..skills.load_events import SkillLoadExecution
from ..skills.local.selector import SkillSelector
from ..skills.selector import SelectionDecision
from ..skills.tool_projection import ToolProjection, project_tools
from ..state.codec import StateCodec
from ..text_utils import sanitize_line
from .bare_intent import could_still_be_bare_intent, is_bare_tool_intent
from .deadlines import DeadlineExceeded, RequestBudget, TurnPhase
from .docs_events import DocsSearchExecution
from .docs_mcp import (
    DOCS_LOOKUP_TIMEOUT_SECONDS,
    DocsLookup,
    DocsLookupError,
    DocsMcpClient,
)
from .output_policy import lint_output, repair_tables, resolve_content_width
from .prompt_assembler import PromptAssembler
from .tool_call_repair import (
    ParameterIndex,
    StreamToolCallBuffer,
    build_parameter_index,
    repair_tool_call_arguments,
)
from .turn_accounting import (
    log_turn_usage,
    response_usage,
    sum_usage,
    turn_cost,
)

logger = get_logger("daintree.respond")


class _PrepOwnedStream:
    """The streaming body, with explicit ownership of the eager prep task.

    The response body may be closed before it is ever polled (an endpoint
    failure after ``complete()`` returned, or a client that vanished before the
    body started). Closing an un-started async generator never enters its body,
    which would leave selection running. This wrapper cancels prep in exactly
    that window; once iteration has started, closure is delegated to the
    generator, whose finally-chain already cancels prep and closes the provider.
    """

    def __init__(
        self,
        gen: AsyncGenerator[str, None],
        prep_task: asyncio.Task[_PreparedTurn],
    ) -> None:
        self._gen = gen
        self._prep_task = prep_task
        self._started = False

    def __aiter__(self) -> _PrepOwnedStream:
        return self

    async def __anext__(self) -> str:
        self._started = True
        return await self._gen.__anext__()

    async def aclose(self) -> None:
        if not self._started:
            self._prep_task.cancel()
            await asyncio.gather(self._prep_task, return_exceptions=True)
        await self._gen.aclose()


# Sent as the sole addition to a re-rolled round whose first draft announced an
# action and emitted no tool call (issue #25). Phrased to correct the behavior
# without restating the dead sentence, and to leave "no tool needed" available —
# the right answer to some of these rounds is a direct reply, not a forced call.
_BARE_INTENT_NUDGE: dict[str, Any] = {
    "role": "user",
    "content": (
        "[System] Your previous draft was discarded: it only announced an action "
        "and emitted no tool call, so nothing would have run. Redo that reply from "
        "the conversation above. If the next step needs a tool, emit that tool call "
        "NOW, in this reply. If it does not, answer directly — either way, do not "
        "describe an action you are not taking in this same reply."
    ),
}



@dataclass
class _RespondMeta:
    """The computed skill/state outcome shared by streaming and non-streaming."""

    request_id: str
    state: str
    catalog_revision: str
    prompt_version: str
    skills: SkillsBlock
    warnings: list[str] = field(default_factory=list)
    # Whether `skills.selector.usage.cost` accounts for every selector call this
    # turn made. A malformed selection is repaired with a SECOND billed call, and
    # if only one of the two reported, the summed figure is short. Internal
    # rather than a wire field: `cost.complete` is the single flag a client is
    # told to trust, so the fact travels to `turn_cost` and is folded into that.
    selector_cost_complete: bool = True
    # Which upstream endpoint served the classification, carried here so it can
    # be logged BESIDE the selector's cache ratio rather than only in the
    # preparation-latency line. Internal for the same reason as above: the CLI
    # was never told which of ~24 hosts answered, and this is a diagnosis for
    # whoever reads the logs. `None` when selection did not run, or when the
    # provider named no endpoint.
    #
    # A repaired selection reports the endpoint that produced the ANSWER; the
    # discarded first attempt's host is not kept. `selector_calls` (in the
    # `respond` line, same request_id) is what says the ratio blends two calls.
    selector_endpoint: str | None = None
    # The selector's usage as the PROVIDER reported it, kept beside the wire
    # projection in `skills.selector.usage` rather than instead of it. The wire
    # model floors every count to 0 for the client, which cannot express "not
    # reported" — and a cache ratio computed off that floor would claim a
    # stone-cold prefix on a call that simply never counted its hits.
    selector_usage: ProviderUsage | None = None


@dataclass
class RespondOutcome:
    """What the API layer needs to produce the HTTP response."""

    headers: dict[str, str]
    streaming: bool
    body: dict[str, Any] | None = None
    stream: AsyncIterator[str] | None = None


# SSE comment-heartbeat cadence while the stream is otherwise quiet (selection,
# provider open/retries, long thinking gaps). 5–10s keeps load balancers and the
# CLI's idle watchdog fed without measurable overhead; hardcoded like the rest of
# the latency posture (one shipped optimum, no env toggle). Read at call time so
# tests can shrink it.
HEARTBEAT_INTERVAL_SECONDS = 8.0

# Grace for tearing down a provider stream. Deliberately short and separate from
# the turn budget: teardown usually runs in a `finally` on the way out of a
# deadline failure, so an unbounded close would let cleanup outlive the deadline
# that fired and swallow the terminal error owed to the client.
_CLOSE_GRACE_SECONDS = 5.0

# Floor for the docs lookup's share of the preparation budget. Below this a call
# would almost certainly time out, and spending the last second of preparation to
# find that out is worse than answering without documentation.
_DOCS_MIN_TIMEOUT_SECONDS = 0.5

# What the docs lookup may NOT spend, no matter how much is left. Preparation
# still has to assemble a >50 KiB prompt and wait out a
# stream teardown after this returns. Handing the lookup every remaining second
# means a slow docs host turns a turn that would have answered unaided into a
# preparation-deadline error — trading the feature's soft failure for a hard one.
_DOCS_BUDGET_RESERVE_SECONDS = 3.0

# Namespace for the upstream cache-affinity key (see `_cache_session_key`). The
# key shares a flat keyspace with every other tenant of the upstream account, so
# it says what it is and which surface produced it: a bare digest would collide
# in meaning with any other use of the same field, and an unlabelled key is
# undebuggable from the far side. `main-` because only the main conversation
# calls carry one — the selector and utility tasks deliberately
# do not (they have no growing per-session prefix to keep warm, and keying them
# per session would only fragment a pool they share fleet-wide).
_CACHE_SESSION_PREFIX = "daintree-main-"


@dataclass
class _Timings:
    """Mutable wall-clock marks, filled in as a turn progresses.

    The public shape is ``contracts.TurnTimings``; this is the accumulator that
    reaches it. Kept mutable and separate because the phases are recorded in
    three different places — preparation, the upstream open, and the terminal
    event — and threading six optional arguments through those instead would put
    the measurement further from the thing it measures.

    Total is deliberately NOT stored: it is read from ``RequestBudget``, the one
    clock started before any work in ``complete()``. A second start time would be
    a second answer to "when did this turn begin", and the two would drift.
    """

    selection_ms: int | None = None
    docs_ms: int | None = None
    preparation_ms: int = 0
    upstream_open_ms: int | None = None
    first_output_ms: int | None = None
    #: When the first chain-of-thought fragment arrived. A MARK, not a flag,
    #: because the duration is then derived from two timestamps and does not
    #: depend on the order two branches happen to run in. The flag version of this
    #: was subtly wrong: `thinking_signaled` is reset per attempt so a bare-intent
    #: re-roll can re-announce the phase, and held deltas flush after that reset.
    reasoning_at_ms: int | None = None

    def to_wire(self, budget: RequestBudget | None) -> TurnTimings:
        """Render the wire shape, deriving what can only be known at the end."""
        total_ms = round(budget.elapsed() * 1000) if budget is not None else 0
        # Generation is the tail after the first visible token. Derived rather
        # than measured so it cannot disagree with the two numbers it sits
        # between, and clamped at zero because a turn that produced no visible
        # output has no generation phase to report rather than a negative one.
        generation_ms: int | None = None
        if self.first_output_ms is not None:
            generation_ms = max(0, total_ms - self.first_output_ms)
        # Chain-of-thought: from the first reasoning fragment to the first visible
        # token. Derived from two marks rather than measured with a flag, so it
        # cannot depend on which branch of the delta loop ran first. Absent when no
        # reasoning was streamed, which is the shipped posture for every
        # interactive call.
        thinking_ms: int | None = None
        if self.reasoning_at_ms is not None and self.first_output_ms is not None:
            thinking_ms = max(0, self.first_output_ms - self.reasoning_at_ms)
        return TurnTimings(
            selection_ms=self.selection_ms,
            docs_ms=self.docs_ms,
            preparation_ms=self.preparation_ms,
            upstream_open_ms=self.upstream_open_ms,
            thinking_ms=thinking_ms,
            first_output_ms=self.first_output_ms,
            generation_ms=generation_ms,
            total_ms=total_ms,
        )


@dataclass
class _PreparedTurn:
    """Everything selection/assembly produces before the provider is consumed."""

    provider_request: ProviderChatRequest
    meta: _RespondMeta
    active_ids: list[str]
    # The caller's own upstream credential when they supplied one, ``None``
    # otherwise — which is every request until account auth lands, and means the
    # provider spends the server's key (see auth.py). Already attached to
    # provider_request; carried separately so a caller that rebuilds a request
    # (the bare-intent re-roll) does not have to reach into it.
    api_key: SecretStr | None
    # The SHADOW tool projection for this turn. Carried so both response paths
    # can grade what the model actually called against what a filter would have
    # withheld — the one number that says whether filtering is safe to enable.
    # default_factory, not a shared instance: ToolProjection is frozen but holds
    # a mutable `reasons` dict, and one instance shared by every default-built
    # turn is a foot-gun waiting for the first caller that writes to it.
    projection: ToolProjection = field(default_factory=ToolProjection)
    # Wall-clock marks for the phases preparation owns. Continues to be filled in
    # by the response paths, which is why it is mutable and travels with the turn.
    timings: _Timings = field(default_factory=_Timings)


def _carries_output(chunk: dict[str, Any] | None) -> bool:
    """Whether a chunk is the thing a user is waiting for.

    Visible content or a tool call ends the "is it broken?" interval. Reasoning
    fragments deliberately do NOT: they are internal, the client does not render
    them as an answer, and counting them would let a stream that produces nothing
    a user can see satisfy the first-output deadline indefinitely.
    """
    if not chunk:
        return False
    choices = chunk.get("choices") or []
    if not choices or not isinstance(choices[0], dict):
        return False
    delta = choices[0].get("delta") or {}
    if not isinstance(delta, dict):
        return False
    return bool(delta.get("content")) or bool(delta.get("tool_calls"))


def _full_tool_names(request: RespondRequest) -> list[str]:
    """Every tool name the request carries, UNCAPPED.

    The complete inventory is what `Catalog.executable_with` has to see: it asks
    "does the caller hold every tool this skill declares", and a truncated view
    answers "no" for a skill whose last required tool happened to be listed late.
    Tool ORDER would then decide the menu, which is neither intended nor stable.
    """
    return [tool.function.name for tool in (request.input.tools or [])]


class ChatOrchestrator:
    def __init__(
        self,
        *,
        settings: Settings,
        catalog_manager: CatalogManager,
        selector: SkillSelector,
        state_codec: StateCodec,
        assembler: PromptAssembler,
        provider: ChatProvider,
        # The public documentation MCP. Optional so a turn is constructible
        # without one — a container built for a test or a benchmark simply never
        # looks anything up, which is the same outcome as a docs server that is
        # down, and is already a path this code has to handle.
        docs_client: DocsMcpClient | None = None,
    ) -> None:
        self.settings = settings
        self.catalog_manager = catalog_manager
        self.selector = selector
        self.state_codec = state_codec
        self.assembler = assembler
        self.provider = provider
        self.docs_client = docs_client
        # In-flight detached tasks — today, the streaming path's preparation task.
        # asyncio holds only a weak reference to a bare task, so we keep a strong
        # one here (discarded on done) to stop one being garbage-collected
        # mid-flight, and drain them on shutdown before the provider closes.
        self._background_tasks: set[asyncio.Task[Any]] = set()

    @property
    def catalog(self) -> Catalog:
        # Read live: CatalogManager supports reload, so never cache the object.
        return self.catalog_manager.catalog

    # ---- cadence --------------------------------------------------------

    def _should_run_selector(
        self,
        *,
        force: bool,
        policy: str,
        state_valid: bool,
        state_catalog_revision: str | None,
        state_turn_id: str | None,
        state_instruction_revision: int | None,
        turn_id: str,
        instruction_revision: int,
        state_profile: str,
        profile: str,
    ) -> bool:
        if force or policy == "always":
            return True
        if not state_valid:
            return True
        # A set resolved under the other profile is not reusable: the profiles
        # choose from different menus, so the prior set can be illegal here rather
        # than merely stale (see StatePayload.profile). Reselect instead.
        if state_profile != profile:
            return True
        if state_catalog_revision != self.catalog.revision:
            return True
        if turn_id != state_turn_id:
            return True
        return instruction_revision != state_instruction_revision

    @staticmethod
    def _grade_projection(
        projection: ToolProjection,
        called: list[str],
        *,
        request_id: str,
        names_complete: bool = True,
    ) -> None:
        """Log whether the shadow projection would have broken this turn.

        `outside_proposed` is THE gate on enabling filtering: it names the calls
        the projection would have withheld. Filtering is safe when that is
        durably empty across real usage, and the admission reasons say which rule
        should have covered a miss — almost always a skill whose `requiredTools`
        is incomplete, which is a metadata fix rather than a design flaw.

        `names_complete=False` means some call this round could not be read (the
        tool-call buffer fell back to passthrough and never consolidated it).
        Such a round is logged as INDETERMINATE rather than as a clean zero: an
        unmeasured call that reads as "nothing outside the projection" is a false
        all-clear, and this number's only job is to authorize withholding tools.

        Never raises. Shadow instrumentation that could fail a turn would cost
        more than the measurement is worth.
        """
        try:
            names = [name for name in called if name]
            if not names and names_complete:
                return
            logger.info(
                "tool_projection_shadow",
                request_id=request_id,
                called=len(names),
                outside_proposed=projection.outside(names),
                proposed=len(projection.proposed),
                offered=len(projection.offered),
                # Explicit on every record, so a query for "rounds with nothing
                # outside the projection" cannot silently include rounds where
                # that was never established.
                measured=names_complete,
            )
        except Exception:  # noqa: BLE001 — measurement must never break a turn
            logger.warning("tool_projection_shadow_failed", request_id=request_id)

    @staticmethod
    def _forced_tool_name(tool_choice: object) -> str | None:
        """The tool a client pinned, if it pinned one by name.

        Only the named form matters here: "required" forces *a* call without
        naming one, so there is nothing specific to keep.
        """
        return getattr(tool_choice, "name", None)

    @staticmethod
    def _selector_turn_context(request: RespondRequest) -> str | None:
        """A short, bounded turn-context hint for the selector (evidence only).

        For background wake-ups and terse queue events the conversation alone may
        not carry enough task signal, so surface the goal and wake flag — plus the
        top client workflow digest (client-ranked, head of list) so a resumed
        workflow selects the runbook that was governing it.
        """
        turn = request.turn
        if turn is None:
            return None
        parts: list[str] = []
        goal = sanitize_line(turn.goal, 512)
        if goal:
            parts.append(f"current goal: {goal}")
        if turn.is_wake:
            parts.append("triggered by a background wake (no new user message)")
        if turn.workflow_state:
            digest = turn.workflow_state[0]
            bits: list[str] = []
            if wf_goal := sanitize_line(digest.goal, 256):
                bits.append(f"goal: {wf_goal}")
            if wf_status := sanitize_line(digest.status, 64):
                bits.append(f"status: {wf_status}")
            if wf_next := sanitize_line(digest.next_action, 256):
                bits.append(f"next: {wf_next}")
            if bits:
                parts.append(f"top active workflow — {'; '.join(bits)}")
        return " | ".join(parts) or None

    @staticmethod
    def _available_tool_names(request: RespondRequest, cap: int = 100) -> list[str]:
        """The caller's tool names, BOUNDED — selector prompt evidence only.

        The cap exists to keep the selector's user message small. Do NOT use this
        for anything that must be COMPLETE: a request may legally carry up to
        `max_tools` (128) entries, so a skill whose last required tool sits at
        position 101 would look unsatisfiable. Use `_full_tool_names` for filtering.
        """
        return _full_tool_names(request)[:cap]

    # ---- main entry -----------------------------------------------------

    async def complete(
        self,
        request: RespondRequest,
        *,
        upstream_model: str,
        api_key: SecretStr | None,
        request_id: str | None = None,
    ) -> RespondOutcome:
        request_id = request_id or new_request_id()
        # ONE clock for the whole turn, started before any work. Every phase
        # deadline and every retry attempt reads from it, so they share a budget
        # instead of each getting a fresh one — the difference between a bounded
        # turn and one that merely looks bounded.
        budget = RequestBudget()
        headers = {
            "X-Daintree-Protocol": str(PROTOCOL_VERSION),
            "X-Request-Id": request_id,
        }

        if request.generation.stream:
            # Return the response scaffold immediately: HTTP headers (and SSE
            # comment heartbeats) reach the client while selection, assembly, and
            # the provider open still run. Prep starts NOW as a concurrent task —
            # not lazily on first poll — so selection and assembly overlap the
            # response handshake. A prep failure becomes a terminal SSE ``error``
            # event on the stream — never an HTTP error, never a silent hang.
            prep_task = asyncio.create_task(
                self._prepare(
                    request,
                    upstream_model=upstream_model,
                    request_id=request_id,
                    api_key=api_key,
                    budget=budget,
                )
            )
            # If the stream is never consumed (client vanished before the body was
            # iterated), retrieve the exception so it can't surface as an orphaned
            # "exception was never retrieved" warning. The generator still sees it
            # via ``result()``.
            prep_task.add_done_callback(
                lambda t: None if t.cancelled() else t.exception()
            )
            # Shutdown backstop: drain_background_tasks waits for (then cancels)
            # any prep still in flight before the provider it uses is closed.
            self._background_tasks.add(prep_task)
            prep_task.add_done_callback(self._background_tasks.discard)
            return RespondOutcome(
                headers=headers,
                streaming=True,
                stream=_PrepOwnedStream(
                    self._stream_with_heartbeat(
                        request,
                        prep_task=prep_task,
                        request_id=request_id,
                        budget=budget,
                    ),
                    prep_task,
                ),
            )

        try:
            return await self._complete_nonstream(
                request,
                upstream_model=upstream_model,
                request_id=request_id,
                api_key=api_key,
                budget=budget,
                headers=headers,
            )
        except DeadlineExceeded as exc:
            # Nothing has been sent yet on this path, so a clean 504 is available
            # — strictly better than the streaming path's in-band error.
            logger.warning(
                "respond_deadline_exceeded",
                request_id=request_id,
                phase=exc.phase.value,
                elapsed_ms=round(exc.elapsed * 1000),
                streaming=False,
            )
            raise turn_deadline_exceeded(exc.phase.value) from exc

    async def _complete_nonstream(
        self,
        request: RespondRequest,
        *,
        upstream_model: str,
        request_id: str,
        api_key: SecretStr | None,
        budget: RequestBudget,
        headers: dict[str, str],
    ) -> RespondOutcome:
        """The non-streaming turn, under the total wall-clock budget.

        One `asyncio.timeout` around the whole thing rather than per-step limits:
        the caller is blocked on a single response, so the only number that
        matters to them is when it arrives — and the per-attempt bounds inside
        (prep deadline, shared retry budget) already stop any one step running
        away.
        """
        try:
            async with asyncio.timeout(budget.remaining_total()):
                return await self._complete_nonstream_inner(
                    request,
                    upstream_model=upstream_model,
                    request_id=request_id,
                    api_key=api_key,
                    budget=budget,
                    headers=headers,
                )
        except TimeoutError as exc:
            raise DeadlineExceeded(TurnPhase.TOTAL, budget.elapsed()) from exc

    async def _complete_nonstream_inner(
        self,
        request: RespondRequest,
        *,
        upstream_model: str,
        request_id: str,
        api_key: SecretStr | None,
        budget: RequestBudget,
        headers: dict[str, str],
    ) -> RespondOutcome:
        prepared = await self._prepare(
            request,
            upstream_model=upstream_model,
            request_id=request_id,
            api_key=api_key,
            budget=budget,
        )
        result = await self._complete_with_retry(prepared.provider_request, budget=budget)
        # Snapshot BEFORE the re-roll: it returns the repaired result in `result`'s
        # place, and only the usage is carried across (`sum_usage` inside). The
        # first attempt's host would otherwise be unrecoverable. Against a
        # single-host provider this is always None; the snapshot stays because
        # the accounting below reads it either way.
        first_endpoint = result.endpoint
        # A round that only announced a tool call it never emitted would end the turn
        # having performed nothing. Re-roll it once here, before anything is built or
        # sent, so neither the client nor the transcript ever sees the dead draft.
        result = await self._repair_bare_intent(
            prepared.provider_request, result, request_id=request_id
        )
        # finish_reason is a second, independent signal: treat a continuation round as
        # such even if a malformed upstream drops the tool_calls payload.
        had_tool_calls = bool(result.tool_calls) or result.finish_reason == "tool_calls"
        self._grade_projection(
            prepared.projection,
            [c.get("function", {}).get("name", "") for c in result.tool_calls],
            request_id=request_id,
        )
        lint_warnings = self._lint_final_output(
            result, request=request, request_id=request_id, had_tool_calls=had_tool_calls
        )
        if lint_warnings:
            # Appended to the turn's existing warnings rather than replacing
            # them: a reply can be both over-wide AND have had an unknown skill
            # id ignored, and dropping either loses a distinct diagnosis.
            prepared.meta.warnings.extend(lint_warnings)
        turn_cost = self._turn_cost_for(result.usage, prepared)
        log_turn_usage(
            result.usage,
            request_id=request_id,
            session_id=request.session.id,
            turn_id=request.session.turn_id,
            session_round=request.session.round,
            stream=False,
            finish_reason=result.finish_reason,
            endpoint=result.endpoint,
            # Both attempts, deduped by the sink. Without a re-roll these are the
            # same value and the count is 1; with one they may differ, which is
            # the whole signal.
            observed_endpoints=(first_endpoint, result.endpoint),
            selector_ran=prepared.meta.skills.selector.ran,
            selector_usage=prepared.meta.selector_usage,
            selector_endpoint=prepared.meta.selector_endpoint,
            turn_cost=turn_cost,
        )
        # A non-streamed call cannot separate opening the model from generating
        # with it — one await covers both — so the whole upstream wait is reported
        # as time-to-output and `upstream_open_ms` stays absent rather than
        # guessing at a split that was never observable.
        prepared.timings.first_output_ms = round(budget.elapsed() * 1000)
        body = self._build_nonstream_body(
            result,
            prepared.meta,
            param_index=build_parameter_index(request.input.tools),
            cost=turn_cost,
            timings=prepared.timings.to_wire(budget),
        )
        return RespondOutcome(headers=headers, streaming=False, body=body)

    async def _prepare(
        self,
        request: RespondRequest,
        *,
        upstream_model: str,
        request_id: str,
        api_key: SecretStr | None,
        budget: RequestBudget | None = None,
    ) -> _PreparedTurn:
        """Validate, decode state, select skills and assemble — under a deadline.

        Everything here happens BEFORE the user has seen anything, so a stall is
        pure dead air. Bounding it separately from the rest of the turn also
        makes the failure attributable: "timed out in preparation" says the model
        was never reached, which is a completely different investigation from a
        slow generation.
        """
        if budget is None:
            return await self._prepare_inner(
                request,
                upstream_model=upstream_model,
                request_id=request_id,
                api_key=api_key,
                budget=None,
            )
        try:
            async with asyncio.timeout(budget.remaining(TurnPhase.PREPARATION)):
                return await self._prepare_inner(
                    request,
                    upstream_model=upstream_model,
                    request_id=request_id,
                    api_key=api_key,
                    budget=budget,
                )
        except TimeoutError as exc:
            phase = (
                TurnPhase.TOTAL if budget.expired() else TurnPhase.PREPARATION
            )
            logger.warning(
                "respond_deadline_exceeded",
                request_id=request_id,
                phase=phase.value,
                elapsed_ms=round(budget.elapsed() * 1000),
            )
            raise turn_deadline_exceeded(phase.value) from exc

    def _cache_session_key(self, request: RespondRequest) -> str | None:
        """The upstream cache-affinity key for this turn's MAIN calls.

        Derived, never carried: it comes from ``request.session.id``, which is
        present on every call, rather than from the signed state token. Affinity
        that only worked while a token was valid would drop out on exactly the
        turns that most need it — a first turn, an expired token, a client that
        lost its state — and the key has no reason to be stateful.

        Hashed rather than forwarded. The value goes to a third party, and a
        client session id is an opaque local string we make no promises about;
        the salted digest is stable across instances (they share the salt) while
        being meaningless off this deployment.

        ``json`` gets its own namespace because a JSON-mode main call is a
        different animal: validation rejects ``response_format`` alongside tools,
        so it carries none of the ~21.5k tokens of schemas, and
        ``require_parameters`` then filters it to the smaller pool of endpoints
        that implement ``response_format``. Different prefix, different pool:
        sharing one key across the two would let each call type evict the other's
        sticky endpoint for a prefix it does not have.

        Returns ``None`` for a caller with no usable session, which leaves the
        router on its own default rather than inventing a shared cohort.
        """
        digest = self.state_codec.session_hash(request.session.id)
        if not digest:
            return None
        if request.generation.response_format == "json_object":
            return f"{_CACHE_SESSION_PREFIX}json-{digest}"
        return f"{_CACHE_SESSION_PREFIX}{digest}"

    async def _prepare_inner(
        self,
        request: RespondRequest,
        *,
        upstream_model: str,
        request_id: str,
        api_key: SecretStr | None,
        # Only the docs lookup reads it: everything else in preparation is bounded
        # by the enclosing `asyncio.timeout`, but an outbound call to a third
        # server needs a slice of its own or a slow docs host silently spends the
        # whole preparation budget the selector also has to fit inside.
        budget: RequestBudget | None = None,
    ) -> _PreparedTurn:
        session_id = request.session.id
        cache_session_id = self._cache_session_key(request)
        t_start = time.perf_counter()
        # Phase marks for this turn. Returned on the prepared turn so the
        # response paths can finish filling it in (see `_Timings`).
        timings = _Timings()

        warnings: list[str] = []
        catalog_at_start = self.catalog
        verification = self.state_codec.verify(request.state, session_id=session_id)
        if request.state and not verification.valid and verification.reason:
            warnings.append(verification.reason)

        # Only a fully valid token carries prior active skills forward; an
        # invalid/expired/wrong-session token is ignored (selection reruns).
        prior_active_ids: list[str] = []
        # The documentation query this TURN already ran, if the turn is still
        # going. Scoped to the turn id AND the instruction revision — the same
        # pair that decides selector cadence, and for the same reason: a
        # mid-turn interjection ("actually, run the tests") is a NEW instruction
        # that gets classified afresh, so a query carried across it would search
        # the documentation for a question the user has moved on from. Same
        # fail-closed posture as the active set — an untrusted token carries
        # nothing forward.
        prior_docs_query: str | None = None
        if verification.valid and verification.payload is not None:
            prior_active_ids = catalog_at_start.filter_known(
                verification.payload.active_ids()
            )
            payload = verification.payload
            if (
                payload.last_turn_id == request.session.turn_id
                and payload.last_instruction_revision == request.session.instruction_revision
            ):
                prior_docs_query = payload.docs_query

        # The deterministic no-task fast path. A greeting or a bare acknowledgement
        # needs no runbook, and concluding that with a model call costs a round
        # trip on the turn a tester judges the product's speed by.
        #
        # It CLEARS rather than reuses, which is what separates it from an
        # ordinary skipped-selector round: the turn has no task, so carrying a
        # workflow skill into it would keep a finished workflow loaded. That is
        # also exactly what the selector is instructed to return here.
        #
        # `selection.force` and `policy == "always"` still win — a client asking
        # for selection gets selection.
        # A sub-agent selects skills too — that is the point of giving it a real
        # tool inventory — but it chooses from a NARROWED menu.
        #
        # Most of this catalog is orchestration: spawning agents, driving
        # worktrees, supervising terminals. Handing one of those to a read-only
        # worker is worse than handing it nothing, because it spends context and
        # then instructs the model toward calls that can only fail. So the menu is
        # filtered to skills whose declared `requiredTools` the caller actually
        # holds (Catalog.executable_with), which also means a research-shaped skill
        # written tomorrow becomes available to sub-agents the day it lands, with
        # no second list to maintain.
        #
        # The docs lookup is switched off for the profile: a sub-agent researches
        # the user's PROJECT, and Daintree's own usage documentation answers a
        # question it was not sent to ask.
        subagent = request.profile == "subagent"
        # UNCAPPED for the menu filter (see _full_tool_names); the selector's own
        # evidence list stays bounded via _available_tool_names below.
        available_tools = _full_tool_names(request)
        selection_catalog = catalog_at_start
        if subagent:
            selection_catalog = catalog_at_start.executable_with(available_tools)
            # `selectable`, not `filter_known`: a server-pinned skill is hidden from
            # selection but still "known", and the docs runbook declares no required
            # tools so it survives every narrowing. Without this, a caller replaying
            # an assistant turn's signed state under profile="subagent" would carry
            # the docs runbook into a profile whose docs lookup is switched off —
            # injecting a runbook about reading documentation that was never fetched.
            prior_active_ids = selection_catalog.selectable(prior_active_ids)
            prior_docs_query = None
            logger.info(
                "subagent_selection_menu",
                request_id=request_id,
                offered_skills=len(selection_catalog),
                catalog_skills=len(catalog_at_start),
            )

        no_task = not subagent and can_skip_selection(
            request, prior_active_ids=prior_active_ids
        )
        if no_task:
            logger.info(
                "selector_fast_path",
                request_id=request_id,
                reason="no_task",
            )

        # With an empty menu there is nothing to decide, so skip the call rather
        # than pay for a classification whose only possible answer is "none".
        # Counted over SELECTABLE ids, not len(): the server-pinned docs runbook
        # declares no required tools, so it survives every narrowing and would make
        # len() >= 1 forever — this branch would then never fire and every sub-agent
        # would pay for a selector call against a menu it cannot choose from.
        # `force`/`policy=="always"` still win, as everywhere else in this cadence:
        # a client that explicitly asked for selection gets a selector run and an
        # honest `ran=true`, even though an empty menu can only answer "none".
        if (
            subagent
            and not selection_catalog.selectable_ids
            and not request.selection.force
            and request.selection.policy != "always"
        ):
            no_task = True
        run_selector = not no_task and self._should_run_selector(
            force=request.selection.force,
            policy=request.selection.policy,
            state_valid=verification.valid,
            state_catalog_revision=(
                verification.payload.catalog_revision if verification.payload else None
            ),
            state_turn_id=verification.payload.last_turn_id if verification.payload else None,
            state_instruction_revision=(
                verification.payload.last_instruction_revision if verification.payload else None
            ),
            turn_id=request.session.turn_id,
            instruction_revision=request.session.instruction_revision,
            state_profile=(
                verification.payload.profile if verification.payload else "assistant"
            ),
            profile=request.profile,
        )

        selector_ms = 0
        if run_selector:
            t_sel = time.perf_counter()
            decision = await self.selector.select(
                request.input.messages,
                prior_active_ids,
                api_key=api_key,
                turn_context=self._selector_turn_context(request),
                # Bounded: this rides the selector's user message. The uncapped
                # list is used for the menu filter above, not here.
                available_tools=self._available_tool_names(request),
                catalog=selection_catalog,
            )
            selector_ms = round((time.perf_counter() - t_sel) * 1000)
            timings.selection_ms = selector_ms
        elif no_task:
            # CLEAR, not reuse. A greeting or acknowledgement has no task, so a
            # workflow skill left loaded would follow a finished workflow into an
            # unrelated conversation — and `resolve_active_skills` treats an
            # authoritative empty result as "clear", which is exactly what the
            # selector itself is told to return for these turns.
            decision = SelectionDecision.cleared()
        else:
            decision = SelectionDecision.reuse(prior_active_ids)
        warnings.extend(decision.warnings)

        # A successful selector run is authoritative (topic-shift replacement); a
        # no-run continuation round or a degraded selector reuses the prior set.
        # Pin one post-selection catalog snapshot for active resolution, prompt,
        # state, and metadata. If the catalog were reloaded during selection,
        # the prompt renders against the same skills the state token records.
        resolved_catalog = self.catalog
        # Resolution and body lookup follow the same NARROWING the decision was made
        # under, so a sub-agent can never end up holding a skill that was not on its
        # menu.
        #
        # Derived from `resolved_catalog` (the post-selection snapshot), NOT from
        # `selection_catalog` (the pre-selection one). CatalogManager can swap the
        # live catalog while the selector is awaiting, and the assistant path already
        # resolves bodies from the post-await snapshot so that bodies, response
        # metadata and the state token all describe ONE generation. Deriving the
        # sub-agent's from the pre-await snapshot would have handed it generation A's
        # runbook while stamping generation B's revision — the next round would then
        # see a matching revision and never reselect. The narrowing is a pure function
        # of (catalog, tools), so re-deriving it here is cheap and cached.
        #
        # The state token records the FULL catalog's revision (below): the narrowed
        # catalog is derived deterministically from it, and stamping the derived
        # revision would make every round's check mismatch and re-run selection for
        # the whole run.
        #
        # That revision therefore identifies the CATALOG, not the narrowed MENU — so
        # in principle a caller could change its tool inventory mid-session, shrink
        # its own menu, and have selection skipped because the catalog revision still
        # matches. Deliberately not defended against: a sub-agent projects its
        # inventory ONCE before its loop starts (internal/subagent Runner.Run) and
        # every run gets a fresh session id with no prior state, so the inventory is
        # constant for the lifetime of the only session this profile ever has. If a
        # future caller reuses one sub-agent session across differing inventories,
        # the fix is to fold the profile and a normalized menu digest into the
        # selection identity — not to stamp the derived revision.
        skills_catalog = (
            resolved_catalog.executable_with(available_tools) if subagent else resolved_catalog
        )

        # ORDER MATTERS: the docs query is settled BEFORE the active set, not
        # after. Both are answers from the same selector call, but the active
        # set has to know whether this turn is a documentation question so it
        # can pin the runbook for one — and the query is available on the
        # decision immediately, while the LOOKUP (below) is the slow part.
        # Resolving before settling `docs_query` would leave the pin depending
        # on a value computed later in the same function.
        #
        # The carry applies ONLY when this turn did not answer for itself.
        # Same authority test the active set uses: a successful run and the
        # no-task fast path each speak for the turn, and their answer stands
        # even when it is `null` — a re-run selector that says "not a
        # documentation question" must not be overridden by what the previous
        # round decided. A continuation or a degraded selector did not answer,
        # so the turn's own finding holds.
        authoritative = (decision.ran or decision.authoritative) and not decision.degraded
        docs_query = decision.docs_query if authoritative else prior_docs_query
        if subagent:
            # See the profile note above: a sub-agent researches the project, not
            # Daintree's own usage documentation.
            docs_query = None

        resolution = resolve_active_skills(
            decision,
            prior_active_ids,
            skills_catalog,
            # A tighter cap for a sub-agent. It works to a hard round budget in a
            # context it will exhaust on tool results, so two 5k-token runbooks
            # would cost it more than they buy — and a narrowed menu rarely has a
            # third worth loading anyway.
            # NOTE: this caps RESOLUTION only; the selectors still render and parse
            # against the global cap. The outcome is correct either way (resolution
            # drops the excess), and the cost is at most one over-broad
            # classification against a menu that is currently 2 skills wide. If the
            # sub-agent menu ever grows enough for that to matter, thread a
            # max_active override alongside the catalog override into both selectors.
            min(self.settings.max_active_skills, 2)
            if subagent
            else self.settings.max_active_skills,
            # Pinned on the CLASSIFICATION, not on the lookup succeeding. The
            # runbook's job includes what to do when no pages arrive — say so
            # and answer unverified rather than fabricate — and that is exactly
            # the turn where a fabricated menu path is most likely.
            pinned_id=DOCS_SKILL_ID if docs_query else None,
        )
        active_ids = resolution.active
        newly_loaded_ids = resolution.newly_loaded

        skills = skills_catalog.get_many(active_ids)
        executions: list[SkillLoadExecution | DocsSearchExecution] = [
            SkillLoadExecution.from_skill(
                new_call_id(), skills_catalog.get(sid)  # type: ignore[arg-type]
            )
            for sid in newly_loaded_ids
        ]

        # The selector's second answer: this turn asked how Daintree works, so
        # the documentation is fetched HERE — before the model opens — and
        # handed to it as context. The alternative is a mid-stream tool loop
        # (intercept the model's docs call, run it, splice a second provider
        # stream into the same client connection), which both response paths
        # are built around not doing.
        #
        # `prior_docs_query` (settled above) is what keeps the pages alive
        # across a tool round: a continuation skips the selector, so
        # `decision.docs_query` is None there, and the volatile tail carrying
        # the pages is rebuilt every round. Without the carry, a usage question
        # that takes one tool call to answer arrives at the answer with its
        # sources gone.
        t_docs = time.perf_counter()
        docs = await self._lookup_docs(
            docs_query, request_id=request_id, budget=budget
        )
        # Recorded even when the lookup returned nothing. A miss still spent
        # the user's wall clock, and a slow FAILING docs server is exactly the
        # thing this is meant to expose.
        timings.docs_ms = round((time.perf_counter() - t_docs) * 1000)
        if docs is not None:
            executions.append(
                DocsSearchExecution(
                    call_id=new_call_id(),
                    query=docs.query,
                    paths=[s.path for s in docs.sections],
                )
            )

        new_state = self.state_codec.issue(
            session_id=session_id,
            active_skills=[s.id for s in skills],
            catalog_revision=resolved_catalog.revision,
            turn_id=request.session.turn_id,
            instruction_revision=request.session.instruction_revision,
            profile=request.profile,
            # Carried only when the lookup actually produced pages. Carrying
            # a query whose search failed would re-attempt it on every
            # continuation round of the turn, turning a docs host that is
            # down into a per-round latency tax — and the turn has already
            # decided to answer without documentation.
            docs_query=docs_query if docs is not None else None,
        )

        provider_request = replace(
            self.assembler.build_main_request(
                request=request,
                skills=skills,
                upstream_model=upstream_model,
                docs=docs,
            ),
            api_key=api_key,
            cache_session_id=cache_session_id,
        )

        meta = self._build_meta(
            request_id=request_id,
            state=new_state,
            active_ids=active_ids,
            newly_loaded_ids=newly_loaded_ids,
            executions=executions,
            decision=decision,
            run_selector=run_selector,
            warnings=warnings,
            catalog=resolved_catalog,
        )


        # Shadow measurement only — it must not be able to fail a turn. An empty
        # projection grades as "nothing was withheld", which is the honest
        # reading when the projection could not be computed.
        try:
            projection = project_tools(
                offered=request.input.tools,
                active_skills=skills,
                # The catalog the caller actually selected from. With the full one, a
                # foundation filtered OUT of a sub-agent's menu is rediscovered here
                # and its tools counted as proposed — understating `outside_proposed`,
                # which is the one metric this measurement exists to produce.
                catalog=skills_catalog,
                messages=request.input.messages,
                forced_tool=self._forced_tool_name(request.input.tool_choice),
            )
        except Exception:  # noqa: BLE001 — measurement must never break a turn
            logger.warning("tool_projection_failed", request_id=request_id)
            projection = ToolProjection()

        timings.preparation_ms = round((time.perf_counter() - t_start) * 1000)
        logger.info(
            "respond",
            request_id=request_id,
            stream=request.generation.stream,
            selector_ran=decision.ran,
            selector_degraded=decision.degraded,
            active_skills=active_ids,
            newly_loaded=newly_loaded_ids,
            # Ids the ``max_active_skills`` budget refused. Log-only on purpose: it
            # is the measurement we need (is the cap costing us skills, and which?)
            # at zero wire cost, without teaching the CLI a new warning code.
            dropped_over_cap=resolution.dropped_over_cap,
            warnings=warnings,
            selector_ms=selector_ms,
            # Which upstream endpoint served the classification, and whether the
            # parse-repair round-trip ran. `selector_ms` alone cannot distinguish a
            # slow endpoint from a silent second call, and the JSON-capable pool
            # spans a >10x latency tail — so a slow first byte is only actionable
            # with these two alongside it.
            selector_endpoint=decision.endpoint,
            selector_calls=decision.calls,
            # SHADOW tool projection: what a skill-aware filter WOULD have sent
            # this turn. The full inventory still goes upstream — this is the
            # measurement that decides whether filtering is safe to enable.
            # `tools_proposed` vs `tools_offered` is the size of the prize;
            # `tools_outside_proposed` (logged per-turn below, on the round that
            # actually calls tools) is the safety signal.
            tools_offered=len(projection.offered),
            tools_proposed=len(projection.proposed),
            pre_upstream_ms=timings.preparation_ms,
        )

        return _PreparedTurn(
            projection=projection,
            timings=timings,
            provider_request=provider_request,
            meta=meta,
            active_ids=active_ids,
            api_key=api_key,
        )

    # ---- server-side documentation lookup -------------------------------

    async def _lookup_docs(
        self,
        query: str | None,
        *,
        request_id: str,
        budget: RequestBudget | None,
    ) -> DocsLookup | None:
        """Search the public Daintree docs for this turn, or don't.

        Returns ``None`` for every outcome that is not "we have pages": no
        question was classified, no client is configured, too little budget is
        left, the server failed, or it simply matched nothing. The caller treats
        all of them identically, because they mean the same thing to the turn —
        the model answers without retrieved documentation, exactly as it did
        before this existed.

        **Nothing here may raise.** A docs lookup is an enhancement to a turn
        that would otherwise have proceeded fine; letting it fail the request
        would make "how do I use Daintree?" the most fragile question a user can
        ask, which is precisely backwards.
        """
        if not query or self.docs_client is None:
            return None
        # Its own slice, clamped by what preparation has left MINUS a reserve for
        # the work that still follows it. Starting a call that cannot finish
        # inside that spends latency to produce a timeout, and — worse — a lookup
        # allowed to run the phase to zero converts this feature's soft failure
        # into a hard one: the turn errors instead of answering unaided.
        timeout = DOCS_LOOKUP_TIMEOUT_SECONDS
        if budget is not None:
            timeout = min(
                timeout,
                budget.remaining(TurnPhase.PREPARATION) - _DOCS_BUDGET_RESERVE_SECONDS,
            )
        if timeout < _DOCS_MIN_TIMEOUT_SECONDS:
            logger.info("docs_lookup_skipped", request_id=request_id, reason="no_budget")
            return None
        started = time.perf_counter()
        try:
            # The httpx timeout bounds each I/O operation, not the call: a server
            # that trickles a byte at a time resets it forever. This makes the
            # slice a wall-clock bound, which is the only kind the phase budget
            # can actually be protected by.
            async with asyncio.timeout(timeout):
                sections = await self.docs_client.search(query, timeout=timeout)
        except TimeoutError:
            logger.info(
                "docs_lookup_timeout",
                request_id=request_id,
                timeout_ms=round(timeout * 1000),
            )
            return None
        except DocsLookupError as exc:
            # WARNING for a rejection, INFO for weather. A 401/403/404 will fail
            # the same way on every turn until someone acts, so it is an operator
            # signal; a timeout or a bad payload is one turn's bad luck. Both
            # still fail open — the volume changes, the posture does not.
            log = logger.warning if exc.is_permanent else logger.info
            log(
                "docs_lookup_failed",
                request_id=request_id,
                reason=str(exc),
                status=exc.status,
                permanent=exc.is_permanent,
                elapsed_ms=round((time.perf_counter() - started) * 1000),
            )
            return None
        except Exception:
            # The docs server is a third party on the first-byte path. A shape we
            # never anticipated must cost the turn its documentation, not the turn.
            logger.exception("docs_lookup_unexpected", request_id=request_id)
            return None
        elapsed_ms = round((time.perf_counter() - started) * 1000)
        if not sections:
            logger.info("docs_lookup_empty", request_id=request_id, elapsed_ms=elapsed_ms)
            return None
        logger.info(
            "docs_lookup",
            request_id=request_id,
            sections=len(sections),
            paths=[s.path for s in sections],
            elapsed_ms=elapsed_ms,
        )
        return DocsLookup(query=query, sections=sections)

    # ---- output linting (non-stream backstop; observability) ------------

    # Output-contract findings, as stable codes the CLI can branch on. Prefixed so
    # they are distinguishable at a glance from the request-level warnings
    # (``unknown_skill_id_ignored``) that share the same list on the non-streaming
    # path.
    _LINT_WARNING_CODES = {
        "think_leak": "output_think_leak",
        "oversized": "output_oversized",
        "pipe_table": "output_wide_table",
    }

    @staticmethod
    def _turn_cost_for(usage: ProviderUsage, prepared: _PreparedTurn) -> TurnCost | None:
        """The turn's total cost — one home for the arithmetic (turn_accounting)."""
        return turn_cost(usage, prepared.meta)

    def _lint_final_output(
        self, result: Any, *, request: RespondRequest, request_id: str, had_tool_calls: bool
    ) -> list[str]:
        """Lint the final assistant message: warn the caller, plus optional table repair.

        Runs only when the turn ENDED (no tool calls) with visible content — a
        continuation round's content is interstitial. When
        ``main_output_repair_tables`` is on, an over-budget table is rewritten to a
        per-entity list in place. Never raises — a lint bug must not break a response
        the user is already owed.

        Returns the warning codes for the caller to surface. Previously these were
        logged and nothing more, which meant the one party who could act on a
        malformed reply — the client rendering it — was the only one not told.
        """
        if not self.settings.main_output_lint_enabled or had_tool_calls:
            return []
        try:
            message = result.message or {}
            text = message.get("content") or ""
            if not text.strip():
                return []
            # The SAME width the display context quoted to the model this turn.
            # Grading a 100-column reply against a hardcoded 56 is what rewrote
            # perfectly good tables into lists for anyone not in a narrow pane.
            terminal_width = resolve_content_width(
                request.runtime, fallback=self.settings.main_terminal_width
            )
            findings = lint_output(
                text,
                terminal_width=terminal_width,
                warn_chars=self.settings.main_output_warn_chars,
            )
            if not findings.has_violation:
                return []
            violating = findings.violating_tables
            repaired = False
            if self.settings.main_output_repair_tables and violating:
                new_text = repair_tables(text, findings)
                if new_text != text:
                    message["content"] = new_text
                    repaired = True
            logger.info(
                "main_output_lint",
                request_id=request_id,
                stream=False,
                terminal_width=terminal_width,
                violations=findings.violation_types(),
                content_chars=findings.char_count,
                table_count=len(findings.tables),
                violating_table_count=len(violating),
                max_table_cols=max((f.table.columns for f in violating), default=0),
                max_table_rows=max((f.table.rows for f in violating), default=0),
                max_table_width=max((f.table.width for f in violating), default=0),
                think_leak=findings.think_leak,
                oversized=findings.oversized,
                repair_attempted=self.settings.main_output_repair_tables and bool(violating),
                repair_succeeded=repaired,
            )
            # Report against what the client will ACTUALLY receive. Re-linting
            # rather than clearing the `pipe_table` finding matters because
            # `repair_tables` converts what it can and leaves the rest: a table
            # with no usable body rows is deliberately untouched. Treating "the
            # text changed" as "every table is fixed" would suppress the warning
            # for a table that still violates the contract.
            final = findings
            if repaired:
                final = lint_output(
                    message.get("content") or "",
                    terminal_width=terminal_width,
                    warn_chars=self.settings.main_output_warn_chars,
                )
            return [
                self._LINT_WARNING_CODES[t]
                for t in final.violation_types()
                if t in self._LINT_WARNING_CODES
            ]
        except Exception as exc:  # noqa: BLE001 - observability must never break the turn
            logger.warning("main_output_lint_error", request_id=request_id, error=str(exc))
            return []

    def _lint_streamed_output(
        self, text: str, *, request: RespondRequest, request_id: str, had_tool_calls: bool
    ) -> list[str]:
        """Lint text that has ALREADY been sent, for the terminal ``done`` event.

        The streaming twin of :meth:`_lint_final_output`, and deliberately weaker:
        nothing can be repaired here, because the bytes left before the defect was
        knowable. What it can still do is tell the client what it just rendered,
        which is enough for the CLI to flag the reply or drop a leaked
        ``<think>`` block from its scrollback.

        No mid-stream termination. The review that prompted this asked for one on
        "severe hidden-content leakage", but there is no third party to protect
        here: the hidden material is our own prompt and the user's own local
        context. Killing a stream the user is reading, to prevent them seeing
        their own data, trades a real cost for a cosmetic one.
        """
        if not self.settings.main_output_lint_enabled or had_tool_calls or not text.strip():
            return []
        try:
            terminal_width = resolve_content_width(
                request.runtime, fallback=self.settings.main_terminal_width
            )
            findings = lint_output(
                text,
                terminal_width=terminal_width,
                warn_chars=self.settings.main_output_warn_chars,
            )
            if not findings.has_violation:
                return []
            logger.info(
                "main_output_lint",
                request_id=request_id,
                stream=True,
                terminal_width=terminal_width,
                violations=findings.violation_types(),
                content_chars=findings.char_count,
                think_leak=findings.think_leak,
                oversized=findings.oversized,
                violating_table_count=len(findings.violating_tables),
                # Repair is structurally impossible once bytes are public. Stated
                # explicitly so the two paths' records stay comparable.
                repair_attempted=False,
                repair_succeeded=False,
            )
            return [
                self._LINT_WARNING_CODES[t]
                for t in findings.violation_types()
                if t in self._LINT_WARNING_CODES
            ]
        except Exception as exc:  # noqa: BLE001 - a lint bug must not break the stream
            logger.warning("main_output_lint_error", request_id=request_id, error=str(exc))
            return []

    async def drain_background_tasks(self, timeout: float = 30.0) -> None:
        """Give in-flight detached tasks a BOUNDED grace period on shutdown, then
        cancel — used before the provider they depend on is closed.

        The bound is what matters. A preparation task owns a live upstream call,
        so an unbounded await here would hang server shutdown after a Ctrl-C for
        as long as the model felt like taking. We wait up to ``timeout``, then
        cancel the stragglers and gather them so nothing surfaces as an orphaned
        "exception was never retrieved" warning.
        """
        tasks = list(self._background_tasks)
        if not tasks:
            return
        logger.info("background_drain_wait", pending=len(tasks), timeout=timeout)
        _, pending = await asyncio.wait(tasks, timeout=timeout)
        if pending:
            logger.warning("background_drain_timeout", abandoned=len(pending))
            for task in pending:
                task.cancel()
            await asyncio.gather(*pending, return_exceptions=True)

    # ---- manual resolve (section 12.2) ---------------------------------

    async def resolve(
        self,
        *,
        action: str,
        session_id: str | None,
        state_token: str | None,
        api_key: SecretStr | None,
        turn_id: str | None = None,
        instruction_revision: int = 0,
        query: str | None = None,
        skill_ids: list[str] | None = None,
        request_id: str | None = None,
    ) -> dict[str, Any]:
        request_id = request_id or new_request_id()
        verification = self.state_codec.verify(state_token, session_id=session_id)
        warnings: list[str] = []
        if state_token and not verification.valid and verification.reason:
            warnings.append(verification.reason)

        prior_active_ids: list[str] = []
        if verification.valid and verification.payload is not None:
            prior_active_ids = self.catalog.filter_known(verification.payload.active_ids())

        run_selector = False
        if action == "clear":
            decision = SelectionDecision(skill_ids=[], ran=False, degraded=False)
            active_ids: list[str] = []
            newly_loaded_ids: list[str] = []
        elif action == "load":
            requested = skill_ids or []
            valid = self.catalog.filter_known(requested)
            if len(valid) != len(requested):
                warnings.append("unknown_skill_id_ignored")
            decision = SelectionDecision(skill_ids=valid, ran=False, degraded=False)
            merged = merge_active_skills(
                valid, prior_active_ids, self.catalog, self.settings.max_active_skills
            )
            active_ids, newly_loaded_ids = merged.active, merged.newly_loaded
        elif action == "find":
            run_selector = True
            conversation = [ChatMessage(role="user", content=query or "")]
            decision = await self.selector.select(
                conversation, prior_active_ids, api_key=api_key
            )
            warnings.extend(decision.warnings)
            # A find re-selects: the selector result is authoritative, same as a turn.
            found = resolve_active_skills(
                decision, prior_active_ids, self.catalog, self.settings.max_active_skills
            )
            active_ids, newly_loaded_ids = found.active, found.newly_loaded
        else:
            raise DaintreeError(
                status_code=400,
                message=f"Unknown resolve action {action!r}.",
                code="invalid_action",
                param="action",
            )

        skills = self.catalog.get_many(active_ids)
        executions = [
            SkillLoadExecution.from_skill(new_call_id(), self.catalog.get(sid))  # type: ignore[arg-type]
            for sid in newly_loaded_ids
        ]
        new_state = self.state_codec.issue(
            session_id=session_id,
            active_skills=[s.id for s in skills],
            catalog_revision=self.catalog.revision,
            turn_id=turn_id,
            instruction_revision=instruction_revision,
        )
        meta = self._build_meta(
            request_id=request_id,
            state=new_state,
            active_ids=active_ids,
            newly_loaded_ids=newly_loaded_ids,
            executions=executions,
            decision=decision,
            run_selector=run_selector,
            warnings=warnings,
        )
        return {
            "request_id": meta.request_id,
            "state": meta.state,
            "catalog_revision": meta.catalog_revision,
            "prompt_version": meta.prompt_version,
            "skills": meta.skills.model_dump(exclude_none=True),
            "warnings": meta.warnings,
        }

    # ---- metadata -------------------------------------------------------

    def _build_meta(
        self,
        *,
        request_id: str,
        state: str,
        active_ids: list[str],
        newly_loaded_ids: list[str],
        # Both kinds of server-run "tool call" the CLI renders as a prelude: a
        # skill that loaded, and a documentation search that answered a usage
        # question. They share the channel because they are the same thing from
        # the client's side — work the server did before the model spoke.
        # Sequence, not list: a caller with only skill loads should not have to
        # widen its own list to hand it over.
        executions: Sequence[SkillLoadExecution | DocsSearchExecution],
        decision: SelectionDecision,
        run_selector: bool,
        warnings: list[str],
        catalog: Catalog | None = None,
    ) -> _RespondMeta:
        selected_catalog = catalog or self.catalog

        def refs(ids: list[str]) -> list[SkillRef]:
            out: list[SkillRef] = []
            for sid in ids:
                skill = selected_catalog.get(sid)
                if skill is not None:
                    out.append(SkillRef(**skill.ref()))
            return out

        selector_usage = None
        if decision.usage is not None:
            u = decision.usage
            selector_usage = SelectorUsage(
                prompt_tokens=u.prompt_tokens or 0,
                completion_tokens=u.completion_tokens or 0,
                total_tokens=u.total_tokens or 0,
                cached_tokens=u.cached_tokens or 0,
                cost=u.cost,
            )

        reason = decision.reason if self.settings.expose_debug_selector_reason else None

        skills = SkillsBlock(
            active=refs(active_ids),
            newly_loaded=refs(newly_loaded_ids),
            prelude=Prelude(tool_executions=[e.prelude_execution() for e in executions]),
            selector=SelectorMeta(
                ran=decision.ran or run_selector,
                degraded=decision.degraded,
                task_type=decision.task_type,
                confidence=decision.confidence,
                usage=selector_usage,
                reason=reason,
            ),
        )
        return _RespondMeta(
            request_id=request_id,
            state=state,
            catalog_revision=selected_catalog.revision,
            prompt_version=MAIN_PROMPT_VERSION,
            skills=skills,
            warnings=warnings,
            selector_cost_complete=(
                decision.usage.cost_complete if decision.usage is not None else True
            ),
            selector_endpoint=decision.endpoint,
            selector_usage=decision.usage,
        )

    # ---- response bodies ------------------------------------------------

    def _build_nonstream_body(
        self,
        result: Any,
        meta: _RespondMeta,
        *,
        param_index: ParameterIndex,
        cost: TurnCost | None = None,
        timings: TurnTimings | None = None,
    ) -> dict[str, Any]:
        message = result.message or {}
        response = RespondResponse(
            request_id=meta.request_id,
            model=PUBLIC_MODEL_ID,
            message=RespondMessage(
                role=message.get("role", "assistant"),
                content=message.get("content"),
                reasoning_content=message.get("reasoning_content"),
                tool_calls=self._normalize_tool_calls(
                    message.get("tool_calls"),
                    param_index=param_index,
                    request_id=meta.request_id,
                ),
            ),
            finish_reason=result.finish_reason,
            usage=response_usage(result.usage),
            cost=cost,
            timings=timings or TurnTimings(),
            skills=meta.skills,
            state=meta.state,
            catalog_revision=meta.catalog_revision,
            prompt_version=meta.prompt_version,
            warnings=meta.warnings,
        )
        return response.model_dump(exclude_none=True)

    def _normalize_tool_calls(
        self, raw: Any, *, param_index: ParameterIndex, request_id: str
    ) -> list[dict[str, Any]] | None:
        """Project upstream tool-call dicts onto the strict response shape,
        dropping provider-specific fields (e.g. DeepSeek's ``index``) and
        repairing near-miss arguments against the client-declared schemas."""
        if not raw:
            return None
        out: list[dict[str, Any]] = []
        for call in raw:
            fn = call.get("function") or {}
            name = fn.get("name", "")
            arguments, notes = repair_tool_call_arguments(
                name, fn.get("arguments", ""), param_index
            )
            if notes:
                self._log_tool_call_repairs([(name, notes)], request_id=request_id)
            out.append(
                {
                    "id": call.get("id", ""),
                    "type": call.get("type", "function"),
                    "function": {"name": name, "arguments": arguments},
                }
            )
        return out

    @staticmethod
    def _log_tool_call_repairs(
        repairs: list[tuple[str, list[str]]], *, request_id: str
    ) -> None:
        for tool, notes in repairs:
            logger.info(
                "tool_call_arguments_repaired",
                request_id=request_id,
                tool=tool,
                repairs=notes,
            )

    async def _stream_with_heartbeat(
        self,
        request: RespondRequest,
        *,
        prep_task: asyncio.Task[_PreparedTurn],
        request_id: str,
        budget: RequestBudget | None = None,
    ) -> AsyncGenerator[str, None]:
        """Wrap the event stream with SSE comment heartbeats.

        Whenever the inner stream is quiet for ``HEARTBEAT_INTERVAL_SECONDS`` —
        during selection, provider open/retries, or long thinking gaps — a
        ``: hb`` comment goes out so intermediaries and the CLI's idle watchdog
        see a live stream. Comments are invisible to SSE event parsing.
        """
        inner = self._stream_events(
            request, prep_task=prep_task, request_id=request_id, budget=budget
        )
        pending: asyncio.Task[str] | None = None
        try:
            while True:
                if pending is None:
                    pending = asyncio.ensure_future(anext(inner))
                done, _ = await asyncio.wait(
                    {pending}, timeout=HEARTBEAT_INTERVAL_SECONDS
                )
                if not done:
                    yield ": hb\n\n"
                    continue
                task, pending = pending, None
                try:
                    item = task.result()
                except StopAsyncIteration:
                    return
                yield item
        finally:
            # Consumer disconnect (GeneratorExit) or an inner failure lands here.
            # Cancel a still-pending pull — the CancelledError unwinds the inner
            # generator's own finally (closing the provider stream) — then close
            # the inner generator for the non-pending suspension case.
            if pending is not None:
                pending.cancel()
                await asyncio.gather(pending, return_exceptions=True)
            await inner.aclose()

    async def _stream_events(
        self,
        request: RespondRequest,
        *,
        prep_task: asyncio.Task[_PreparedTurn],
        request_id: str,
        budget: RequestBudget | None = None,
    ) -> AsyncGenerator[str, None]:
        """Await prep inside the stream, then delegate to the provider event body.

        Any failure after the HTTP response has started can only be reported
        in-band: a prep failure or an unexpected mid-stream exception becomes one
        terminal SSE ``error`` event — never an abrupt EOF, never a fabricated
        ``done``. GeneratorExit/CancelledError (client disconnect, shutdown) pass
        through untouched — except that a disconnect while prep is still running
        also cancels the prep task, so selection stops promptly.
        """
        try:
            prepared = await prep_task
        except asyncio.CancelledError:
            prep_task.cancel()
            raise
        except DaintreeError as exc:
            logger.warning(
                "respond_prepare_error",
                request_id=request_id,
                code=exc.code,
                status=exc.status_code,
            )
            yield self._sse(
                "error",
                StreamError(
                    error=exc.envelope().error,
                    request_id=request_id,
                    retry_after=exc.headers.get("Retry-After"),
                ),
            )
            return
        except Exception:
            logger.exception("respond_prepare_unexpected", request_id=request_id)
            yield self._sse(
                "error",
                StreamError(error=internal_error().envelope().error, request_id=request_id),
            )
            return

        body = self._stream_body(
            prepared.provider_request,
            meta=prepared.meta,
            request=request,
            budget=budget,
            projection=prepared.projection,
            timings=prepared.timings,
        )
        try:
            async for event in body:
                yield event
        except DeadlineExceeded as exc:
            # The turn outran its budget mid-generation. The HTTP 200 committed
            # long ago, so this can only be reported in-band — as a terminal
            # error naming the phase, never as a fabricated ``done`` that would
            # present a truncated answer as a complete one.
            logger.warning(
                "respond_deadline_exceeded",
                request_id=request_id,
                phase=exc.phase.value,
                elapsed_ms=round(exc.elapsed * 1000),
            )
            yield self._sse(
                "error",
                StreamError(
                    error=turn_deadline_exceeded(exc.phase.value).envelope().error,
                    request_id=request_id,
                ),
            )
        except Exception:
            # _stream_body already maps DaintreeError to a terminal event; this
            # catches what it could not anticipate (e.g. a malformed provider
            # chunk crashing delta handling) so the client still gets a typed end.
            logger.exception("respond_stream_unexpected", request_id=request_id)
            yield self._sse(
                "error",
                StreamError(error=internal_error().envelope().error, request_id=request_id),
            )
        finally:
            # ``async for`` does not close a generator it abandoned mid-iteration
            # (GeneratorExit lands HERE, not in the body's finally) — close it
            # explicitly so the provider stream never outlives the response.
            await body.aclose()

    async def _stream_body(
        self,
        provider_request: ProviderChatRequest,
        *,
        meta: _RespondMeta,
        request: RespondRequest,
        budget: RequestBudget | None = None,
        projection: ToolProjection,
        timings: _Timings,
    ) -> AsyncGenerator[str, None]:
        finish_reason: str | None = None
        thinking_signaled = False

        def _mark_first_output(delta: StreamDelta) -> None:
            """Stamp the first VISIBLE token, once per turn.

            Called at every point a delta actually reaches the client, rather
            than where chunks arrive from upstream: a held bare-intent delta has
            not reached the user yet, and stamping on arrival would report a
            first-output time the user never experienced.

            Reasoning fragments deliberately do not qualify. They are internal,
            the client does not render them as an answer, and counting them would
            let a model that thinks for two minutes report an instant first byte
            — the same rule `_carries_output` applies to the deadline.
            """
            if budget is None or timings.first_output_ms is not None:
                return
            if not (delta.content or delta.tool_calls):
                return
            timings.first_output_ms = round(budget.elapsed() * 1000)


        # Accumulate the VISIBLE reply. It is what the bare-intent detector reads
        # (a round that only announces an action is re-rolled rather than sent) and
        # what the runaway guard measures against.
        content_parts: list[str] = []
        # Running length of the VISIBLE reply, kept beside content_parts so the
        # runaway guard costs an int add per chunk instead of a join per chunk.
        content_chars = 0
        # Resolved once per turn, not per chunk. 0 disables the guard.
        runaway_chars = self.settings.main_stream_runaway_chars
        saw_tool_calls = False
        completed = False
        # Tool-call fragments are held per call (never forwarded piecemeal) so the
        # completed arguments can be schema-repaired before the client sees them.
        # All calls flush together at stream end: the client only dispatches tools
        # once the round is complete, so consolidation adds no real latency, and
        # holding everything tolerates providers that interleave call fragments.
        # Built once and reused: the re-roll rebuilds its buffer from the same index.
        param_index = build_parameter_index(request.input.tools)
        tool_call_buffer = StreamToolCallBuffer(param_index)
        stream: ProviderStream | None = None
        # THE COMMIT GATE (issue #25). A round that announces a tool call and emits
        # none must not reach the client as a finished turn — but on this path the
        # decision can only be made once the reply has ENDED, and content deltas are
        # normally forwarded the instant they arrive. So the first attempt's pure
        # content is HELD until the reply can no longer be that shape, then released
        # and forwarded live for the rest of the round.
        #
        # What this costs, stated plainly: HTTP/SSE and the `meta` event are
        # untouched, but the first visible TOKEN of a gated round waits for up to
        # BARE_INTENT_MAX_CHARS of generation. Anything that rules the shape out —
        # a tool-call fragment, reasoning, a question mark, passing the bound —
        # releases the gate immediately, so continuation rounds (the common case,
        # which open with a tool call) never wait at all. A round with nothing
        # callable is never gated, because it cannot have dropped an action.
        #
        # ``held`` is the gate: a list means armed and holding, ``None`` means this
        # attempt's content is already public and can never be discarded.
        tools_callable = self._tools_callable(provider_request)
        held: list[StreamDelta] | None = None
        # The re-roll gets exactly one shot and is never itself gated: whatever it
        # produces is what the user gets, so there is nothing to delay it for.
        attempt = 0
        active_request = provider_request
        # Starts as None, not ProviderUsage(): an empty record is what a real
        # attempt that reported NOTHING looks like, and `sum_usage` has to be
        # able to tell that gap from an accumulator with nothing in it yet.
        carried_usage: ProviderUsage | None = None
        # Endpoint attribution for the usage accumulating beside it: every host
        # that contributed tokens to `carried_usage`, and the one that served the
        # attempt finally accepted. Updated wherever `carried_usage` is, so the
        # two can never describe different sets of attempts.
        #
        # Bounded, not open-ended: this loop runs at most twice (the round, then
        # one bare-intent re-roll). A tool-continuation round is a NEW request
        # with its own `turn_usage` line, joined to this one by `turn_id`.
        main_endpoints: list[str | None] = []
        main_endpoint: str | None = None
        # Open attempts abandoned mid-prefetch. An upstream that took the request,
        # prefilled it and then went quiet may well have BILLED that prefill, and
        # we will never see the usage trailer that would have told us. So the count
        # is not a statistic — it clears `cost.complete`, which is exactly the case
        # turn_accounting documents as "a cost we know was incurred and cannot
        # observe at all". Reporting the surviving attempt's cost as the whole
        # turn's would understate a real bill on the caller's own key.
        abandoned_opens = 0
        # The discarded draft, kept only until the re-roll's stream actually opens.
        # Up to that point nothing of the retry is public, so the first attempt is
        # still replay-safe and remains the honest fail-open answer.
        fallback: tuple[list[StreamDelta], str | None, str] | None = None
        try:
            # The metadata event is always first, before any token: the client gets
            # the skill prelude, active set, refreshed state, and selector outcome
            # as soon as selection finishes — local selection is ~10ms, so this
            # arrives effectively immediately, well before the upstream opens.
            yield self._sse(
                "meta",
                StreamMeta(
                    request_id=meta.request_id,
                    model=PUBLIC_MODEL_ID,
                    skills=meta.skills,
                    state=meta.state,
                    catalog_revision=meta.catalog_revision,
                    prompt_version=meta.prompt_version,
                    warnings=meta.warnings,
                ).model_dump(exclude_none=True),
            )

            while True:
                t_open = time.perf_counter()
                if stream is None:
                    try:
                        (
                            opened_stream,
                            first_event,
                            abandoned,
                        ) = await self._open_stream_with_retry(
                            active_request, budget=budget, request_id=meta.request_id
                        )
                        abandoned_opens += abandoned
                    except DaintreeError as exc:
                        # A stalled open that exhausted its retries lands here too,
                        # which is the point: it is a `DaintreeError` now, so the
                        # re-roll's fallback below can still save the draft. As a
                        # `DeadlineExceeded` it flew straight past this handler and
                        # took a perfectly good held answer down with the turn.
                        if fallback is None:
                            raise
                        # The re-roll never opened, so nothing of it is public and
                        # the first attempt is still replay-safe. Fail open to the
                        # draft we already have rather than costing the user the
                        # turn over a weak-but-real reply.
                        self._log_bare_intent(
                            "failed_open",
                            request_id=meta.request_id,
                            stream=True,
                            code=exc.code,
                        )
                        held, finish_reason, restored = fallback
                        content_parts = [restored]
                        # Restored together, though this path breaks immediately
                        # and checks no further chunks: "the counter tracks
                        # content_parts" is the invariant that makes the guard
                        # readable, and a single place where it silently does not
                        # is how the next reader gets it wrong.
                        content_chars = len(restored)
                        break
                    stream = opened_stream
                # Phase timing for the round's remaining "dead air": upstream
                # open-to-first-event. It dominates on cache misses (prefill) and on
                # thinking-heavy rounds (reasoning precedes the event), but no longer
                # delays the already-emitted skill metadata. Measured from t_open —
                # a PHASE duration, matching the wire contract — not from request
                # start; preparation already owns everything before this point.
                if timings.upstream_open_ms is None:
                    timings.upstream_open_ms = round(
                        (time.perf_counter() - t_open) * 1000
                    )
                logger.info(
                    "respond_upstream_open",
                    request_id=meta.request_id,
                    # Which semantic attempt this open served: 0 is the round itself,
                    # 1 the bare-intent re-roll.
                    semantic_attempt=attempt,
                    upstream_first_event_ms=round((time.perf_counter() - t_open) * 1000),
                    # The endpoint serving the main generation. Read after the first
                    # event because that is the earliest chunk that names it; still
                    # `None` if the stream ended before yielding one.
                    upstream_endpoint=opened_stream.endpoint,
                )

                if attempt == 0 and tools_callable:
                    held = []

                async for chunk in self._chunk_iter(
                    opened_stream, first_event, budget=budget
                ):
                    finish_reason = self._chunk_finish_reason(chunk) or finish_reason
                    delta = self._chunk_to_delta(chunk)
                    if delta is None:
                        continue
                    if delta.content:
                        content_parts.append(delta.content)
                        content_chars += len(delta.content)
                        # RUNAWAY GUARD. A degenerating endpoint does not stop on
                        # its own: it streams to the token ceiling and reports
                        # finish_reason=length, which we only learn once the whole
                        # dump has been delivered and billed (observed twice on
                        # 2026-08-20, ~33.5k chars each). Cut it off instead.
                        #
                        # A ceiling on PROSE, unconditionally — deliberately NOT
                        # exempted for a round that is also calling tools. The
                        # exemption is the tempting shape ("a tool-calling round is
                        # converging") and it is a hole: only `delta.content` is
                        # counted, so a legitimate multi-spawn batch already costs
                        # nothing against this limit no matter how large its
                        # arguments are, while the exemption would let ONE junk
                        # tool-call fragment — precisely what a degenerating
                        # endpoint also emits — buy the rest of the token ceiling
                        # for unbounded prose. Nothing is protected by the gate
                        # that is not already protected by counting content alone.
                        #
                        # The raise unwinds into the DaintreeError handler below,
                        # which emits the terminal error event and closes the
                        # upstream in its `finally` — the existing mid-stream
                        # failure path, not a new one.
                        if runaway_chars and content_chars > runaway_chars:
                            logger.warning(
                                "stream_runaway_stopped",
                                request_id=meta.request_id,
                                content_chars=content_chars,
                                limit=runaway_chars,
                                upstream_endpoint=opened_stream.endpoint,
                                semantic_attempt=attempt,
                                saw_tool_calls=saw_tool_calls,
                            )
                            raise upstream_runaway_output()
                    # Release the gate on anything that rules the failure shape out.
                    # Reasoning and tool-call fragments are public model output the
                    # moment they arrive, so an attempt that produces either can no
                    # longer be discarded; the probe covers length and questions.
                    # Held content flushes FIRST, preserving the order the model
                    # generated it in.
                    if held is not None and (
                        delta.reasoning_content
                        or delta.tool_calls
                        or not could_still_be_bare_intent("".join(content_parts))
                    ):
                        for pending in held:
                            _mark_first_output(pending)
                            yield self._sse("delta", pending)
                        held = None
                    # Announce the thinking phase once, the instant the model's
                    # chain-of-thought starts streaming, so the CLI can show it.
                    if delta.reasoning_content and timings.reasoning_at_ms is None:
                        timings.reasoning_at_ms = (
                            round(budget.elapsed() * 1000) if budget is not None else 0
                        )
                    if delta.reasoning_content and not thinking_signaled:
                        thinking_signaled = True
                        yield self._sse("status", StreamStatus(phase="thinking").model_dump())
                    if delta.tool_calls:
                        saw_tool_calls = True
                        ready = tool_call_buffer.add(delta.tool_calls)
                        if delta.content or delta.reasoning_content:
                            yield self._sse(
                                "delta",
                                StreamDelta(
                                    content=delta.content,
                                    reasoning_content=delta.reasoning_content,
                                ),
                            )
                        if ready:
                            _mark_first_output(StreamDelta(tool_calls=ready))
                            yield self._sse("delta", StreamDelta(tool_calls=ready))
                        continue
                    if held is not None:
                        held.append(delta)
                        continue
                    _mark_first_output(delta)
                    yield self._sse("delta", delta)

                # The reply has ended with its content still private. This is the one
                # moment the dead round can be caught, and discarding it is free.
                if held is not None and self._is_bare_intent(
                    "".join(content_parts),
                    finish_reason=finish_reason,
                    saw_tool_calls=saw_tool_calls,
                    tools_callable=tools_callable,
                    request_id=meta.request_id,
                ):
                    self._log_bare_intent(
                        "detected",
                        request_id=meta.request_id,
                        stream=True,
                        first_chars=len("".join(content_parts)),
                    )
                    carried_usage = sum_usage(
                        carried_usage, opened_stream.usage or ProviderUsage()
                    )
                    # Read at the FOLD, not at the open-timing log: the endpoint is
                    # named by the first chunk that carries it, so by now it is as
                    # complete as this attempt will ever make it. Kept as the
                    # accepted one too — if the re-roll's open then fails, this
                    # draft is what the turn falls open to, and its host is the
                    # honest attribution for the tokens being reported.
                    main_endpoints.append(opened_stream.endpoint)
                    main_endpoint = opened_stream.endpoint
                    # Hand off ownership BEFORE tearing anything down: the draft has
                    # to survive a close failure, or a teardown error would cost the
                    # user a reply that is complete and in hand.
                    fallback = (held, finish_reason, "".join(content_parts))
                    # Close the discarded producer NOW rather than leaving it to the
                    # outer ``finally``, so the re-roll's open never contends with a
                    # connection this attempt no longer needs. Best-effort, for the
                    # same reason the terminal teardown is: a failed close must
                    # never become the turn's outcome.
                    #
                    # ``stream`` keeps pointing at it ACROSS the await and is cleared
                    # only once the close has actually returned. Clearing first would
                    # leak the producer if cancellation landed inside ``aclose()``:
                    # ``_close_quietly`` swallows ordinary failures but not
                    # ``CancelledError``, and the outer ``finally`` would then have no
                    # handle left to finish the teardown with. On the normal path the
                    # close still happens exactly once — ``stream`` is ``None`` by the
                    # time that ``finally`` runs.
                    await self._close_quietly(stream, request_id=meta.request_id)
                    stream = None
                    attempt += 1
                    active_request = self._bare_intent_repair_request(provider_request)
                    finish_reason = None
                    saw_tool_calls = False
                    thinking_signaled = False
                    content_parts = []
                    content_chars = 0
                    tool_call_buffer = StreamToolCallBuffer(param_index)
                    held = None
                    continue

                carried_usage = sum_usage(
                    carried_usage, opened_stream.usage or ProviderUsage()
                )
                main_endpoints.append(opened_stream.endpoint)
                # Assigned, never OR-ed: an accepted attempt that named no host
                # leaves this `None`, which is the truth. Falling back to the
                # discarded draft's host would attribute the ratio being logged
                # to a node that served none of it.
                #
                # The fail-open path (the re-roll never opened) is why the fold
                # above assigns too: it breaks before reaching here, so attempt
                # 0's host survives as the attribution for the draft the turn
                # actually falls open to.
                main_endpoint = opened_stream.endpoint
                if attempt:
                    self._log_bare_intent(
                        "completed",
                        request_id=meta.request_id,
                        stream=True,
                        retry_finish_reason=finish_reason,
                        retry_chars=len("".join(content_parts)),
                        outcome=("tool_calls" if saw_tool_calls else "final_text"),
                    )
                break

            # Every path out of the loop above has summed at least once; the
            # fallback keeps the accumulator's "nothing yet" state expressible
            # without leaking an Optional into the reporting helpers.
            total_usage = carried_usage or ProviderUsage()

            # One terminal event for the whole round, whichever attempt produced it.
            # Anything still held belongs to the accepted attempt and goes out now.
            for pending in held or []:
                _mark_first_output(pending)
                yield self._sse("delta", pending)
            held = None
            remaining = tool_call_buffer.flush()
            if remaining:
                _mark_first_output(StreamDelta(tool_calls=remaining))
                yield self._sse("delta", StreamDelta(tool_calls=remaining))
            self._log_tool_call_repairs(tool_call_buffer.repairs, request_id=meta.request_id)
            yield self._sse(
                "done",
                StreamDone(
                    finish_reason=finish_reason,
                    usage=response_usage(total_usage),
                    # `total_usage` already sums a discarded re-roll, so this
                    # total covers every generation the caller paid for — except
                    # an open we abandoned before it could report, which is what
                    # `complete` exists to admit.
                    cost=turn_cost(total_usage, meta, complete=abandoned_opens == 0),
                    # Lint what was actually streamed. A round that ended in tool
                    # calls is interstitial and is not linted, matching the
                    # non-streaming rule.
                    warnings=self._lint_streamed_output(
                        "".join(content_parts),
                        request=request,
                        request_id=meta.request_id,
                        had_tool_calls=saw_tool_calls or finish_reason == "tool_calls",
                    ),
                    timings=timings.to_wire(budget),
                ).model_dump(exclude_none=True),
            )
            completed = True
        except DaintreeError as exc:
            # Public provider consumption starts after the meta event. An exhausted
            # canonical open and a later stream failure therefore share
            # one honest terminal SSE shape; never fabricate a successful ``done``.
            logger.warning(
                "stream_error_after_start",
                request_id=meta.request_id,
                code=exc.code,
                status=exc.status_code,
            )
            yield self._sse(
                "error",
                StreamError(
                    error=exc.envelope().error,
                    request_id=meta.request_id,
                    retry_after=exc.headers.get("Retry-After"),
                ),
            )
        finally:
            if stream is not None:
                await self._close_quietly(stream, request_id=meta.request_id)

        # Learn only from a stream that reached a clean ``done`` (not an error, and
        # not an aborted stream — a client disconnect raises GeneratorExit at a
        # yield above and never reaches here). A tool-continuation round is filtered
        # by had_tool_calls inside the trigger.
        if completed:
            final_usage = carried_usage or ProviderUsage()
            log_turn_usage(
                final_usage,
                request_id=meta.request_id,
                session_id=request.session.id,
                turn_id=request.session.turn_id,
                session_round=request.session.round,
                stream=True,
                finish_reason=finish_reason,
                endpoint=main_endpoint,
                observed_endpoints=main_endpoints,
                selector_ran=meta.skills.selector.ran,
                selector_usage=meta.selector_usage,
                selector_endpoint=meta.selector_endpoint,
                turn_cost=turn_cost(final_usage, meta, complete=abandoned_opens == 0),
            )
            # Consolidated names only, and only from the buffer that survived:
            # a re-rolled attempt got a fresh buffer, so its discarded calls are
            # not graded as if the model had made them. `names_complete` carries
            # whether that buffer could read every call at all.
            self._grade_projection(
                projection,
                tool_call_buffer.called_names,
                request_id=meta.request_id,
                names_complete=tool_call_buffer.names_complete,
            )

    @staticmethod
    async def _chunk_iter(
        stream: ProviderStream,
        first_event: dict[str, Any] | None,
        *,
        budget: RequestBudget | None = None,
    ) -> AsyncIterator[dict[str, Any]]:
        """The stream's chunks, with the already-prefetched first event back in front.

        Also THE enforcement point for the streaming deadlines, because it is the
        single funnel every chunk passes through. Two limits apply, and which ones
        depends on whether the user has seen anything yet:

        * **stream_idle**, always — the per-gap allowance. A chunk arriving is the
          evidence the stream is alive, so this renews, and a long generation
          never trips it.
        * **first_output**, until the first chunk carrying visible content or a
          tool call — cumulative from THIS stream's open, and deliberately not
          renewed by arriving chunks. Otherwise an endpoint emitting a role-only
          delta or a reasoning fragment just inside every gap satisfies
          `stream_idle` forever while the user waits out the entire total budget
          for an answer that never starts. Progress they cannot see is not
          progress.

        The phase reported answers the question an operator actually has —
        `first_output` means the user never saw a token, `stream_idle` means the
        answer was cut off part-way — so it follows whether visible output had
        arrived, not which of the two allowances happened to be the tighter one.

        `first_output` is measured from the open rather than from the start of the
        turn — the correction that made the retry above it safe. Turn-relative, an
        attempt that stalled for its full allowance handed the live stream that
        replaced it an already-spent budget, and this loop would cut a healthy
        generation for arriving late.

        Both allowances are clamped by the total inside `RequestBudget`, so neither
        can outlive the turn.
        """
        if first_event is not None:
            if budget is not None and budget.expired():
                # The open/prefetch consumed the whole turn. Delivering this now
                # would let an expired budget still produce output — and, if the
                # iterator then ended, a `done` claiming the turn succeeded.
                raise DeadlineExceeded(TurnPhase.TOTAL, budget.elapsed())
            yield first_event
        if budget is None:
            async for chunk in stream:
                yield chunk
            return

        saw_output = _carries_output(first_event) if first_event else False
        iterator = stream.__aiter__()
        # This function is entered immediately after the open succeeded, so "now"
        # IS the open — the anchor `first_output` is measured from, per stream
        # rather than per turn.
        opened_at = time.monotonic()
        last_chunk_at = opened_at
        while True:
            # Before any visible token BOTH limits are in force and the tighter one
            # governs; after it, only the gap. The phase reported is not "which
            # allowance expired" but the question an operator actually needs
            # answered — had the user seen anything yet? — so it follows
            # `saw_output`, and the two allowances are visible in the log line.
            allowance = budget.remaining_idle(last_chunk_at)
            phase = TurnPhase.STREAM_IDLE
            if not saw_output:
                phase = TurnPhase.FIRST_OUTPUT
                allowance = min(allowance, budget.remaining_first_visible(opened_at))
            try:
                async with asyncio.timeout(allowance):
                    chunk = await iterator.__anext__()
            except StopAsyncIteration:
                return
            except TimeoutError as exc:
                # Total exhaustion outranks the phase limit in the report: it is
                # the more useful fact, and the phase limit merely happened to be
                # the one being waited on.
                effective = TurnPhase.TOTAL if budget.expired() else phase
                raise DeadlineExceeded(effective, budget.elapsed()) from exc
            last_chunk_at = time.monotonic()
            if not saw_output and _carries_output(chunk):
                saw_output = True
            yield chunk

    @staticmethod
    def _chunk_finish_reason(chunk: dict[str, Any]) -> str | None:
        choices = chunk.get("choices") or []
        if not choices:
            return None
        reason = choices[0].get("finish_reason")
        return reason if isinstance(reason, str) else None

    @staticmethod
    def _chunk_to_delta(chunk: dict[str, Any]) -> StreamDelta | None:
        choices = chunk.get("choices") or []
        if not choices:
            return None
        delta = choices[0].get("delta") or {}
        content = delta.get("content")
        reasoning_content = delta.get("reasoning_content")
        tool_calls = delta.get("tool_calls")
        if content is None and reasoning_content is None and not tool_calls:
            return None
        return StreamDelta(
            content=content, reasoning_content=reasoning_content, tool_calls=tool_calls
        )

    @staticmethod
    def _sse(event: str, data: dict[str, Any] | StreamDelta | StreamError) -> str:
        payload = (
            data.model_dump(exclude_none=True)
            if isinstance(data, (StreamDelta, StreamError))
            else data
        )
        body = json.dumps(payload, separators=(",", ":"), ensure_ascii=False)
        return f"event: {event}\ndata: {body}\n\n"

    # ---- bare-tool-intent repair ----------------------------------------

    @staticmethod
    async def _close_quietly(stream: ProviderStream, *, request_id: str) -> None:
        """Tear a provider stream down without letting the teardown become the answer.

        A terminal event (done/error) may already be on the wire, and a discarded
        attempt's replacement is still owed to the user — neither can be displaced
        by a close failure.
        """
        try:
            # Bounded, because this runs in a `finally` on the way OUT of a
            # deadline failure: an `aclose()` that hangs waiting on the same dead
            # upstream would swallow the terminal error the client is waiting for
            # and let the request outlive the very budget that just fired. A close
            # we abandon leaks a connection until the pool reaps it — strictly
            # better than never answering.
            async with asyncio.timeout(_CLOSE_GRACE_SECONDS):
                await stream.aclose()
        except TimeoutError:
            logger.warning("provider_close_timeout", request_id=request_id)
        except Exception:
            logger.warning("provider_close_failed", request_id=request_id, exc_info=True)

    def _is_bare_intent(
        self,
        text: str,
        *,
        finish_reason: str | None,
        saw_tool_calls: bool,
        tools_callable: bool,
        request_id: str,
    ) -> bool:
        """``is_bare_tool_intent``, but a bug in it can never cost the user a turn.

        Same posture as ``_lint_final_output``: this is an optional improvement to a
        reply that is already generated and already owed, so it fails open rather
        than propagating. (The streaming gate's ``could_still_be_bare_intent`` probe
        needs no such guard — it is a length test and a substring test on a ``str``.)
        """
        try:
            return is_bare_tool_intent(
                text,
                finish_reason=finish_reason,
                saw_tool_calls=saw_tool_calls,
                tools_callable=tools_callable,
            )
        except Exception:
            logger.warning("bare_intent_detector_failed", request_id=request_id, exc_info=True)
            return False

    @staticmethod
    def _tools_callable(request: ProviderChatRequest) -> bool:
        """Could this request have produced a tool call at all?

        A round with nothing callable cannot have dropped an action, so a sentence
        about acting is at worst awkward prose — never the failure in issue #25.
        """
        if not request.tools:
            return False
        return request.tool_choice != "none"

    @staticmethod
    def _bare_intent_repair_request(request: ProviderChatRequest) -> ProviderChatRequest:
        """The same request plus one corrective instruction.

        The discarded draft is deliberately NOT replayed. Its whole problem is that
        it is a persuasive in-context example of announcing-then-stopping — feeding
        it back is what turns one model slip into the self-reinforcing loop the
        issue describes. The nudge names the failure without demonstrating it.

        ``tool_choice`` is carried over untouched: forcing a call here would
        over-correct a round whose right answer is a direct reply with no call,
        which the tool-discipline prompt explicitly protects.
        """
        return replace(request, messages=[*request.messages, dict(_BARE_INTENT_NUDGE)])

    def _log_bare_intent(self, phase: str, *, request_id: str, **fields: Any) -> None:
        """Structured record of a re-roll. Never logs response content — only its
        shape — so a transcript can be diagnosed from logs without leaking it."""
        logger.info("main_bare_intent_retry", phase=phase, request_id=request_id, **fields)

    async def _repair_bare_intent(
        self, provider_request: ProviderChatRequest, result: Any, *, request_id: str
    ) -> Any:
        """One re-roll of a non-streaming round that announced an action and took none.

        Mirrors the selector's repair posture (``skills/selector.py`` ``_run``):
        detect the bad shape, re-ask ONCE with a corrective message, then accept
        whatever comes back — there is never a third attempt, and an upstream
        failure fails open to the reply already in hand. Nothing has reached the
        client at this point, so discarding the first draft is free.
        """
        if not self._is_bare_intent(
            result.text,
            finish_reason=result.finish_reason,
            saw_tool_calls=bool(result.tool_calls),
            tools_callable=self._tools_callable(provider_request),
            request_id=request_id,
        ):
            return result

        self._log_bare_intent(
            "detected", request_id=request_id, stream=False, first_chars=len(result.text or "")
        )
        try:
            repaired = await self._complete_with_retry(
                self._bare_intent_repair_request(provider_request)
            )
        except DaintreeError as exc:
            # Fail open: the first reply is weak, but it is a real reply the user is
            # already owed. A repair error must never cost them the turn.
            self._log_bare_intent(
                "failed_open", request_id=request_id, stream=False, code=exc.code
            )
            return result

        repaired.usage = sum_usage(result.usage, repaired.usage)
        self._log_bare_intent(
            "completed",
            request_id=request_id,
            stream=False,
            retry_finish_reason=repaired.finish_reason,
            retry_tool_calls=len(repaired.tool_calls or []),
            retry_chars=len(repaired.text or ""),
            outcome=("tool_calls" if repaired.tool_calls else "final_text"),
        )
        return repaired

    # ---- retries (before any model output is emitted) -------------------

    async def _complete_with_retry(
        self, request: ProviderChatRequest, *, budget: RequestBudget | None = None
    ) -> Any:
        return await retry_upstream(
            lambda: self.provider.complete(request),
            retries=self.settings.max_upstream_retries,
            backoff_base_seconds=self.settings.retry_backoff_base_seconds,
            # Attempts share the turn's remaining time rather than each getting a
            # fresh timeout, so N retries cannot multiply into an unbounded turn.
            remaining_seconds=(budget.remaining_total if budget is not None else None),
        )

    async def _open_stream_with_retry(
        self,
        request: ProviderChatRequest,
        *,
        budget: RequestBudget | None = None,
        request_id: str,
    ) -> tuple[ProviderStream, dict[str, Any] | None, int]:
        """Open the stream, retrying a stall. Returns the stream, its first event,
        and how many attempts were ABANDONED getting there.

        That count is not diagnostics: an abandoned attempt may already have been
        billed a prefill it never got to report, so the caller owes the user a cost
        marked incomplete rather than a confident total that omits it.
        """
        # Opening the stream and reading its first event are one retryable unit:
        # a failure in either is transient and recoverable before model output,
        # even though the public metadata event is already visible. A failed
        # prefetch must close its half-open stream before the attempt unwinds — on
        # ANY exception, including cancellation, so the httpx context never leaks.
        # Only DaintreeError is retried (retry_upstream catches nothing else).
        abandoned = 0

        async def _open() -> tuple[ProviderStream, dict[str, Any] | None]:
            stream = await self.provider.open_stream(request)
            ok = False
            try:
                first = await stream.prefetch_first_event()
                ok = True
                return stream, first
            finally:
                if not ok:
                    # BOUNDED, and that is load-bearing here in a way it is not in
                    # an ordinary teardown. This `finally` runs while the deadline
                    # below is cancelling the prefetch, and `asyncio.timeout` waits
                    # for cleanup to finish before it converts that cancellation
                    # into `TimeoutError`. A bare `await stream.aclose()` that hung
                    # on the same dead upstream would therefore never become a
                    # timeout at all: no retryable error, no retry, no fallback —
                    # the request simply outliving every budget until Cloud Run
                    # killed it. The one place a slow close can eat the deadline is
                    # the place the deadline exists for.
                    await self._close_quietly(stream, request_id=request_id)

        async def _bounded_open() -> tuple[ProviderStream, dict[str, Any] | None]:
            # Open and first-event prefetch were the largest hole in the budget:
            # a provider can send HTTP headers and then never complete an SSE
            # event, which no `httpx` read timeout inside the attempt bounds
            # usefully (ours is 840s, an inactivity bound) and which `_chunk_iter`
            # never sees because it has not started yet.
            #
            # A stall here is RETRYABLE, and getting that wrong cost a real turn:
            # this used to raise `DeadlineExceeded`, which `retry_upstream` cannot
            # catch (it handles `DaintreeError` only), so a provider that accepted
            # the connection and sent nothing killed the turn on the spot — with
            # the configured retry unused and the overwhelming majority of the
            # total budget unspent, while the same request had opened in ~1.2s one
            # round earlier. Nothing of the turn is public at this point (the
            # `meta` event carries no model output), so replaying is invisible and
            # correct. `upstream_timeout` is marked retryable, so this now costs a
            # pause instead of the workflow.
            #
            # Terminal only when the TOTAL is gone: then a retry has nothing to run
            # in, and the deadline error is the honest report.
            nonlocal abandoned
            if budget is None:
                return await _open()
            allowance = budget.remaining_open_attempt()
            try:
                async with asyncio.timeout(allowance):
                    return await _open()
            except TimeoutError as exc:
                abandoned += 1
                # The incident's own signature, logged at the attempt that caused
                # it. Without this a recovered stall leaves only an aggregated
                # `respond_upstream_open` and a terminal one leaves a generic
                # stream error — in both cases an operator can see the turn was
                # slow but not that an endpoint went silent, which is the fact
                # that names the endpoint worth investigating.
                logger.warning(
                    "respond_open_stall",
                    request_id=request_id,
                    phase=TurnPhase.OPEN_STALL.value,
                    allowance_ms=round(allowance * 1000),
                    elapsed_ms=round(budget.elapsed() * 1000),
                    budget_exhausted=budget.expired(),
                )
                if budget.expired():
                    raise DeadlineExceeded(TurnPhase.TOTAL, budget.elapsed()) from exc
                raise upstream_timeout(
                    "The upstream model provider did not send a response within "
                    f"{allowance:.0f}s of the request."
                ) from exc

        stream, first_event = await retry_upstream(
            _bounded_open,
            retries=self.settings.max_upstream_retries,
            backoff_base_seconds=self.settings.retry_backoff_base_seconds,
            # Open retries share the turn's clock too — otherwise each attempt
            # got a fresh upstream timeout and the retry multiplied the bound.
            remaining_seconds=(budget.remaining_total if budget is not None else None),
        )
        return stream, first_event, abandoned
