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
import random
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from typing import Any

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
from ..skills.catalog import Catalog
from ..skills.load_events import SkillLoadExecution
from ..skills.selector import SelectionDecision, Selector
from ..state.codec import StateCodec
from ..text_utils import sanitize_line
from .prompt_assembler import PromptAssembler

logger = get_logger("daintree.respond")


def merge_active_skills(
    selected_ids: list[str],
    prior_active_ids: list[str],
    catalog: Catalog,
    max_active: int,
) -> tuple[list[str], list[str]]:
    """Additive, new-first merge (section 7.9).

    Returns ``(active, newly_loaded)``.
    """
    candidate = list(selected_ids) + [p for p in prior_active_ids if p not in selected_ids]
    active: list[str] = []
    seen: set[str] = set()
    for sid in candidate:
        if sid in seen or sid not in catalog:
            continue
        seen.add(sid)
        active.append(sid)
        if len(active) >= max_active:
            break
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
        catalog: Catalog,
        selector: Selector,
        state_codec: StateCodec,
        assembler: PromptAssembler,
        provider: ChatProvider,
    ) -> None:
        self.settings = settings
        self.catalog = catalog
        self.selector = selector
        self.state_codec = state_codec
        self.assembler = assembler
        self.provider = provider

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
        not carry enough task signal, so surface the goal and wake flag.
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
        return " | ".join(parts) or None

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

        if run_selector:
            decision = await self.selector.select(
                request.input.messages,
                prior_active_ids,
                turn_context=self._selector_turn_context(request),
            )
        else:
            decision = SelectionDecision.reuse(prior_active_ids)
        warnings.extend(decision.warnings)

        active_ids, newly_loaded_ids = merge_active_skills(
            decision.skill_ids, prior_active_ids, self.catalog, self.settings.max_active_skills
        )
        skills = self.catalog.get_many(active_ids)
        executions = [
            SkillLoadExecution.from_skill(new_call_id(), self.catalog.get(sid))  # type: ignore[arg-type]
            for sid in newly_loaded_ids
        ]

        new_state = self.state_codec.issue(
            session_id=session_id,
            active_skills=[(s.id, s.version) for s in skills],
            catalog_revision=self.catalog.revision,
            turn_id=request.session.turn_id,
            instruction_revision=request.session.instruction_revision,
        )

        provider_request = self.assembler.build_main_request(
            request=request,
            skills=skills,
            synthetic_executions=executions,
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
            "X-Daintree-Protocol": "1",
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
        )

        if request.generation.stream:
            stream, first_event = await self._open_stream_with_retry(provider_request)
            body_iter = self._stream_body(stream, first_event, meta=meta)
            return RespondOutcome(headers=headers, streaming=True, stream=body_iter)

        result = await self._complete_with_retry(provider_request)
        body = self._build_nonstream_body(result, meta)
        return RespondOutcome(headers=headers, streaming=False, body=body)

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
            active_ids, newly_loaded_ids = merge_active_skills(
                decision.skill_ids, prior_active_ids, self.catalog, self.settings.max_active_skills
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
            active_skills=[(s.id, s.version) for s in skills],
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

    def _build_nonstream_body(self, result: Any, meta: _RespondMeta) -> dict[str, Any]:
        message = result.message or {}
        response = RespondResponse(
            request_id=meta.request_id,
            model=PUBLIC_MODEL_ID,
            message=RespondMessage(
                role=message.get("role", "assistant"),
                content=message.get("content"),
                tool_calls=self._normalize_tool_calls(message.get("tool_calls")),
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

    @staticmethod
    def _normalize_tool_calls(raw: Any) -> list[dict[str, Any]] | None:
        """Project upstream tool-call dicts onto the strict response shape,
        dropping provider-specific fields (e.g. DeepSeek's ``index``)."""
        if not raw:
            return None
        out: list[dict[str, Any]] = []
        for call in raw:
            fn = call.get("function") or {}
            out.append(
                {
                    "id": call.get("id", ""),
                    "type": call.get("type", "function"),
                    "function": {"name": fn.get("name", ""), "arguments": fn.get("arguments", "")},
                }
            )
        return out

    @staticmethod
    def _usage(usage: ProviderUsage) -> RespondUsage:
        return RespondUsage(
            prompt_tokens=usage.prompt_tokens or 0,
            completion_tokens=usage.completion_tokens or 0,
            total_tokens=usage.total_tokens or 0,
            cached_tokens=usage.cached_tokens or 0,
        )

    async def _stream_body(
        self, stream: ProviderStream, first_event: dict[str, Any] | None, *, meta: _RespondMeta
    ) -> AsyncIterator[str]:
        # The metadata event is always first, before any token: the client gets
        # the skill prelude, active set, refreshed state, and selector outcome
        # up front, then renders the load as if it happened before the reply.
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

        finish_reason: str | None = None
        try:
            for chunk in [first_event] if first_event is not None else []:
                finish_reason = self._chunk_finish_reason(chunk) or finish_reason
                delta = self._chunk_to_delta(chunk)
                if delta is not None:
                    yield self._sse("delta", delta)
            async for chunk in stream:
                finish_reason = self._chunk_finish_reason(chunk) or finish_reason
                delta = self._chunk_to_delta(chunk)
                if delta is not None:
                    yield self._sse("delta", delta)
            usage = self._usage(stream.usage or ProviderUsage())
            yield self._sse(
                "done", StreamDone(finish_reason=finish_reason, usage=usage).model_dump()
            )
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
        tool_calls = delta.get("tool_calls")
        if content is None and not tool_calls:
            return None
        return StreamDelta(content=content, tool_calls=tool_calls)

    @staticmethod
    def _sse(event: str, data: dict[str, Any] | StreamDelta) -> str:
        payload = data.model_dump(exclude_none=True) if isinstance(data, StreamDelta) else data
        body = json.dumps(payload, separators=(",", ":"), ensure_ascii=False)
        return f"event: {event}\ndata: {body}\n\n"

    # ---- retries (only before any public bytes) -------------------------

    async def _backoff(self, attempt: int) -> None:
        base = self.settings.retry_backoff_base_seconds
        if base <= 0:
            return
        delay = min(base * (2**attempt), 5.0)
        delay += random.uniform(0, base)  # noqa: S311 - jitter, not security-sensitive
        await asyncio.sleep(delay)

    async def _complete_with_retry(self, request: ProviderChatRequest) -> Any:
        attempts = 1 + max(0, self.settings.max_upstream_retries)
        last: DaintreeError | None = None
        for i in range(attempts):
            try:
                return await self.provider.complete(request)
            except DaintreeError as exc:
                last = exc
                if not exc.retryable or i == attempts - 1:
                    raise
                await self._backoff(i)
        assert last is not None  # pragma: no cover
        raise last

    async def _open_stream_with_retry(
        self, request: ProviderChatRequest
    ) -> tuple[ProviderStream, dict[str, Any] | None]:
        attempts = 1 + max(0, self.settings.max_upstream_retries)
        last: DaintreeError | None = None
        for i in range(attempts):
            try:
                stream = await self.provider.open_stream(request)
            except DaintreeError as exc:
                last = exc
                if not exc.retryable or i == attempts - 1:
                    raise
                await self._backoff(i)
                continue
            try:
                first = await stream.prefetch_first_event()
            except DaintreeError as exc:
                await stream.aclose()
                last = exc
                if not exc.retryable or i == attempts - 1:
                    raise
                await self._backoff(i)
                continue
            return stream, first
        assert last is not None  # pragma: no cover
        raise last
