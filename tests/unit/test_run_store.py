"""RunStore: event log, projections, and the idempotency fences."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from daintree_assistant_server.runs.models import LeaseDraft
from daintree_assistant_server.runs.store import RunStore, build_transcript


class FakeClock:
    def __init__(self, start: float = 1_700_000_000.0) -> None:
        self.now = start

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


@pytest.fixture
def clock() -> FakeClock:
    return FakeClock()


@pytest.fixture
async def store(tmp_path: Path, clock: FakeClock) -> Any:
    s = RunStore(tmp_path / "runs.sqlite3", clock=clock)
    s.open()
    yield s
    await s.aclose()


async def make_run(store: RunStore, run_id: str = "run-1") -> str:
    outcome = await store.create_run(
        run_id=run_id, session_id="sess", project_id="proj", title=None
    )
    assert outcome.kind == "created"
    return run_id


async def submit(store: RunStore, run_id: str, turn_id: str = "t1") -> Any:
    return await store.submit_turn(
        run_id=run_id,
        turn_id=turn_id,
        source="user",
        messages=[{"role": "user", "content": "hello"}],
        context_update={"runtime": None, "turn": None, "tools": None, "generation": None},
        selection_policy="new_instruction",
        selection_force=False,
        title_hint="hello",
    )


async def assistant_with_tools(
    store: RunStore, run_id: str, round_no: int = 1, *, requires_approval: bool = False
) -> Any:
    return await store.record_assistant_message(
        run_id=run_id,
        round_no=round_no,
        turn_id="t1",
        message={
            "role": "assistant",
            "content": None,
            "tool_calls": [
                {
                    "id": "call_1",
                    "type": "function",
                    "function": {"name": "fs__read", "arguments": "{}"},
                }
            ],
        },
        finish_reason="tool_calls",
        usage={"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2, "cached_tokens": 0},
        leases=[
            LeaseDraft(
                tool_call_id="call_1",
                tool_name="fs__read",
                arguments="{}",
                risk="read",
                requires_approval=requires_approval,
            )
        ],
        lease_deadline_seconds=3600,
    )


# ---- run + turn lifecycle -------------------------------------------------


async def test_create_run_is_idempotent_on_id(store: RunStore) -> None:
    await make_run(store)
    duplicate = await store.create_run(
        run_id="run-1", session_id=None, project_id=None, title=None
    )
    assert duplicate.kind == "exists"


async def test_submit_turn_moves_run_to_running_and_appends_events(store: RunStore) -> None:
    await make_run(store)
    outcome = await submit(store, "run-1")
    assert outcome.kind == "accepted"
    assert outcome.run is not None and outcome.run.status == "running"
    assert outcome.run.needs_selection is True
    types = [e.type for e in outcome.events]
    assert types == ["turn.submitted", "run.status"]


async def test_submit_turn_identical_resubmission_is_duplicate(store: RunStore) -> None:
    await make_run(store)
    await submit(store, "run-1")
    again = await submit(store, "run-1")
    assert again.kind == "duplicate"
    assert again.seq is not None


async def test_submit_turn_same_id_different_payload_conflicts(store: RunStore) -> None:
    await make_run(store)
    await submit(store, "run-1")
    conflicting = await store.submit_turn(
        run_id="run-1",
        turn_id="t1",
        source="user",
        messages=[{"role": "user", "content": "DIFFERENT"}],
        context_update={"runtime": None, "turn": None, "tools": None, "generation": None},
        selection_policy="new_instruction",
        selection_force=False,
        title_hint=None,
    )
    assert conflicting.kind == "turn_conflict"


async def test_submit_turn_while_busy_is_rejected(store: RunStore) -> None:
    await make_run(store)
    await submit(store, "run-1")
    second = await submit(store, "run-1", turn_id="t2")
    assert second.kind == "busy"


async def test_final_answer_closes_turn_atomically(store: RunStore) -> None:
    await make_run(store)
    await submit(store, "run-1")
    outcome = await store.record_assistant_message(
        run_id="run-1",
        round_no=1,
        turn_id="t1",
        message={"role": "assistant", "content": "done"},
        finish_reason="stop",
        usage={"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2, "cached_tokens": 0},
        leases=[],
        lease_deadline_seconds=3600,
    )
    assert outcome.kind == "final"
    run = await store.get_run("run-1")
    assert run is not None and run.status == "idle"
    types = [e.type for e in outcome.events]
    assert types == ["assistant.message", "turn.completed", "run.status"]


# ---- lease fences ----------------------------------------------------------


async def test_lease_claim_conflict_and_takeover_after_ttl(
    store: RunStore, clock: FakeClock
) -> None:
    await make_run(store)
    await submit(store, "run-1")
    await assistant_with_tools(store, "run-1")
    (lease,) = await store.list_leases("run-1")
    first = await store.claim_lease(lease.lease_id, executor_id="e1", claim_ttl_seconds=60)
    assert first.kind == "claimed"
    # A different executor cannot take a live claim.
    other = await store.claim_lease(lease.lease_id, executor_id="e2", claim_ttl_seconds=60)
    assert other.kind == "conflict"
    # The claim holder re-claiming extends, without a new claim event.
    extend = await store.claim_lease(lease.lease_id, executor_id="e1", claim_ttl_seconds=60)
    assert extend.kind == "extended" and extend.events == []
    # After the TTL lapses anyone may take over; the attempt counter increments.
    clock.advance(120)
    takeover = await store.claim_lease(lease.lease_id, executor_id="e2", claim_ttl_seconds=60)
    assert takeover.kind == "claimed"
    assert takeover.lease is not None and takeover.lease.attempt == 2
    types = [e.type for e in takeover.events]
    assert types == ["lease.released", "lease.claimed"]


async def test_lease_result_exactly_once(store: RunStore) -> None:
    await make_run(store)
    await submit(store, "run-1")
    await assistant_with_tools(store, "run-1")
    (lease,) = await store.list_leases("run-1")

    recorded = await store.record_lease_result(
        lease.lease_id, executor_id="e1", status="ok", content="file contents"
    )
    assert recorded.kind == "recorded"
    assert recorded.lease is not None and recorded.lease.status == "completed"

    duplicate = await store.record_lease_result(
        lease.lease_id, executor_id="e1", status="ok", content="file contents"
    )
    assert duplicate.kind == "duplicate" and duplicate.events == []

    conflicting = await store.record_lease_result(
        lease.lease_id, executor_id="e1", status="ok", content="DIFFERENT"
    )
    assert conflicting.kind == "conflict"

    # Exactly one tool.result event exists.
    events = await store.all_events("run-1")
    assert sum(1 for e in events if e.type == "tool.result") == 1


async def test_result_from_other_executor_with_live_claim_conflicts(store: RunStore) -> None:
    await make_run(store)
    await submit(store, "run-1")
    await assistant_with_tools(store, "run-1")
    (lease,) = await store.list_leases("run-1")
    await store.claim_lease(lease.lease_id, executor_id="e1", claim_ttl_seconds=600)
    outcome = await store.record_lease_result(
        lease.lease_id, executor_id="e2", status="ok", content="x"
    )
    assert outcome.kind == "conflict"


async def test_continuation_cas_fires_once(store: RunStore) -> None:
    await make_run(store)
    await submit(store, "run-1")
    await assistant_with_tools(store, "run-1")
    (lease,) = await store.list_leases("run-1")
    await store.record_lease_result(lease.lease_id, executor_id="e1", status="ok", content="x")
    assert await store.all_round_leases_terminal("run-1", 1)
    first = await store.try_begin_continuation("run-1")
    second = await store.try_begin_continuation("run-1")
    assert first.kind == "continuing"
    assert second.kind == "no"


# ---- approvals ---------------------------------------------------------------


async def test_approval_decline_synthesizes_result_and_unparks_run(store: RunStore) -> None:
    await make_run(store)
    await submit(store, "run-1")
    outcome = await assistant_with_tools(store, "run-1", requires_approval=True)
    run = await store.get_run("run-1")
    assert run is not None and run.status == "awaiting_approval"
    (lease,) = await store.list_leases("run-1")
    assert lease.status == "awaiting_approval" and lease.approval_id

    resolved = await store.resolve_approval(lease.approval_id, approve=False, note="no")
    assert resolved.kind == "declined"
    types = [e.type for e in resolved.events]
    assert "tool.result" in types and "run.status" in types
    refreshed = await store.get_lease(lease.lease_id)
    assert refreshed is not None and refreshed.status == "declined"
    run = await store.get_run("run-1")
    assert run is not None and run.status == "awaiting_tools"
    # The synthesized result keeps every tool call answered.
    assert await store.all_round_leases_terminal("run-1", 1)
    # Resolution is one-shot.
    again = await store.resolve_approval(lease.approval_id, approve=True, note=None)
    assert again.kind == "already"
    assert outcome.kind == "tools"


async def test_approval_approve_releases_lease_to_pending(store: RunStore) -> None:
    await make_run(store)
    await submit(store, "run-1")
    await assistant_with_tools(store, "run-1", requires_approval=True)
    (lease,) = await store.list_leases("run-1")
    assert lease.approval_id is not None
    resolved = await store.resolve_approval(lease.approval_id, approve=True, note=None)
    assert resolved.kind == "approved"
    refreshed = await store.get_lease(lease.lease_id)
    assert refreshed is not None and refreshed.status == "pending"


# ---- expiry sweeps -------------------------------------------------------------


async def test_lease_deadline_expiry_synthesizes_result(
    store: RunStore, clock: FakeClock
) -> None:
    await make_run(store)
    await submit(store, "run-1")
    await assistant_with_tools(store, "run-1")
    clock.advance(4000)
    outcomes = await store.expire_due_leases(now=clock())
    kinds = [o.kind for o in outcomes]
    assert "expired" in kinds
    (lease,) = await store.list_leases("run-1")
    assert lease.status == "expired"
    assert await store.all_round_leases_terminal("run-1", 1)


async def test_expiry_racing_late_result_keeps_single_outcome(
    store: RunStore, clock: FakeClock
) -> None:
    """Expiry wins → a late executor post must not rewrite the synthesized
    result (and vice versa: an earlier real result makes expiry a no-op)."""
    await make_run(store)
    await submit(store, "run-1")
    await assistant_with_tools(store, "run-1")
    clock.advance(4000)
    outcomes = await store.expire_due_leases(now=clock())
    assert any(o.kind == "expired" for o in outcomes)

    (lease,) = await store.list_leases("run-1")
    late = await store.record_lease_result(
        lease.lease_id, executor_id="e", status="ok", content="real work output"
    )
    assert late.kind == "conflict"
    events = await store.all_events("run-1")
    assert sum(1 for e in events if e.type == "tool.result") == 1

    # Converse order on a second run: a recorded result makes expiry a no-op.
    await make_run(store, "run-2")
    await submit(store, "run-2")
    await assistant_with_tools(store, "run-2")
    (lease2,) = await store.list_leases("run-2")
    await store.record_lease_result(
        lease2.lease_id, executor_id="e", status="ok", content="done"
    )
    clock.advance(4000)
    outcomes = await store.expire_due_leases(now=clock())
    assert not any(
        o.lease is not None and o.lease.run_id == "run-2" for o in outcomes
    )
    events = await store.all_events("run-2")
    assert sum(1 for e in events if e.type == "tool.result") == 1


async def test_expired_approval_lease_appends_approval_resolved(
    store: RunStore, clock: FakeClock
) -> None:
    await make_run(store)
    await submit(store, "run-1")
    await assistant_with_tools(store, "run-1", requires_approval=True)
    clock.advance(4000)
    await store.expire_due_leases(now=clock())
    events = await store.all_events("run-1")
    resolved = [e for e in events if e.type == "approval.resolved"]
    assert len(resolved) == 1 and resolved[0].payload["approved"] is False


async def test_claim_release_sweep(store: RunStore, clock: FakeClock) -> None:
    await make_run(store)
    await submit(store, "run-1")
    await assistant_with_tools(store, "run-1")
    (lease,) = await store.list_leases("run-1")
    await store.claim_lease(lease.lease_id, executor_id="e1", claim_ttl_seconds=30)
    clock.advance(60)
    outcomes = await store.release_expired_claims(now=clock())
    assert [o.kind for o in outcomes] == ["released"]
    refreshed = await store.get_lease(lease.lease_id)
    assert refreshed is not None and refreshed.status == "pending"


# ---- cancellation ----------------------------------------------------------------


async def test_cancel_turn_synthesizes_results_for_open_leases(store: RunStore) -> None:
    await make_run(store)
    await submit(store, "run-1")
    await assistant_with_tools(store, "run-1")
    outcome = await store.cancel_turn("run-1", reason="user")
    assert outcome.kind == "cancelled"
    (lease,) = await store.list_leases("run-1")
    assert lease.status == "cancelled"
    run = await store.get_run("run-1")
    assert run is not None and run.status == "idle"
    # Transcript still answers the tool call (provider replay invariant).
    transcript = await store.transcript("run-1")
    roles = [m["role"] for m in transcript]
    assert roles == ["user", "assistant", "tool"]


async def test_cancel_without_inflight_turn(store: RunStore) -> None:
    await make_run(store)
    outcome = await store.cancel_turn("run-1", reason="user")
    assert outcome.kind == "not_busy"


# ---- wakeups -----------------------------------------------------------------------


async def test_wakeup_cas_fires_once(store: RunStore, clock: FakeClock) -> None:
    await make_run(store)
    scheduled = await store.schedule_wakeup(
        wakeup_id="w1", run_id="run-1", fire_at=clock() + 10, note="check PR"
    )
    assert scheduled.kind == "scheduled"
    assert await store.due_wakeups(now=clock()) == []
    clock.advance(11)
    due = await store.due_wakeups(now=clock())
    assert [w.wakeup_id for w in due] == ["w1"]
    first = await store.claim_wakeup("w1")
    second = await store.claim_wakeup("w1")
    assert first.kind == "fired" and second.kind == "no"


async def test_wakeup_cancel(store: RunStore, clock: FakeClock) -> None:
    await make_run(store)
    await store.schedule_wakeup(wakeup_id="w1", run_id="run-1", fire_at=clock() + 5, note=None)
    cancelled = await store.cancel_wakeup("w1")
    assert cancelled.kind == "cancelled"
    clock.advance(10)
    assert await store.due_wakeups(now=clock()) == []


# ---- transcript projection -----------------------------------------------------------


def test_build_transcript_orders_tool_results_by_call_order() -> None:
    events: list[tuple[str, dict[str, Any]]] = [
        ("turn.submitted", {"messages": [{"role": "user", "content": "go"}]}),
        (
            "assistant.message",
            {
                "round": 1,
                "message": {
                    "role": "assistant",
                    "content": None,
                    "tool_calls": [
                        {"id": "a", "type": "function", "function": {"name": "x", "arguments": ""}},
                        {"id": "b", "type": "function", "function": {"name": "y", "arguments": ""}},
                    ],
                },
            },
        ),
        # Results arrive in REVERSE order; the transcript must not care.
        ("tool.result", {"round": 1, "tool_call_id": "b", "content": "B"}),
        ("tool.result", {"round": 1, "tool_call_id": "a", "content": "A"}),
    ]
    messages = build_transcript(events)
    assert [m.get("tool_call_id") for m in messages if m["role"] == "tool"] == ["a", "b"]
    assert [m["content"] for m in messages if m["role"] == "tool"] == ["A", "B"]


async def test_events_after_cursor(store: RunStore) -> None:
    await make_run(store)
    outcome = await submit(store, "run-1")
    assert outcome.seq is not None
    tail = await store.events_after("run-1", outcome.seq)
    # Only the run.status event follows the turn.submitted seq.
    assert [e.type for e in tail] == ["run.status"]
    assert await store.last_seq("run-1") >= outcome.seq
