"""Round replay: reconstruct the exact upstream request for any recorded round.

Every ``round.started`` event carries a prompt fingerprint. The replayer
re-derives the request purely from the event log (transcript, per-turn
context, active skill set) plus the current catalog and assembler settings,
recomputes the fingerprint, and reports whether it matches — with an explicit
reason when it cannot (catalog drift, missing events). This is the
regression-replay primitive: a green replay proves the stored events fully
determine what the model saw.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from ..config import DEEPSEEK_MODEL, Settings
from ..services.prompt_assembler import PromptAssembler
from ..skills.catalog import Catalog, CatalogManager
from .models import (
    EVENT_ASSISTANT_MESSAGE,
    EVENT_ROUND_STARTED,
    EVENT_SELECTION_COMPLETED,
    EVENT_TOOL_RESULT,
    EVENT_TURN_SUBMITTED,
    EventRecord,
)
from .store import RunStore, build_transcript
from .supervisor import build_internal_request, prompt_fingerprint


@dataclass(frozen=True)
class ReplayReport:
    found: bool
    match: bool = False
    round: int = 0
    recorded_fingerprint: str | None = None
    computed_fingerprint: str | None = None
    mismatch_reason: str | None = None
    catalog_revision_recorded: str | None = None
    catalog_revision_current: str | None = None
    request: dict[str, Any] | None = None

    def to_dict(self, *, include_request: bool) -> dict[str, Any]:
        out: dict[str, Any] = {
            "found": self.found,
            "match": self.match,
            "round": self.round,
            "recorded_fingerprint": self.recorded_fingerprint,
            "computed_fingerprint": self.computed_fingerprint,
            "mismatch_reason": self.mismatch_reason,
            "catalog_revision_recorded": self.catalog_revision_recorded,
            "catalog_revision_current": self.catalog_revision_current,
        }
        if include_request:
            out["request"] = self.request
        return {k: v for k, v in out.items() if v is not None or k in ("found", "match")}


@dataclass
class _ReplayState:
    context: dict[str, Any] = field(default_factory=dict)
    active_skills: list[str] = field(default_factory=list)
    turn_id: str = "turn"
    conversation: list[tuple[str, dict[str, Any]]] = field(default_factory=list)


class RoundReplayer:
    def __init__(
        self,
        *,
        store: RunStore,
        catalog_manager: CatalogManager,
        assembler: PromptAssembler,
        settings: Settings,
    ) -> None:
        self.store = store
        self.catalog_manager = catalog_manager
        self.assembler = assembler
        self.settings = settings

    @property
    def catalog(self) -> Catalog:
        return self.catalog_manager.catalog

    async def replay(self, run_id: str, round_no: int) -> ReplayReport:
        events = await self.store.all_events(run_id)
        target = self._last_round_started(events, round_no)
        if target is None:
            return ReplayReport(found=False, round=round_no)

        state = self._state_before(events, target)
        transcript = build_transcript(state.conversation)
        if not transcript:
            return ReplayReport(
                found=True,
                round=round_no,
                recorded_fingerprint=str(target.payload.get("prompt_fingerprint") or ""),
                mismatch_reason="empty_transcript",
            )

        run = await self.store.get_run(run_id)
        session_id = (run.session_id if run else None) or run_id
        internal = build_internal_request(
            context=state.context,
            transcript=transcript,
            session_id=session_id,
            turn_id=state.turn_id,
            round_no=round_no,
        )
        skills = self.catalog.get_many(self.catalog.filter_known(state.active_skills))
        provider_request = self.assembler.build_main_request(
            request=internal, skills=skills, upstream_model=DEEPSEEK_MODEL
        )
        computed = prompt_fingerprint(provider_request)
        recorded = str(target.payload.get("prompt_fingerprint") or "")

        mismatch_reason: str | None = None
        if computed != recorded:
            mismatch_reason = self._diagnose(target, skills)

        return ReplayReport(
            found=True,
            match=computed == recorded,
            round=round_no,
            recorded_fingerprint=recorded,
            computed_fingerprint=computed,
            mismatch_reason=mismatch_reason,
            catalog_revision_recorded=str(target.payload.get("catalog_revision") or ""),
            catalog_revision_current=self.catalog.revision,
            request={
                "model": provider_request.model,
                "messages": provider_request.messages,
                "tools": provider_request.tools,
                "tool_choice": provider_request.tool_choice,
                "temperature": provider_request.temperature,
                "max_tokens": provider_request.max_tokens,
                "response_format": provider_request.response_format,
                "stop": provider_request.stop,
                "thinking_disabled": provider_request.thinking_disabled,
                "reasoning_effort": provider_request.reasoning_effort,
            },
        )

    @staticmethod
    def _last_round_started(events: list[EventRecord], round_no: int) -> EventRecord | None:
        target: EventRecord | None = None
        for event in events:
            if event.type == EVENT_ROUND_STARTED and event.payload.get("round") == round_no:
                target = event
        return target

    @staticmethod
    def _state_before(events: list[EventRecord], target: EventRecord) -> _ReplayState:
        state = _ReplayState()
        boundary = target.seq or 0
        for event in events:
            if (event.seq or 0) >= boundary:
                break
            if event.type == EVENT_TURN_SUBMITTED:
                context = event.payload.get("context")
                if isinstance(context, dict):
                    state.context = context
                turn_id = event.payload.get("turn_id")
                if isinstance(turn_id, str) and turn_id:
                    state.turn_id = turn_id
                state.conversation.append((event.type, event.payload))
            elif event.type == EVENT_SELECTION_COMPLETED:
                active = event.payload.get("active")
                if isinstance(active, list):
                    state.active_skills = [str(a) for a in active]
            elif event.type in (EVENT_ASSISTANT_MESSAGE, EVENT_TOOL_RESULT):
                state.conversation.append((event.type, event.payload))
        return state

    def _diagnose(self, target: EventRecord, skills: list[Any]) -> str:
        recorded_revision = str(target.payload.get("catalog_revision") or "")
        if recorded_revision and recorded_revision != self.catalog.revision:
            recorded_hashes = target.payload.get("skill_hashes") or {}
            current_hashes = {s.id: s.content_hash for s in skills}
            if recorded_hashes != current_hashes:
                return "catalog_drift"
            return "catalog_revision_changed"
        return "assembly_drift"
