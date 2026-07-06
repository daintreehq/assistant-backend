"""RunSupervisor: the backend-owned model/tool loop over the durable store.

One driver task per run drives exactly one round per invocation: selection (on
a fresh turn), assembly from the server-side transcript via the same
:class:`PromptAssembler` (and therefore the same cache-optimized layout) as
v1, the streamed model call, then one atomic persistence step — tool calls
become leases, a final answer closes the turn. Continuation after tool
results, approval resolutions, or expiries re-enters through
:meth:`maybe_continue`, whose compare-and-swap guarantees at most one
continuation per round.

Restart safety: the driver is a resumable state machine keyed off persisted
events. Re-driving a crashed round repeats only the (side-effect-free) model
call; lease creation is insert-or-ignore keyed on (run, round, tool_call_id).
"""

from __future__ import annotations

import asyncio
import contextlib
import time
from dataclasses import dataclass, field
from typing import Any

from ..config import DEEPSEEK_MODEL, Settings
from ..contracts.daintree_api import Generation, RespondInput, RespondRequest, RespondSession
from ..contracts.extensions import RuntimeContext, Selection, TurnContext
from ..contracts.openai import ChatMessage, ToolDef
from ..errors import DaintreeError
from ..observability import get_logger
from ..providers.base import ChatProvider, ProviderChatRequest, ProviderUsage
from ..services.chat_orchestrator import resolve_active_skills
from ..services.prompt_assembler import PromptAssembler
from ..services.skill_learning import SkillLearner
from ..skills.catalog import Catalog, CatalogManager
from ..skills.selector import Selector
from ..text_utils import sanitize_line
from .hub import EventHub
from .models import (
    EVENT_MODEL_DELTA,
    EVENT_MODEL_STATUS,
    READONLY_RISKS,
    EventRecord,
    LeaseDraft,
    RunRecord,
    StoreOutcome,
    canonical_json,
    new_run_id,
    sha256_hex,
)
from .store import RunStore

logger = get_logger("daintree.runs")


def build_internal_request(
    *,
    context: dict[str, Any],
    transcript: list[dict[str, Any]],
    session_id: str,
    turn_id: str,
    round_no: int,
) -> RespondRequest:
    """The internal (server-synthesized) respond request fed to the assembler.

    Shared by the live driver and the replay reconstructor so both assemble
    from identical inputs. Session fields are bookkeeping only — assembly (and
    therefore the prompt fingerprint) does not depend on them.
    """
    runtime = RuntimeContext.model_validate(context["runtime"]) if context.get("runtime") else None
    turn = TurnContext.model_validate(context["turn"]) if context.get("turn") else None
    tool_defs = [ToolDef.model_validate(t) for t in (context.get("tools") or [])]
    gen = dict(context.get("generation") or {})
    gen["stream"] = True
    return RespondRequest(
        protocol_version=1,
        session=RespondSession(
            id=session_id,
            turn_id=turn_id,
            instruction_revision=0,
            round=max(round_no, 0),
        ),
        state=None,
        input=RespondInput(
            messages=[ChatMessage.model_validate(m) for m in transcript],
            tools=tool_defs or None,
            tool_choice=None,
        ),
        runtime=runtime,
        turn=turn,
        selection=Selection(),
        generation=Generation.model_validate(gen),
    )


def prompt_fingerprint(request: ProviderChatRequest) -> str:
    """SHA-256 over the canonical, replay-relevant content of an upstream request.

    Volatile transport fields (stream/include_usage/timeout/prompt_cache_key)
    are excluded: they do not change what the model sees.
    """
    material = {
        "model": request.model,
        "messages": request.messages,
        "tools": request.tools,
        "tool_choice": request.tool_choice,
        "temperature": request.temperature,
        "max_tokens": request.max_tokens,
        "response_format": request.response_format,
        "stop": request.stop,
        "thinking_disabled": request.thinking_disabled,
        "reasoning_effort": request.reasoning_effort,
    }
    return sha256_hex(canonical_json(material))


class _ToolCallAccumulator:
    """Reassemble OpenAI-style streamed tool-call fragments by index."""

    def __init__(self) -> None:
        self._calls: dict[int, dict[str, Any]] = {}

    def add(self, fragments: list[dict[str, Any]]) -> None:
        for frag in fragments:
            if not isinstance(frag, dict):
                continue
            try:
                idx = int(frag.get("index") or 0)
            except (TypeError, ValueError):
                idx = 0
            slot = self._calls.setdefault(
                idx, {"id": "", "type": "function", "function": {"name": "", "arguments": ""}}
            )
            if frag.get("id"):
                slot["id"] = str(frag["id"])
            fn = frag.get("function") or {}
            if isinstance(fn, dict):
                if fn.get("name"):
                    slot["function"]["name"] = str(fn["name"])
                if fn.get("arguments"):
                    slot["function"]["arguments"] += str(fn["arguments"])

    def result(self) -> list[dict[str, Any]]:
        return [self._calls[i] for i in sorted(self._calls)]


@dataclass
class _RoundResult:
    message: dict[str, Any]
    finish_reason: str | None
    usage: dict[str, int]
    tool_calls: list[dict[str, Any]] = field(default_factory=list)
    text: str = ""


class RunSupervisor:
    def __init__(
        self,
        *,
        settings: Settings,
        store: RunStore,
        hub: EventHub,
        catalog_manager: CatalogManager,
        selector: Selector,
        assembler: PromptAssembler,
        provider: ChatProvider,
        learner: SkillLearner | None = None,
    ) -> None:
        self.settings = settings
        self.store = store
        self.hub = hub
        self.catalog_manager = catalog_manager
        self.selector = selector
        self.assembler = assembler
        self.provider = provider
        self.learner = learner
        self._drivers: dict[str, asyncio.Task[None]] = {}
        self._background_tasks: set[asyncio.Task[Any]] = set()
        self._risk_cache: dict[str, str] = {}
        self._closed = False

    @property
    def catalog(self) -> Catalog:
        return self.catalog_manager.catalog

    # ---- public entry points --------------------------------------------

    async def create_run(
        self,
        *,
        session_id: str | None,
        project_id: str | None,
        title: str | None,
        run_id: str | None = None,
    ) -> StoreOutcome:
        outcome = await self.store.create_run(
            run_id=run_id or new_run_id(),
            session_id=session_id,
            project_id=project_id,
            title=title,
        )
        self.hub.publish_all(outcome.events)
        return outcome

    async def submit_turn(
        self,
        *,
        run_id: str,
        turn_id: str,
        source: str,
        messages: list[dict[str, Any]],
        runtime: dict[str, Any] | None,
        turn: dict[str, Any] | None,
        tools: list[dict[str, Any]] | None,
        generation: dict[str, Any] | None,
        selection_policy: str,
        selection_force: bool,
    ) -> StoreOutcome:
        title_hint = None
        for msg in messages:
            content = msg.get("content")
            if isinstance(content, str) and content.strip():
                title_hint = sanitize_line(content, 120)
                break
        outcome = await self.store.submit_turn(
            run_id=run_id,
            turn_id=turn_id,
            source=source,
            messages=messages,
            context_update={
                "runtime": runtime,
                "turn": turn,
                "tools": tools,
                "generation": generation,
            },
            selection_policy=selection_policy,
            selection_force=selection_force,
            title_hint=title_hint,
        )
        if outcome.kind == "accepted":
            self.hub.publish_all(outcome.events)
            self._spawn_driver(run_id)
        return outcome

    async def submit_wake_turn(
        self, *, run_id: str, turn_id: str, note: str | None
    ) -> StoreOutcome:
        """A server-initiated wake turn: runs whether or not a CLI is connected.

        Local tool calls it produces become pending leases that wait durably
        for an executor; mutating ones additionally require approval.
        """
        detail = sanitize_line(note, 512) or "Scheduled wake-up fired."
        message = (
            f"[background wake] {detail} Review the current run state and act on it; "
            "if nothing needs doing, say so briefly."
        )
        return await self.submit_turn(
            run_id=run_id,
            turn_id=turn_id,
            source="wake",
            messages=[{"role": "user", "content": message}],
            runtime=None,
            turn={"goal": detail, "is_wake": True},
            tools=None,
            generation=None,
            selection_policy="new_instruction",
            selection_force=False,
        )

    async def cancel(self, run_id: str, *, reason: str = "client_cancelled") -> StoreOutcome:
        task = self._drivers.get(run_id)
        if task is not None and not task.done():
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task
        outcome = await self.store.cancel_turn(run_id, reason=reason)
        if outcome.kind == "cancelled":
            self.hub.publish_all(outcome.events)
        return outcome

    async def on_lease_resolution(self, run_id: str) -> bool:
        """Called after any lease reaches a terminal state (result posted,
        approval declined, expiry). Continues the model loop when the waiting
        round is fully resolved; the store CAS makes this at-most-once."""
        return await self.maybe_continue(run_id)

    async def maybe_continue(self, run_id: str) -> bool:
        run = await self.store.get_run(run_id)
        if run is None or run.status != "awaiting_tools":
            return False
        if not await self.store.all_round_leases_terminal(run_id, run.current_round):
            return False
        outcome = await self.store.try_begin_continuation(run_id)
        if outcome.kind != "continuing":
            return False
        self.hub.publish_all(outcome.events)
        self._spawn_driver(run_id)
        return True

    async def recover(self) -> None:
        """Resume durable work after a process restart."""
        for run in await self.store.runs_in_status(["running"]):
            logger.info("run_recover_redrive", run_id=run.run_id)
            self._spawn_driver(run.run_id)
        for run in await self.store.runs_in_status(["awaiting_tools"]):
            if await self.maybe_continue(run.run_id):
                logger.info("run_recover_continue", run_id=run.run_id)

    async def aclose(self) -> None:
        """Cancel live drivers (their state is durable; recovery resumes them)
        and give fire-and-forget learning a bounded drain."""
        self._closed = True
        tasks = [t for t in self._drivers.values() if not t.done()]
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        pending = list(self._background_tasks)
        if pending:
            _, still = await asyncio.wait(pending, timeout=30.0)
            for task in still:
                task.cancel()
            if still:
                await asyncio.gather(*still, return_exceptions=True)

    # ---- driver ------------------------------------------------------------

    def _spawn_driver(self, run_id: str) -> None:
        if self._closed:
            return
        existing = self._drivers.get(run_id)
        if existing is not None and not existing.done():
            return
        task = asyncio.create_task(self._drive_guarded(run_id))
        self._drivers[run_id] = task

        def _cleanup(t: asyncio.Task[None], rid: str = run_id) -> None:
            # Identity check: a late done-callback from a finished driver must
            # never evict a REPLACEMENT driver registered under the same run.
            if self._drivers.get(rid) is t:
                self._drivers.pop(rid, None)

        task.add_done_callback(_cleanup)

    async def _drive_guarded(self, run_id: str) -> None:
        try:
            await self._drive(run_id)
        except asyncio.CancelledError:
            raise
        except DaintreeError as exc:
            await self._record_failure(run_id, code=exc.code or "upstream_error",
                                       message=exc.message)
        except Exception as exc:  # noqa: BLE001 - driver must never die silently
            logger.error("run_driver_crashed", run_id=run_id, error=str(exc), exc_info=exc)
            await self._record_failure(run_id, code="internal_error", message=str(exc))

    async def _record_failure(self, run_id: str, *, code: str, message: str) -> None:
        run = await self.store.get_run(run_id)
        round_no = run.current_round if run is not None else 0
        with contextlib.suppress(Exception):
            outcome = await self.store.record_round_failed(
                run_id, round_no, code=code, message=message
            )
            self.hub.publish_all(outcome.events)

    async def _drive(self, run_id: str) -> None:
        run = await self.store.get_run(run_id)
        if run is None or run.status != "running":
            return

        if run.needs_selection:
            await self._run_selection(run)
            run = await self.store.get_run(run_id)
            if run is None or run.status != "running":
                return

        # Resume point: a started round with no persisted assistant message is a
        # crashed round — re-drive it. Otherwise start the next round.
        started, assistant = await self.store.round_state(run_id, run.current_round)
        if started and assistant is None and run.current_round > 0:
            round_no = run.current_round
        else:
            round_no = run.current_round + 1
        await self._drive_round(run, round_no)

    # ---- selection -----------------------------------------------------------

    async def _run_selection(self, run: RunRecord) -> None:
        transcript = await self.store.transcript(run.run_id)
        messages = [ChatMessage.model_validate(m) for m in transcript]
        prior = self.catalog.filter_known(run.active_skills)

        turn_ctx = (run.context.get("turn") or {}) if isinstance(run.context, dict) else {}
        hints: list[str] = []
        goal = sanitize_line(str(turn_ctx.get("goal") or ""), 512)
        if goal:
            hints.append(f"current goal: {goal}")
        if run.current_turn_source == "wake":
            hints.append("triggered by a background wake (no new user message)")
        tools = run.context.get("tools") or []
        tool_names = [
            str(t.get("function", {}).get("name", ""))
            for t in tools
            if isinstance(t, dict)
        ][:100]

        decision = await self.selector.select(
            messages,
            prior,
            turn_context=" | ".join(hints) or None,
            available_tools=[n for n in tool_names if n],
        )
        active_ids, newly_loaded = resolve_active_skills(
            decision, prior, self.catalog, self.settings.max_active_skills
        )
        usage = decision.usage
        outcome = await self.store.record_selection(
            run.run_id,
            active_skills=active_ids,
            payload={
                "turn_id": run.current_turn_id,
                "active": active_ids,
                "newly_loaded": newly_loaded,
                "ran": decision.ran,
                "degraded": decision.degraded,
                "task_type": decision.task_type,
                "confidence": decision.confidence,
                "usage": self._usage_dict(usage),
                "warnings": list(decision.warnings),
            },
        )
        self.hub.publish_all(outcome.events)

    # ---- one round -------------------------------------------------------------

    async def _drive_round(self, run: RunRecord, round_no: int) -> None:
        run_id = run.run_id
        refreshed = await self.store.get_run(run_id)
        if refreshed is None:
            return
        run = refreshed

        transcript = await self.store.transcript(run_id)
        internal = self._internal_request(run, transcript)
        skills = self.catalog.get_many(self.catalog.filter_known(run.active_skills))
        provider_request = self.assembler.build_main_request(
            request=internal, skills=skills, upstream_model=DEEPSEEK_MODEL
        )
        fingerprint = prompt_fingerprint(provider_request)

        await self._refresh_risk_cache(now=time.time())
        started = await self.store.record_round_started(
            run_id,
            round_no,
            {
                "round": round_no,
                "turn_id": run.current_turn_id,
                "prompt_fingerprint": fingerprint,
                "catalog_revision": self.catalog.revision,
                "prompt_version": "main",
                "model": DEEPSEEK_MODEL,
                "active_skills": [s.id for s in skills],
                "skill_hashes": {s.id: s.content_hash for s in skills},
                "message_count": len(provider_request.messages),
            },
        )
        self.hub.publish_all(started.events)

        result = await self._call_model(provider_request, run_id, round_no)

        drafts = self._lease_drafts(run, result.tool_calls)
        # The persisted assistant message must reference EXACTLY the tool calls
        # that became leases: a malformed streamed call (missing id/name, or a
        # duplicate id) is dropped from the message too, otherwise the durable
        # transcript would carry a tool call no result can ever answer — which
        # the upstream provider rejects on every later round.
        if result.tool_calls:
            lease_ids = {d.tool_call_id for d in drafts}
            kept = [c for c in result.tool_calls if str(c.get("id") or "") in lease_ids]
            if len(kept) != len(result.tool_calls):
                logger.warning(
                    "run_malformed_tool_calls_dropped",
                    run_id=run_id,
                    round=round_no,
                    dropped=len(result.tool_calls) - len(kept),
                )
            if kept:
                result.message["tool_calls"] = kept
            else:
                result.message.pop("tool_calls", None)
        outcome = await self.store.record_assistant_message(
            run_id=run_id,
            round_no=round_no,
            turn_id=run.current_turn_id,
            message=result.message,
            finish_reason=result.finish_reason,
            usage=result.usage,
            leases=drafts,
            lease_deadline_seconds=self.settings.runs_lease_deadline_seconds,
        )
        self.hub.publish_all(outcome.events)
        logger.info(
            "run_round_complete",
            run_id=run_id,
            round=round_no,
            tool_calls=len(drafts),
            finish_reason=result.finish_reason,
        )
        if outcome.kind == "final":
            self._maybe_learn(internal, run, result.text)

    def _lease_drafts(
        self, run: RunRecord, tool_calls: list[dict[str, Any]]
    ) -> list[LeaseDraft]:
        drafts: list[LeaseDraft] = []
        seen: set[str] = set()
        risks = self._risk_cache
        for call in tool_calls:
            call_id = str(call.get("id") or "")
            fn = call.get("function") or {}
            name = str(fn.get("name") or "")
            if not call_id or not name or call_id in seen:
                continue
            seen.add(call_id)
            risk = risks.get(name)
            drafts.append(
                LeaseDraft(
                    tool_call_id=call_id,
                    tool_name=name,
                    arguments=str(fn.get("arguments") or ""),
                    risk=risk,
                    requires_approval=self._requires_approval(run, risk),
                )
            )
        return drafts

    async def _refresh_risk_cache(self, *, now: float) -> None:
        self._risk_cache = await self.store.tool_risks(now=now)

    @staticmethod
    def _requires_approval(run: RunRecord, risk: str | None) -> bool:
        """Background mutation is never auto-authorized: on a wake-sourced turn
        any tool whose advertised risk is not clearly read-only (unknown counts
        as mutating) parks behind an explicit approval. Interactive turns keep
        the CLI's local confirmation flow."""
        if run.current_turn_source != "wake":
            return False
        return (risk or "unknown") not in READONLY_RISKS

    # ---- model call (streamed, retried as a whole round) ------------------------

    async def _call_model(
        self, request: ProviderChatRequest, run_id: str, round_no: int
    ) -> _RoundResult:
        """Run the round's model call, relaying ephemeral deltas to subscribers.

        Deltas are advisory (never persisted), so unlike v1 there is no
        "public bytes" point of no return: a mid-stream failure retries the
        whole round, bounded by MAX_UPSTREAM_RETRIES.
        """
        retries = self.settings.max_upstream_retries
        base = self.settings.retry_backoff_base_seconds
        attempt = 0
        while True:
            try:
                return await self._stream_once(request, run_id)
            except DaintreeError as exc:
                if not exc.retryable or attempt >= retries:
                    raise
                logger.warning(
                    "run_round_retry",
                    run_id=run_id,
                    round=round_no,
                    attempt=attempt + 1,
                    code=exc.code,
                )
                self._publish_ephemeral(
                    run_id, EVENT_MODEL_STATUS, {"phase": "retrying", "attempt": attempt + 1}
                )
                await asyncio.sleep(base * (2**attempt))
                attempt += 1

    async def _stream_once(self, request: ProviderChatRequest, run_id: str) -> _RoundResult:
        stream = await self.provider.open_stream(request)
        content_parts: list[str] = []
        reasoning_parts: list[str] = []
        accumulator = _ToolCallAccumulator()
        finish_reason: str | None = None
        thinking_signaled = False
        try:
            first = await stream.prefetch_first_event()

            async def chunks() -> Any:
                if first is not None:
                    yield first
                async for chunk in stream:
                    yield chunk

            async for chunk in chunks():
                choices = chunk.get("choices") or []
                if not choices:
                    continue
                reason = choices[0].get("finish_reason")
                if isinstance(reason, str):
                    finish_reason = reason
                delta = choices[0].get("delta") or {}
                content = delta.get("content")
                reasoning = delta.get("reasoning_content")
                tool_calls = delta.get("tool_calls")
                if content is None and reasoning is None and not tool_calls:
                    continue
                if content:
                    content_parts.append(str(content))
                if reasoning:
                    reasoning_parts.append(str(reasoning))
                    if not thinking_signaled:
                        thinking_signaled = True
                        self._publish_ephemeral(
                            run_id, EVENT_MODEL_STATUS, {"phase": "thinking"}
                        )
                if tool_calls:
                    accumulator.add(list(tool_calls))
                self._publish_ephemeral(
                    run_id,
                    EVENT_MODEL_DELTA,
                    {
                        k: v
                        for k, v in (
                            ("content", content),
                            ("reasoning_content", reasoning),
                            ("tool_calls", tool_calls),
                        )
                        if v is not None
                    },
                )
            usage = stream.usage or ProviderUsage()
        finally:
            await stream.aclose()

        tool_calls_out = accumulator.result()
        text = "".join(content_parts)
        message: dict[str, Any] = {
            "role": "assistant",
            "content": text if text else None,
        }
        if reasoning_parts:
            message["reasoning_content"] = "".join(reasoning_parts)
        if tool_calls_out:
            message["tool_calls"] = tool_calls_out
        return _RoundResult(
            message=message,
            finish_reason=finish_reason,
            usage=self._usage_dict(usage),
            tool_calls=tool_calls_out,
            text=text,
        )

    def _publish_ephemeral(self, run_id: str, type_: str, payload: dict[str, Any]) -> None:
        self.hub.publish(
            EventRecord(run_id=run_id, type=type_, payload=payload, ts=0.0, seq=None)
        )

    # ---- internal request construction --------------------------------------

    def _internal_request(
        self, run: RunRecord, transcript: list[dict[str, Any]]
    ) -> RespondRequest:
        return build_internal_request(
            context=run.context if isinstance(run.context, dict) else {},
            transcript=transcript,
            session_id=run.session_id or run.run_id,
            turn_id=run.current_turn_id or "turn",
            round_no=run.current_round,
        )

    @staticmethod
    def _usage_dict(usage: ProviderUsage | None) -> dict[str, int]:
        u = usage or ProviderUsage()
        return {
            "prompt_tokens": u.prompt_tokens or 0,
            "completion_tokens": u.completion_tokens or 0,
            "total_tokens": u.total_tokens or 0,
            "cached_tokens": u.cached_tokens or 0,
        }

    # ---- skill learning (same contract as v1, sourced from durable state) ----

    def _maybe_learn(self, request: RespondRequest, run: RunRecord, text: str) -> None:
        if self.learner is None or not self.learner.enabled or not text.strip():
            return
        trailing = [ChatMessage(role="assistant", content=text)]
        active_ids = self.catalog.filter_known(run.active_skills)
        task = asyncio.create_task(self._learn_background(request, active_ids, trailing))
        self._background_tasks.add(task)
        task.add_done_callback(self._background_tasks.discard)

    async def _learn_background(
        self,
        request: RespondRequest,
        active_ids: list[str],
        trailing: list[ChatMessage],
    ) -> None:
        assert self.learner is not None
        try:
            await self.learner.run(
                request=request, active_ids=active_ids, trailing_messages=trailing
            )
        except Exception as exc:  # noqa: BLE001 - detached task must swallow everything
            logger.warning("run_skill_learning_failed", error=str(exc))
