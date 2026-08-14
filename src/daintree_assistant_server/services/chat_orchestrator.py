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
from dataclasses import dataclass, field, replace
from typing import Any

from pydantic import SecretStr

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
    TurnCost,
)
from ..contracts.extensions import (
    Prelude,
    SelectorMeta,
    SelectorUsage,
    SkillRef,
)
from ..contracts.openai import ChatMessage
from ..errors import DaintreeError, internal_error, turn_deadline_exceeded
from ..observability import get_logger, new_call_id, new_request_id
from ..prompts.loader import MAIN_PROMPT_VERSION
from ..providers.base import ChatProvider, ProviderChatRequest, ProviderStream, ProviderUsage
from ..providers.policy import RoutingPreference, routing_preference
from ..providers.retry import retry_upstream
from ..skills.catalog import Catalog, CatalogManager
from ..skills.fast_path import can_skip_selection
from ..skills.load_events import SkillLoadExecution
from ..skills.selector import SelectionDecision, Selector
from ..skills.tool_projection import ToolProjection, project_tools
from ..state.codec import StateCodec
from ..text_utils import sanitize_line
from .bare_intent import could_still_be_bare_intent, is_bare_tool_intent
from .deadlines import DeadlineExceeded, RequestBudget, TurnPhase
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
    # Authority, not merely "did a call happen". A successful selector run is
    # authoritative; so is the no-task fast path, which reaches the same answer
    # without a call. Deriving authority from `ran` alone silently turned the
    # fast path's empty result into a REUSE of the prior set — the opposite of
    # the clear it advertised.
    if (decision.ran or decision.authoritative) and not decision.degraded:
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

# Grace for tearing down a provider stream. Deliberately short and separate from
# the turn budget: teardown usually runs in a `finally` on the way out of a
# deadline failure, so an unbounded close would let cleanup outlive the deadline
# that fired and swallow the terminal error owed to the client.
_CLOSE_GRACE_SECONDS = 5.0


@dataclass
class _PreparedTurn:
    """Everything selection/assembly produces before the provider is consumed."""

    provider_request: ProviderChatRequest
    meta: _RespondMeta
    active_ids: list[str]
    speculation: _SpeculativeStream | None
    # The caller's upstream credential (already attached to provider_request);
    # carried separately so the post-stream learning dispatch can capture it by
    # value without re-narrowing the request's optional field.
    api_key: SecretStr
    # The SHADOW tool projection for this turn. Carried so both response paths
    # can grade what the model actually called against what a filter would have
    # withheld — the one number that says whether filtering is safe to enable.
    # default_factory, not a shared instance: ToolProjection is frozen but holds
    # a mutable `reasons` dict, and one instance shared by every default-built
    # turn is a foot-gun waiting for the first caller that writes to it.
    projection: ToolProjection = field(default_factory=ToolProjection)
    # Spend on a speculative generation this turn opened and then threw away.
    # Billed to the caller either way; see `_turn_cost`.
    discarded_usage: ProviderUsage = field(default_factory=ProviderUsage)
    # True when such a generation happened at all, which is what distinguishes
    # "no speculation" from "speculation whose cost we could not read".
    speculation_billed: bool = False


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


def _carries_output(chunk: dict[str, Any] | None) -> bool:
    """Whether a chunk is the thing a user is waiting for.

    Visible content or a tool call ends the "is it broken?" interval. Reasoning
    fragments deliberately do NOT: they are internal, the client does not render
    them as an answer, and counting them would let a model that thinks for two
    minutes escape the first-output deadline entirely.
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
        api_key: SecretStr,
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
            # not lazily on first poll — so selection/speculation overlap the
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
        api_key: SecretStr,
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
        api_key: SecretStr,
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
            result, request_id=request_id, had_tool_calls=had_tool_calls
        )
        if lint_warnings:
            # Appended to the turn's existing warnings rather than replacing
            # them: a reply can be both over-wide AND have had an unknown skill
            # id ignored, and dropping either loses a distinct diagnosis.
            prepared.meta.warnings.extend(lint_warnings)
        turn_cost = self._turn_cost_for(result.usage, prepared)
        self._log_turn_usage(
            result.usage,
            request_id=request_id,
            stream=False,
            finish_reason=result.finish_reason,
            turn_cost=turn_cost,
        )
        body = self._build_nonstream_body(
            result,
            prepared.meta,
            param_index=build_parameter_index(request.input.tools),
            cost=turn_cost,
        )
        self._maybe_learn_after_turn(
            request=request,
            active_ids=prepared.active_ids,
            assistant_text=result.text,
            had_tool_calls=had_tool_calls,
            finish_reason=result.finish_reason,
            tools_callable=self._tools_callable(prepared.provider_request),
            request_id=request_id,
            api_key=api_key,
        )
        return RespondOutcome(headers=headers, streaming=False, body=body)

    async def _prepare(
        self,
        request: RespondRequest,
        *,
        upstream_model: str,
        request_id: str,
        api_key: SecretStr,
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
            )
        try:
            async with asyncio.timeout(budget.remaining(TurnPhase.PREPARATION)):
                return await self._prepare_inner(
                    request,
                    upstream_model=upstream_model,
                    request_id=request_id,
                    api_key=api_key,
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

    async def _prepare_inner(
        self,
        request: RespondRequest,
        *,
        upstream_model: str,
        request_id: str,
        api_key: SecretStr,
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
        no_task = can_skip_selection(request, prior_active_ids=prior_active_ids)
        if no_task:
            logger.info(
                "selector_fast_path",
                request_id=request_id,
                reason="no_task",
            )

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
        )

        # Speculation opens the main stream against a GUESS at what the selector is
        # about to return, in parallel with the selector call, and keeps it only if
        # the guess proves exact. It is what decouples the turn's first byte from
        # selection latency: measured, a winning speculation drops the post-meta
        # first-event time to ~0ms, so the turn's TTFT becomes the selector's
        # latency alone rather than selector + upstream open + prefill.
        #
        # THE GUESS. With usable state, the prior active set is the guess (a
        # continuation almost always keeps its skills). With NO usable state — a
        # cold first turn — the guess is the EMPTY set, because "no skills" is both
        # the single most likely selector answer and the only one available without
        # running the selector first. That covers the shape a user judges the
        # product by: the opening message of a session.
        #
        # THE TRADE, STATED PLAINLY. A wrong guess costs one discarded upstream
        # open. A cold turn that does select skills therefore pays for a prefill it
        # throws away — bounded by the concurrency cap, and cheap in practice
        # because the discarded prompt's cacheable prefix (base prompt + the ~18k
        # token tool region) is exactly the prefix the real request then reuses, so
        # the waste is the uncached tail plus a handful of generated tokens, not a
        # whole second call. Latency is bought with tokens here, deliberately.
        speculation: _SpeculativeStream | None = None
        speculative_request: ProviderChatRequest | None = None
        speculative_active_ids: list[str] = []
        # The RENDER order of the speculative set — the only ordering the assembled
        # bytes actually depend on (see the winner comparison below).
        speculative_render_ids: list[str] = []
        speculation_slot = False
        speculation_outcome = "non_streaming"
        # Which guess was made, so a discard rate in the logs is attributable to the
        # cold empty-set guess or to a prior set that went stale.
        speculation_basis = "none"
        speculation_stats: dict[str, Any] | None = None
        # A losing speculation's spend. `speculation_billed` is the honest half:
        # it records that a generation WAS paid for, even on the branch where the
        # amount is unknowable, so the turn's cost can be marked incomplete
        # instead of quietly reporting less than the caller was charged.
        discarded_usage = ProviderUsage()
        speculation_billed = False

        def release_speculation_slot() -> None:
            nonlocal speculation_slot
            if speculation_slot:
                self._skill_speculations_in_flight -= 1
                speculation_slot = False

        if request.generation.stream:
            if not run_selector:
                # Nothing to race: the active set is already known, so the caller
                # opens the real request directly.
                speculation_outcome = "selector_not_run"
            elif self.settings.skill_speculation_max_concurrency == 0:
                speculation_outcome = "disabled"
            elif (
                self._skill_speculations_in_flight
                >= self.settings.skill_speculation_max_concurrency
            ):
                speculation_outcome = "capacity"
            else:
                # A token only supplies the guess when it verifies against THIS
                # session AND names the catalog snapshot being assembled against.
                # Otherwise its ids may describe skills that no longer resolve the
                # same way, and the empty guess is the safer one.
                state_usable = (
                    verification.valid
                    and verification.payload is not None
                    and verification.payload.catalog_revision == catalog_at_start.revision
                )
                if state_usable:
                    speculative_active_ids, _ = _filter_dedup_cap(
                        prior_active_ids,
                        catalog_at_start,
                        self.settings.max_active_skills,
                    )
                    speculation_basis = "prior_set" if speculative_active_ids else "empty_prior"
                else:
                    speculative_active_ids = []
                    speculation_basis = "cold_empty"
                speculation_slot = True
                self._skill_speculations_in_flight += 1
                speculation_outcome = "eligible"
                try:
                    speculative_skills = catalog_at_start.get_many(
                        speculative_active_ids
                    )
                    speculative_render_ids = render_order_ids(speculative_skills)
                    # The assembler stays credential-free (it builds PROMPT bytes);
                    # the caller's key is attached here, as on every construction
                    # path — an omission would fail the call, since the provider
                    # holds no default credential.
                    speculative_request = replace(
                        self.assembler.build_main_request(
                            request=request,
                            skills=speculative_skills,
                            upstream_model=upstream_model,
                        ),
                        api_key=api_key,
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
                    api_key=api_key,
                    turn_context=self._selector_turn_context(request),
                    available_tools=self._available_tool_names(request),
                    # The selector sends the conversation upstream too, so the
                    # caller's routing choice has to reach it — otherwise a ZDR
                    # turn would honour the request on the visible call and quietly
                    # route the same content under the default policy here.
                    routing=routing_preference(request.routing),
                )
                selector_ms = round((time.perf_counter() - t_sel) * 1000)
            except BaseException:
                if speculation is not None:
                    speculation_stats = speculation.stats()
                    await speculation.discard()
                    speculation_outcome = "selector_exception"
                raise
        elif no_task:
            # CLEAR, not reuse. A greeting or acknowledgement has no task, so a
            # workflow skill left loaded would follow a finished workflow into an
            # unrelated conversation — and `resolve_active_skills` treats an
            # authoritative empty result as "clear", which is exactly what the
            # selector itself is told to return for these turns.
            decision = SelectionDecision.cleared()
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
                    # The caller was billed for this generation whether or not we
                    # used a byte of it. Read the usage AFTER discard: the
                    # producer records it in its `finally`, which discard awaits,
                    # so a speculation that finished before losing still reports
                    # a real cost here. One cancelled mid-flight reports nothing —
                    # OpenRouter only sends usage in a stream's final chunk — and
                    # that unknowable-but-real spend is what clears `complete`
                    # below rather than being silently treated as zero.
                    discarded_usage = speculation.usage or ProviderUsage()
                    speculation_billed = True
                    speculation = None
                    provider_request = replace(
                        self.assembler.build_main_request(
                            request=request,
                            skills=skills,
                            upstream_model=upstream_model,
                        ),
                        api_key=api_key,
                    )
            else:
                provider_request = replace(
                    self.assembler.build_main_request(
                        request=request,
                        skills=skills,
                        upstream_model=upstream_model,
                    ),
                    api_key=api_key,
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

        # Shadow measurement only — it must not be able to fail a turn. An empty
        # projection grades as "nothing was withheld", which is the honest
        # reading when the projection could not be computed.
        try:
            projection = project_tools(
                offered=request.input.tools,
                active_skills=skills,
                catalog=resolved_catalog,
                messages=request.input.messages,
                forced_tool=self._forced_tool_name(request.input.tool_choice),
            )
        except Exception:  # noqa: BLE001 — measurement must never break a turn
            logger.warning("tool_projection_failed", request_id=request_id)
            projection = ToolProjection()

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
            # Which guess was made. `reused` vs `discarded` alone cannot say whether
            # a discard rate comes from the cold empty-set guess (expected on turns
            # that do select skills) or from a prior set going stale (a cadence
            # problem worth chasing).
            skill_speculation_basis=speculation_basis,
            skill_speculation_stats=speculation_stats,
            # SHADOW tool projection: what a skill-aware filter WOULD have sent
            # this turn. The full inventory still goes upstream — this is the
            # measurement that decides whether filtering is safe to enable.
            # `tools_proposed` vs `tools_offered` is the size of the prize;
            # `tools_outside_proposed` (logged per-turn below, on the round that
            # actually calls tools) is the safety signal.
            tools_offered=len(projection.offered),
            tools_proposed=len(projection.proposed),
            pre_upstream_ms=round((time.perf_counter() - t_start) * 1000),
        )

        return _PreparedTurn(
            projection=projection,
            provider_request=provider_request,
            meta=meta,
            active_ids=active_ids,
            speculation=speculation,
            api_key=api_key,
            discarded_usage=discarded_usage,
            speculation_billed=speculation_billed,
        )

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

    def _lint_final_output(
        self, result: Any, *, request_id: str, had_tool_calls: bool
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
            findings = lint_output(
                text,
                terminal_width=self.settings.main_terminal_width,
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
                    terminal_width=self.settings.main_terminal_width,
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
        self, text: str, *, request_id: str, had_tool_calls: bool
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
            findings = lint_output(
                text,
                terminal_width=self.settings.main_terminal_width,
                warn_chars=self.settings.main_output_warn_chars,
            )
            if not findings.has_violation:
                return []
            logger.info(
                "main_output_lint",
                request_id=request_id,
                stream=True,
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

    # ---- skill learning (local dev only, off the response path) ---------

    def _maybe_learn_after_turn(
        self,
        *,
        request: RespondRequest,
        active_ids: list[str],
        assistant_text: str,
        had_tool_calls: bool,
        finish_reason: str | None,
        tools_callable: bool,
        request_id: str,
        api_key: SecretStr,
    ) -> None:
        """Fire skill learning on the just-completed workflow, off the response path.

        Runs ONLY when the turn ended with a final answer (no tool calls) — a
        tool-continuation round is a half-finished workflow, so we wait for the
        round that actually closes it. This is exactly what lets the LAST workflow
        of a session be learned: there is no next instruction to trigger on, but a
        final answer is the natural completion boundary. The learner reloads the
        catalog in place, so a created/improved skill is live for the next turn.

        A *bare intent* round is the second thing that is not a completion boundary,
        and the reason this takes the same structural signals ``is_bare_tool_intent``
        does: a round that only ANNOUNCED an action wears the exact shape of a final
        answer (no tool calls, ``stop``, non-empty text), so ``had_tool_calls`` cannot
        see it. The response path already re-rolls such a round once; this catches the
        ones the re-roll did not fix, which arrive here having accomplished nothing.

        Fire-and-forget: a slow (thinking-model) learning pass must never delay or
        block the response the user already has, and the learner fails open
        internally. Invisible to the CLI — no wire fields, no feedback tool.
        """
        if self.learner is None or not self.learner.enabled:
            return
        if had_tool_calls or not assistant_text.strip():
            return
        try:
            bare_intent = is_bare_tool_intent(
                assistant_text,
                finish_reason=finish_reason,
                saw_tool_calls=had_tool_calls,
                tools_callable=tools_callable,
            )
        except Exception:
            # Same never-propagate posture as ``_is_bare_intent``, opposite fail
            # direction: on the response path a detector bug must not cost a reply
            # the user is already owed, but here the cheap side is NOT learning. A
            # skipped pass costs nothing (learning is opportunistic, and the same
            # workflow can be learned on a later run), while learning from a dead
            # round can write a real skill file out of a non-answer.
            logger.warning(
                "skill_learning_bare_intent_check_failed", request_id=request_id, exc_info=True
            )
            bare_intent = True
        if bare_intent:
            # Content-free by design: the skip rate is worth watching, but assistant
            # text never belongs in a log line.
            logger.info("skill_learning_skipped_bare_intent", request_id=request_id)
            return
        trailing = [ChatMessage(role="assistant", content=assistant_text)]
        # The caller's credential is captured BY VALUE into the task's arguments:
        # by the time the fire-and-forget pass runs, the HTTP request (and its
        # Principal) is gone, so nothing ambient (contextvars included) would
        # still hold it.
        task = asyncio.create_task(
            self._run_learner_background(request, list(active_ids), trailing, api_key)
        )
        self._background_tasks.add(task)
        task.add_done_callback(self._background_tasks.discard)

    async def _run_learner_background(
        self,
        request: RespondRequest,
        active_ids: list[str],
        trailing: list[ChatMessage],
        api_key: SecretStr,
    ) -> None:
        """Guarded wrapper so a background learning pass can never surface an
        exception to the (already-detached) task — ``learner.run`` is fail-open,
        this is belt-and-suspenders for anything around it."""
        assert self.learner is not None  # guarded by the caller
        try:
            await self.learner.run(
                request=request,
                active_ids=active_ids,
                trailing_messages=trailing,
                api_key=api_key,
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
        api_key: SecretStr,
        turn_id: str | None = None,
        instruction_revision: int = 0,
        query: str | None = None,
        skill_ids: list[str] | None = None,
        request_id: str | None = None,
        routing: RoutingPreference | None = None,
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
                conversation, prior_active_ids, api_key=api_key, routing=routing
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
        )

    # ---- response bodies ------------------------------------------------

    def _build_nonstream_body(
        self,
        result: Any,
        meta: _RespondMeta,
        *,
        param_index: ParameterIndex,
        cost: TurnCost | None = None,
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
            cost=cost,
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

    @classmethod
    def _turn_cost_with_speculation(
        cls,
        usage: ProviderUsage,
        meta: _RespondMeta,
        *,
        discarded_usage: ProviderUsage,
        speculation_billed: bool,
    ) -> TurnCost | None:
        """``_turn_cost`` with a losing speculation's spend folded in.

        One home for the arithmetic, called identically by both response paths —
        the streaming path cannot see the ``_PreparedTurn`` itself, so the two
        pieces it needs are passed explicitly rather than duplicated inline.
        """
        if not speculation_billed:
            return cls._turn_cost(usage, meta)
        return cls._turn_cost(
            cls._sum_usage(usage, discarded_usage),
            meta,
            # A losing speculation is only fully accounted for if it reported a
            # cost before we cancelled it. Otherwise real spend happened that
            # nothing can measure, and the total is a floor.
            complete=discarded_usage.cost is not None,
        )

    @classmethod
    def _turn_cost_for(cls, usage: ProviderUsage, prepared: _PreparedTurn) -> TurnCost | None:
        """The non-streaming caller's shorthand, which does hold a prepared turn."""
        return cls._turn_cost_with_speculation(
            usage,
            prepared.meta,
            discarded_usage=prepared.discarded_usage,
            speculation_billed=prepared.speculation_billed,
        )

    @staticmethod
    def _turn_cost(
        main: ProviderUsage, meta: _RespondMeta, *, complete: bool = True
    ) -> TurnCost | None:
        """Total USD this request charged the caller, across every upstream call.

        A turn is normally TWO billed calls — the skill selector and the main
        completion — plus, on a re-rolled round, a generation the user never saw.
        All of it is charged to the caller's own key, so reporting only the main
        call would understate their bill by the part they cannot see.

        Both figures are OpenRouter's own reported ``cost``, not a token-price
        estimate we compute: the router knows which of ~24 endpoints served the
        request and what cache discount applied, and we do not.

        Returns ``None`` when NOTHING reported a cost — the absence says "we do
        not know", which a zero would misreport as "free".

        The subtler case is a PARTIAL answer, and it is why ``complete`` exists.
        A selector that ran but whose cost went unreported would otherwise
        contribute 0.0 to a field named ``total``, quietly turning a partial sum
        into a claimed complete one. So a component is treated as unknown only
        when its call actually happened, and any unknown clears ``complete``.

        ``complete=False`` may also be passed by the caller for a cost we know
        was incurred and cannot observe at all — a losing speculative stream that
        was cancelled before its usage arrived.
        """
        selector_usage = meta.skills.selector.usage
        selector = selector_usage.cost if selector_usage is not None else None
        # "Ran" is what makes a missing cost UNKNOWN rather than genuinely zero.
        # A turn that skipped selection entirely (continuation round, no-task
        # fast path) has no selector usage at all, and its absent cost is a
        # truthful zero rather than a gap.
        selector_ran = selector_usage is not None
        if main.cost is None and selector is None and not selector_ran:
            return None
        if main.cost is None and selector is None:
            return None
        known_complete = complete and main.cost is not None
        if selector_ran and selector is None:
            known_complete = False
        return TurnCost(
            total=(main.cost or 0.0) + (selector or 0.0),
            main=main.cost,
            selector=selector,
            complete=known_complete,
        )

    @staticmethod
    def _log_turn_usage(
        usage: ProviderUsage,
        *,
        request_id: str,
        stream: bool,
        finish_reason: str | None,
        turn_cost: TurnCost | None = None,
    ) -> None:
        """What this turn cost the caller, as metadata.

        The response body carries the same numbers to the CLI; this is the half
        that survives the request, so a tester reporting "it burned through my
        credit" can be answered from logs rather than from memory.

        ``cache_ratio`` is the number the prompt-cache work exists to move, and
        the one most worth watching regress: the assembler's whole layout is
        built to keep the tool schemas byte-stable, and a drop here is the first
        symptom that something started re-encoding them.
        """
        prompt = usage.prompt_tokens or 0
        logger.info(
            "turn_usage",
            request_id=request_id,
            stream=stream,
            finish_reason=finish_reason,
            prompt_tokens=prompt,
            completion_tokens=usage.completion_tokens or 0,
            cached_tokens=usage.cached_tokens or 0,
            cache_ratio=(round((usage.cached_tokens or 0) / prompt, 3) if prompt else None),
            cost_usd=usage.cost,
            # The figure that answers "what did this request cost me?" — every
            # upstream call the turn made, including the selector.
            turn_cost_usd=turn_cost.total if turn_cost else None,
            selector_cost_usd=turn_cost.selector if turn_cost else None,
        )

    @staticmethod
    def _usage(usage: ProviderUsage) -> RespondUsage:
        return RespondUsage(
            prompt_tokens=usage.prompt_tokens or 0,
            completion_tokens=usage.completion_tokens or 0,
            total_tokens=usage.total_tokens or 0,
            cached_tokens=usage.cached_tokens or 0,
            # NOT `or 0`: an unreported cost must stay None. Zero would read as
            # "this turn was free", which is a claim about the caller's bill we
            # have no basis for.
            cost=usage.cost,
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
            api_key=prepared.api_key,
            budget=budget,
            projection=prepared.projection,
            discarded_usage=prepared.discarded_usage,
            speculation_billed=prepared.speculation_billed,
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
                    error=turn_deadline_exceeded(exc.phase.value).envelope().error
                ),
            )
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
        api_key: SecretStr,
        speculative_stream: _SpeculativeStream | None = None,
        budget: RequestBudget | None = None,
        projection: ToolProjection,
        discarded_usage: ProviderUsage,
        speculation_billed: bool,
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
        # Built once and reused: the re-roll rebuilds its buffer from the same index.
        param_index = build_parameter_index(request.input.tools)
        tool_call_buffer = StreamToolCallBuffer(param_index)
        stream: ProviderStream | None = None
        used_speculation = False
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
        carried_usage = ProviderUsage()
        # The discarded draft, kept only until the re-roll's stream actually opens.
        # Up to that point nothing of the retry is public, so the first attempt is
        # still replay-safe and remains the honest fail-open answer.
        fallback: tuple[list[StreamDelta], str | None, str] | None = None
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

            while True:
                t_open = time.perf_counter()
                if stream is None:
                    try:
                        opened_stream, first_event = await self._open_stream_with_retry(
                            active_request, budget=budget
                        )
                    except DaintreeError as exc:
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
                        break
                    stream = opened_stream
                else:
                    opened_stream = stream
                    first_event = await opened_stream.prefetch_first_event()
                # Phase timing for the round's remaining "dead air": upstream
                # open-to-first-event. It dominates on cache misses (prefill) and on
                # thinking-heavy rounds (reasoning precedes the event), but no longer
                # delays the already-emitted skill metadata.
                # The re-roll is never speculative: its request carries the corrective
                # message, so no speculation opened against the original can serve it.
                # Attributing attempt 0's speculation stats to it would make the
                # speculation hit rate read higher than it is.
                served_by_speculation = used_speculation and attempt == 0
                logger.info(
                    "respond_upstream_open",
                    request_id=meta.request_id,
                    speculative=served_by_speculation,
                    # Which semantic attempt this open served: 0 is the round itself,
                    # 1 the bare-intent re-roll.
                    semantic_attempt=attempt,
                    upstream_first_event_ms=(
                        speculative_stream.open_ms
                        if (
                            served_by_speculation
                            and speculative_stream is not None
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
                        if speculative_stream is not None and served_by_speculation
                        else 0
                    ),
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
                            yield self._sse("delta", pending)
                        held = None
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
                    if held is not None:
                        held.append(delta)
                        continue
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
                    carried_usage = self._sum_usage(
                        carried_usage, opened_stream.usage or ProviderUsage()
                    )
                    # Hand off ownership BEFORE tearing anything down: the draft has
                    # to survive a close failure, or a teardown error would cost the
                    # user a reply that is complete and in hand.
                    fallback = (held, finish_reason, "".join(content_parts))
                    # Close the discarded producer NOW rather than leaving it to the
                    # outer ``finally``: a speculative stream holds an overlap slot
                    # until it closes, and the replacement open should not queue
                    # behind capacity this attempt no longer needs. Best-effort, for
                    # the same reason the terminal teardown is: a failed close must
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
                    tool_call_buffer = StreamToolCallBuffer(param_index)
                    held = None
                    continue

                carried_usage = self._sum_usage(
                    carried_usage, opened_stream.usage or ProviderUsage()
                )
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

            # One terminal event for the whole round, whichever attempt produced it.
            # Anything still held belongs to the accepted attempt and goes out now.
            for pending in held or []:
                yield self._sse("delta", pending)
            held = None
            remaining = tool_call_buffer.flush()
            if remaining:
                yield self._sse("delta", StreamDelta(tool_calls=remaining))
            self._log_tool_call_repairs(tool_call_buffer.repairs, request_id=meta.request_id)
            yield self._sse(
                "done",
                StreamDone(
                    finish_reason=finish_reason,
                    usage=self._usage(carried_usage),
                    # `carried_usage` already sums a discarded re-roll, so this
                    # total covers every generation the caller paid for.
                    cost=self._turn_cost_with_speculation(
                        carried_usage,
                        meta,
                        discarded_usage=discarded_usage,
                        speculation_billed=speculation_billed,
                    ),
                    # Lint what was actually streamed. A round that ended in tool
                    # calls is interstitial and is not linted, matching the
                    # non-streaming rule.
                    warnings=self._lint_streamed_output(
                        "".join(content_parts),
                        request_id=meta.request_id,
                        had_tool_calls=saw_tool_calls or finish_reason == "tool_calls",
                    ),
                ).model_dump(exclude_none=True),
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
                await self._close_quietly(stream, request_id=meta.request_id)

        # Learn only from a stream that reached a clean ``done`` (not an error, and
        # not an aborted stream — a client disconnect raises GeneratorExit at a
        # yield above and never reaches here). A tool-continuation round is filtered
        # by had_tool_calls inside the trigger.
        if completed:
            self._log_turn_usage(
                carried_usage,
                request_id=meta.request_id,
                stream=True,
                finish_reason=finish_reason,
                turn_cost=self._turn_cost_with_speculation(
                    carried_usage,
                    meta,
                    discarded_usage=discarded_usage,
                    speculation_billed=speculation_billed,
                ),
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
            self._maybe_learn_after_turn(
                request=request,
                active_ids=active_ids,
                assistant_text="".join(content_parts),
                had_tool_calls=saw_tool_calls or finish_reason == "tool_calls",
                finish_reason=finish_reason,
                tools_callable=tools_callable,
                request_id=meta.request_id,
                api_key=api_key,
            )

    @staticmethod
    async def _chunk_iter(
        stream: ProviderStream,
        first_event: dict[str, Any] | None,
        *,
        budget: RequestBudget | None = None,
    ) -> AsyncIterator[dict[str, Any]]:
        """The stream's chunks, with the already-prefetched first event back in front.

        Also THE enforcement point for the two streaming deadlines, because it is
        the single funnel every chunk passes through. Two different limits apply
        depending on what has happened so far:

        * **first_output** until the first chunk carrying visible content or a
          tool call — the interval a user experiences as "is this broken?".
        * **stream_idle** thereafter, per gap — which distinguishes a slow
          generation (chunks still arriving) from a dead one (nothing arriving),
          a distinction an `httpx` read timeout can make but a total budget
          cannot.

        Both are clamped by the total budget inside :meth:`RequestBudget.remaining`,
        so a generous idle allowance cannot outlive the turn.
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
        last_chunk_at = time.monotonic()
        while True:
            # Before output: an ABSOLUTE first-output deadline measured from the
            # start of the turn, so reasoning fragments cannot renew it.
            # After output: a per-gap idle allowance, which legitimately resets
            # because a chunk arriving IS the evidence the stream is alive.
            if saw_output:
                phase = TurnPhase.STREAM_IDLE
                allowance = budget.remaining_idle(last_chunk_at)
            else:
                phase = TurnPhase.FIRST_OUTPUT
                allowance = budget.remaining(phase)
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

    @staticmethod
    def _sum_usage(first: ProviderUsage | None, second: ProviderUsage | None) -> ProviderUsage:
        """Total tokens across both attempts.

        The caller's own key funded the discarded generation too, so reporting only
        the accepted attempt would under-bill what the turn actually cost them.
        ``None`` means "not reported" and stays ``None`` only if neither side had a
        number, so a provider that omits usage is not silently turned into a zero.
        """

        def add(a: int | None, b: int | None) -> int | None:
            if a is None:
                return b
            return a if b is None else a + b

        def add_cost(a: float | None, b: float | None) -> float | None:
            if a is None:
                return b
            return a if b is None else a + b

        first = first or ProviderUsage()
        second = second or ProviderUsage()
        return ProviderUsage(
            prompt_tokens=add(first.prompt_tokens, second.prompt_tokens),
            completion_tokens=add(first.completion_tokens, second.completion_tokens),
            total_tokens=add(first.total_tokens, second.total_tokens),
            cached_tokens=add(first.cached_tokens, second.cached_tokens),
            # A discarded attempt was still billed to the caller, so its cost is
            # part of what this turn cost them — the same reason the tokens are
            # summed rather than replaced.
            cost=add_cost(first.cost, second.cost),
        )

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

        repaired.usage = self._sum_usage(result.usage, repaired.usage)
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
        self, request: ProviderChatRequest, *, budget: RequestBudget | None = None
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

        async def _bounded_open() -> tuple[ProviderStream, dict[str, Any] | None]:
            # Open and first-event prefetch were the largest hole in the budget:
            # a provider can send HTTP headers and then never complete an SSE
            # event, which no `httpx` read timeout inside the attempt bounds
            # usefully and which `_chunk_iter` never sees because it has not
            # started yet. Bound the pair by the same absolute first-output
            # deadline the chunk loop uses, so "time until something visible"
            # means the same thing before and after the stream exists.
            if budget is None:
                return await _open()
            try:
                async with asyncio.timeout(budget.remaining(TurnPhase.FIRST_OUTPUT)):
                    return await _open()
            except TimeoutError as exc:
                phase = (
                    TurnPhase.TOTAL if budget.expired() else TurnPhase.FIRST_OUTPUT
                )
                raise DeadlineExceeded(phase, budget.elapsed()) from exc

        return await retry_upstream(
            _bounded_open,
            retries=self.settings.max_upstream_retries,
            backoff_base_seconds=self.settings.retry_backoff_base_seconds,
            # Open retries share the turn's clock too — otherwise each attempt
            # got a fresh upstream timeout and the retry multiplied the bound.
            remaining_seconds=(budget.remaining_total if budget is not None else None),
        )
