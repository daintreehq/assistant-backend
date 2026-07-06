"""The v2 durable run protocol endpoints (``/v2/runs`` and friends).

The backend owns the turn loop; these routes are the thin HTTP surface over
the RunSupervisor and RunStore: submit turns, tail the event log over SSE,
claim/fulfil tool leases, resolve approvals, heartbeat executors, schedule
wakeups, and replay rounds. See ``docs/RUN_SUPERVISOR.md``.
"""

from __future__ import annotations

import asyncio
import json
import time
from collections.abc import AsyncIterator
from typing import Any

from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse, StreamingResponse

from ..auth import Principal, require_auth
from ..contracts.runs_api import (
    ApprovalResolveRequest,
    ApprovalView,
    CancelRequest,
    HeartbeatRequest,
    HeartbeatResponse,
    LeaseClaimRequest,
    LeaseClaimResponse,
    LeaseResultRequest,
    LeaseResultResponse,
    LeaseView,
    RunCreateRequest,
    RunView,
    TurnAccepted,
    TurnSubmitRequest,
    WakeupCreateRequest,
    WakeupView,
)
from ..errors import DaintreeError, invalid_request
from ..runs.hub import EventHub
from ..runs.models import (
    EVENT_STREAM_RESET,
    EventRecord,
    LeaseRecord,
    RunRecord,
    WakeupRecord,
    new_wakeup_id,
)
from ..runs.replay import RoundReplayer
from ..runs.store import RunStore
from ..runs.supervisor import RunSupervisor
from ..services.validation import validate_turn_submission
from .deps import require_ready

router = APIRouter()


def _not_found(what: str) -> DaintreeError:
    return DaintreeError(
        status_code=404, message=f"{what} not found.", code="not_found"
    )


def _conflict(message: str, code: str) -> DaintreeError:
    return DaintreeError(status_code=409, message=message, code=code)


def _store(request: Request) -> RunStore:
    store: RunStore = request.app.state.container.run_store
    return store


def _supervisor(request: Request) -> RunSupervisor:
    supervisor: RunSupervisor = request.app.state.container.run_supervisor
    return supervisor


def _hub(request: Request) -> EventHub:
    hub: EventHub = request.app.state.container.event_hub
    return hub


def _run_view(run: RunRecord, *, last_seq: int, executor_present: bool) -> dict[str, Any]:
    return RunView(
        run_id=run.run_id,
        status=run.status,
        session_id=run.session_id,
        project_id=run.project_id,
        title=run.title,
        active_skills=list(run.active_skills),
        current_round=run.current_round,
        current_turn_id=run.current_turn_id,
        last_seq=last_seq,
        executor_present=executor_present,
        last_error=run.last_error,
        created_at=run.created_at,
        updated_at=run.updated_at,
    ).model_dump(exclude_none=True)


def _lease_view(lease: LeaseRecord) -> LeaseView:
    return LeaseView(
        lease_id=lease.lease_id,
        run_id=lease.run_id,
        round=lease.round,
        tool_call_id=lease.tool_call_id,
        tool_name=lease.tool_name,
        arguments=lease.arguments,
        risk=lease.risk,
        status=lease.status,
        requires_approval=lease.requires_approval,
        approval_id=lease.approval_id,
        executor_id=lease.executor_id,
        attempt=lease.attempt,
        claim_expires_at=lease.claim_expires_at,
        deadline=lease.deadline,
        idempotency_key=lease.idempotency_key,
        result_status=lease.result_status,
        created_at=lease.created_at,
        updated_at=lease.updated_at,
    )


def _wakeup_view(wakeup: WakeupRecord) -> dict[str, Any]:
    return WakeupView(
        wakeup_id=wakeup.wakeup_id,
        run_id=wakeup.run_id,
        fire_at=wakeup.fire_at,
        note=wakeup.note,
        status=wakeup.status,
        created_at=wakeup.created_at,
        fired_at=wakeup.fired_at,
    ).model_dump(exclude_none=True)


async def _submit_turn(
    request: Request, run_id: str, body: TurnSubmitRequest
) -> dict[str, Any]:
    settings = request.app.state.settings
    validate_turn_submission(body.messages, body.tools, settings)
    supervisor = _supervisor(request)
    outcome = await supervisor.submit_turn(
        run_id=run_id,
        turn_id=body.turn_id,
        source="user",
        messages=[m.model_dump(exclude_none=True) for m in body.messages],
        runtime=body.runtime.model_dump(exclude_none=True) if body.runtime else None,
        turn=body.turn.model_dump(exclude_none=True) if body.turn else None,
        tools=[t.model_dump(exclude_none=True) for t in body.tools] if body.tools else None,
        generation=body.generation.model_dump(exclude_none=True) if body.generation else None,
        selection_policy=body.selection.policy,
        selection_force=body.selection.force,
    )
    if outcome.kind == "not_found":
        raise _not_found("Run")
    if outcome.kind == "busy":
        raise _conflict(
            "A turn is already in flight for this run; cancel it first.", "run_busy"
        )
    if outcome.kind == "turn_conflict":
        raise _conflict(
            "This turn_id was already submitted with a different payload.", "turn_conflict"
        )
    status = outcome.run.status if outcome.run is not None else "running"
    return TurnAccepted(
        run_id=run_id,
        turn_id=body.turn_id,
        status=status,
        seq=outcome.seq,
        duplicate=outcome.kind == "duplicate",
    ).model_dump(exclude_none=True)


# --------------------------------------------------------------------------
# Runs.
# --------------------------------------------------------------------------


@router.post("/v2/runs")
async def create_run(
    body: RunCreateRequest,
    request: Request,
    _principal: Principal = Depends(require_auth),
    _ready: None = Depends(require_ready),
) -> JSONResponse:
    # Validate the first turn BEFORE creating the run, so a rejected submission
    # leaves no orphaned idle run behind.
    validate_turn_submission(body.turn.messages, body.turn.tools, request.app.state.settings)
    supervisor = _supervisor(request)
    created = await supervisor.create_run(
        session_id=body.session_id, project_id=body.project_id, title=body.title
    )
    assert created.run is not None
    run_id = created.run.run_id
    turn = await _submit_turn(request, run_id, body.turn)
    store = _store(request)
    run = await store.get_run(run_id)
    assert run is not None
    last_seq = await store.last_seq(run_id)
    present = bool(await store.alive_executors(now=time.time()))
    return JSONResponse(
        status_code=201,
        content={
            "run": _run_view(run, last_seq=last_seq, executor_present=present),
            "turn": turn,
        },
    )


@router.get("/v2/runs")
async def list_runs(
    request: Request,
    limit: int = 50,
    _principal: Principal = Depends(require_auth),
    _ready: None = Depends(require_ready),
) -> JSONResponse:
    store = _store(request)
    limit = max(1, min(int(limit), 500))
    runs = await store.list_runs(limit=limit)
    present = bool(await store.alive_executors(now=time.time()))
    out = []
    for run in runs:
        last_seq = await store.last_seq(run.run_id)
        out.append(_run_view(run, last_seq=last_seq, executor_present=present))
    return JSONResponse(content={"runs": out})


@router.get("/v2/runs/{run_id}")
async def get_run(
    run_id: str,
    request: Request,
    _principal: Principal = Depends(require_auth),
    _ready: None = Depends(require_ready),
) -> JSONResponse:
    store = _store(request)
    run = await store.get_run(run_id)
    if run is None:
        raise _not_found("Run")
    last_seq = await store.last_seq(run_id)
    present = bool(await store.alive_executors(now=time.time()))
    return JSONResponse(
        content={"run": _run_view(run, last_seq=last_seq, executor_present=present)}
    )


@router.post("/v2/runs/{run_id}/turns")
async def submit_turn(
    run_id: str,
    body: TurnSubmitRequest,
    request: Request,
    _principal: Principal = Depends(require_auth),
    _ready: None = Depends(require_ready),
) -> JSONResponse:
    return JSONResponse(content=await _submit_turn(request, run_id, body))


@router.post("/v2/runs/{run_id}/cancel")
async def cancel_run_turn(
    run_id: str,
    request: Request,
    body: CancelRequest | None = None,
    _principal: Principal = Depends(require_auth),
    _ready: None = Depends(require_ready),
) -> JSONResponse:
    supervisor = _supervisor(request)
    reason = (body.reason if body else None) or "client_cancelled"
    outcome = await supervisor.cancel(run_id, reason=reason)
    if outcome.kind == "not_found":
        raise _not_found("Run")
    if outcome.kind == "not_busy":
        raise _conflict("No turn is in flight for this run.", "run_not_busy")
    return JSONResponse(content={"run_id": run_id, "status": "idle"})


# --------------------------------------------------------------------------
# Event stream (SSE).
# --------------------------------------------------------------------------


def _sse_event(event: EventRecord) -> str:
    data = json.dumps(
        {"seq": event.seq, "ts": event.ts, "payload": event.payload},
        separators=(",", ":"),
        ensure_ascii=False,
    )
    id_line = f"id: {event.seq}\n" if event.seq is not None else ""
    return f"{id_line}event: {event.type}\ndata: {data}\n\n"


@router.get("/v2/runs/{run_id}/events")
async def run_events(
    run_id: str,
    request: Request,
    after: int | None = None,
    live: bool = True,
    _principal: Principal = Depends(require_auth),
    _ready: None = Depends(require_ready),
) -> StreamingResponse:
    store = _store(request)
    hub = _hub(request)
    settings = request.app.state.settings
    run = await store.get_run(run_id)
    if run is None:
        raise _not_found("Run")

    cursor = after
    if cursor is None:
        header = request.headers.get("last-event-id")
        if header is not None:
            try:
                cursor = int(header)
            except ValueError:
                cursor = None
    start_after = max(0, cursor or 0)
    heartbeat = settings.runs_sse_heartbeat_seconds

    async def stream() -> AsyncIterator[str]:
        # Subscribe BEFORE the replay query so no committed event can fall in
        # the gap; duplicates across the boundary are filtered by seq.
        sub = hub.subscribe(run_id)
        try:
            last = start_after
            replayed = await store.events_after(run_id, last)
            for event in replayed:
                yield _sse_event(event)
                if event.seq is not None:
                    last = event.seq
            if not live:
                return
            while True:
                if sub.lagged:
                    yield _sse_event(
                        EventRecord(
                            run_id=run_id,
                            type=EVENT_STREAM_RESET,
                            payload={"reason": "subscriber_lagged", "last_seq": last},
                            ts=time.time(),
                        )
                    )
                    return
                try:
                    item = await asyncio.wait_for(sub.queue.get(), timeout=heartbeat)
                except TimeoutError:
                    yield ": ping\n\n"
                    continue
                if item.seq is not None and item.seq <= last:
                    continue
                yield _sse_event(item)
                if item.seq is not None:
                    last = item.seq
        finally:
            hub.unsubscribe(sub)

    return StreamingResponse(
        stream(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


# --------------------------------------------------------------------------
# Leases.
# --------------------------------------------------------------------------


@router.get("/v2/runs/{run_id}/leases")
async def run_leases(
    run_id: str,
    request: Request,
    status: str | None = None,
    _principal: Principal = Depends(require_auth),
    _ready: None = Depends(require_ready),
) -> JSONResponse:
    store = _store(request)
    if await store.get_run(run_id) is None:
        raise _not_found("Run")
    leases = await store.list_leases(run_id, status=status)
    return JSONResponse(
        content={"leases": [_lease_view(x).model_dump(exclude_none=True) for x in leases]}
    )


@router.get("/v2/tool-leases")
async def pending_leases(
    request: Request,
    limit: int = 100,
    _principal: Principal = Depends(require_auth),
    _ready: None = Depends(require_ready),
) -> JSONResponse:
    store = _store(request)
    leases = await store.pending_leases(limit=max(1, min(int(limit), 500)))
    return JSONResponse(
        content={"leases": [_lease_view(x).model_dump(exclude_none=True) for x in leases]}
    )


@router.get("/v2/tool-leases/{lease_id}")
async def get_lease(
    lease_id: str,
    request: Request,
    _principal: Principal = Depends(require_auth),
    _ready: None = Depends(require_ready),
) -> JSONResponse:
    lease = await _store(request).get_lease(lease_id)
    if lease is None:
        raise _not_found("Lease")
    return JSONResponse(content={"lease": _lease_view(lease).model_dump(exclude_none=True)})


@router.post("/v2/tool-leases/{lease_id}/claim")
async def claim_lease(
    lease_id: str,
    body: LeaseClaimRequest,
    request: Request,
    _principal: Principal = Depends(require_auth),
    _ready: None = Depends(require_ready),
) -> JSONResponse:
    store = _store(request)
    settings = request.app.state.settings
    outcome = await store.claim_lease(
        lease_id,
        executor_id=body.executor_id,
        claim_ttl_seconds=settings.runs_lease_claim_ttl_seconds,
    )
    if outcome.kind == "not_found":
        raise _not_found("Lease")
    if outcome.kind == "conflict":
        raise _conflict("Lease is claimed by another executor.", "lease_claimed")
    if outcome.kind == "awaiting_approval":
        raise _conflict("Lease is awaiting approval.", "lease_awaiting_approval")
    if outcome.kind == "terminal":
        raise _conflict("Lease already reached a terminal state.", "lease_terminal")
    _hub(request).publish_all(outcome.events)
    assert outcome.lease is not None
    return JSONResponse(
        content=LeaseClaimResponse(
            outcome=outcome.kind, lease=_lease_view(outcome.lease)
        ).model_dump(exclude_none=True)
    )


@router.post("/v2/tool-leases/{lease_id}/result")
async def lease_result(
    lease_id: str,
    body: LeaseResultRequest,
    request: Request,
    _principal: Principal = Depends(require_auth),
    _ready: None = Depends(require_ready),
) -> JSONResponse:
    store = _store(request)
    outcome = await store.record_lease_result(
        lease_id,
        executor_id=body.executor_id,
        status=body.status,
        content=body.content,
    )
    if outcome.kind == "not_found":
        raise _not_found("Lease")
    if outcome.kind == "conflict":
        raise _conflict(
            "A different result is already recorded for this lease (or another "
            "executor holds the claim).",
            "result_conflict",
        )
    if outcome.kind == "awaiting_approval":
        raise _conflict("Lease is awaiting approval.", "lease_awaiting_approval")
    _hub(request).publish_all(outcome.events)
    assert outcome.lease is not None
    if outcome.kind == "recorded":
        await _supervisor(request).on_lease_resolution(outcome.lease.run_id)
    return JSONResponse(
        content=LeaseResultResponse(
            outcome=outcome.kind, lease=_lease_view(outcome.lease)
        ).model_dump(exclude_none=True)
    )


# --------------------------------------------------------------------------
# Approvals.
# --------------------------------------------------------------------------


@router.post("/v2/approvals/{approval_id}")
async def resolve_approval(
    approval_id: str,
    body: ApprovalResolveRequest,
    request: Request,
    _principal: Principal = Depends(require_auth),
    _ready: None = Depends(require_ready),
) -> JSONResponse:
    store = _store(request)
    outcome = await store.resolve_approval(
        approval_id, approve=body.approve, note=body.note
    )
    if outcome.kind == "not_found":
        raise _not_found("Approval")
    if outcome.kind == "already":
        raise _conflict("Approval is already resolved.", "approval_resolved")
    _hub(request).publish_all(outcome.events)
    assert outcome.approval is not None
    # A decline may have completed the waiting round (synthesized result).
    await _supervisor(request).on_lease_resolution(outcome.approval.run_id)
    view = ApprovalView(
        approval_id=outcome.approval.approval_id,
        run_id=outcome.approval.run_id,
        lease_id=outcome.approval.lease_id,
        status=outcome.approval.status,
        reason=outcome.approval.reason,
        note=outcome.approval.note,
        requested_at=outcome.approval.requested_at,
        resolved_at=outcome.approval.resolved_at,
    )
    return JSONResponse(content={"approval": view.model_dump(exclude_none=True)})


# --------------------------------------------------------------------------
# Executors.
# --------------------------------------------------------------------------


@router.post("/v2/executors/heartbeat")
async def executor_heartbeat(
    body: HeartbeatRequest,
    request: Request,
    _principal: Principal = Depends(require_auth),
    _ready: None = Depends(require_ready),
) -> JSONResponse:
    store = _store(request)
    record = await store.heartbeat(
        executor_id=body.executor_id,
        project_id=body.project_id,
        client=body.client.model_dump(exclude_none=True) if body.client else {},
        tools=[t.model_dump(exclude_none=True) for t in body.tools],
        capabilities=body.capabilities,
        ttl_seconds=body.ttl_seconds,
    )
    pending = await store.pending_leases()
    return JSONResponse(
        content=HeartbeatResponse(
            executor_id=record.executor_id,
            expires_at=record.last_seen + record.ttl_seconds,
            pending_leases=[_lease_view(x) for x in pending],
        ).model_dump(exclude_none=True)
    )


# --------------------------------------------------------------------------
# Wakeups.
# --------------------------------------------------------------------------


@router.post("/v2/runs/{run_id}/wakeups")
async def schedule_wakeup(
    run_id: str,
    body: WakeupCreateRequest,
    request: Request,
    _principal: Principal = Depends(require_auth),
    _ready: None = Depends(require_ready),
) -> JSONResponse:
    if (body.delay_seconds is None) == (body.fire_at is None):
        raise invalid_request(
            "Exactly one of delay_seconds or fire_at is required.",
            param="delay_seconds",
            code="invalid_wakeup",
        )
    fire_at = body.fire_at if body.fire_at is not None else time.time() + (
        body.delay_seconds or 0
    )
    store = _store(request)
    wakeup_id = new_wakeup_id()
    outcome = await store.schedule_wakeup(
        wakeup_id=wakeup_id, run_id=run_id, fire_at=fire_at, note=body.note
    )
    if outcome.kind == "not_found":
        raise _not_found("Run")
    _hub(request).publish_all(outcome.events)
    return JSONResponse(
        status_code=201,
        content={"wakeup": {"wakeup_id": wakeup_id, "run_id": run_id, "fire_at": fire_at}},
    )


@router.get("/v2/runs/{run_id}/wakeups")
async def list_wakeups(
    run_id: str,
    request: Request,
    _principal: Principal = Depends(require_auth),
    _ready: None = Depends(require_ready),
) -> JSONResponse:
    store = _store(request)
    if await store.get_run(run_id) is None:
        raise _not_found("Run")
    wakeups = await store.list_wakeups(run_id)
    return JSONResponse(content={"wakeups": [_wakeup_view(w) for w in wakeups]})


@router.delete("/v2/runs/{run_id}/wakeups/{wakeup_id}")
async def cancel_wakeup(
    run_id: str,
    wakeup_id: str,
    request: Request,
    _principal: Principal = Depends(require_auth),
    _ready: None = Depends(require_ready),
) -> JSONResponse:
    store = _store(request)
    outcome = await store.cancel_wakeup(wakeup_id)
    if outcome.kind == "not_found":
        raise _not_found("Wakeup")
    if outcome.kind == "already":
        raise _conflict("Wakeup already fired or was cancelled.", "wakeup_resolved")
    _hub(request).publish_all(outcome.events)
    return JSONResponse(content={"wakeup_id": wakeup_id, "status": "cancelled"})


# --------------------------------------------------------------------------
# Replay.
# --------------------------------------------------------------------------


@router.get("/v2/runs/{run_id}/rounds/{round_no}/replay")
async def replay_round(
    run_id: str,
    round_no: int,
    request: Request,
    include_request: bool = False,
    _principal: Principal = Depends(require_auth),
    _ready: None = Depends(require_ready),
) -> JSONResponse:
    replayer: RoundReplayer = request.app.state.container.run_replayer
    if await _store(request).get_run(run_id) is None:
        raise _not_found("Run")
    report = await replayer.replay(run_id, round_no)
    if not report.found:
        raise _not_found("Round")
    return JSONResponse(content=report.to_dict(include_request=include_request))
