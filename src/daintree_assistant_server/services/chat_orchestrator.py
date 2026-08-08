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
from collections import deque
from collections.abc import AsyncGenerator, AsyncIterator, Awaitable, Callable, Sequence
from dataclasses import dataclass, field
from typing import Any

from .. import PROTOCOL_VERSION
from ..config import PUBLIC_MODEL_ID, Settings
from ..contracts.daintree_api import (
    RespondMessage,
    RespondRequest,
    RespondResponse,
    RespondUsage,
    SkillsBlock,
    StreamDelta,
    StreamDone,
    StreamError,
    StreamMeta,
    StreamStatus,
)
from ..contracts.extensions import (
    Prelude,
    SelectorMeta,
    SelectorUsage,
    SkillRef,
)
from ..contracts.openai import ChatMessage
from ..errors import DaintreeError, internal_error
from ..observability import get_logger, new_call_id, new_request_id
from ..prompts.loader import MAIN_PROMPT_VERSION
from ..providers.base import ChatProvider, ProviderChatRequest, ProviderStream, ProviderUsage
from ..providers.retry import retry_upstream
from ..skills.catalog import Catalog, CatalogManager
from ..skills.load_events import SkillLoadExecution
from ..skills.selector import SelectionDecision, Selector
from ..state.codec import StateCodec
from ..text_utils import sanitize_line
from .output_policy import lint_output, repair_tables
from .prompt_assembler import PromptAssembler, render_order_ids
from .skill_learning import SkillLearner
from .tool_call_repair import (
    ParameterIndex,
    StreamToolCallBuffer,
    build_parameter_index,
    repair_tool_call_arguments,
)

logger = get_logger("daintree.respond")

_SPECULATION_CLOSE_TIMEOUT_SECONDS = 2.0


@dataclass(frozen=True)
class ActiveSkillSet:
    """A resolved active set, plus what the ``max_active`` budget actually cost.

    ``dropped_over_cap`` is the observability half: ids a caller asked for that the
    cap refused. Unknown and duplicate ids are NOT reported there — they carry no
    capacity signal.

    One caveat if this field is ever wired to a metric or a warning: on the explicit
    ``skill.load`` path (:func:`merge_active_skills`) the candidate list is
    ``selected + prior``, so what lands here is *previously active skills evicted to
    make room*, not *requested skills the cap refused*. Same field, different meaning —
    split it before treating the two as one number.
    """

    active: list[str]
    newly_loaded: list[str]
    dropped_over_cap: list[str]


def _filter_dedup_cap(
    candidate: Sequence[str], catalog: Catalog, max_active: int
) -> tuple[list[str], list[str]]:
    """Keep the first ``max_active`` known, de-duplicated ids in order.

    This is the VERBATIM path: a continuation round, a fail-open selector, and the
    explicit-load merge all reuse a set exactly as given. Foundation pairing belongs
    only to an authoritative selection (see :func:`_admit_with_domain_foundations`),
    so a mid-workflow round never surfaces a surprise load.

    Returns ``(active, dropped_over_cap)``.
    """
    active: list[str] = []
    seen: set[str] = set()
    dropped: list[str] = []
    for sid in candidate:
        if sid in seen or sid not in catalog:
            continue
        seen.add(sid)
        # Check the cap BEFORE admitting — appending first and breaking after would
        # always let one id through, even at max_active == 0. Unknown and duplicate
        # ids are filtered ABOVE this check so they never consume capacity, and a
        # refused id is RECORDED rather than breaking the scan, so the structured log
        # reports everything the cap actually cost instead of just the first casualty.
        if len(active) >= max_active:
            dropped.append(sid)
            continue
        active.append(sid)
    return active, dropped


def _admit_with_domain_foundations(
    candidate: Sequence[str], catalog: Catalog, max_active: int
) -> tuple[list[str], list[str]]:
    """Budget-aware ATOMIC-UNIT admission of an authoritative selection.

    Pairing used to append every missing domain foundation and then truncate to
    ``max_active`` — which, at full capacity, was deterministically DEAD CODE: the
    selector already truncates its own output to ``max_active``, so whenever the
    classifier returned a full set without the foundation, the appended foundation
    was GUARANTEED to be the first id the cap discarded. Pairing was not "outranked
    by explicit picks"; it simply never ran when it mattered, leaving front-loading
    model-dependent — the exact opposite of why it exists. Worse, a new full
    selection silently EVICTED an already-active foundation mid-workflow (and, per
    the assembler's cache rules, forced a full tools+conversation re-encode). The
    selector prompt's own tie-break puts the domain foundation ABOVE the marginal
    specific; this now matches it.

    The fix is unit admission, not slot reservation. Each selected specific is
    walked in rank order and admitted as one atomic unit — itself plus its domain
    foundation when that foundation is not already admitted — and only while the
    remaining budget fits the WHOLE unit. A unit that does not fit is skipped and
    the next specific is tried: a later specific whose foundation is already
    admitted costs one slot and can still fill the gap. Naively reserving a slot
    for the foundation is wrong in both directions — with three cross-domain
    specifics and a cap of three it evicts the third specific AND then drops the
    reserved-but-unclaimed foundation, returning two skills against a budget of
    three. Here no slot is ever reserved for a foundation that is then discarded.

    Every admitted skill's domain foundation is itself admitted, which holds even
    when the catalog resolves a foundation for EVERY id (a prefix walk-up).

    The policy is RANK RETENTION, not maximum cardinality, and the two can disagree
    once a catalog holds more than one foundation. With foundations ``a.base`` and
    ``b.base``, a selection of ``[a.one, b.one, b.two]`` and a cap of 3, this admits
    ``[a.one, a.base]`` and stops — both ``b.*`` units cost two at the moment they are
    considered. ``[b.one, b.base, b.two]`` is closed and fills all three slots, but
    reaching it means dropping the classifier's TOP-ranked pick to fit two lower-ranked
    ones. The selector returns best-match-first, so its first id is the strongest signal
    on the turn and keeping it wins. Deliberate — do not "fix" this with a subset search.

    Returns ``(active, dropped_over_cap)``; unknown and duplicate ids are dropped
    silently, as before.
    """
    active: list[str] = []
    admitted: set[str] = set()
    dropped: list[str] = []
    considered: set[str] = set()
    for sid in candidate:
        if sid in considered or sid not in catalog:
            continue
        considered.add(sid)
        if sid in admitted:
            continue
        unit = [sid]
        foundation_id = catalog.domain_foundation(sid)
        if (
            foundation_id is not None
            and foundation_id != sid
            and foundation_id not in admitted
            and foundation_id in catalog
        ):
            unit.append(foundation_id)
        if len(active) + len(unit) > max_active:
            # The unit does not fit. Skip it (never a partial admission) and try
            # the next specific, which may be cheaper because its foundation is
            # already in.
            dropped.append(sid)
            continue
        for uid in unit:
            admitted.add(uid)
            active.append(uid)
    return active, dropped


def merge_active_skills(
    selected_ids: list[str],
    prior_active_ids: list[str],
    catalog: Catalog,
    max_active: int,
) -> ActiveSkillSet:
    """Additive, new-first merge — the EXPLICIT-load path (``resolve`` action ``load``).

    A caller that already knows which skills to ADD on top of the prior set uses this;
    the prior set is preserved and the new ids land first. The automatic per-turn path
    uses :func:`resolve_active_skills` instead, where a selector run is authoritative.

    Foundation closure applies here TOO, via the same unit admission. This path signs a
    state token, and the reuse/degraded paths then replay that set verbatim on the
    strength of "a prior set already carries its foundation" — so an unclosed set minted
    here would propagate a foundation-less active set through every following round.
    """
    candidate = list(selected_ids) + [p for p in prior_active_ids if p not in selected_ids]
    active, dropped = _admit_with_domain_foundations(candidate, catalog, max_active)
    newly = [sid for sid in active if sid not in prior_active_ids]
    return ActiveSkillSet(active=active, newly_loaded=newly, dropped_over_cap=dropped)


def resolve_active_skills(
    decision: SelectionDecision,
    prior_active_ids: list[str],
    catalog: Catalog,
    max_active: int,
) -> ActiveSkillSet:
    """Per-turn active set from a selection decision (section 7.9).

    A SUCCESSFUL selector run is authoritative: its ``skill_ids`` ARE the complete
    active set for this instruction — a topic-shift replacement, so an empty list
    clears all prior skills. When the selector did NOT run (a tool-continuation round,
    ``SelectionDecision.reuse``) or failed OPEN (``degraded``), the prior active set is
    reused unchanged — a classifier hiccup never evicts an in-progress runbook. The
    paired selector prompt therefore instructs the model to re-list any active skill it
    wants to keep.

    On an authoritative run the selection is admitted as specific+foundation UNITS
    (:func:`_admit_with_domain_foundations`) so a foundation loads on the SAME turn its
    specific is selected, not a turn late, and can never be squeezed out by the cap.
    The reuse/degraded path is left untouched: a prior set resolved on a successful run
    already carries its foundation, so a continuation round never surfaces a surprise
    foundation load.
    """
    if decision.ran and not decision.degraded:
        # authoritative — replaces the prior set, admitted as foundation-paired units
        active, dropped = _admit_with_domain_foundations(
            decision.skill_ids, catalog, max_active
        )
    else:
        # reuse (no run) / fail-open (degraded) — verbatim, never re-paired
        active, dropped = _filter_dedup_cap(prior_active_ids, catalog, max_active)
    newly = [sid for sid in active if sid not in prior_active_ids]
    return ActiveSkillSet(active=active, newly_loaded=newly, dropped_over_cap=dropped)


@dataclass
class _RespondMeta:
    """The computed skill/state outcome shared by streaming and non-streaming."""

    request_id: str
    state: str
    catalog_revision: str
    prompt_version: str
    skills: SkillsBlock
    warnings: list[str] = field(default_factory=list)


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


@dataclass
class _PreparedTurn:
    """Everything selection/assembly produces before the provider is consumed."""

    provider_request: ProviderChatRequest
    meta: _RespondMeta
    active_ids: list[str]
    speculation: _SpeculativeStream | None


class _PrepOwnedStream:
    """The streaming body, with explicit ownership of the eager prep task.

    The response body may be closed before it is ever polled (an endpoint
    failure after ``complete()`` returned, or a client that vanished before the
    body started). Closing an un-started async generator never enters its body,
    which would leave selection running and a committed speculation idling
    until its claim timeout. This wrapper cancels prep in exactly that window;
    once iteration has started, closure is delegated to the generator, whose
    finally-chain already cancels prep and closes the provider.
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
            if (
                self._prep_task.done()
                and not self._prep_task.cancelled()
                and self._prep_task.exception() is None
            ):
                # Prep finished before the cancel landed: nobody will ever claim
                # its speculation, so discard it now instead of letting the
                # producer idle out its claim timeout.
                prepared = self._prep_task.result()
                if prepared.speculation is not None:
                    await prepared.speculation.discard()
        await self._gen.aclose()


class _SpeculationBufferOverflow(Exception):
    """A pre-handoff provider event exceeded the private byte budget."""


class _SpeculativeStream(ProviderStream):
    """A private, bounded producer for a possible main-stream winner.

    The producer is the sole reader of the upstream ``ProviderStream``. While the
    selector is pending it advances generation into a byte-and-chunk bounded
    deque; after an exact selection match, ``_stream_body`` claims this object and
    consumes that deque followed by live chunks through the same iterator. A
    mismatch (or abandoned handoff) cancels the producer and awaits its upstream
    close, so no half-open request or unobserved task survives.

    No public wire object is built here. Chunks, errors, and usage remain private
    until the orchestrator has committed the authoritative selector result.
    """

    def __init__(
        self,
        *,
        request: ProviderChatRequest,
        open_stream: Callable[
            [ProviderChatRequest],
            Awaitable[tuple[ProviderStream, dict[str, Any] | None]],
        ],
        max_chunks: int,
        max_bytes: int,
        claim_timeout_seconds: float,
        request_id: str,
        release_slot: Callable[[], None],
    ) -> None:
        self._request = request
        self._open_stream = open_stream
        self._max_chunks = max_chunks
        self._max_bytes = max_bytes
        self._claim_timeout_seconds = claim_timeout_seconds
        self._request_id = request_id
        self._release_slot_callback = release_slot
        self._slot_released = False
        self._cleanup_pending: set[asyncio.Task[Any]] = set()

        self._condition = asyncio.Condition()
        self._buffer: deque[tuple[dict[str, Any], int]] = deque()
        self._buffered_bytes = 0
        self._peak_buffered_bytes = 0
        self._peak_buffered_chunks = 0
        self._prefetched_chunks = 0
        self._prefetched_bytes = 0
        self._done = False
        self._error: BaseException | None = None
        self._discarded = False
        self._claimed = False
        self._committed = False
        self._stream_completed = False
        self._upstream_opened = False
        self.claim_failure_reason: str | None = None
        self._started_at = time.perf_counter()
        self.open_ms: int | None = None
        self.usage: ProviderUsage | None = None
        self.endpoint: str | None = None
        self._expiry_task: asyncio.Task[None] | None = None
        self._producer_task = asyncio.create_task(
            self._produce(), name=f"skill-speculation:{request_id}"
        )
        self._producer_task.add_done_callback(self._consume_task_exception)

    @staticmethod
    def _consume_task_exception(task: asyncio.Task[Any]) -> None:
        if not task.cancelled():
            task.exception()

    @staticmethod
    def _chunk_bytes(chunk: dict[str, Any]) -> int:
        # This is also the representation used on the upstream/public JSON wires,
        # making it a useful deterministic memory-pressure proxy.
        return len(
            json.dumps(chunk, separators=(",", ":"), ensure_ascii=False, default=str).encode(
                "utf-8"
            )
        )

    async def _put(self, chunk: dict[str, Any]) -> bool:
        size = self._chunk_bytes(chunk)
        # Before handoff this is optional work: fail the private attempt rather
        # than let one malicious/malformed event defeat its byte budget. ``claim``
        # observes the error and opens canonically. After claim the source is the
        # canonical response, so an indivisible event must be delivered intact.
        if size > self._max_bytes and not self._claimed:
            raise _SpeculationBufferOverflow
        async with self._condition:
            await self._condition.wait_for(
                lambda: self._discarded
                or (
                    len(self._buffer) < self._max_chunks
                    and (
                        not self._buffer
                        or self._buffered_bytes + size <= self._max_bytes
                    )
                )
            )
            if self._discarded:
                return False
            self._buffer.append((chunk, size))
            self._buffered_bytes += size
            self._prefetched_chunks += 1
            self._prefetched_bytes += size
            self._peak_buffered_chunks = max(
                self._peak_buffered_chunks, len(self._buffer)
            )
            self._peak_buffered_bytes = max(
                self._peak_buffered_bytes, self._buffered_bytes
            )
            self._condition.notify_all()
            return True

    async def _set_terminal(self, error: BaseException | None) -> None:
        async with self._condition:
            if error is not None:
                self._error = error
            self._done = True
            self._condition.notify_all()

    async def _produce(self) -> None:
        upstream: ProviderStream | None = None
        error: BaseException | None = None
        try:
            upstream, first_event = await self._open_stream(self._request)
            self._upstream_opened = True
            self.open_ms = round((time.perf_counter() - self._started_at) * 1000)
            # Mirror the serving endpoint out as soon as the open completes: the
            # claim path logs it, and waiting for the `finally` (where usage is
            # published) would always be too late for that line.
            self.endpoint = upstream.endpoint
            if first_event is not None and not await self._put(first_event):
                return
            async for chunk in upstream:
                if not await self._put(chunk):
                    return
            self._stream_completed = True
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # propagated privately by ``__anext__``
            error = exc
            # Publish the failure before close cleanup so claim can distinguish an
            # exhausted pre-open retry budget (preserve as terminal) from a private
            # post-open failure (safe to replay through one canonical fallback).
            async with self._condition:
                self._error = exc
                self._condition.notify_all()
        finally:
            if upstream is not None:
                self.usage = upstream.usage
                self.endpoint = upstream.endpoint or self.endpoint
                try:
                    async with asyncio.timeout(_SPECULATION_CLOSE_TIMEOUT_SECONDS):
                        await upstream.aclose()
                except TimeoutError:
                    logger.warning(
                        "skill_speculation_close_timeout",
                        request_id=self._request_id,
                        timeout_ms=round(_SPECULATION_CLOSE_TIMEOUT_SECONDS * 1000),
                    )
                except Exception as exc:  # pragma: no cover - provider close is best-effort
                    logger.warning(
                        "skill_speculation_close_failed",
                        request_id=self._request_id,
                        error_type=type(exc).__name__,
                    )
            await self._set_terminal(error)

    def commit(self) -> None:
        """Transfer the matching source toward the public iterator.

        The iterator does not actually own it until ``claim`` runs. The deadline
        closes it if FastAPI/the client never begins body iteration.
        """
        if self._discarded or self._committed:
            return
        self._committed = True
        self._expiry_task = asyncio.create_task(
            self._expire_unclaimed(), name=f"skill-speculation-handoff:{self._request_id}"
        )

    async def _expire_unclaimed(self) -> None:
        try:
            await asyncio.sleep(self._claim_timeout_seconds)
            if not self._claimed:
                await self.discard()
                logger.info(
                    "skill_speculation_abandoned",
                    request_id=self._request_id,
                    claim_timeout_ms=round(self._claim_timeout_seconds * 1000),
                )
        except asyncio.CancelledError:
            return

    async def claim(self) -> bool:
        """Claim a committed winner, or decline a failed/expired source.

        An exhausted open/prefetch retry budget is claimed and preserved as the
        terminal SSE result after authoritative metadata; it never receives a
        race-dependent second budget. A private post-open failure or buffer-cap
        overflow declines the source so the caller can open canonically. Once
        claimed, later failures keep ordinary terminal SSE semantics.
        """
        if self._discarded or not self._committed:
            self.claim_failure_reason = (
                "discarded" if self._discarded else "not_committed"
            )
            return False
        self._claimed = True
        expiry = self._expiry_task
        if expiry is not None and expiry is not asyncio.current_task():
            expiry.cancel()
            await asyncio.gather(expiry, return_exceptions=True)
        if self._error is not None:
            # `_open_stream_with_retry` already spent the canonical retry budget.
            # Preserve that exhausted error for SSE after authoritative `meta`
            # instead of race-dependently granting a second full budget. A private
            # failure after a stream DID open is replay-safe (no model bytes were
            # public), so it may still fall back to one fresh canonical stream.
            if isinstance(self._error, DaintreeError) and not self._upstream_opened:
                self.claim_failure_reason = "upstream_open_exhausted"
                self._release_slot()
                return True
            self.claim_failure_reason = (
                "buffer_overflow"
                if isinstance(self._error, _SpeculationBufferOverflow)
                else "private_stream_error"
            )
            await self.discard()
            return False
        # Claimed means the request is now the one canonical main stream, not an
        # overlapping speculative attempt. Its bounded buffer is immediately
        # owned/drained by the public iterator, so release overlap capacity here.
        self._release_slot()
        return True

    async def prefetch_first_event(self) -> dict[str, Any] | None:
        try:
            return await self.__anext__()
        except StopAsyncIteration:
            return None

    def __aiter__(self) -> _SpeculativeStream:
        return self

    async def __anext__(self) -> dict[str, Any]:
        async with self._condition:
            await self._condition.wait_for(lambda: bool(self._buffer) or self._done)
            if self._buffer:
                chunk, size = self._buffer.popleft()
                self._buffered_bytes -= size
                self._condition.notify_all()
                return chunk
            if self._error is not None:
                raise self._error
            raise StopAsyncIteration

    async def discard(self) -> None:
        """Cancel production, await closure, and make buffered chunks unreachable."""
        # Mark synchronously before the first await. This makes expiry vs claim an
        # atomic event-loop transition: either claim sets ``_claimed`` first and
        # expiry does nothing, or expiry/discard sets ``_discarded`` first and a
        # late claim cleanly falls back to the canonical request.
        newly_discarded = not self._discarded
        self._discarded = True
        if newly_discarded:
            async with self._condition:
                self._buffer.clear()
                self._buffered_bytes = 0
                self._condition.notify_all()

        current = asyncio.current_task()
        expiry = self._expiry_task
        if expiry is not None and expiry is not current:
            expiry.cancel()
        producer = self._producer_task
        if not producer.done() and producer is not current:
            producer.cancel()

        waits = [
            task
            for task in (producer, expiry)
            if task is not None and task is not current
        ]
        defer_slot_release = False
        try:
            if waits:
                done, pending = await asyncio.wait(
                    waits, timeout=_SPECULATION_CLOSE_TIMEOUT_SECONDS
                )
                for task in done:
                    if not task.cancelled():
                        task.exception()
                if pending:
                    defer_slot_release = self._defer_slot_release(waits)
                    logger.warning(
                        "skill_speculation_cleanup_timeout",
                        request_id=self._request_id,
                        timeout_ms=round(_SPECULATION_CLOSE_TIMEOUT_SECONDS * 1000),
                    )
        except BaseException:
            # A caller cancellation must not make overlap capacity available while
            # a cancellation-resistant producer still owns an upstream request.
            defer_slot_release = self._defer_slot_release(waits)
            raise
        finally:
            # A loser occupies overlap capacity until its upstream has actually
            # closed. Idempotence also covers selector/request cancellation races.
            if not defer_slot_release:
                self._release_slot()

    async def aclose(self) -> None:
        await self.discard()

    def _release_slot(self) -> None:
        if self._slot_released:
            return
        self._slot_released = True
        self._release_slot_callback()

    def _defer_slot_release(self, tasks: list[asyncio.Task[Any]]) -> bool:
        pending = {task for task in tasks if not task.done()}
        if not pending:
            return False
        for task in pending - self._cleanup_pending:
            task.add_done_callback(self._cleanup_finished)
        self._cleanup_pending.update(pending)
        return True

    def _cleanup_finished(self, task: asyncio.Task[Any]) -> None:
        if not task.cancelled():
            task.exception()
        self._cleanup_pending.discard(task)
        if not self._cleanup_pending:
            self._release_slot()

    def stats(self) -> dict[str, Any]:
        return {
            "open_ms": self.open_ms,
            "buffered_chunks": len(self._buffer),
            "buffered_bytes": self._buffered_bytes,
            "peak_buffered_chunks": self._peak_buffered_chunks,
            "peak_buffered_bytes": self._peak_buffered_bytes,
            "prefetched_chunks": self._prefetched_chunks,
            "prefetched_bytes": self._prefetched_bytes,
            "stream_completed": self._stream_completed,
        }


class ChatOrchestrator:
    def __init__(
        self,
        *,
        settings: Settings,
        catalog_manager: CatalogManager,
        selector: Selector,
        state_codec: StateCodec,
        assembler: PromptAssembler,
        provider: ChatProvider,
        learner: SkillLearner | None = None,
    ) -> None:
        self.settings = settings
        self.catalog_manager = catalog_manager
        self.selector = selector
        self.state_codec = state_codec
        self.assembler = assembler
        self.provider = provider
        self.learner = learner
        # In-flight fire-and-forget skill-learning tasks. asyncio holds only a weak
        # reference to a bare task, so we keep a strong one here (discarded on done)
        # to stop a learning pass being garbage-collected mid-flight.
        self._background_tasks: set[asyncio.Task[Any]] = set()
        # This counter is mutated without an ``await`` between check/increment, so
        # it is an event-loop-local non-blocking capacity gate. A turn that finds
        # the cap full stays on the serial path; it never queues behind speculation.
        self._skill_speculations_in_flight = 0

    @property
    def catalog(self) -> Catalog:
        # Read live: the skill-learning harness may reload it in place mid-request.
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
    ) -> bool:
        if force or policy == "always":
            return True
        if not state_valid:
            return True
        if state_catalog_revision != self.catalog.revision:
            return True
        if turn_id != state_turn_id:
            return True
        return instruction_revision != state_instruction_revision

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
        """The assistant's tool names, so the selector won't load a skill for a
        task the base function list already covers. Names are already validated."""
        names = [tool.function.name for tool in (request.input.tools or [])]
        return names[:cap]

    # ---- main entry -----------------------------------------------------

    async def complete(
        self,
        request: RespondRequest,
        *,
        upstream_model: str,
        request_id: str | None = None,
    ) -> RespondOutcome:
        request_id = request_id or new_request_id()
        headers = {
            "X-Daintree-Protocol": str(PROTOCOL_VERSION),
            "X-Request-Id": request_id,
        }

        if request.generation.stream:
            # Return the response scaffold immediately: HTTP headers (and SSE
            # comment heartbeats) reach the client while selection, assembly, and
            # the provider open still run. Prep starts NOW as a concurrent task —
            # not lazily on first poll — so selection/speculation overlap the
            # response handshake. A prep failure becomes a terminal SSE ``error``
            # event on the stream — never an HTTP error, never a silent hang.
            prep_task = asyncio.create_task(
                self._prepare(request, upstream_model=upstream_model, request_id=request_id)
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
                        request, prep_task=prep_task, request_id=request_id
                    ),
                    prep_task,
                ),
            )

        prepared = await self._prepare(
            request, upstream_model=upstream_model, request_id=request_id
        )
        result = await self._complete_with_retry(prepared.provider_request)
        # finish_reason is a second, independent signal: treat a continuation round as
        # such even if a malformed upstream drops the tool_calls payload.
        had_tool_calls = bool(result.tool_calls) or result.finish_reason == "tool_calls"
        self._lint_final_output(result, request_id=request_id, had_tool_calls=had_tool_calls)
        body = self._build_nonstream_body(
            result, prepared.meta, param_index=build_parameter_index(request.input.tools)
        )
        self._maybe_learn_after_turn(
            request=request,
            active_ids=prepared.active_ids,
            assistant_text=result.text,
            had_tool_calls=had_tool_calls,
        )
        return RespondOutcome(headers=headers, streaming=False, body=body)

    async def _prepare(
        self,
        request: RespondRequest,
        *,
        upstream_model: str,
        request_id: str,
    ) -> _PreparedTurn:
        session_id = request.session.id
        t_start = time.perf_counter()

        warnings: list[str] = []
        catalog_at_start = self.catalog
        verification = self.state_codec.verify(request.state, session_id=session_id)
        if request.state and not verification.valid and verification.reason:
            warnings.append(verification.reason)

        # Only a fully valid token carries prior active skills forward; an
        # invalid/expired/wrong-session token is ignored (selection reruns).
        prior_active_ids: list[str] = []
        if verification.valid and verification.payload is not None:
            prior_active_ids = catalog_at_start.filter_known(
                verification.payload.active_ids()
            )

        run_selector = self._should_run_selector(
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
        )

        # Speculation is intentionally narrower than "has prior ids": the token
        # must be verified against this session AND describe the exact catalog
        # snapshot used to assemble the private request. Empty, invalid, expired,
        # cross-session, and catalog-stale state all retain the serial first-turn
        # behavior. Non-streaming responses are unchanged.
        speculation: _SpeculativeStream | None = None
        speculative_request: ProviderChatRequest | None = None
        speculative_active_ids: list[str] = []
        # The RENDER order of the speculative set — the only ordering the assembled
        # bytes actually depend on (see the winner comparison below).
        speculative_render_ids: list[str] = []
        speculation_slot = False
        speculation_outcome = "non_streaming"
        speculation_stats: dict[str, Any] | None = None

        def release_speculation_slot() -> None:
            nonlocal speculation_slot
            if speculation_slot:
                self._skill_speculations_in_flight -= 1
                speculation_slot = False

        if request.generation.stream:
            if not run_selector:
                speculation_outcome = "selector_not_run"
            elif not verification.valid or verification.payload is None:
                speculation_outcome = "unverified_state"
            elif verification.payload.catalog_revision != catalog_at_start.revision:
                speculation_outcome = "stale_catalog"
            else:
                speculative_active_ids, _ = _filter_dedup_cap(
                    prior_active_ids,
                    catalog_at_start,
                    self.settings.max_active_skills,
                )
                if not speculative_active_ids:
                    speculation_outcome = "no_active_skills"
                elif self.settings.skill_speculation_max_concurrency == 0:
                    speculation_outcome = "disabled"
                elif (
                    self._skill_speculations_in_flight
                    >= self.settings.skill_speculation_max_concurrency
                ):
                    speculation_outcome = "capacity"
                else:
                    speculation_slot = True
                    self._skill_speculations_in_flight += 1
                    speculation_outcome = "eligible"
                    try:
                        speculative_skills = catalog_at_start.get_many(
                            speculative_active_ids
                        )
                        speculative_render_ids = render_order_ids(speculative_skills)
                        speculative_request = self.assembler.build_main_request(
                            request=request,
                            skills=speculative_skills,
                            upstream_model=upstream_model,
                        )
                        speculation = _SpeculativeStream(
                            request=speculative_request,
                            open_stream=self._open_stream_with_retry,
                            max_chunks=self.settings.skill_speculation_buffer_chunks,
                            max_bytes=self.settings.skill_speculation_buffer_bytes,
                            claim_timeout_seconds=(
                                self.settings.skill_speculation_claim_timeout_seconds
                            ),
                            request_id=request_id,
                            release_slot=release_speculation_slot,
                        )
                    except BaseException:
                        release_speculation_slot()
                        raise

        selector_ms = 0
        if run_selector:
            t_sel = time.perf_counter()
            try:
                decision = await self.selector.select(
                    request.input.messages,
                    prior_active_ids,
                    turn_context=self._selector_turn_context(request),
                    available_tools=self._available_tool_names(request),
                )
                selector_ms = round((time.perf_counter() - t_sel) * 1000)
            except BaseException:
                if speculation is not None:
                    speculation_stats = speculation.stats()
                    await speculation.discard()
                    speculation_outcome = "selector_exception"
                raise
        else:
            decision = SelectionDecision.reuse(prior_active_ids)
        try:
            warnings.extend(decision.warnings)

            # A successful selector run is authoritative (topic-shift replacement); a
            # no-run continuation round or a degraded selector reuses the prior set.
            # Pin one post-selection catalog snapshot for active resolution, prompt,
            # state, and metadata. If learning reloaded the catalog during selection,
            # the speculative prompt can never win even when its ids happen to match.
            resolved_catalog = self.catalog
            resolution = resolve_active_skills(
                decision,
                prior_active_ids,
                resolved_catalog,
                self.settings.max_active_skills,
            )
            active_ids = resolution.active
            newly_loaded_ids = resolution.newly_loaded

            skills = resolved_catalog.get_many(active_ids)
            executions = [
                SkillLoadExecution.from_skill(
                    new_call_id(), resolved_catalog.get(sid)  # type: ignore[arg-type]
                )
                for sid in newly_loaded_ids
            ]

            new_state = self.state_codec.issue(
                session_id=session_id,
                active_skills=[s.id for s in skills],
                catalog_revision=resolved_catalog.revision,
                turn_id=request.session.turn_id,
                instruction_revision=request.session.instruction_revision,
            )

            if speculation is not None:
                speculation_stats = speculation.stats()
                # Compare RENDER order, not list order: the loaded-skills block is
                # rendered foundations-first (stable), so [foundation, X] and
                # [X, foundation] assemble to byte-identical requests. An ordered
                # list comparison threw away a live, already-producing stream and
                # rebuilt an identical >50 KiB payload — and it fired on exactly the
                # turn shape pairing creates (the model lists the foundation first
                # on turn 1, then only the specific on turn 2, with pairing
                # appending the foundation last). A plain SET comparison would be
                # wrong: swapping two specifics genuinely changes the bytes.
                exact_winner = (
                    resolved_catalog.revision == catalog_at_start.revision
                    and render_order_ids(skills) == speculative_render_ids
                )
                if exact_winner:
                    # The request, settings, and catalog snapshot that produced
                    # this object are unchanged. Reuse the exact object already
                    # executing upstream instead of rebuilding/deep-comparing a
                    # prompt + tool payload that can exceed 100 KiB.
                    assert speculative_request is not None
                    provider_request = speculative_request
                    speculation_outcome = "reused"
                else:
                    t_discard = time.perf_counter()
                    await speculation.discard()
                    speculation_stats["discard_ms"] = round(
                        (time.perf_counter() - t_discard) * 1000
                    )
                    speculation_outcome = "discarded"
                    speculation = None
                    provider_request = self.assembler.build_main_request(
                        request=request,
                        skills=skills,
                        upstream_model=upstream_model,
                    )
            else:
                provider_request = self.assembler.build_main_request(
                    request=request,
                    skills=skills,
                    upstream_model=upstream_model,
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
            if speculation is not None:
                speculation.commit()
        except BaseException:
            if speculation is not None:
                await speculation.discard()
            raise

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
            skill_speculation=speculation_outcome,
            skill_speculation_stats=speculation_stats,
            pre_upstream_ms=round((time.perf_counter() - t_start) * 1000),
        )

        return _PreparedTurn(
            provider_request=provider_request,
            meta=meta,
            active_ids=active_ids,
            speculation=speculation,
        )

    # ---- output linting (non-stream backstop; observability) ------------

    def _lint_final_output(
        self, result: Any, *, request_id: str, had_tool_calls: bool
    ) -> None:
        """Lint the final assistant message: observability, plus optional table repair.

        Runs only when the turn ENDED (no tool calls) with visible content — a
        continuation round's content is interstitial. Logs any contract violation
        (over-budget pipe table, visible ``<think>`` leakage, oversized dump). When
        ``main_output_repair_tables`` is on, an over-budget table is rewritten to a
        per-entity list in place. Never raises — a lint bug must not break a response
        the user is already owed. Streaming bypasses this (bytes already sent).
        """
        if not self.settings.main_output_lint_enabled or had_tool_calls:
            return
        try:
            message = result.message or {}
            text = message.get("content") or ""
            if not text.strip():
                return
            findings = lint_output(
                text,
                terminal_width=self.settings.main_terminal_width,
                warn_chars=self.settings.main_output_warn_chars,
            )
            if not findings.has_violation:
                return
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
        except Exception as exc:  # noqa: BLE001 - observability must never break the turn
            logger.warning("main_output_lint_error", request_id=request_id, error=str(exc))

    # ---- skill learning (local dev only, off the response path) ---------

    def _maybe_learn_after_turn(
        self,
        *,
        request: RespondRequest,
        active_ids: list[str],
        assistant_text: str,
        had_tool_calls: bool,
    ) -> None:
        """Fire skill learning on the just-completed workflow, off the response path.

        Runs ONLY when the turn ended with a final answer (no tool calls) — a
        tool-continuation round is a half-finished workflow, so we wait for the
        round that actually closes it. This is exactly what lets the LAST workflow
        of a session be learned: there is no next instruction to trigger on, but a
        final answer is the natural completion boundary. The learner reloads the
        catalog in place, so a created/improved skill is live for the next turn.

        Fire-and-forget: a slow (thinking-model) learning pass must never delay or
        block the response the user already has, and the learner fails open
        internally. Invisible to the CLI — no wire fields, no feedback tool.
        """
        if self.learner is None or not self.learner.enabled:
            return
        if had_tool_calls or not assistant_text.strip():
            return
        trailing = [ChatMessage(role="assistant", content=assistant_text)]
        task = asyncio.create_task(
            self._run_learner_background(request, list(active_ids), trailing)
        )
        self._background_tasks.add(task)
        task.add_done_callback(self._background_tasks.discard)

    async def _run_learner_background(
        self,
        request: RespondRequest,
        active_ids: list[str],
        trailing: list[ChatMessage],
    ) -> None:
        """Guarded wrapper so a background learning pass can never surface an
        exception to the (already-detached) task — ``learner.run`` is fail-open,
        this is belt-and-suspenders for anything around it."""
        assert self.learner is not None  # guarded by the caller
        try:
            await self.learner.run(
                request=request, active_ids=active_ids, trailing_messages=trailing
            )
        except Exception as exc:  # noqa: BLE001 - detached task must swallow everything
            logger.warning("skill_learning_background_failed", error=str(exc))

    async def drain_background_tasks(self, timeout: float = 30.0) -> None:
        """Give in-flight learning tasks a BOUNDED grace period on shutdown, then
        cancel — used before the provider they depend on is closed.

        A learning pass is a slow (thinking-model) call that can legitimately run
        for minutes, so an unbounded await here would hang server shutdown after a
        Ctrl-C (the common case: stopping the local server right when the last
        turn's pass just fired). We wait up to ``timeout`` for passes to finish, then
        cancel the stragglers. Cancelling only interrupts the model call; a pass
        already inside its write+reload is cancellation-shielded (see
        ``SkillLearner._apply``), so the catalog is never left half-written. Losing
        an unfinished pass is acceptable — learning is fire-and-forget and
        best-effort, and the same workflow can be re-learned on a later run.
        """
        tasks = list(self._background_tasks)
        if not tasks:
            return
        logger.info("skill_learning_drain_wait", pending=len(tasks), timeout=timeout)
        _, pending = await asyncio.wait(tasks, timeout=timeout)
        if pending:
            logger.warning("skill_learning_drain_timeout", abandoned=len(pending))
            for task in pending:
                task.cancel()
            # A cancelled pass unwinds fast unless it is in the shielded commit,
            # which finishes on its own first (a file write + reload, not a model
            # call), so this final gather stays bounded.
            await asyncio.gather(*pending, return_exceptions=True)

    # ---- manual resolve (section 12.2) ---------------------------------

    async def resolve(
        self,
        *,
        action: str,
        session_id: str | None,
        state_token: str | None,
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
            decision = await self.selector.select(conversation, prior_active_ids)
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
        executions: list[SkillLoadExecution],
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
        )

    # ---- response bodies ------------------------------------------------

    def _build_nonstream_body(
        self, result: Any, meta: _RespondMeta, *, param_index: ParameterIndex
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
            usage=self._usage(result.usage),
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

    @staticmethod
    def _usage(usage: ProviderUsage) -> RespondUsage:
        return RespondUsage(
            prompt_tokens=usage.prompt_tokens or 0,
            completion_tokens=usage.completion_tokens or 0,
            total_tokens=usage.total_tokens or 0,
            cached_tokens=usage.cached_tokens or 0,
        )

    async def _stream_with_heartbeat(
        self,
        request: RespondRequest,
        *,
        prep_task: asyncio.Task[_PreparedTurn],
        request_id: str,
    ) -> AsyncGenerator[str, None]:
        """Wrap the event stream with SSE comment heartbeats.

        Whenever the inner stream is quiet for ``HEARTBEAT_INTERVAL_SECONDS`` —
        during selection, provider open/retries, or long thinking gaps — a
        ``: hb`` comment goes out so intermediaries and the CLI's idle watchdog
        see a live stream. Comments are invisible to SSE event parsing.
        """
        inner = self._stream_events(request, prep_task=prep_task, request_id=request_id)
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
    ) -> AsyncGenerator[str, None]:
        """Await prep inside the stream, then delegate to the provider event body.

        Any failure after the HTTP response has started can only be reported
        in-band: a prep failure or an unexpected mid-stream exception becomes one
        terminal SSE ``error`` event — never an abrupt EOF, never a fabricated
        ``done``. GeneratorExit/CancelledError (client disconnect, shutdown) pass
        through untouched — except that a disconnect while prep is still running
        also cancels the prep task, so selection/speculation stop promptly.
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
                    retry_after=exc.headers.get("Retry-After"),
                ),
            )
            return
        except Exception:
            logger.exception("respond_prepare_unexpected", request_id=request_id)
            yield self._sse("error", StreamError(error=internal_error().envelope().error))
            return

        body = self._stream_body(
            prepared.provider_request,
            meta=prepared.meta,
            request=request,
            active_ids=prepared.active_ids,
            speculative_stream=prepared.speculation,
        )
        try:
            async for event in body:
                yield event
        except Exception:
            # _stream_body already maps DaintreeError to a terminal event; this
            # catches what it could not anticipate (e.g. a malformed provider
            # chunk crashing delta handling) so the client still gets a typed end.
            logger.exception("respond_stream_unexpected", request_id=request_id)
            yield self._sse("error", StreamError(error=internal_error().envelope().error))
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
        active_ids: list[str],
        speculative_stream: _SpeculativeStream | None = None,
    ) -> AsyncGenerator[str, None]:
        finish_reason: str | None = None
        thinking_signaled = False
        # Accumulate the reply so post-completion learning can see the workflow it
        # just finished. content_parts is joined into the final answer; a tool_calls
        # delta marks this as a continuation round (not a completion boundary).
        content_parts: list[str] = []
        saw_tool_calls = False
        completed = False
        # Tool-call fragments are held per call (never forwarded piecemeal) so the
        # completed arguments can be schema-repaired before the client sees them.
        # All calls flush together at stream end: the client only dispatches tools
        # once the round is complete, so consolidation adds no real latency, and
        # holding everything tolerates providers that interleave call fragments.
        tool_call_buffer = StreamToolCallBuffer(build_parameter_index(request.input.tools))
        stream: ProviderStream | None = None
        used_speculation = False
        try:
            if speculative_stream is not None:
                # Provisional ownership is installed before ``claim`` awaits its
                # handoff-timer cleanup. If cancellation lands inside that await,
                # this method's ``finally`` still closes the producer.
                stream = speculative_stream
                if await speculative_stream.claim():
                    used_speculation = True
                else:
                    stream = None
                    logger.info(
                        "skill_speculation_fallback",
                        request_id=meta.request_id,
                        reason=speculative_stream.claim_failure_reason,
                        **speculative_stream.stats(),
                    )

            # The metadata event is always first, before any token: the client gets
            # the skill prelude, active set, refreshed state, and selector outcome
            # as soon as selection finishes. An eligible speculative upstream may
            # already be privately producing, but none of its status/reasoning/
            # content/tool/usage events can cross this authoritative boundary.
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

            t_open = time.perf_counter()
            if stream is None:
                opened_stream, first_event = await self._open_stream_with_retry(
                    provider_request
                )
                stream = opened_stream
            else:
                opened_stream = stream
                first_event = await opened_stream.prefetch_first_event()
            # Phase timing for the round's remaining "dead air": upstream
            # open-to-first-event. It dominates on cache misses (prefill) and on
            # thinking-heavy rounds (reasoning precedes the event), but no longer
            # delays the already-emitted skill metadata.
            logger.info(
                "respond_upstream_open",
                request_id=meta.request_id,
                speculative=used_speculation,
                upstream_first_event_ms=(
                    speculative_stream.open_ms
                    if (
                        speculative_stream is not None
                        and speculative_stream.open_ms is not None
                    )
                    else round((time.perf_counter() - t_open) * 1000)
                ),
                post_meta_first_event_ms=round((time.perf_counter() - t_open) * 1000),
                # The endpoint serving the main generation. Read after the first
                # event because that is the earliest chunk that names it; still
                # `None` if the stream ended before yielding one.
                upstream_endpoint=opened_stream.endpoint,
                speculative_prefetched_chunks=(
                    speculative_stream.stats()["prefetched_chunks"]
                    if speculative_stream is not None and used_speculation
                    else 0
                ),
            )

            async def _chunks() -> AsyncIterator[dict[str, Any]]:
                if first_event is not None:
                    yield first_event
                async for chunk in opened_stream:
                    yield chunk

            async for chunk in _chunks():
                finish_reason = self._chunk_finish_reason(chunk) or finish_reason
                delta = self._chunk_to_delta(chunk)
                if delta is None:
                    continue
                if delta.content:
                    content_parts.append(delta.content)
                # Announce the thinking phase once, the instant the model's
                # chain-of-thought starts streaming, so the CLI can show it.
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
                        yield self._sse("delta", StreamDelta(tool_calls=ready))
                    continue
                yield self._sse("delta", delta)
            remaining = tool_call_buffer.flush()
            if remaining:
                yield self._sse("delta", StreamDelta(tool_calls=remaining))
            self._log_tool_call_repairs(tool_call_buffer.repairs, request_id=meta.request_id)
            usage = self._usage(opened_stream.usage or ProviderUsage())
            yield self._sse(
                "done", StreamDone(finish_reason=finish_reason, usage=usage).model_dump()
            )
            completed = True
        except DaintreeError as exc:
            # Public provider consumption starts after the meta event. An exhausted
            # canonical/speculative open and a later stream failure therefore share
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
                    retry_after=exc.headers.get("Retry-After"),
                ),
            )
        finally:
            if stream is not None:
                try:
                    await stream.aclose()
                except Exception:
                    # A terminal event (done/error) may already be on the wire;
                    # a teardown failure must never surface as a second one.
                    logger.warning(
                        "provider_close_failed",
                        request_id=meta.request_id,
                        exc_info=True,
                    )

        # Learn only from a stream that reached a clean ``done`` (not an error, and
        # not an aborted stream — a client disconnect raises GeneratorExit at a
        # yield above and never reaches here). A tool-continuation round is filtered
        # by had_tool_calls inside the trigger.
        if completed:
            self._maybe_learn_after_turn(
                request=request,
                active_ids=active_ids,
                assistant_text="".join(content_parts),
                had_tool_calls=saw_tool_calls or finish_reason == "tool_calls",
            )

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

    # ---- retries (before any model output is emitted) -------------------

    async def _complete_with_retry(self, request: ProviderChatRequest) -> Any:
        return await retry_upstream(
            lambda: self.provider.complete(request),
            retries=self.settings.max_upstream_retries,
            backoff_base_seconds=self.settings.retry_backoff_base_seconds,
        )

    async def _open_stream_with_retry(
        self, request: ProviderChatRequest
    ) -> tuple[ProviderStream, dict[str, Any] | None]:
        # Opening the stream and reading its first event are one retryable unit:
        # a failure in either is transient and recoverable before model output,
        # even though the public metadata event is already visible. A failed
        # prefetch must close its half-open stream before the attempt unwinds — on
        # ANY exception, including cancellation, so the httpx context never leaks.
        # Only DaintreeError is retried (retry_upstream catches nothing else).
        async def _open() -> tuple[ProviderStream, dict[str, Any] | None]:
            stream = await self.provider.open_stream(request)
            ok = False
            try:
                first = await stream.prefetch_first_event()
                ok = True
                return stream, first
            finally:
                if not ok:
                    await stream.aclose()

        return await retry_upstream(
            _open,
            retries=self.settings.max_upstream_retries,
            backoff_base_seconds=self.settings.retry_backoff_base_seconds,
        )
