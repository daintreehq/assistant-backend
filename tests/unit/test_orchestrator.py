"""Respond orchestrator unit tests (merge + selector cadence)."""

from __future__ import annotations

import asyncio
import json
from collections.abc import Callable
from typing import Any

import pytest
from fakes import FakeProvider, FakeStream, make_text_chunks, selection_result

from daintree_assistant_server.contracts.daintree_api import RespondRequest
from daintree_assistant_server.errors import upstream_error
from daintree_assistant_server.providers.base import ProviderStream
from daintree_assistant_server.services import chat_orchestrator as orchestrator_module
from daintree_assistant_server.services.chat_orchestrator import (
    ChatOrchestrator,
    _SpeculativeStream,
    merge_active_skills,
    resolve_active_skills,
)
from daintree_assistant_server.skills.selector import SelectionDecision


def test_merge_additive_new_first(catalog) -> None:
    ids = sorted(catalog.known_ids)
    active, newly = merge_active_skills([ids[2]], [ids[0], ids[1]], catalog, 3)
    assert active == [ids[2], ids[0], ids[1]]
    assert newly == [ids[2]]


def test_merge_caps_at_three(catalog) -> None:
    ids = sorted(catalog.known_ids)
    active, newly = merge_active_skills([ids[3]], [ids[0], ids[1], ids[2]], catalog, 3)
    assert len(active) == 3
    assert active[0] == ids[3]
    assert ids[2] not in active  # oldest dropped


def test_merge_empty_selection_keeps_prior(catalog) -> None:
    ids = sorted(catalog.known_ids)
    active, newly = merge_active_skills([], [ids[0]], catalog, 3)
    assert active == [ids[0]]
    assert newly == []


def test_merge_drops_unknown(catalog) -> None:
    ids = sorted(catalog.known_ids)
    active, newly = merge_active_skills(["nope", ids[0]], [], catalog, 3)
    assert active == [ids[0]]


def test_merge_no_duplicate_load(catalog) -> None:
    ids = sorted(catalog.known_ids)
    active, newly = merge_active_skills([ids[0]], [ids[0]], catalog, 3)
    assert active == [ids[0]]
    assert newly == []


# ---- resolve_active_skills: per-turn authoritative selection ----------------


def test_resolve_successful_run_replaces_prior(catalog) -> None:
    # A successful selector run is authoritative: prior skills not re-listed are evicted.
    ids = sorted(catalog.known_ids)
    decision = SelectionDecision(skill_ids=[ids[1]], ran=True, degraded=False)
    active, newly = resolve_active_skills(decision, [ids[0]], catalog, 3)
    assert active == [ids[1]]
    assert newly == [ids[1]]


def test_resolve_empty_successful_run_clears_prior(catalog) -> None:
    # An empty result on a real run CLEARS the active set (topic shift to no-task).
    ids = sorted(catalog.known_ids)
    decision = SelectionDecision(skill_ids=[], ran=True, degraded=False)
    active, newly = resolve_active_skills(decision, [ids[0]], catalog, 3)
    assert active == []
    assert newly == []


def test_resolve_degraded_keeps_prior(catalog) -> None:
    # A selector that failed open must NOT evict — reuse the prior set.
    ids = sorted(catalog.known_ids)
    decision = SelectionDecision.degraded_open(["selector_failed"])
    active, newly = resolve_active_skills(decision, [ids[0]], catalog, 3)
    assert active == [ids[0]]
    assert newly == []


def test_resolve_no_run_reuses_prior(catalog) -> None:
    # A tool-continuation round does not run the selector → reuse decision → keep prior.
    ids = sorted(catalog.known_ids)
    decision = SelectionDecision.reuse([ids[0], ids[1]])
    active, newly = resolve_active_skills(decision, [ids[0], ids[1]], catalog, 3)
    assert active == [ids[0], ids[1]]
    assert newly == []


def test_resolve_keeps_active_when_relisted(catalog) -> None:
    # Re-listing an active skill keeps it; a genuinely new one loads alongside.
    ids = sorted(catalog.known_ids)
    decision = SelectionDecision(skill_ids=[ids[0], ids[1]], ran=True, degraded=False)
    active, newly = resolve_active_skills(decision, [ids[0]], catalog, 3)
    assert active == [ids[0], ids[1]]
    assert newly == [ids[1]]


def test_resolve_caps_and_drops_unknown(catalog) -> None:
    ids = sorted(catalog.known_ids)
    decision = SelectionDecision(
        skill_ids=["nope", ids[0], ids[1], ids[2], ids[3]], ran=True, degraded=False
    )
    active, _ = resolve_active_skills(decision, [], catalog, 3)
    assert active == [ids[0], ids[1], ids[2]]


def _delta_chunk(delta: dict) -> dict:
    return {"choices": [{"index": 0, "delta": delta, "finish_reason": None}]}


def test_chunk_to_delta_surfaces_reasoning_only() -> None:
    # A thinking-phase chunk carries only reasoning_content; it must still emit a
    # delta (the old guard dropped any chunk without content/tool_calls).
    delta = ChatOrchestrator._chunk_to_delta(_delta_chunk({"reasoning_content": "cot"}))
    assert delta is not None
    assert delta.reasoning_content == "cot"
    assert delta.content is None


def test_chunk_to_delta_surfaces_content_and_reasoning() -> None:
    delta = ChatOrchestrator._chunk_to_delta(
        _delta_chunk({"content": "hi", "reasoning_content": "cot"})
    )
    assert delta is not None
    assert delta.content == "hi"
    assert delta.reasoning_content == "cot"


def test_chunk_to_delta_empty_delta_is_none() -> None:
    assert ChatOrchestrator._chunk_to_delta(_delta_chunk({})) is None


@pytest.mark.asyncio
async def test_stream_meta_is_yielded_before_upstream_open(
    orchestrator: ChatOrchestrator, fake_provider: FakeProvider
) -> None:
    skill_id = sorted(orchestrator.catalog.known_ids)[0]
    fake_provider.selection_result = selection_result([skill_id])
    request = RespondRequest.model_validate(
        {
            "protocol_version": 2,
            "session": {"id": "stream-session", "turn_id": "turn-1"},
            "input": {"messages": [{"role": "user", "content": "hello"}]},
            "startup": {},
            "generation": {"stream": True},
        }
    )

    outcome = await orchestrator.complete(request, upstream_model="deepseek-v4-flash")
    assert outcome.stream is not None
    assert fake_provider.stream_requests == []

    first = await anext(outcome.stream)
    assert first.startswith("event: meta\n")
    meta = json.loads(first.split("data: ", 1)[1])
    assert [skill["id"] for skill in meta["skills"]["newly_loaded"]] == [skill_id]
    # The generator suspends at the metadata yield. Opening and prefetching the
    # provider cannot begin until the client asks for the next SSE event.
    assert fake_provider.stream_requests == []

    second = await anext(outcome.stream)
    assert second.startswith("event: delta\n")
    assert len(fake_provider.stream_requests) == 1
    await outcome.stream.aclose()
    assert fake_provider.last_stream is not None
    assert fake_provider.last_stream.closed is True


# ---- selector/main-stream speculation ---------------------------------------


def _stream_request_with_state(
    orchestrator: ChatOrchestrator,
    active_ids: list[str],
    *,
    session_id: str = "spec-session",
    prior_turn_id: str = "turn-0",
    turn_id: str = "turn-1",
    catalog_revision: str | None = None,
    state: str | None = None,
) -> RespondRequest:
    if state is None:
        state = orchestrator.state_codec.issue(
            session_id=session_id,
            active_skills=active_ids,
            catalog_revision=catalog_revision or orchestrator.catalog.revision,
            turn_id=prior_turn_id,
            instruction_revision=0,
        )
    return RespondRequest.model_validate(
        {
            "protocol_version": 2,
            "session": {"id": session_id, "turn_id": turn_id},
            "input": {"messages": [{"role": "user", "content": "next instruction"}]},
            "startup": {},
            "generation": {"stream": True},
            "state": state,
        }
    )


def _gate_selector(
    monkeypatch: pytest.MonkeyPatch,
    orchestrator: ChatOrchestrator,
    decision: SelectionDecision,
) -> tuple[asyncio.Event, asyncio.Event]:
    started = asyncio.Event()
    release = asyncio.Event()

    async def select(*args: object, **kwargs: object) -> SelectionDecision:
        started.set()
        await release.wait()
        return decision

    monkeypatch.setattr(orchestrator.selector, "select", select)
    return started, release


async def _wait_until(predicate: Callable[[], bool], attempts: int = 100) -> None:
    for _ in range(attempts):
        if predicate():
            return
        await asyncio.sleep(0)
    raise AssertionError("condition did not become true")


async def _drain(stream: object) -> list[str]:
    events: list[str] = []
    async for event in stream:  # type: ignore[attr-defined]
        events.append(event)
    return events


def _streamed_content(events: list[str]) -> str:
    out: list[str] = []
    for event in events:
        if not event.startswith("event: delta\n"):
            continue
        data = json.loads(event.split("data: ", 1)[1])
        out.append(data.get("content", ""))
    return "".join(out)


@pytest.mark.asyncio
async def test_speculation_starts_during_selector_and_exact_match_reuses_once(
    orchestrator: ChatOrchestrator,
    fake_provider: FakeProvider,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    skill_id = sorted(orchestrator.catalog.known_ids)[0]
    started, release = _gate_selector(
        monkeypatch,
        orchestrator,
        SelectionDecision(skill_ids=[skill_id], ran=True, degraded=False),
    )
    streams: list[FakeStream] = []

    def stream_factory(_request: object) -> FakeStream:
        stream = FakeStream(make_text_chunks("speculative winner"))
        streams.append(stream)
        return stream

    fake_provider.stream_factory = stream_factory  # type: ignore[assignment]
    request = _stream_request_with_state(orchestrator, [skill_id])
    original_build = orchestrator.assembler.build_main_request
    assembled: list[object] = []

    def tracked_build(**kwargs: Any) -> Any:
        built = original_build(**kwargs)
        assembled.append(built)
        return built

    monkeypatch.setattr(orchestrator.assembler, "build_main_request", tracked_build)

    pending = asyncio.create_task(
        orchestrator.complete(request, upstream_model="deepseek-v4-flash")
    )
    await started.wait()
    await _wait_until(lambda: len(fake_provider.stream_requests) == 1)
    # The response scaffold may already be public (stream-first): the guarantee
    # that matters is byte-level — no meta/delta can precede authoritative
    # selection, asserted on the drained stream below.

    release.set()
    outcome = await pending
    assert outcome.stream is not None
    assert len(fake_provider.stream_requests) == 1
    # A committed-but-unclaimed buffer still occupies overlap capacity.
    assert orchestrator._skill_speculations_in_flight == 1

    events = await _drain(outcome.stream)
    assert events[0].startswith("event: meta\n")
    assert events[-1].startswith("event: done\n")
    assert _streamed_content(events) == "speculative winner "
    assert len(assembled) == 1  # no duplicate 100+ KiB assembly/deep comparison
    assert fake_provider.stream_requests[0] is assembled[0]
    assert len(fake_provider.stream_requests) == 1  # winner consumed exactly once
    assert streams[0].closed is True
    assert orchestrator._skill_speculations_in_flight == 0


@pytest.mark.asyncio
async def test_degraded_selector_reuses_speculative_prior_stream(
    orchestrator: ChatOrchestrator,
    fake_provider: FakeProvider,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    skill_id = sorted(orchestrator.catalog.known_ids)[0]
    started, release = _gate_selector(
        monkeypatch,
        orchestrator,
        SelectionDecision.degraded_open(["skill_selector_failed_open"]),
    )
    request = _stream_request_with_state(orchestrator, [skill_id])
    pending = asyncio.create_task(
        orchestrator.complete(request, upstream_model="deepseek-v4-flash")
    )
    await started.wait()
    await _wait_until(lambda: len(fake_provider.stream_requests) == 1)
    release.set()
    outcome = await pending
    assert outcome.stream is not None
    events = await _drain(outcome.stream)

    assert len(fake_provider.stream_requests) == 1
    meta = json.loads(events[0].split("data: ", 1)[1])
    assert meta["skills"]["selector"]["degraded"] is True
    assert [s["id"] for s in meta["skills"]["active"]] == [skill_id]


@pytest.mark.asyncio
@pytest.mark.parametrize("selection", ["empty", "reordered", "additional"])
async def test_changed_selection_discards_private_stream_and_opens_canonical(
    selection: str,
    orchestrator: ChatOrchestrator,
    fake_provider: FakeProvider,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    ids = sorted(orchestrator.catalog.known_ids)
    prior = ids[:2]
    selected = {
        "empty": [],
        "reordered": list(reversed(prior)),
        "additional": [prior[0], ids[2]],
    }[selection]
    started, release = _gate_selector(
        monkeypatch,
        orchestrator,
        SelectionDecision(skill_ids=selected, ran=True, degraded=False),
    )
    streams: list[FakeStream] = []

    def stream_factory(_request: object) -> FakeStream:
        label = "private loser" if not streams else "canonical winner"
        chunks = make_text_chunks(label)
        chunks[-1]["usage"] = {
            "prompt_tokens": 900 if not streams else 12,
            "completion_tokens": 90 if not streams else 8,
            "total_tokens": 990 if not streams else 20,
        }
        stream = FakeStream(chunks)
        streams.append(stream)
        return stream

    fake_provider.stream_factory = stream_factory  # type: ignore[assignment]
    pending = asyncio.create_task(
        orchestrator.complete(
            _stream_request_with_state(orchestrator, prior),
            upstream_model="deepseek-v4-flash",
        )
    )
    await started.wait()
    await _wait_until(lambda: len(streams) == 1)
    release.set()
    outcome = await pending
    assert outcome.stream is not None

    # Prep (and the losing stream's discard) settles concurrently with the
    # stream-first scaffold; wait for it rather than assuming it beat ``pending``.
    await _wait_until(lambda: orchestrator._skill_speculations_in_flight == 0)
    assert streams[0].closed is True
    assert len(fake_provider.stream_requests) == 1
    first = await anext(outcome.stream)
    assert first.startswith("event: meta\n")
    assert len(fake_provider.stream_requests) == 1  # canonical still starts after meta
    remaining = await _drain(outcome.stream)
    events = [first, *remaining]

    assert len(fake_provider.stream_requests) == 2
    assert _streamed_content(events) == "canonical winner "
    assert "private loser" not in "".join(events)
    done = json.loads(events[-1].split("data: ", 1)[1])
    assert done["usage"]["total_tokens"] == 20  # winner usage only


@pytest.mark.asyncio
@pytest.mark.parametrize("state_kind", ["empty", "stale", "invalid"])
async def test_empty_invalid_and_stale_state_never_speculates(
    state_kind: str,
    orchestrator: ChatOrchestrator,
    fake_provider: FakeProvider,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    skill_id = sorted(orchestrator.catalog.known_ids)[0]
    if state_kind == "empty":
        request = _stream_request_with_state(orchestrator, [])
    elif state_kind == "stale":
        request = _stream_request_with_state(
            orchestrator, [skill_id], catalog_revision="sha256:stale"
        )
    else:
        request = _stream_request_with_state(orchestrator, [], state="not-a-state-token")
    started, release = _gate_selector(
        monkeypatch,
        orchestrator,
        SelectionDecision(skill_ids=[], ran=True, degraded=False),
    )

    pending = asyncio.create_task(
        orchestrator.complete(request, upstream_model="deepseek-v4-flash")
    )
    await started.wait()
    await asyncio.sleep(0)
    assert fake_provider.stream_requests == []
    release.set()
    outcome = await pending
    assert outcome.stream is not None
    first = await anext(outcome.stream)
    assert first.startswith("event: meta\n")
    assert fake_provider.stream_requests == []
    await outcome.stream.aclose()


@pytest.mark.asyncio
async def test_continuation_round_with_active_skill_does_not_speculate(
    orchestrator: ChatOrchestrator,
    fake_provider: FakeProvider,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    skill_id = sorted(orchestrator.catalog.known_ids)[0]
    request = _stream_request_with_state(
        orchestrator,
        [skill_id],
        prior_turn_id="same-turn",
        turn_id="same-turn",
    )

    async def unexpected_select(*args: object, **kwargs: object) -> SelectionDecision:
        raise AssertionError("selector must not run on a continuation round")

    monkeypatch.setattr(orchestrator.selector, "select", unexpected_select)
    outcome = await orchestrator.complete(request, upstream_model="deepseek-v4-flash")
    assert outcome.stream is not None
    first = await anext(outcome.stream)
    assert first.startswith("event: meta\n")
    assert fake_provider.stream_requests == []
    await outcome.stream.aclose()


@pytest.mark.asyncio
async def test_private_midstream_failure_falls_back_to_fresh_canonical_stream(
    orchestrator: ChatOrchestrator,
    fake_provider: FakeProvider,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    orchestrator.settings = orchestrator.settings.model_copy(
        update={"max_upstream_retries": 0}
    )
    skill_id = sorted(orchestrator.catalog.known_ids)[0]
    started, release = _gate_selector(
        monkeypatch,
        orchestrator,
        SelectionDecision(skill_ids=[skill_id], ran=True, degraded=False),
    )
    streams: list[FakeStream] = []

    def stream_factory(_request: object) -> FakeStream:
        if not streams:
            stream = FakeStream(
                make_text_chunks("private partial"),
                error_at=1,
                error=upstream_error("private stream failed"),
            )
        else:
            stream = FakeStream(make_text_chunks("fallback winner"))
        streams.append(stream)
        return stream

    fake_provider.stream_factory = stream_factory  # type: ignore[assignment]
    pending = asyncio.create_task(
        orchestrator.complete(
            _stream_request_with_state(orchestrator, [skill_id]),
            upstream_model="deepseek-v4-flash",
        )
    )
    await started.wait()
    await _wait_until(lambda: bool(streams) and streams[0].closed)
    release.set()
    outcome = await pending
    assert outcome.stream is not None
    events = await _drain(outcome.stream)

    assert len(fake_provider.stream_requests) == 2
    assert all(stream.closed for stream in streams)
    assert _streamed_content(events) == "fallback winner "
    assert orchestrator._skill_speculations_in_flight == 0


@pytest.mark.asyncio
async def test_exhausted_speculative_open_does_not_get_second_retry_budget(
    orchestrator: ChatOrchestrator,
    fake_provider: FakeProvider,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    orchestrator.settings = orchestrator.settings.model_copy(
        update={"max_upstream_retries": 2}
    )
    skill_id = sorted(orchestrator.catalog.known_ids)[0]
    started, release = _gate_selector(
        monkeypatch,
        orchestrator,
        SelectionDecision(skill_ids=[skill_id], ran=True, degraded=False),
    )
    streams: list[FakeStream] = []

    def stream_factory(_request: object) -> FakeStream:
        stream = FakeStream(
            [],
            error_at=0,
            error=upstream_error("private open exhausted", retryable=True),
        )
        streams.append(stream)
        return stream

    fake_provider.stream_factory = stream_factory  # type: ignore[assignment]
    pending = asyncio.create_task(
        orchestrator.complete(
            _stream_request_with_state(orchestrator, [skill_id]),
            upstream_model="deepseek-v4-flash",
        )
    )
    await started.wait()
    await _wait_until(lambda: len(streams) == 3 and all(stream.closed for stream in streams))
    release.set()
    outcome = await pending
    assert outcome.stream is not None
    events = await _drain(outcome.stream)

    assert [event.split("\n", 1)[0] for event in events] == [
        "event: meta",
        "event: error",
    ]
    assert len(fake_provider.stream_requests) == 3  # exactly 1 + max_upstream_retries
    assert orchestrator._skill_speculations_in_flight == 0


@pytest.mark.asyncio
async def test_oversized_private_event_respects_byte_cap_and_falls_back(
    orchestrator: ChatOrchestrator,
    fake_provider: FakeProvider,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    orchestrator.settings = orchestrator.settings.model_copy(
        update={"skill_speculation_buffer_bytes": 1024}
    )
    skill_id = sorted(orchestrator.catalog.known_ids)[0]
    started, release = _gate_selector(
        monkeypatch,
        orchestrator,
        SelectionDecision(skill_ids=[skill_id], ran=True, degraded=False),
    )
    streams: list[FakeStream] = []

    def stream_factory(_request: object) -> FakeStream:
        chunks = (
            make_text_chunks("x" * 2048)
            if not streams
            else make_text_chunks("bounded fallback")
        )
        stream = FakeStream(chunks)
        streams.append(stream)
        return stream

    fake_provider.stream_factory = stream_factory  # type: ignore[assignment]
    pending = asyncio.create_task(
        orchestrator.complete(
            _stream_request_with_state(orchestrator, [skill_id]),
            upstream_model="deepseek-v4-flash",
        )
    )
    await started.wait()
    await _wait_until(lambda: bool(streams) and streams[0].closed)
    release.set()
    outcome = await pending
    assert outcome.stream is not None
    events = await _drain(outcome.stream)

    assert len(fake_provider.stream_requests) == 2
    assert _streamed_content(events) == "bounded fallback "
    assert orchestrator._skill_speculations_in_flight == 0


@pytest.mark.asyncio
async def test_uniterated_winner_expires_closes_and_late_consumer_falls_back(
    orchestrator: ChatOrchestrator,
    fake_provider: FakeProvider,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    orchestrator.settings = orchestrator.settings.model_copy(
        update={
            "skill_speculation_buffer_chunks": 1,
            "skill_speculation_claim_timeout_seconds": 0.01,
        }
    )
    skill_id = sorted(orchestrator.catalog.known_ids)[0]
    started, release = _gate_selector(
        monkeypatch,
        orchestrator,
        SelectionDecision(skill_ids=[skill_id], ran=True, degraded=False),
    )
    streams: list[FakeStream] = []

    def stream_factory(_request: object) -> FakeStream:
        text = " ".join(["private"] * 300) if not streams else "late canonical"
        stream = FakeStream(make_text_chunks(text))
        streams.append(stream)
        return stream

    fake_provider.stream_factory = stream_factory  # type: ignore[assignment]
    pending = asyncio.create_task(
        orchestrator.complete(
            _stream_request_with_state(orchestrator, [skill_id]),
            upstream_model="deepseek-v4-flash",
        )
    )
    await started.wait()
    await _wait_until(lambda: bool(streams))
    release.set()
    outcome = await pending
    assert outcome.stream is not None
    assert orchestrator._skill_speculations_in_flight == 1

    await asyncio.sleep(0.03)
    assert streams[0].closed is True
    assert orchestrator._skill_speculations_in_flight == 0

    events = await _drain(outcome.stream)
    assert len(fake_provider.stream_requests) == 2
    assert _streamed_content(events) == "late canonical "


@pytest.mark.asyncio
async def test_mismatch_cancels_producer_blocked_on_chunk_backpressure(
    orchestrator: ChatOrchestrator,
    fake_provider: FakeProvider,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    orchestrator.settings = orchestrator.settings.model_copy(
        update={"skill_speculation_buffer_chunks": 2}
    )
    ids = sorted(orchestrator.catalog.known_ids)
    started, release = _gate_selector(
        monkeypatch,
        orchestrator,
        SelectionDecision(skill_ids=[ids[1]], ran=True, degraded=False),
    )
    stream = FakeStream(make_text_chunks(" ".join(["chunk"] * 500)))
    fake_provider.stream_factory = lambda _request: stream
    pending = asyncio.create_task(
        orchestrator.complete(
            _stream_request_with_state(orchestrator, [ids[0]]),
            upstream_model="deepseek-v4-flash",
        )
    )
    await started.wait()
    await _wait_until(lambda: stream._index >= 3)
    await asyncio.sleep(0)
    assert stream._index <= 3  # two buffered chunks plus one producer-local event
    release.set()
    outcome = await pending
    assert outcome.stream is not None
    await _wait_until(lambda: orchestrator._skill_speculations_in_flight == 0)
    assert stream.closed is True
    await outcome.stream.aclose()


@pytest.mark.asyncio
async def test_cleanup_timeout_keeps_capacity_until_resistant_producer_finishes(
    orchestrator: ChatOrchestrator,
    fake_provider: FakeProvider,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(orchestrator_module, "_SPECULATION_CLOSE_TIMEOUT_SECONDS", 0.01)
    ids = sorted(orchestrator.catalog.known_ids)
    started, release_selector = _gate_selector(
        monkeypatch,
        orchestrator,
        SelectionDecision(skill_ids=[ids[1]], ran=True, degraded=False),
    )
    allow_close = asyncio.Event()

    class ResistantCloseStream(FakeStream):
        async def aclose(self) -> None:
            while not allow_close.is_set():
                try:
                    await allow_close.wait()
                except asyncio.CancelledError:
                    # Simulate a provider primitive that does not promptly honor
                    # task cancellation; the overlap gate must remain occupied.
                    continue
            self.closed = True

    upstream: ProviderStream = ResistantCloseStream(make_text_chunks("private loser"))
    fake_provider.stream_factory = lambda _request: upstream
    pending = asyncio.create_task(
        orchestrator.complete(
            _stream_request_with_state(orchestrator, [ids[0]]),
            upstream_model="deepseek-v4-flash",
        )
    )
    await started.wait()
    await _wait_until(lambda: len(fake_provider.stream_requests) == 1)
    release_selector.set()
    outcome = await pending
    assert outcome.stream is not None

    assert orchestrator._skill_speculations_in_flight == 1
    allow_close.set()
    await _wait_until(lambda: orchestrator._skill_speculations_in_flight == 0)
    assert upstream.closed is True  # type: ignore[attr-defined]
    await outcome.stream.aclose()


@pytest.mark.asyncio
async def test_cancellation_inside_claim_still_closes_provisional_owner(
    orchestrator: ChatOrchestrator,
    fake_provider: FakeProvider,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    orchestrator.settings = orchestrator.settings.model_copy(
        update={"skill_speculation_buffer_chunks": 1}
    )
    skill_id = sorted(orchestrator.catalog.known_ids)[0]

    async def matching_select(*args: object, **kwargs: object) -> SelectionDecision:
        await asyncio.sleep(0)
        return SelectionDecision(skill_ids=[skill_id], ran=True, degraded=False)

    monkeypatch.setattr(orchestrator.selector, "select", matching_select)
    upstream = FakeStream(make_text_chunks(" ".join(["held"] * 300)))
    fake_provider.stream_factory = lambda _request: upstream
    outcome = await orchestrator.complete(
        _stream_request_with_state(orchestrator, [skill_id]),
        upstream_model="deepseek-v4-flash",
    )
    assert outcome.stream is not None
    claim_started = asyncio.Event()
    never = asyncio.Event()

    async def blocking_claim(self: _SpeculativeStream) -> bool:
        self._claimed = True
        claim_started.set()
        await never.wait()
        return True

    monkeypatch.setattr(_SpeculativeStream, "claim", blocking_claim)
    first_event = asyncio.create_task(anext(outcome.stream))
    await claim_started.wait()
    first_event.cancel()
    with pytest.raises(asyncio.CancelledError):
        await first_event

    assert upstream.closed is True
    assert orchestrator._skill_speculations_in_flight == 0


def _cadence(
    orch: ChatOrchestrator,
    *,
    force: bool = False,
    policy: str = "new_instruction",
    state_valid: bool = True,
    state_catalog_revision: str | None = None,
    state_turn_id: str | None = "t1",
    state_instruction_revision: int | None = 0,
    turn_id: str = "t1",
    instruction_revision: int = 0,
) -> bool:
    if state_catalog_revision is None:
        state_catalog_revision = orch.catalog.revision
    return orch._should_run_selector(
        force=force,
        policy=policy,
        state_valid=state_valid,
        state_catalog_revision=state_catalog_revision,
        state_turn_id=state_turn_id,
        state_instruction_revision=state_instruction_revision,
        turn_id=turn_id,
        instruction_revision=instruction_revision,
    )


def test_cadence_force_runs(orchestrator: ChatOrchestrator) -> None:
    assert _cadence(orchestrator, force=True) is True


def test_cadence_always_runs(orchestrator: ChatOrchestrator) -> None:
    assert _cadence(orchestrator, policy="always") is True


def test_cadence_invalid_state_runs(orchestrator: ChatOrchestrator) -> None:
    assert _cadence(orchestrator, state_valid=False) is True


def test_cadence_catalog_mismatch_runs(orchestrator: ChatOrchestrator) -> None:
    assert _cadence(orchestrator, state_catalog_revision="sha256:old") is True


def test_cadence_new_turn_runs(orchestrator: ChatOrchestrator) -> None:
    assert _cadence(orchestrator, turn_id="t2") is True


def test_cadence_instruction_change_runs(orchestrator: ChatOrchestrator) -> None:
    assert _cadence(orchestrator, instruction_revision=1) is True


def test_cadence_same_turn_does_not_run(orchestrator: ChatOrchestrator) -> None:
    # Same turn, valid state, matching catalog/instruction → reuse (no rerun).
    assert _cadence(orchestrator) is False


# ---- selector turn context (bounded evidence for the classifier) -------------


def _turn_request(turn: dict) -> RespondRequest:
    return RespondRequest.model_validate(
        {
            "protocol_version": 2,
            "session": {"id": "s", "turn_id": "t"},
            "input": {"messages": [{"role": "user", "content": "go"}]},
            "startup": {},
            "turn": turn,
        }
    )


def test_selector_turn_context_includes_top_workflow_digest() -> None:
    req = _turn_request(
        {
            "goal": "ship the auth feature",
            "workflow_state": [
                {
                    "id": "wf-1",
                    "goal": "implement auth",
                    "status": "in_progress",
                    "next_action": "spawn an agent for the login form",
                },
                {"id": "wf-2", "goal": "second workflow", "status": "blocked"},
            ],
        }
    )
    ctx = ChatOrchestrator._selector_turn_context(req)
    assert ctx is not None
    assert "current goal: ship the auth feature" in ctx
    assert "top active workflow" in ctx
    assert "goal: implement auth" in ctx
    assert "status: in_progress" in ctx
    assert "next: spawn an agent for the login form" in ctx
    # Only the client-ranked head digest is surfaced.
    assert "second workflow" not in ctx


def test_selector_turn_context_workflow_digest_is_sanitized() -> None:
    req = _turn_request(
        {"workflow_state": [{"id": "wf-1", "goal": "line1\nline2", "status": "ok"}]}
    )
    ctx = ChatOrchestrator._selector_turn_context(req)
    assert ctx is not None
    assert "\n" not in ctx
    assert "line1 line2" in ctx


def test_selector_turn_context_empty_digest_adds_nothing() -> None:
    req = _turn_request({"workflow_state": [{"id": "wf-1", "goal": "", "status": ""}]})
    assert ChatOrchestrator._selector_turn_context(req) is None


# ---- stream-first prep, heartbeats, terminal error honesty -------------------


def _basic_stream_request(session_id: str = "hb-session") -> RespondRequest:
    return RespondRequest.model_validate(
        {
            "protocol_version": 2,
            "session": {"id": session_id, "turn_id": "turn-1"},
            "input": {"messages": [{"role": "user", "content": "hello"}]},
            "startup": {},
            "generation": {"stream": True},
        }
    )


@pytest.mark.asyncio
async def test_streaming_complete_returns_before_selection_finishes(
    orchestrator: ChatOrchestrator,
    fake_provider: FakeProvider,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The response scaffold (headers) is available while the selector still runs."""
    skill_id = sorted(orchestrator.catalog.known_ids)[0]
    started, release = _gate_selector(
        monkeypatch, orchestrator, SelectionDecision(skill_ids=[skill_id], ran=True, degraded=False)
    )

    outcome = await orchestrator.complete(
        _basic_stream_request(), upstream_model="deepseek-v4-flash"
    )
    assert outcome.streaming and outcome.stream is not None
    assert outcome.headers["X-Request-Id"]
    # Prep starts eagerly (not lazily on first poll): the selector is already in.
    await _wait_until(started.is_set)
    release.set()

    events = await _drain(outcome.stream)
    named = [e for e in events if not e.startswith(":")]
    assert named[0].startswith("event: meta\n")
    assert named[-1].startswith("event: done\n")


@pytest.mark.asyncio
async def test_heartbeat_comments_flow_while_selection_is_quiet(
    orchestrator: ChatOrchestrator,
    fake_provider: FakeProvider,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(orchestrator_module, "HEARTBEAT_INTERVAL_SECONDS", 0.02)
    skill_id = sorted(orchestrator.catalog.known_ids)[0]
    started, release = _gate_selector(
        monkeypatch, orchestrator, SelectionDecision(skill_ids=[skill_id], ran=True, degraded=False)
    )

    outcome = await orchestrator.complete(
        _basic_stream_request(), upstream_model="deepseek-v4-flash"
    )
    assert outcome.stream is not None
    # With the selector gated, the first thing on the wire is a comment heartbeat,
    # not dead air.
    first = await anext(outcome.stream)
    assert first == ": hb\n\n"
    release.set()
    events = await _drain(outcome.stream)
    named = [e for e in events if not e.startswith(":")]
    assert named[0].startswith("event: meta\n")
    assert named[-1].startswith("event: done\n")


@pytest.mark.asyncio
async def test_prep_failure_streams_terminal_error_event(
    orchestrator: ChatOrchestrator,
    fake_provider: FakeProvider,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A DaintreeError during prep (post-headers) is reported in-band, once."""

    def boom(**kwargs: object) -> object:
        raise upstream_error("assembly failed")

    monkeypatch.setattr(orchestrator.assembler, "build_main_request", boom)

    outcome = await orchestrator.complete(
        _basic_stream_request(), upstream_model="deepseek-v4-flash"
    )
    assert outcome.stream is not None
    events = [e for e in await _drain(outcome.stream) if not e.startswith(":")]
    assert len(events) == 1
    assert events[0].startswith("event: error\n")
    payload = json.loads(events[0].split("data: ", 1)[1])
    assert payload["error"]["code"] == "upstream_error"
    # The provider was never opened and no done was fabricated.
    assert fake_provider.stream_requests == []


@pytest.mark.asyncio
async def test_unexpected_prep_exception_streams_internal_error(
    orchestrator: ChatOrchestrator,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def boom(**kwargs: object) -> object:
        raise RuntimeError("bug")

    monkeypatch.setattr(orchestrator.assembler, "build_main_request", boom)

    outcome = await orchestrator.complete(
        _basic_stream_request(), upstream_model="deepseek-v4-flash"
    )
    assert outcome.stream is not None
    events = [e for e in await _drain(outcome.stream) if not e.startswith(":")]
    assert len(events) == 1
    payload = json.loads(events[0].split("data: ", 1)[1])
    assert payload["error"]["code"] == "internal_error"


@pytest.mark.asyncio
async def test_unexpected_midstream_exception_emits_terminal_error(
    orchestrator: ChatOrchestrator,
    fake_provider: FakeProvider,
) -> None:
    """A non-DaintreeError escaping mid-stream ends with an error event, not EOF."""
    fake_provider.stream_factory = lambda _request: FakeStream(
        make_text_chunks("partial"), error_at=1, error=ValueError("malformed chunk")
    )

    outcome = await orchestrator.complete(
        _basic_stream_request(), upstream_model="deepseek-v4-flash"
    )
    assert outcome.stream is not None
    events = [e for e in await _drain(outcome.stream) if not e.startswith(":")]
    assert events[0].startswith("event: meta\n")
    assert events[-1].startswith("event: error\n")
    payload = json.loads(events[-1].split("data: ", 1)[1])
    assert payload["error"]["code"] == "internal_error"
    assert not any(e.startswith("event: done\n") for e in events)
    assert fake_provider.last_stream is not None
    assert fake_provider.last_stream.closed is True


@pytest.mark.asyncio
async def test_disconnect_during_prep_cancels_selection(
    orchestrator: ChatOrchestrator,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    started = asyncio.Event()
    cancelled = asyncio.Event()

    async def select(*args: object, **kwargs: object) -> SelectionDecision:
        started.set()
        try:
            await asyncio.sleep(3600)
        except asyncio.CancelledError:
            cancelled.set()
            raise
        raise AssertionError("unreachable")

    monkeypatch.setattr(orchestrator.selector, "select", select)

    outcome = await orchestrator.complete(
        _basic_stream_request(), upstream_model="deepseek-v4-flash"
    )
    assert outcome.stream is not None
    pull = asyncio.create_task(anext(outcome.stream))
    await started.wait()
    pull.cancel()
    await asyncio.gather(pull, return_exceptions=True)
    await outcome.stream.aclose()
    await _wait_until(cancelled.is_set)


@pytest.mark.asyncio
async def test_aclose_before_first_poll_cancels_prep(
    orchestrator: ChatOrchestrator,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Closing the never-polled body cancels selection instead of orphaning it."""
    started = asyncio.Event()
    cancelled = asyncio.Event()

    async def select(*args: object, **kwargs: object) -> SelectionDecision:
        started.set()
        try:
            await asyncio.sleep(3600)
        except asyncio.CancelledError:
            cancelled.set()
            raise
        raise AssertionError("unreachable")

    monkeypatch.setattr(orchestrator.selector, "select", select)

    outcome = await orchestrator.complete(
        _basic_stream_request(), upstream_model="deepseek-v4-flash"
    )
    assert outcome.stream is not None
    await started.wait()
    # Never poll the stream — close it straight away (endpoint failure /
    # vanished client between scaffold and body).
    await outcome.stream.aclose()
    assert cancelled.is_set()


@pytest.mark.asyncio
async def test_aclose_before_first_poll_discards_completed_speculation(
    orchestrator: ChatOrchestrator,
    fake_provider: FakeProvider,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Prep finished (speculation committed) but nobody consumed the stream:
    closing the body discards the speculative producer immediately."""
    ids = sorted(orchestrator.catalog.known_ids)
    started, release = _gate_selector(
        monkeypatch,
        orchestrator,
        SelectionDecision(skill_ids=[ids[0]], ran=True, degraded=False),
    )
    fake_provider.stream_factory = lambda _request: FakeStream(
        make_text_chunks("speculative")
    )
    outcome = await orchestrator.complete(
        _stream_request_with_state(orchestrator, [ids[0]]),
        upstream_model="deepseek-v4-flash",
    )
    assert outcome.stream is not None
    await started.wait()
    release.set()
    # Let prep run to completion (speculation committed, unclaimed).
    await _wait_until(lambda: len(fake_provider.stream_requests) == 1)
    await asyncio.sleep(0)
    await outcome.stream.aclose()
    await _wait_until(lambda: orchestrator._skill_speculations_in_flight == 0)
    assert fake_provider.last_stream is not None
    await _wait_until(lambda: fake_provider.last_stream.closed)


@pytest.mark.asyncio
async def test_provider_close_failure_after_done_emits_nothing_extra(
    orchestrator: ChatOrchestrator,
    fake_provider: FakeProvider,
) -> None:
    """A teardown failure after the terminal event must not become a second event."""

    class ExplodingCloseStream(FakeStream):
        async def aclose(self) -> None:
            self.closed = True
            raise RuntimeError("close failed")

    fake_provider.stream_factory = lambda _request: ExplodingCloseStream(
        make_text_chunks("fine")
    )
    outcome = await orchestrator.complete(
        _basic_stream_request(), upstream_model="deepseek-v4-flash"
    )
    assert outcome.stream is not None
    events = [e for e in await _drain(outcome.stream) if not e.startswith(":")]
    assert events[0].startswith("event: meta\n")
    assert events[-1].startswith("event: done\n")
    assert sum(1 for e in events if e.startswith("event: done\n")) == 1
    assert not any(e.startswith("event: error\n") for e in events)
