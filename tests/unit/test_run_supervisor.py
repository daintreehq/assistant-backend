"""RunSupervisor: the backend-owned loop, recovery, approvals, replay, wakeups."""

from __future__ import annotations

import asyncio
import time
from pathlib import Path
from typing import Any

import pytest
from fakes import FakeProvider, make_text_chunks

from daintree_assistant_server.config import Settings
from daintree_assistant_server.errors import upstream_error
from daintree_assistant_server.prompts.loader import PromptLoader
from daintree_assistant_server.runs.hub import EventHub
from daintree_assistant_server.runs.replay import RoundReplayer
from daintree_assistant_server.runs.scheduler import RunMaintenanceScheduler
from daintree_assistant_server.runs.store import RunStore
from daintree_assistant_server.runs.supervisor import RunSupervisor
from daintree_assistant_server.services.prompt_assembler import PromptAssembler
from daintree_assistant_server.skills.catalog import (
    CatalogManager,
    default_skills_dir,
    load_catalog,
)
from daintree_assistant_server.skills.selector import Selector


def tool_call_chunks(
    name: str = "fs__read", call_id: str = "call_1", arguments: str = '{"path": "x"}'
) -> list[dict[str, Any]]:
    """Streamed tool-call fragments split across chunks (id/name first, args after)."""
    return [
        {
            "choices": [
                {
                    "index": 0,
                    "delta": {
                        "role": "assistant",
                        "tool_calls": [
                            {
                                "index": 0,
                                "id": call_id,
                                "type": "function",
                                "function": {"name": name, "arguments": ""},
                            }
                        ],
                    },
                    "finish_reason": None,
                }
            ]
        },
        {
            "choices": [
                {
                    "index": 0,
                    "delta": {
                        "tool_calls": [{"index": 0, "function": {"arguments": arguments}}]
                    },
                    "finish_reason": None,
                }
            ]
        },
        {
            "choices": [{"index": 0, "delta": {}, "finish_reason": "tool_calls"}],
            "usage": {"prompt_tokens": 10, "completion_tokens": 4, "total_tokens": 14},
        },
    ]


def tools_then_text(provider: FakeProvider) -> None:
    """First round emits a tool call; any round whose transcript already has a
    tool message emits a final text answer."""

    def chunks(request: Any) -> list[dict[str, Any]]:
        has_tool_result = any(m.get("role") == "tool" for m in request.messages)
        if has_tool_result:
            return make_text_chunks("all done")
        return tool_call_chunks()

    provider.stream_chunks = chunks


async def wait_for_status(store: RunStore, run_id: str, statuses: set[str]) -> str:
    deadline = time.monotonic() + 5.0
    while time.monotonic() < deadline:
        run = await store.get_run(run_id)
        if run is not None and run.status in statuses:
            return run.status
        await asyncio.sleep(0.01)
    run = await store.get_run(run_id)
    raise AssertionError(f"run never reached {statuses}; status={run and run.status}")


@pytest.fixture
def hub() -> EventHub:
    return EventHub()


@pytest.fixture
async def store(tmp_path: Path) -> Any:
    s = RunStore(tmp_path / "runs.sqlite3")
    s.open()
    yield s
    await s.aclose()


def build_supervisor(
    settings: Settings,
    store: RunStore,
    hub: EventHub,
    provider: FakeProvider,
) -> RunSupervisor:
    prompt_loader = PromptLoader()
    prompt_loader.validate()
    primary = default_skills_dir()
    catalog_manager = CatalogManager(load_catalog(primary), primary_dir=primary)
    return RunSupervisor(
        settings=settings,
        store=store,
        hub=hub,
        catalog_manager=catalog_manager,
        selector=Selector(provider, prompt_loader, catalog_manager, settings),
        assembler=PromptAssembler(prompt_loader, settings),
        provider=provider,
    )


@pytest.fixture
def fake_provider() -> FakeProvider:
    return FakeProvider()


@pytest.fixture
def supervisor(
    settings: Settings, store: RunStore, hub: EventHub, fake_provider: FakeProvider
) -> RunSupervisor:
    return build_supervisor(settings, store, hub, fake_provider)


async def start_run(
    supervisor: RunSupervisor, *, content: str = "please read x"
) -> str:
    created = await supervisor.create_run(session_id="s", project_id="p", title=None)
    assert created.run is not None
    run_id = created.run.run_id
    outcome = await supervisor.submit_turn(
        run_id=run_id,
        turn_id="t1",
        source="user",
        messages=[{"role": "user", "content": content}],
        runtime=None,
        turn=None,
        tools=[
            {
                "type": "function",
                "function": {"name": "fs__read", "description": "read", "parameters": None},
            }
        ],
        generation=None,
        selection_policy="new_instruction",
        selection_force=False,
    )
    assert outcome.kind == "accepted"
    return run_id


# ---- the loop -------------------------------------------------------------


async def test_text_only_turn_completes(
    supervisor: RunSupervisor, store: RunStore, fake_provider: FakeProvider
) -> None:
    fake_provider.stream_chunks = make_text_chunks("hi there")
    run_id = await start_run(supervisor)
    assert await wait_for_status(store, run_id, {"idle"}) == "idle"
    events = await store.all_events(run_id)
    types = [e.type for e in events]
    assert "selection.completed" in types
    assert "round.started" in types
    assert "assistant.message" in types
    assert "turn.completed" in types
    transcript = await store.transcript(run_id)
    assert transcript[-1]["role"] == "assistant"
    assert transcript[-1]["content"] == "hi there "


async def test_tool_round_creates_lease_then_continues_to_final(
    supervisor: RunSupervisor, store: RunStore, fake_provider: FakeProvider
) -> None:
    tools_then_text(fake_provider)
    run_id = await start_run(supervisor)
    await wait_for_status(store, run_id, {"awaiting_tools"})

    (lease,) = await store.list_leases(run_id)
    assert lease.status == "pending"
    assert lease.tool_name == "fs__read"
    assert lease.arguments == '{"path": "x"}'

    claim = await store.claim_lease(lease.lease_id, executor_id="cli-1", claim_ttl_seconds=60)
    assert claim.kind == "claimed"
    result = await store.record_lease_result(
        lease.lease_id, executor_id="cli-1", status="ok", content="contents of x"
    )
    assert result.kind == "recorded"
    assert await supervisor.on_lease_resolution(run_id) is True
    await wait_for_status(store, run_id, {"idle"})

    transcript = await store.transcript(run_id)
    roles = [m["role"] for m in transcript]
    assert roles == ["user", "assistant", "tool", "assistant"]
    assert transcript[2]["content"] == "contents of x"
    # The second upstream round saw the tool result in its transcript.
    assert len(fake_provider.stream_requests) == 2
    round2_messages = fake_provider.stream_requests[1].messages
    assert any(m.get("role") == "tool" for m in round2_messages)
    # Selection ran once for the turn, not per round.
    events = await store.all_events(run_id)
    assert sum(1 for e in events if e.type == "selection.completed") == 1


async def test_duplicate_result_does_not_double_continue(
    supervisor: RunSupervisor, store: RunStore, fake_provider: FakeProvider
) -> None:
    tools_then_text(fake_provider)
    run_id = await start_run(supervisor)
    await wait_for_status(store, run_id, {"awaiting_tools"})
    (lease,) = await store.list_leases(run_id)
    await store.record_lease_result(lease.lease_id, executor_id="e", status="ok", content="x")
    first = await supervisor.on_lease_resolution(run_id)
    second = await supervisor.on_lease_resolution(run_id)
    assert first is True
    assert second is False  # the CAS already fired; the duplicate must be a no-op
    await wait_for_status(store, run_id, {"idle"})
    assert len(fake_provider.stream_requests) == 2
    events = await store.all_events(run_id)
    assert sum(1 for e in events if e.type == "assistant.message") == 2
    continuations = [
        e
        for e in events
        if e.type == "run.status" and e.payload.get("reason") == "tool_results_complete"
    ]
    assert len(continuations) == 1


async def test_concurrent_result_posts_record_once_and_continue_once(
    supervisor: RunSupervisor, store: RunStore, fake_provider: FakeProvider
) -> None:
    """Racing identical result posts (client retries in flight simultaneously)
    must record one result and drive exactly one continuation."""
    tools_then_text(fake_provider)
    run_id = await start_run(supervisor)
    await wait_for_status(store, run_id, {"awaiting_tools"})
    (lease,) = await store.list_leases(run_id)

    posts = await asyncio.gather(
        *(
            store.record_lease_result(
                lease.lease_id, executor_id="e", status="ok", content="same"
            )
            for _ in range(5)
        )
    )
    kinds = sorted(p.kind for p in posts)
    assert kinds == ["duplicate"] * 4 + ["recorded"]

    # A conflicting late post never rewrites the recorded result.
    conflict = await store.record_lease_result(
        lease.lease_id, executor_id="e2", status="ok", content="different"
    )
    assert conflict.kind == "conflict"

    continuations = await asyncio.gather(
        *(supervisor.on_lease_resolution(run_id) for _ in range(5))
    )
    assert sum(1 for c in continuations if c) == 1
    await wait_for_status(store, run_id, {"idle"})
    assert len(fake_provider.stream_requests) == 2
    events = await store.all_events(run_id)
    assert sum(1 for e in events if e.type == "tool.result") == 1


async def test_round_failure_after_retries_parks_run_failed(
    supervisor: RunSupervisor, store: RunStore, fake_provider: FakeProvider
) -> None:
    fake_provider.stream_error = upstream_error("boom", retryable=False)
    run_id = await start_run(supervisor)
    await wait_for_status(store, run_id, {"failed"})
    events = await store.all_events(run_id)
    assert any(e.type == "round.failed" for e in events)
    run = await store.get_run(run_id)
    assert run is not None and run.last_error is not None


async def test_retryable_midstream_failure_retries_whole_round(
    supervisor: RunSupervisor, store: RunStore, fake_provider: FakeProvider
) -> None:
    calls = {"n": 0}

    def factory(request: Any) -> Any:
        from fakes import FakeStream

        calls["n"] += 1
        if calls["n"] == 1:
            chunks = make_text_chunks("partial answer")
            return FakeStream(chunks, error_at=2, error=upstream_error("mid", retryable=True))
        return FakeStream(make_text_chunks("clean answer"))

    fake_provider.stream_factory = factory
    run_id = await start_run(supervisor)
    await wait_for_status(store, run_id, {"idle"})
    transcript = await store.transcript(run_id)
    assert transcript[-1]["content"] == "clean answer "
    assert calls["n"] == 2
    # Exactly one durable assistant message: the partial stream left no trace.
    events = await store.all_events(run_id)
    assert sum(1 for e in events if e.type == "assistant.message") == 1


async def test_failed_run_accepts_new_turn_and_recovers(
    supervisor: RunSupervisor, store: RunStore, fake_provider: FakeProvider
) -> None:
    fake_provider.stream_error = upstream_error("boom", retryable=False)
    run_id = await start_run(supervisor)
    await wait_for_status(store, run_id, {"failed"})

    fake_provider.stream_error = None
    fake_provider.stream_chunks = make_text_chunks("recovered")
    outcome = await supervisor.submit_turn(
        run_id=run_id,
        turn_id="t2",
        source="user",
        messages=[{"role": "user", "content": "try again"}],
        runtime=None,
        turn=None,
        tools=None,
        generation=None,
        selection_policy="new_instruction",
        selection_force=False,
    )
    assert outcome.kind == "accepted"
    await wait_for_status(store, run_id, {"idle"})
    run = await store.get_run(run_id)
    assert run is not None and run.last_error is None
    transcript = await store.transcript(run_id)
    assert transcript[-1]["content"] == "recovered "


async def test_cancel_inflight_turn(
    supervisor: RunSupervisor, store: RunStore, fake_provider: FakeProvider
) -> None:
    tools_then_text(fake_provider)
    run_id = await start_run(supervisor)
    await wait_for_status(store, run_id, {"awaiting_tools"})
    outcome = await supervisor.cancel(run_id)
    assert outcome.kind == "cancelled"
    run = await store.get_run(run_id)
    assert run is not None and run.status == "idle"
    (lease,) = await store.list_leases(run_id)
    assert lease.status == "cancelled"


# ---- restart / reconnect ---------------------------------------------------


async def test_restart_recovery_continues_after_results_arrived_while_down(
    settings: Settings, store: RunStore, fake_provider: FakeProvider
) -> None:
    """CLI closed + server restarted: results recorded, no driver alive. A fresh
    supervisor's recover() must finish the turn from durable state alone."""
    hub1 = EventHub()
    supervisor1 = build_supervisor(settings, store, hub1, fake_provider)
    tools_then_text(fake_provider)
    run_id = await start_run(supervisor1)
    await wait_for_status(store, run_id, {"awaiting_tools"})
    await supervisor1.aclose()  # "process death" — driver gone, state durable

    # Result arrives (e.g. recorded before the crash, or via another instance).
    (lease,) = await store.list_leases(run_id)
    await store.record_lease_result(lease.lease_id, executor_id="e", status="ok", content="x")

    supervisor2 = build_supervisor(settings, store, EventHub(), fake_provider)
    await supervisor2.recover()
    await wait_for_status(store, run_id, {"idle"})
    transcript = await store.transcript(run_id)
    assert transcript[-1]["role"] == "assistant"
    await supervisor2.aclose()


async def test_restart_recovery_redrives_crashed_round(
    settings: Settings, store: RunStore, fake_provider: FakeProvider
) -> None:
    """Crash between round.started and the assistant message: the round is
    re-driven (model calls are side-effect free), not stuck."""
    fake_provider.stream_chunks = make_text_chunks("recovered answer")
    supervisor = build_supervisor(settings, store, EventHub(), fake_provider)
    created = await supervisor.create_run(session_id=None, project_id=None, title=None)
    assert created.run is not None
    run_id = created.run.run_id
    # Simulate the pre-crash persisted state by hand: turn accepted, selection
    # done, round started — then the process died before the model returned.
    await store.submit_turn(
        run_id=run_id,
        turn_id="t1",
        source="user",
        messages=[{"role": "user", "content": "hi"}],
        context_update={"runtime": None, "turn": None, "tools": None, "generation": None},
        selection_policy="new_instruction",
        selection_force=False,
        title_hint=None,
    )
    await store.record_selection(run_id, active_skills=[], payload={"active": []})
    await store.record_round_started(
        run_id, 1, {"round": 1, "prompt_fingerprint": "pre-crash", "catalog_revision": "r"}
    )
    await supervisor.recover()
    await wait_for_status(store, run_id, {"idle"})
    transcript = await store.transcript(run_id)
    assert transcript[-1]["content"] == "recovered answer "
    await supervisor.aclose()


# ---- wake turns + approval policy --------------------------------------------


async def test_wake_turn_parks_mutating_tool_behind_approval(
    supervisor: RunSupervisor, store: RunStore, fake_provider: FakeProvider
) -> None:
    # An executor advertised tool risks earlier (heartbeat).
    await store.heartbeat(
        executor_id="cli-1",
        project_id="p",
        client={},
        tools=[{"name": "git__push", "risk": "git"}, {"name": "fs__read", "risk": "read"}],
        capabilities={},
        ttl_seconds=3600,
    )

    def chunks(request: Any) -> list[dict[str, Any]]:
        if any(m.get("role") == "tool" for m in request.messages):
            return make_text_chunks("done")
        return [
            {
                "choices": [
                    {
                        "index": 0,
                        "delta": {
                            "role": "assistant",
                            "tool_calls": [
                                {
                                    "index": 0,
                                    "id": "c1",
                                    "type": "function",
                                    "function": {"name": "git__push", "arguments": "{}"},
                                },
                                {
                                    "index": 1,
                                    "id": "c2",
                                    "type": "function",
                                    "function": {"name": "fs__read", "arguments": "{}"},
                                },
                            ],
                        },
                        "finish_reason": None,
                    }
                ]
            },
            {"choices": [{"index": 0, "delta": {}, "finish_reason": "tool_calls"}]},
        ]

    fake_provider.stream_chunks = chunks

    created = await supervisor.create_run(session_id="s", project_id="p", title=None)
    assert created.run is not None
    run_id = created.run.run_id
    outcome = await supervisor.submit_wake_turn(run_id=run_id, turn_id="wake-1", note="PR check")
    assert outcome.kind == "accepted"
    await wait_for_status(store, run_id, {"awaiting_approval"})

    leases = {x.tool_name: x for x in await store.list_leases(run_id)}
    assert leases["git__push"].status == "awaiting_approval"
    assert leases["git__push"].requires_approval is True
    assert leases["fs__read"].status == "pending"
    assert leases["fs__read"].requires_approval is False

    # Decline the mutation; fulfil the read; the turn completes.
    approval_id = leases["git__push"].approval_id
    assert approval_id is not None
    await store.resolve_approval(approval_id, approve=False, note="not now")
    await supervisor.on_lease_resolution(run_id)
    await store.record_lease_result(
        leases["fs__read"].lease_id, executor_id="cli-1", status="ok", content="data"
    )
    await supervisor.on_lease_resolution(run_id)
    await wait_for_status(store, run_id, {"idle"})
    transcript = await store.transcript(run_id)
    tool_msgs = [m for m in transcript if m["role"] == "tool"]
    assert len(tool_msgs) == 2  # declined call answered synthetically


async def test_interactive_turn_never_requires_server_approval(
    supervisor: RunSupervisor, store: RunStore, fake_provider: FakeProvider
) -> None:
    await store.heartbeat(
        executor_id="cli-1",
        project_id="p",
        client={},
        tools=[{"name": "fs__read", "risk": "git"}],  # even a scary risk class
        capabilities={},
        ttl_seconds=3600,
    )
    tools_then_text(fake_provider)
    run_id = await start_run(supervisor)
    await wait_for_status(store, run_id, {"awaiting_tools"})
    (lease,) = await store.list_leases(run_id)
    assert lease.requires_approval is False


# ---- replay ---------------------------------------------------------------------


async def test_replay_reproduces_exact_prompts_for_every_round(
    supervisor: RunSupervisor,
    store: RunStore,
    fake_provider: FakeProvider,
    settings: Settings,
) -> None:
    tools_then_text(fake_provider)
    run_id = await start_run(supervisor)
    await wait_for_status(store, run_id, {"awaiting_tools"})
    (lease,) = await store.list_leases(run_id)
    await store.record_lease_result(lease.lease_id, executor_id="e", status="ok", content="x")
    await supervisor.on_lease_resolution(run_id)
    await wait_for_status(store, run_id, {"idle"})

    replayer = RoundReplayer(
        store=store,
        catalog_manager=supervisor.catalog_manager,
        assembler=supervisor.assembler,
        settings=settings,
    )
    for round_no in (1, 2):
        report = await replayer.replay(run_id, round_no)
        assert report.found, round_no
        assert report.match, (round_no, report.mismatch_reason)
        # The reconstructed request is byte-identical to what the provider saw.
        assert report.request is not None
        assert report.request["messages"] == fake_provider.stream_requests[round_no - 1].messages

    missing = await replayer.replay(run_id, 99)
    assert missing.found is False


# ---- scheduler --------------------------------------------------------------------


async def test_scheduler_fires_due_wakeup_and_run_executes(
    supervisor: RunSupervisor,
    store: RunStore,
    hub: EventHub,
    fake_provider: FakeProvider,
    settings: Settings,
) -> None:
    fake_provider.stream_chunks = make_text_chunks("wake handled")
    created = await supervisor.create_run(session_id=None, project_id=None, title=None)
    assert created.run is not None
    run_id = created.run.run_id
    await store.schedule_wakeup(
        wakeup_id="w1", run_id=run_id, fire_at=time.time() - 1, note="poll the PR"
    )
    scheduler = RunMaintenanceScheduler(
        settings=settings, store=store, supervisor=supervisor, hub=hub
    )
    await scheduler.tick()
    await wait_for_status(store, run_id, {"idle"})
    events = await store.all_events(run_id)
    types = [e.type for e in events]
    assert "wake.fired" in types
    assert "turn.completed" in types
    transcript = await store.transcript(run_id)
    assert "poll the PR" in transcript[0]["content"]
    # CAS: a second tick cannot double-fire.
    await scheduler.tick()
    assert sum(1 for e in await store.all_events(run_id) if e.type == "wake.fired") == 1


async def test_scheduler_defers_wake_while_run_busy(
    supervisor: RunSupervisor,
    store: RunStore,
    hub: EventHub,
    fake_provider: FakeProvider,
    settings: Settings,
) -> None:
    """A due wake firing mid-turn must not submit a turn; the SAME wakeup row is
    pushed back to scheduled (no replacement proliferation)."""
    tools_then_text(fake_provider)
    run_id = await start_run(supervisor)
    await wait_for_status(store, run_id, {"awaiting_tools"})
    await store.schedule_wakeup(
        wakeup_id="w1", run_id=run_id, fire_at=time.time() - 1, note="busy wake"
    )
    scheduler = RunMaintenanceScheduler(
        settings=settings, store=store, supervisor=supervisor, hub=hub
    )
    await scheduler.tick()

    run = await store.get_run(run_id)
    assert run is not None and run.status == "awaiting_tools"
    events = await store.all_events(run_id)
    assert sum(1 for e in events if e.type == "turn.submitted") == 1  # only t1
    (wakeup,) = await store.list_wakeups(run_id)
    assert wakeup.status == "scheduled"  # deferred back, same row
    assert wakeup.fire_at > time.time()
    deferrals = [
        e for e in events if e.type == "wake.scheduled" and e.payload.get("deferred")
    ]
    assert len(deferrals) == 1


async def test_scheduler_resubmits_wake_lost_in_crash_window(
    supervisor: RunSupervisor,
    store: RunStore,
    hub: EventHub,
    fake_provider: FakeProvider,
    settings: Settings,
) -> None:
    """Crash between the fired-CAS and the wake-turn submission: the sweep must
    retry the (idempotent) submission rather than drop the wake forever."""
    fake_provider.stream_chunks = make_text_chunks("wake handled")
    created = await supervisor.create_run(session_id=None, project_id=None, title=None)
    assert created.run is not None
    run_id = created.run.run_id
    await store.schedule_wakeup(
        wakeup_id="w1", run_id=run_id, fire_at=time.time() - 1, note="lost wake"
    )
    claimed = await store.claim_wakeup("w1")
    assert claimed.kind == "fired"  # ...and then the process "died" here

    scheduler = RunMaintenanceScheduler(
        settings=settings, store=store, supervisor=supervisor, hub=hub
    )
    await scheduler.tick()
    await wait_for_status(store, run_id, {"idle"})
    events = await store.all_events(run_id)
    submitted = [e for e in events if e.type == "turn.submitted"]
    assert len(submitted) == 1
    assert submitted[0].payload.get("turn_id") == "wake-w1"
    # Once the turn exists, later sweeps are no-ops.
    await scheduler.tick()
    events = await store.all_events(run_id)
    assert sum(1 for e in events if e.type == "turn.submitted") == 1


async def test_scheduler_expires_overdue_lease_and_continues_run(
    store: RunStore,
    hub: EventHub,
    fake_provider: FakeProvider,
    tmp_path: Path,
) -> None:
    # A dedicated settings with the minimum lease deadline, driven by a shifted clock.
    settings = Settings(
        DAINTREE_API_KEY="k",
        DEEPSEEK_API_KEY="k",
        STATE_SIGNING_KEY_CURRENT="k",
        RETRY_BACKOFF_BASE_SECONDS=0.0,
        RUNS_DB_PATH=str(tmp_path / "unused.sqlite3"),
        RUNS_LEASE_DEADLINE_SECONDS=10.0,
    )
    supervisor = build_supervisor(settings, store, hub, fake_provider)
    tools_then_text(fake_provider)
    run_id = await start_run(supervisor)
    await wait_for_status(store, run_id, {"awaiting_tools"})

    scheduler = RunMaintenanceScheduler(
        settings=settings,
        store=store,
        supervisor=supervisor,
        hub=hub,
        clock=lambda: time.time() + 60,  # one minute later
    )
    await scheduler.tick()
    await wait_for_status(store, run_id, {"idle"})
    (lease,) = await store.list_leases(run_id)
    assert lease.status == "expired"
    transcript = await store.transcript(run_id)
    tool_msg = next(m for m in transcript if m["role"] == "tool")
    assert "expired" in tool_msg["content"]
    await supervisor.aclose()
