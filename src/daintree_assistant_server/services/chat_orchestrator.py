"""The respond orchestrator: selection cadence, prompt assembly, skill metadata,
and the delayed streaming start.

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
from collections.abc import AsyncIterator
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
from ..errors import DaintreeError
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
from .prompt_assembler import PromptAssembler
from .skill_learning import SkillLearner
from .tool_call_repair import (
    ParameterIndex,
    StreamToolCallBuffer,
    build_parameter_index,
    repair_tool_call_arguments,
)

logger = get_logger("daintree.respond")


def _filter_dedup_cap(candidate: list[str], catalog: Catalog, max_active: int) -> list[str]:
    """Keep the first ``max_active`` known, de-duplicated ids in order."""
    active: list[str] = []
    seen: set[str] = set()
    for sid in candidate:
        if sid in seen or sid not in catalog:
            continue
        seen.add(sid)
        active.append(sid)
        if len(active) >= max_active:
            break
    return active


def merge_active_skills(
    selected_ids: list[str],
    prior_active_ids: list[str],
    catalog: Catalog,
    max_active: int,
) -> tuple[list[str], list[str]]:
    """Additive, new-first merge — the EXPLICIT-load path (``resolve`` action ``load``).

    A caller that already knows which skills to ADD on top of the prior set uses this;
    the prior set is preserved and the new ids land first. The automatic per-turn path
    uses :func:`resolve_active_skills` instead, where a selector run is authoritative.

    Returns ``(active, newly_loaded)``.
    """
    candidate = list(selected_ids) + [p for p in prior_active_ids if p not in selected_ids]
    active = _filter_dedup_cap(candidate, catalog, max_active)
    newly = [sid for sid in active if sid not in prior_active_ids]
    return active, newly


def resolve_active_skills(
    decision: SelectionDecision,
    prior_active_ids: list[str],
    catalog: Catalog,
    max_active: int,
) -> tuple[list[str], list[str]]:
    """Per-turn active set from a selection decision (section 7.9).

    A SUCCESSFUL selector run is authoritative: its ``skill_ids`` ARE the complete
    active set for this instruction — a topic-shift replacement, so an empty list
    clears all prior skills. When the selector did NOT run (a tool-continuation round,
    ``SelectionDecision.reuse``) or failed OPEN (``degraded``), the prior active set is
    reused unchanged — a classifier hiccup never evicts an in-progress runbook. The
    paired selector prompt therefore instructs the model to re-list any active skill it
    wants to keep.

    Returns ``(active, newly_loaded)``.
    """
    if decision.ran and not decision.degraded:
        candidate = list(decision.skill_ids)  # authoritative — replaces the prior set
    else:
        candidate = list(prior_active_ids)  # reuse (no run) / fail-open (degraded)
    active = _filter_dedup_cap(candidate, catalog, max_active)
    newly = [sid for sid in active if sid not in prior_active_ids]
    return active, newly


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
        session_id = request.session.id
        t_start = time.perf_counter()

        warnings: list[str] = []
        verification = self.state_codec.verify(request.state, session_id=session_id)
        if request.state and not verification.valid and verification.reason:
            warnings.append(verification.reason)

        # Only a fully valid token carries prior active skills forward; an
        # invalid/expired/wrong-session token is ignored (selection reruns).
        prior_active_ids: list[str] = []
        if verification.valid and verification.payload is not None:
            prior_active_ids = self.catalog.filter_known(verification.payload.active_ids())

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

        selector_ms = 0
        if run_selector:
            t_sel = time.perf_counter()
            decision = await self.selector.select(
                request.input.messages,
                prior_active_ids,
                turn_context=self._selector_turn_context(request),
                available_tools=self._available_tool_names(request),
            )
            selector_ms = round((time.perf_counter() - t_sel) * 1000)
        else:
            decision = SelectionDecision.reuse(prior_active_ids)
        warnings.extend(decision.warnings)

        # A successful selector run is authoritative (topic-shift replacement); a
        # no-run continuation round or a degraded selector reuses the prior set.
        active_ids, newly_loaded_ids = resolve_active_skills(
            decision, prior_active_ids, self.catalog, self.settings.max_active_skills
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
            turn_id=request.session.turn_id,
            instruction_revision=request.session.instruction_revision,
        )

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
        )
        headers = {
            "X-Daintree-Protocol": str(PROTOCOL_VERSION),
            "X-Request-Id": request_id,
        }

        logger.info(
            "respond",
            request_id=request_id,
            stream=request.generation.stream,
            selector_ran=decision.ran,
            selector_degraded=decision.degraded,
            active_skills=active_ids,
            newly_loaded=newly_loaded_ids,
            warnings=warnings,
            selector_ms=selector_ms,
            pre_upstream_ms=round((time.perf_counter() - t_start) * 1000),
        )

        if request.generation.stream:
            t_open = time.perf_counter()
            stream, first_event = await self._open_stream_with_retry(provider_request)
            # Phase timing for the round's "dead air": selector (above) + upstream
            # open-to-first-event. upstream_first_event_ms dominates on cache misses
            # (prefill) and on thinking-heavy rounds (reasoning precedes the event).
            logger.info(
                "respond_upstream_open",
                request_id=request_id,
                upstream_first_event_ms=round((time.perf_counter() - t_open) * 1000),
            )
            body_iter = self._stream_body(
                stream, first_event, meta=meta, request=request, active_ids=active_ids
            )
            return RespondOutcome(headers=headers, streaming=True, stream=body_iter)

        result = await self._complete_with_retry(provider_request)
        # finish_reason is a second, independent signal: treat a continuation round as
        # such even if a malformed upstream drops the tool_calls payload.
        had_tool_calls = bool(result.tool_calls) or result.finish_reason == "tool_calls"
        self._lint_final_output(result, request_id=request_id, had_tool_calls=had_tool_calls)
        body = self._build_nonstream_body(
            result, meta, param_index=build_parameter_index(request.input.tools)
        )
        self._maybe_learn_after_turn(
            request=request,
            active_ids=active_ids,
            assistant_text=result.text,
            had_tool_calls=had_tool_calls,
        )
        return RespondOutcome(headers=headers, streaming=False, body=body)

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
            active_ids, newly_loaded_ids = merge_active_skills(
                valid, prior_active_ids, self.catalog, self.settings.max_active_skills
            )
        elif action == "find":
            run_selector = True
            conversation = [ChatMessage(role="user", content=query or "")]
            decision = await self.selector.select(conversation, prior_active_ids)
            warnings.extend(decision.warnings)
            # A find re-selects: the selector result is authoritative, same as a turn.
            active_ids, newly_loaded_ids = resolve_active_skills(
                decision, prior_active_ids, self.catalog, self.settings.max_active_skills
            )
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
    ) -> _RespondMeta:
        def refs(ids: list[str]) -> list[SkillRef]:
            out: list[SkillRef] = []
            for sid in ids:
                skill = self.catalog.get(sid)
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
            catalog_revision=self.catalog.revision,
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

    async def _stream_body(
        self,
        stream: ProviderStream,
        first_event: dict[str, Any] | None,
        *,
        meta: _RespondMeta,
        request: RespondRequest,
        active_ids: list[str],
    ) -> AsyncIterator[str]:
        async def _chunks() -> AsyncIterator[dict[str, Any]]:
            if first_event is not None:
                yield first_event
            async for chunk in stream:
                yield chunk

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
        try:
            # The metadata event is always first, before any token: the client gets
            # the skill prelude, active set, refreshed state, and selector outcome
            # up front, then renders the load as if it happened before the reply.
            # Emitted inside the try so a client disconnect at this very first yield
            # still runs the stream's aclose() in the finally (no upstream leak).
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
            usage = self._usage(stream.usage or ProviderUsage())
            yield self._sse(
                "done", StreamDone(finish_reason=finish_reason, usage=usage).model_dump()
            )
            completed = True
        except DaintreeError as exc:
            # Failure after the meta event: emit a terminal error event and stop.
            # Never fabricate a successful ``done``.
            logger.warning(
                "stream_error_after_start",
                request_id=meta.request_id,
                code=exc.code,
                status=exc.status_code,
            )
            error_body = exc.envelope().model_dump(exclude_none=True)["error"]
            yield self._sse("error", {"error": error_body})
        finally:
            await stream.aclose()

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
    def _sse(event: str, data: dict[str, Any] | StreamDelta) -> str:
        payload = data.model_dump(exclude_none=True) if isinstance(data, StreamDelta) else data
        body = json.dumps(payload, separators=(",", ":"), ensure_ascii=False)
        return f"event: {event}\ndata: {body}\n\n"

    # ---- retries (only before any public bytes) -------------------------

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
        # a failure in either is transient and recoverable so long as no public
        # bytes have left the building. A failed prefetch must close its
        # half-open stream before the attempt unwinds — on ANY exception,
        # including cancellation, so the httpx context never leaks. Only
        # DaintreeError is retried (retry_upstream catches nothing else).
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
