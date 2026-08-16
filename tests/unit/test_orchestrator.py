"""Respond orchestrator unit tests (active-set resolution + selector cadence)."""

from __future__ import annotations

import asyncio
import dataclasses
import itertools
import json
from collections.abc import Callable, Sequence
from typing import Any

import pytest
from fakes import FakeProvider, FakeStream, make_result, make_text_chunks, selection_result
from pydantic import SecretStr

from daintree_assistant_server.contracts.daintree_api import RespondRequest
from daintree_assistant_server.errors import upstream_error
from daintree_assistant_server.providers.base import (
    ProviderChatResult,
    ProviderStream,
    ProviderUsage,
)
from daintree_assistant_server.services import chat_orchestrator as orchestrator_module
from daintree_assistant_server.services.chat_orchestrator import (
    ChatOrchestrator,
    _SpeculativeStream,
    merge_active_skills,
    resolve_active_skills,
)
from daintree_assistant_server.services.prompt_assembler import (
    PromptAssembler,
    render_order_ids,
)
from daintree_assistant_server.skills.catalog import Catalog
from daintree_assistant_server.skills.loader import parse_skill
from daintree_assistant_server.skills.selector import SelectionDecision

TEST_API_KEY = SecretStr("test-caller-key")


def _assert_foundation_closed(catalog, active: list[str]) -> None:
    """Every admitted skill's domain foundation is admitted too.

    The explicit-load path signs a state token, and the reuse/degraded paths replay
    that set verbatim — so an unclosed set minted here would propagate a
    foundation-less active set through every following round.
    """
    for sid in active:
        foundation_id = catalog.domain_foundation(sid)
        if foundation_id is not None:
            assert foundation_id in active, f"{sid} admitted without {foundation_id}"


def test_merge_additive_new_first(catalog) -> None:
    ids = sorted(catalog.known_ids)
    merged = merge_active_skills([ids[2]], [ids[0], ids[1]], catalog, 3)
    # The explicitly loaded id leads; prior ids follow in their existing order.
    assert merged.active[0] == ids[2]
    assert merged.newly_loaded[0] == ids[2]
    assert [s for s in merged.active if s in (ids[0], ids[1])] == [
        s for s in (ids[0], ids[1]) if s in merged.active
    ]
    _assert_foundation_closed(catalog, merged.active)


def test_merge_caps_at_max_active(catalog) -> None:
    ids = sorted(catalog.known_ids)
    merged = merge_active_skills([ids[3]], [ids[0], ids[1], ids[2]], catalog, 3)
    assert len(merged.active) <= 3
    assert merged.active[0] == ids[3]  # the explicit load always wins its slot
    assert merged.dropped_over_cap  # the oldest prior ids yield
    assert set(merged.dropped_over_cap).isdisjoint(merged.active)
    _assert_foundation_closed(catalog, merged.active)


def test_merge_empty_selection_keeps_prior(catalog) -> None:
    ids = sorted(catalog.known_ids)
    merged = merge_active_skills([], [ids[0]], catalog, 3)
    assert ids[0] in merged.active
    # Closure may pull the foundation in, but nothing the caller already had is "new".
    assert ids[0] not in merged.newly_loaded
    _assert_foundation_closed(catalog, merged.active)


def test_merge_drops_unknown(catalog) -> None:
    ids = sorted(catalog.known_ids)
    merged = merge_active_skills(["nope", ids[0]], [], catalog, 3)
    assert "nope" not in merged.active
    assert ids[0] in merged.active
    # An unknown id is not a capacity signal.
    assert merged.dropped_over_cap == []
    _assert_foundation_closed(catalog, merged.active)


def test_merge_no_duplicate_load(catalog) -> None:
    ids = sorted(catalog.known_ids)
    merged = merge_active_skills([ids[0]], [ids[0]], catalog, 3)
    assert merged.active.count(ids[0]) == 1
    assert merged.newly_loaded == [
        s for s in merged.active if s != ids[0]
    ]  # only a pulled-in foundation can be new
    _assert_foundation_closed(catalog, merged.active)


# ---- resolve_active_skills: per-turn authoritative selection ----------------
#
# These run against the REAL catalog, whose foundation topology moves (ids are
# reshaped; a foundation may resolve for every skill). They therefore assert the
# topology-independent half of the contract — what is evicted, what is newly
# loaded, the cap — while the synthetic-catalog block below pins the exact
# admission shape and the property test re-checks all of it over real ids.


def test_resolve_successful_run_replaces_prior(catalog) -> None:
    # A successful selector run is authoritative: prior skills not re-listed are evicted.
    ids = sorted(catalog.known_ids)
    decision = SelectionDecision(skill_ids=[ids[1]], ran=True, degraded=False)
    result = resolve_active_skills(decision, [ids[0]], catalog, 3)
    assert ids[1] in result.active
    assert ids[0] not in result.active
    assert ids[1] in result.newly_loaded


def test_resolve_empty_successful_run_clears_prior(catalog) -> None:
    # An empty result on a real run CLEARS the active set (topic shift to no-task).
    ids = sorted(catalog.known_ids)
    decision = SelectionDecision(skill_ids=[], ran=True, degraded=False)
    result = resolve_active_skills(decision, [ids[0]], catalog, 3)
    assert result.active == []
    assert result.newly_loaded == []


def test_resolve_degraded_keeps_prior(catalog) -> None:
    # A selector that failed open must NOT evict — reuse the prior set.
    ids = sorted(catalog.known_ids)
    decision = SelectionDecision.degraded_open(["selector_failed"])
    result = resolve_active_skills(decision, [ids[0]], catalog, 3)
    assert result.active == [ids[0]]
    assert result.newly_loaded == []


def test_resolve_no_run_reuses_prior(catalog) -> None:
    # A tool-continuation round does not run the selector → reuse decision → keep prior.
    ids = sorted(catalog.known_ids)
    decision = SelectionDecision.reuse([ids[0], ids[1]])
    result = resolve_active_skills(decision, [ids[0], ids[1]], catalog, 3)
    assert result.active == [ids[0], ids[1]]
    assert result.newly_loaded == []


def test_resolve_keeps_active_when_relisted(catalog) -> None:
    # Re-listing an active skill keeps it; a genuinely new one loads alongside.
    ids = sorted(catalog.known_ids)
    decision = SelectionDecision(skill_ids=[ids[0], ids[1]], ran=True, degraded=False)
    result = resolve_active_skills(decision, [ids[0]], catalog, 3)
    assert {ids[0], ids[1]} <= set(result.active)
    assert ids[1] in result.newly_loaded
    assert ids[0] not in result.newly_loaded


def test_resolve_caps_and_drops_unknown(catalog) -> None:
    ids = sorted(catalog.known_ids)
    decision = SelectionDecision(
        skill_ids=["nope", ids[0], ids[1], ids[2], ids[3]], ran=True, degraded=False
    )
    result = resolve_active_skills(decision, [], catalog, 3)
    assert len(result.active) == 3
    assert "nope" not in result.active
    assert result.active[0] == ids[0]  # rank order is preserved
    # The cap is now reported; the unknown id is not part of that signal.
    assert result.dropped_over_cap and "nope" not in result.dropped_over_cap


# ---- resolve_active_skills: domain-foundation UNIT admission -----------------
#
# A synthetic catalog, so the admission contract is pinned deterministically:
# the real catalog's foundation topology is being reshaped (single root
# foundation, nearest-first prefix walk-up), and this contract must hold under
# ANY topology. The property test below then re-checks it over the real one.

_DOMAIN_BASE = "spec.dom.base"
_ONE = "spec.dom.one"
_TWO = "spec.dom.two"
_THREE = "spec.dom.three"
_OTHER_BASE = "spec.other.base"
_OTHER_ONE = "spec.other.one"


def _skill_markdown(skill_id: str, *, foundation: bool) -> str:
    return (
        "---\n"
        f"id: {skill_id}\n"
        f"title: {skill_id}\n"
        "summary: Synthetic admission fixture.\n"
        "whenToUse: Use only in the admission tests.\n"
        + ("foundation: true\n" if foundation else "")
        + "---\n"
        "Use when: never — this is a test fixture.\n\n"
        "Report back: done.\n"
    )


def _synthetic_catalog() -> Catalog:
    """Two domains, each with its own foundation, three specifics in the first."""
    specs = [
        (_DOMAIN_BASE, True),
        (_ONE, False),
        (_TWO, False),
        (_THREE, False),
        (_OTHER_BASE, True),
        (_OTHER_ONE, False),
    ]
    return Catalog(
        parse_skill(_skill_markdown(sid, foundation=f), expected_id=sid)
        for sid, f in specs
    )


@pytest.fixture
def paired_catalog() -> Catalog:
    return _synthetic_catalog()


def test_resolve_pairs_domain_foundation_on_first_selection(paired_catalog) -> None:
    # Round 1: selector returns only the specific. The foundation is admitted with it so
    # it loads on the SAME turn (front-loaded), not a turn late.
    decision = SelectionDecision(skill_ids=[_ONE], ran=True, degraded=False)
    result = resolve_active_skills(decision, [], paired_catalog, 3)
    assert result.active == [_ONE, _DOMAIN_BASE]
    assert set(result.newly_loaded) == {_ONE, _DOMAIN_BASE}


def test_resolve_stable_across_rounds_no_late_load(paired_catalog) -> None:
    # Round 2: task still governed by the same specific. Even if the selector re-lists
    # only the specific (forgetting the foundation), admission keeps the set stable — the
    # foundation is not re-loaded, and nothing new appears mid-conversation.
    prior = [_ONE, _DOMAIN_BASE]
    decision = SelectionDecision(skill_ids=[_ONE], ran=True, degraded=False)
    result = resolve_active_skills(decision, prior, paired_catalog, 3)
    assert result.active == [_ONE, _DOMAIN_BASE]
    assert result.newly_loaded == []


def test_resolve_foundation_selected_alone_not_duplicated(paired_catalog) -> None:
    decision = SelectionDecision(skill_ids=[_DOMAIN_BASE], ran=True, degraded=False)
    result = resolve_active_skills(decision, [], paired_catalog, 3)
    assert result.active == [_DOMAIN_BASE]


def test_resolve_paired_foundation_is_never_displaced_by_the_cap(paired_catalog) -> None:
    # THE inverted contract. The selector truncates its own output to max_active, so a
    # full set of specifics used to guarantee the appended foundation was the first id
    # cut — pairing was dead code exactly when it mattered. Now the foundation rides in
    # with the top-ranked specific as one unit and the MARGINAL specific is what the cap
    # costs, matching the selector prompt's own tie-break order.
    decision = SelectionDecision(skill_ids=[_ONE, _TWO, _THREE], ran=True, degraded=False)
    result = resolve_active_skills(decision, [], paired_catalog, 3)
    assert result.active == [_ONE, _DOMAIN_BASE, _TWO]
    assert len(result.active) == 3  # the budget is fully used
    assert result.dropped_over_cap == [_THREE]


def test_resolve_never_evicts_an_active_foundation_mid_workflow(paired_catalog) -> None:
    # A new full selection used to silently evict the already-loaded foundation (and, per
    # the assembler's cache rules, force a full tools+conversation re-encode).
    prior = [_ONE, _DOMAIN_BASE]
    decision = SelectionDecision(skill_ids=[_ONE, _TWO, _THREE], ran=True, degraded=False)
    result = resolve_active_skills(decision, prior, paired_catalog, 3)
    assert _DOMAIN_BASE in result.active
    assert result.newly_loaded == [_TWO]


def test_resolve_skips_an_unaffordable_unit_and_admits_a_later_one(
    paired_catalog,
) -> None:
    # Cross-domain: the second pick needs its OWN foundation (2 slots) and does not fit,
    # so it is skipped — and the third pick, whose foundation is already in, still fills
    # the last slot. No slot is reserved for a foundation that is then discarded, and no
    # slot is left empty while a candidate could have filled it.
    decision = SelectionDecision(
        skill_ids=[_ONE, _OTHER_ONE, _TWO], ran=True, degraded=False
    )
    result = resolve_active_skills(decision, [], paired_catalog, 3)
    assert result.active == [_ONE, _DOMAIN_BASE, _TWO]
    assert result.dropped_over_cap == [_OTHER_ONE]
    assert _OTHER_BASE not in result.active  # never reserved, never orphaned


def test_resolve_reuse_path_does_not_inject_foundation(paired_catalog) -> None:
    # A continuation round (no selector run) reuses the prior set verbatim — admission
    # must not surface a surprise foundation load mid-workflow.
    decision = SelectionDecision.reuse([_ONE])
    result = resolve_active_skills(decision, [_ONE], paired_catalog, 3)
    assert result.active == [_ONE]
    assert result.newly_loaded == []


# ---- admission properties over the REAL catalog -----------------------------

# The exact cross-domain selection that broke a proposed "reserve a slot for the
# foundation" fix: it evicted the third specific AND then dropped the reserved
# foundation nobody claimed, returning two skills against a budget of three.
_CROSS_DOMAIN_SELECTION = (
    "daintree.workflow.start-work-on-issue",
    "daintree.agents.ask-and-relay",
    "daintree.orchestration.competition",
)


def _closure_holds(active: Sequence[str], catalog: Catalog) -> bool:
    """Every admitted skill's domain foundation is itself admitted."""
    return all(
        catalog.domain_foundation(sid) is None or catalog.domain_foundation(sid) in active
        for sid in active
    )


def _max_admissible(selection: Sequence[str], catalog: Catalog, cap: int) -> int:
    """Brute force: the largest foundation-closed set reachable from ``selection``."""
    candidates: list[str] = []
    for sid in selection:
        if sid in catalog and sid not in candidates:
            candidates.append(sid)
    best = 0
    for size in range(len(candidates) + 1):
        for combo in itertools.combinations(candidates, size):
            closed = set(combo)
            for sid in combo:
                foundation = catalog.domain_foundation(sid)
                if foundation is not None:
                    closed.add(foundation)
            if len(closed) <= cap:
                best = max(best, len(closed))
    return best


@pytest.mark.parametrize("max_active", [2, 3, 4])
def test_admission_properties_over_the_real_catalog(catalog, max_active: int) -> None:
    ids = sorted(catalog.known_ids)
    selections: list[list[str]] = [list(c) for c in itertools.combinations(ids, 3)]
    selections += [list(reversed(sel)) for sel in selections[:64]]
    selections += [
        [],
        [ids[0]],
        [ids[0], ids[0]],
        ["unknown.skill", *ids[:3]],
        ids[:5],
        ids[-4:],
    ]
    for selection in selections:
        decision = SelectionDecision(skill_ids=selection, ran=True, degraded=False)
        active = resolve_active_skills(decision, [], catalog, max_active).active

        # (a) never over the budget, never duplicated, never unknown.
        assert len(active) <= max_active
        assert len(set(active)) == len(active)
        assert all(sid in catalog for sid in active)

        # (b) every admitted skill's domain foundation is admitted too.
        assert _closure_holds(active, catalog), (selection, max_active, active)

        # (c) never shorter than it needs to be: nothing that was skipped could
        #     still have fit in the leftover budget.
        free = max_active - len(active)
        for sid in selection:
            if sid not in catalog or sid in active:
                continue
            foundation = catalog.domain_foundation(sid)
            cost = 1 + (
                1 if foundation is not None and foundation not in active else 0
            )
            assert cost > free, (selection, max_active, active, sid)

        # ...and, where rank priority and count cannot disagree (at most one
        # distinct foundation in play — what the real catalog does), the admitted
        # count equals the brute-forced optimum. With two or more foundations the
        # top-ranked pick is deliberately kept even when dropping it would fit one
        # more skill; (c) above is the guarantee that holds in every topology.
        foundations = {
            catalog.domain_foundation(sid)
            for sid in selection
            if sid in catalog and catalog.domain_foundation(sid) is not None
        }
        if len(foundations) <= 1:
            assert len(active) == _max_admissible(selection, catalog, max_active), (
                selection,
                max_active,
                active,
            )


def test_resolve_cross_domain_selection_never_returns_a_short_set(catalog) -> None:
    """Regression for the naive "reserve a slot" fix: three cross-domain specifics
    against a cap of three must still spend all three slots."""
    selection = [sid for sid in _CROSS_DOMAIN_SELECTION if sid in catalog]
    if len(selection) != len(_CROSS_DOMAIN_SELECTION):
        pytest.skip("catalog no longer carries the cross-domain regression ids")
    decision = SelectionDecision(skill_ids=selection, ran=True, degraded=False)
    result = resolve_active_skills(decision, [], catalog, 3)
    assert len(result.active) == 3
    assert _closure_holds(result.active, catalog)
    assert result.active[0] == selection[0]  # the top-ranked pick is never sacrificed


# ---- speculation winners are compared on RENDER order -----------------------


def test_render_order_makes_every_permutation_the_same_speculation(
    prompt_loader, settings
) -> None:
    # The handshake compares CANONICAL render ids. Foundations sort first and ids
    # sort within each tier, so the selector's rank cannot distinguish one active
    # set from a reshuffle of itself — every ordering has to assemble to the same
    # bytes. When it did not, an ordered compare threw a live, already-producing
    # stream away to rebuild a payload that differed only in which skill section
    # came first.
    #
    # The SYNTHETIC catalog, because it pins TWO foundations: the real one
    # carries a single root foundation today, and against it a half-fix that
    # canonicalized specifics while leaving foundations in selector order would
    # pass every assertion here.
    catalog = _synthetic_catalog()
    foundations = [s for s in catalog.skills if s.meta.foundation]
    specifics = [s for s in catalog.skills if not s.meta.foundation][:2]
    assert len(foundations) == 2, "synthetic catalog pins two domain foundations"

    members = [*foundations, *specifics]
    # Tier first, id within each tier — foundations lead regardless of how their
    # ids compare to the specifics'.
    expected_ids = sorted(s.id for s in foundations) + sorted(s.id for s in specifics)
    expected_bytes = PromptAssembler(prompt_loader, settings).render_loaded_skills(members)

    for permutation in itertools.permutations(members):
        candidate = list(permutation)
        assert render_order_ids(candidate) == expected_ids, candidate
        # A FRESH assembler per permutation. Reusing one lets its render cache
        # answer from the first (canonical) call, which would hide a regression
        # that canonicalized only the CACHE KEY and still handed Jinja the raw
        # order — every permutation would hit, while a process whose first call
        # was a reshuffle rendered different bytes.
        fresh = PromptAssembler(prompt_loader, settings)
        assert fresh.render_loaded_skills(candidate) == expected_bytes, candidate


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
            "input": {"messages": [{"role": "user", "content": "rename my terminals"}]},
            "startup": {},
            "generation": {"stream": True},
        }
    )

    outcome = await orchestrator.complete(request, upstream_model="deepseek-v4-flash", api_key=TEST_API_KEY)
    assert outcome.stream is not None
    assert fake_provider.stream_requests == []

    first = await anext(outcome.stream)
    assert first.startswith("event: meta\n")
    meta = json.loads(first.split("data: ", 1)[1])
    # The pick, plus whatever domain foundation the catalog pairs with it.
    assert [skill["id"] for skill in meta["skills"]["newly_loaded"]] == _resolved(
        orchestrator, [skill_id]
    )
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


def _resolved(orchestrator: ChatOrchestrator, selected_ids: list[str]) -> list[str]:
    """The active set a turn selecting ``selected_ids`` actually resolves to.

    Tests that stand in for a PREVIOUS turn (a prior state token) or assert on the
    active/newly-loaded block go through this instead of hardcoding the selection:
    the catalog decides which domain foundation rides along, and that topology
    moves. Hardcoding it would make the speculation match under test an artifact
    of the fixture rather than of the orchestrator.
    """
    return resolve_active_skills(
        SelectionDecision(skill_ids=selected_ids, ran=True, degraded=False),
        [],
        orchestrator.catalog,
        orchestrator.settings.max_active_skills,
    ).active


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
            active_skills=_resolved(orchestrator, active_ids),
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
        orchestrator.complete(request, upstream_model="deepseek-v4-flash", api_key=TEST_API_KEY)
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
    # The orchestrator wraps the assembled request exactly once with the caller's
    # key (dataclasses.replace — a shallow copy): the sent request must be that
    # credentialed copy field-for-field, and the expensive payload objects must be
    # the SAME objects the assembler built (shallow reuse, not a rebuild).
    sent = fake_provider.stream_requests[0]
    assert sent == dataclasses.replace(assembled[0], api_key=TEST_API_KEY)
    assert sent.messages is assembled[0].messages
    assert sent.api_key is TEST_API_KEY
    assert len(fake_provider.stream_requests) == 1  # winner consumed exactly once
    assert streams[0].closed is True
    assert orchestrator._skill_speculations_in_flight == 0


@pytest.mark.asyncio
async def test_foundation_reordering_still_reuses_the_speculative_stream(
    orchestrator: ChatOrchestrator,
    fake_provider: FakeProvider,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """[foundation, X] and [X, foundation] assemble to identical bytes, so the
    live speculative stream must be reused rather than rebuilt — this is exactly
    the turn shape pairing creates (foundation-first one turn, specific-first the
    next)."""
    catalog = orchestrator.catalog
    foundation = next((s.id for s in catalog.skills if s.meta.foundation), None)
    assert foundation is not None, "catalog has no foundation skill"
    specific = next(s.id for s in catalog.skills if not s.meta.foundation)
    started, release = _gate_selector(
        monkeypatch,
        orchestrator,
        SelectionDecision(skill_ids=[specific, foundation], ran=True, degraded=False),
    )
    fake_provider.stream_factory = lambda _request: FakeStream(
        make_text_chunks("speculative winner")
    )
    original_build = orchestrator.assembler.build_main_request
    assembled: list[object] = []

    def tracked_build(**kwargs: Any) -> Any:
        built = original_build(**kwargs)
        assembled.append(built)
        return built

    monkeypatch.setattr(orchestrator.assembler, "build_main_request", tracked_build)

    # Prior turn listed the foundation FIRST; this turn lists it last.
    request = _stream_request_with_state(orchestrator, [foundation, specific])
    pending = asyncio.create_task(
        orchestrator.complete(request, upstream_model="deepseek-v4-flash", api_key=TEST_API_KEY)
    )
    await started.wait()
    await _wait_until(lambda: len(fake_provider.stream_requests) == 1)
    release.set()
    outcome = await pending
    assert outcome.stream is not None

    events = await _drain(outcome.stream)
    assert _streamed_content(events) == "speculative winner "
    assert len(fake_provider.stream_requests) == 1  # nothing rebuilt, nothing reopened
    assert len(assembled) == 1
    meta = json.loads(events[0].split("data: ", 1)[1])
    # The ACTIVE set keeps selector rank order; only the render order is normalized.
    assert [s["id"] for s in meta["skills"]["active"]] == [specific, foundation]


def _reshuffleable_pair(
    orchestrator: ChatOrchestrator,
) -> tuple[list[str], list[str]] | None:
    """Two specific ids whose two orderings resolve to the SAME active set.

    A reshuffle is only the same request when both orders land on one set, and
    domain-foundation admission is RANK-sensitive: two specifics from different
    domains each drag their own foundation in, so under the cap whichever ranks
    first can decide which unit survives. Derive the pair from the catalog the
    orchestrator actually loaded rather than assuming the first two ids qualify
    — that assumption holds only for today's topology and today's cap.
    """
    specifics = [s.id for s in orchestrator.catalog.skills if not s.meta.foundation]
    for first, second in itertools.combinations(specifics, 2):
        forward, reverse = [first, second], [second, first]
        forward_active = _resolved(orchestrator, forward)
        reverse_active = _resolved(orchestrator, reverse)
        # Same SET so the render is byte-identical, different ORDER so a
        # reshuffle is genuinely exercised — a pair resolving to one identical
        # list would pass on the pre-fix code and prove nothing.
        if set(forward_active) == set(reverse_active) and forward_active != reverse_active:
            return forward, reverse
    return None


@pytest.mark.asyncio
async def test_same_tier_reordering_still_reuses_the_speculative_stream(
    orchestrator: ChatOrchestrator,
    fake_provider: FakeProvider,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The classifier ranking the same two skills the other way round is not a
    changed selection either. Render order is canonical WITHIN a tier as well as
    across tiers, so the permutation assembles to identical bytes and the live
    stream must be reused — this used to tear down a producing stream to rebuild
    a payload differing only in which skill section came first."""
    pair = _reshuffleable_pair(orchestrator)
    if pair is None:
        pytest.skip("catalog topology admits no pair whose orderings resolve alike")
    prior, selected = pair
    started, release = _gate_selector(
        monkeypatch,
        orchestrator,
        SelectionDecision(skill_ids=selected, ran=True, degraded=False),
    )
    fake_provider.stream_factory = lambda _request: FakeStream(
        make_text_chunks("speculative winner")
    )
    request = _stream_request_with_state(orchestrator, prior)
    pending = asyncio.create_task(
        orchestrator.complete(request, upstream_model="deepseek-v4-flash", api_key=TEST_API_KEY)
    )
    await started.wait()
    await _wait_until(lambda: len(fake_provider.stream_requests) == 1)
    release.set()
    outcome = await pending
    assert outcome.stream is not None

    events = await _drain(outcome.stream)
    assert _streamed_content(events) == "speculative winner "
    assert len(fake_provider.stream_requests) == 1  # nothing rebuilt, nothing reopened
    meta = json.loads(events[0].split("data: ", 1)[1])
    # The ACTIVE set keeps selector rank order; only the RENDER order is normalized.
    assert [s["id"] for s in meta["skills"]["active"]] == _resolved(orchestrator, selected)


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
        orchestrator.complete(request, upstream_model="deepseek-v4-flash", api_key=TEST_API_KEY)
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
    # Fail-open reuses the PRIOR set verbatim — never re-paired, never re-capped.
    assert [s["id"] for s in meta["skills"]["active"]] == _resolved(
        orchestrator, [skill_id]
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("selection", ["empty", "additional"])
async def test_changed_selection_discards_private_stream_and_opens_canonical(
    selection: str,
    orchestrator: ChatOrchestrator,
    fake_provider: FakeProvider,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # A reshuffle is deliberately NOT a case here: render order is canonical, so
    # the same set in a different order is the same request and must REUSE the
    # private stream — see the reordering-reuse tests above.
    ids = sorted(orchestrator.catalog.known_ids)
    prior = ids[:2]
    selected = {
        "empty": [],
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
            upstream_model="deepseek-v4-flash", api_key=TEST_API_KEY,
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
async def test_unusable_state_speculates_on_the_empty_set_and_wins(
    state_kind: str,
    orchestrator: ChatOrchestrator,
    fake_provider: FakeProvider,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A cold turn guesses "no skills" and races the selector with it.

    Empty / stale / invalid state all supply no usable prior set, so the guess is the
    empty set — which is the single most likely selector answer on an opening message.
    When the selector agrees, the already-open stream is kept and the turn's first byte
    never waits on the upstream open at all. This is the first-turn latency path; the
    old behavior was to sit serial and open only after selection.
    """
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
        orchestrator.complete(request, upstream_model="deepseek-v4-flash", api_key=TEST_API_KEY)
    )
    await started.wait()
    # The speculative stream opens WHILE the selector is still gated — that overlap
    # is the entire point, so assert it before releasing rather than after.
    await _wait_until(lambda: len(fake_provider.stream_requests) == 1)
    release.set()
    outcome = await pending
    assert outcome.stream is not None
    events = await _drain(outcome.stream)
    assert events[0].startswith("event: meta\n")
    # Still ONE upstream open: the guess was exact, so nothing was re-opened.
    assert len(fake_provider.stream_requests) == 1
    meta = json.loads(events[0].split("data: ", 1)[1])
    assert meta["skills"]["active"] == []


@pytest.mark.asyncio
async def test_cold_speculation_is_discarded_when_the_selector_picks_skills(
    orchestrator: ChatOrchestrator,
    fake_provider: FakeProvider,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The other half of the cold-turn trade: a wrong guess costs one discarded open.

    The empty guess loses whenever an opening message genuinely routes to a skill. The
    turn must still be CORRECT — the canonical request is rebuilt with the selected
    skills and the speculative output never reaches the client — it just costs a
    thrown-away prefill. Pinning it here keeps that cost visible and bounded to one
    extra open rather than silently growing.
    """
    ids = sorted(orchestrator.catalog.known_ids)
    started, release = _gate_selector(
        monkeypatch,
        orchestrator,
        SelectionDecision(skill_ids=[ids[0]], ran=True, degraded=False),
    )
    streams: list[FakeStream] = []

    def stream_factory(_request: object) -> FakeStream:
        label = "private loser" if not streams else "canonical winner"
        stream = FakeStream(make_text_chunks(label))
        streams.append(stream)
        return stream

    fake_provider.stream_factory = stream_factory  # type: ignore[assignment]
    pending = asyncio.create_task(
        orchestrator.complete(
            _stream_request_with_state(orchestrator, []),
            upstream_model="deepseek-v4-flash", api_key=TEST_API_KEY,
        )
    )
    await started.wait()
    await _wait_until(lambda: len(streams) == 1)
    release.set()
    outcome = await pending
    assert outcome.stream is not None
    events = await _drain(outcome.stream)

    assert len(fake_provider.stream_requests) == 2  # loser + canonical
    assert streams[0].closed is True
    assert _streamed_content(events) == "canonical winner "
    assert "private loser" not in "".join(events)


# ---- speculation, on the wire, as money ----------------------------------
#
# `turn_cost_with_speculation` is unit-tested directly in
# test_cost_transparency.py. These prove the OTHER half: that `_PreparedTurn`
# actually hands it the losing generation's spend, so a broken handoff cannot
# pass by leaving the arithmetic correct over an empty record.


def _priced_decision(skill_ids: list[str], cost: float = 0.0004) -> SelectionDecision:
    return SelectionDecision(
        skill_ids=skill_ids,
        ran=True,
        degraded=False,
        usage=ProviderUsage(prompt_tokens=3000, cost=cost),
    )


def _done_cost(events: list[str]) -> dict[str, Any]:
    done = json.loads(events[-1].split("data: ", 1)[1])
    assert "cost" in done, "the turn reported no cost at all"
    cost: dict[str, Any] = done["cost"]
    return cost


@pytest.mark.asyncio
async def test_a_reported_losing_speculation_lands_in_the_streamed_total(
    orchestrator: ChatOrchestrator,
    fake_provider: FakeProvider,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The caller paid for the guess that lost. It belongs in their total.

    The loser finishes before it is discarded, so OpenRouter did send its final
    usage chunk and the spend is exactly known — nothing about this turn is a
    floor.
    """
    ids = sorted(orchestrator.catalog.known_ids)
    started, release = _gate_selector(monkeypatch, orchestrator, _priced_decision([ids[0]]))
    streams: list[FakeStream] = []

    def stream_factory(_request: object) -> FakeStream:
        first = not streams
        stream = FakeStream(
            make_text_chunks("private loser" if first else "canonical winner",
                             cost=0.001 if first else 0.002)
        )
        streams.append(stream)
        return stream

    fake_provider.stream_factory = stream_factory  # type: ignore[assignment]
    pending = asyncio.create_task(
        orchestrator.complete(
            _stream_request_with_state(orchestrator, []),
            upstream_model="deepseek-v4-flash", api_key=TEST_API_KEY,
        )
    )
    await started.wait()
    # Released only once the loser has closed, which is the moment AFTER its
    # producer published usage — otherwise the discard races the final chunk and
    # the test silently becomes the "cancelled mid-flight" case below.
    await _wait_until(lambda: bool(streams) and streams[0].closed)
    release.set()
    outcome = await pending
    assert outcome.stream is not None
    cost = _done_cost(await _drain(outcome.stream))

    assert cost["main"] == pytest.approx(0.003), "canonical + the discarded guess"
    assert cost["selector"] == pytest.approx(0.0004)
    assert cost["total"] == pytest.approx(0.0034)
    assert cost["complete"] is True


@pytest.mark.asyncio
async def test_a_speculation_cancelled_mid_flight_makes_the_streamed_total_a_floor(
    orchestrator: ChatOrchestrator,
    fake_provider: FakeProvider,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Real spend that nothing can measure — so the total says so.

    Usage arrives only in a stream's final chunk. A loser cut off before that
    charged the caller an amount no API call can recover, and the honest report
    is a total that admits it is a lower bound.
    """
    ids = sorted(orchestrator.catalog.known_ids)
    started, release = _gate_selector(monkeypatch, orchestrator, _priced_decision([ids[0]]))
    streams: list[FakeStream] = []

    def stream_factory(_request: object) -> FakeStream:
        first = not streams
        stream = FakeStream(
            make_text_chunks("private loser", with_usage=False)
            if first
            else make_text_chunks("canonical winner", cost=0.002)
        )
        streams.append(stream)
        return stream

    fake_provider.stream_factory = stream_factory  # type: ignore[assignment]
    pending = asyncio.create_task(
        orchestrator.complete(
            _stream_request_with_state(orchestrator, []),
            upstream_model="deepseek-v4-flash", api_key=TEST_API_KEY,
        )
    )
    await started.wait()
    await _wait_until(lambda: len(streams) == 1)
    release.set()
    outcome = await pending
    assert outcome.stream is not None
    cost = _done_cost(await _drain(outcome.stream))

    assert cost["main"] == pytest.approx(0.002), "only what was observed"
    assert cost["complete"] is False


@pytest.mark.asyncio
async def test_a_declined_claim_still_bills_the_generation_it_throws_away(
    orchestrator: ChatOrchestrator,
    fake_provider: FakeProvider,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The other way a speculation is discarded — and the one nothing counted.

    Here the selector AGREES with the guess, so the mismatch path that records
    discarded spend never runs. The claim is then declined anyway (a private
    post-open failure), the stream is thrown away, and a canonical one replaces
    it. That generation opened upstream and was billed exactly like a mismatched
    one, so a total reported as exact would be short by a whole stream.
    """
    skill_id = sorted(orchestrator.catalog.known_ids)[0]
    started, release = _gate_selector(monkeypatch, orchestrator, _priced_decision([skill_id]))
    streams: list[FakeStream] = []

    def stream_factory(_request: object) -> FakeStream:
        first = not streams
        stream = FakeStream(
            # Opens, produces, then fails privately — no byte of it was public,
            # so the claim declines and falls back to one canonical open.
            make_text_chunks("private loser", with_usage=False) if first
            else make_text_chunks("canonical winner", cost=0.002),
            error_at=2 if first else None,
            error=RuntimeError("private post-open failure") if first else None,
        )
        streams.append(stream)
        return stream

    fake_provider.stream_factory = stream_factory  # type: ignore[assignment]
    pending = asyncio.create_task(
        orchestrator.complete(
            _stream_request_with_state(orchestrator, [skill_id]),
            upstream_model="deepseek-v4-flash", api_key=TEST_API_KEY,
        )
    )
    await started.wait()
    await _wait_until(lambda: len(streams) == 1)
    release.set()
    outcome = await pending
    assert outcome.stream is not None
    events = await _drain(outcome.stream)

    assert len(streams) == 2, "the declined claim must have fallen back to a canonical open"
    assert _streamed_content(events) == "canonical winner "
    cost = _done_cost(events)
    assert cost["main"] == pytest.approx(0.002)
    assert cost["complete"] is False, "a thrown-away generation was billed and not measured"


@pytest.mark.asyncio
@pytest.mark.parametrize("docs_query", ["create worktree", None])
async def test_a_docs_hit_disqualifies_an_otherwise_exact_speculation(
    orchestrator: ChatOrchestrator,
    fake_provider: FakeProvider,
    monkeypatch: pytest.MonkeyPatch,
    docs_query: str | None,
) -> None:
    """Matching skill ids do not make it the same request.

    Speculation opens before selection finishes, so its prompt cannot contain
    documentation fetched afterwards. Reusing it would stream an answer generated
    without the pages it is supposed to be citing. Parameterized against its own
    control: with no docs lookup, the identical setup reuses the live stream —
    so this proves the docs condition is what forces the rebuild, not the setup.
    """
    import httpx

    from daintree_assistant_server.services.docs_mcp import DocsMcpClient

    body = {
        "jsonrpc": "2.0",
        "id": 1,
        "result": {
            "content": [
                {
                    "type": "text",
                    "text": json.dumps(
                        {"q": "q", "results": [{"url": "/docs/worktrees", "chunkText": "body"}]}
                    ),
                }
            ]
        },
    }
    orchestrator.docs_client = DocsMcpClient(
        httpx.AsyncClient(
            transport=httpx.MockTransport(lambda _r: httpx.Response(200, json=body))
        )
    )
    skill_id = sorted(orchestrator.catalog.known_ids)[0]
    started, release = _gate_selector(
        monkeypatch,
        orchestrator,
        SelectionDecision(
            skill_ids=[skill_id], ran=True, degraded=False, docs_query=docs_query
        ),
    )
    fake_provider.stream_chunks = lambda _request: make_text_chunks(  # type: ignore[assignment]
        "answer"
    )
    pending = asyncio.create_task(
        orchestrator.complete(
            _stream_request_with_state(orchestrator, [skill_id]),
            upstream_model="deepseek-v4-flash", api_key=TEST_API_KEY,
        )
    )
    await started.wait()
    await _wait_until(lambda: len(fake_provider.stream_requests) == 1)
    release.set()
    outcome = await pending
    assert outcome.stream is not None
    await _drain(outcome.stream)

    sent = fake_provider.stream_requests[-1].messages[-1]["content"]
    if docs_query is None:
        assert len(fake_provider.stream_requests) == 1, "no docs: the guess still wins"
        assert "daintree_docs" not in sent
    else:
        assert len(fake_provider.stream_requests) == 2, "docs: the guess must be rebuilt"
        assert "daintree_docs" in sent


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
    outcome = await orchestrator.complete(request, upstream_model="deepseek-v4-flash", api_key=TEST_API_KEY)
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
            upstream_model="deepseek-v4-flash", api_key=TEST_API_KEY,
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
            upstream_model="deepseek-v4-flash", api_key=TEST_API_KEY,
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
            upstream_model="deepseek-v4-flash", api_key=TEST_API_KEY,
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
            upstream_model="deepseek-v4-flash", api_key=TEST_API_KEY,
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
            upstream_model="deepseek-v4-flash", api_key=TEST_API_KEY,
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
    # The constant now lives with the speculation machinery it bounds; patching
    # the orchestrator module would silently no-op.
    from daintree_assistant_server.services import speculation as speculation_module

    monkeypatch.setattr(speculation_module, "_SPECULATION_CLOSE_TIMEOUT_SECONDS", 0.01)
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
            upstream_model="deepseek-v4-flash", api_key=TEST_API_KEY,
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
        upstream_model="deepseek-v4-flash", api_key=TEST_API_KEY,
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


@pytest.mark.asyncio
async def test_respond_log_reports_the_ids_the_cap_dropped(
    orchestrator: ChatOrchestrator,
    fake_provider: FakeProvider,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Cap truncation used to be invisible. It is measured on the structured log
    line only — no new wire warning code the CLI would have to learn."""
    ids = sorted(orchestrator.catalog.known_ids)[:6]

    async def select(*args: object, **kwargs: object) -> SelectionDecision:
        return SelectionDecision(skill_ids=ids, ran=True, degraded=False)

    monkeypatch.setattr(orchestrator.selector, "select", select)
    records: list[tuple[str, dict[str, Any]]] = []

    class _Recorder:
        def info(self, event: str, **kwargs: Any) -> None:
            records.append((event, kwargs))

        def warning(self, event: str, **kwargs: Any) -> None:
            records.append((event, kwargs))

        def exception(self, event: str, **kwargs: Any) -> None:
            records.append((event, kwargs))

    monkeypatch.setattr(orchestrator_module, "logger", _Recorder())
    outcome = await orchestrator.complete(
        _basic_stream_request(), upstream_model="deepseek-v4-flash", api_key=TEST_API_KEY
    )
    assert outcome.stream is not None
    await _drain(outcome.stream)

    respond = next(fields for event, fields in records if event == "respond")
    dropped = respond["dropped_over_cap"]
    assert dropped, "a 6-id selection against a cap of 3 must report drops"
    assert set(dropped).isdisjoint(respond["active_skills"])
    assert set(dropped) <= set(ids)


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
            "input": {"messages": [{"role": "user", "content": "rename my terminals"}]},
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
        _basic_stream_request(), upstream_model="deepseek-v4-flash", api_key=TEST_API_KEY
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
        _basic_stream_request(), upstream_model="deepseek-v4-flash", api_key=TEST_API_KEY
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
        _basic_stream_request(), upstream_model="deepseek-v4-flash", api_key=TEST_API_KEY
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
        _basic_stream_request(), upstream_model="deepseek-v4-flash", api_key=TEST_API_KEY
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
        _basic_stream_request(), upstream_model="deepseek-v4-flash", api_key=TEST_API_KEY
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
        _basic_stream_request(), upstream_model="deepseek-v4-flash", api_key=TEST_API_KEY
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
        _basic_stream_request(), upstream_model="deepseek-v4-flash", api_key=TEST_API_KEY
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
        upstream_model="deepseek-v4-flash", api_key=TEST_API_KEY,
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
        _basic_stream_request(), upstream_model="deepseek-v4-flash", api_key=TEST_API_KEY
    )
    assert outcome.stream is not None
    events = [e for e in await _drain(outcome.stream) if not e.startswith(":")]
    assert events[0].startswith("event: meta\n")
    assert events[-1].startswith("event: done\n")
    assert sum(1 for e in events if e.startswith("event: done\n")) == 1
    assert not any(e.startswith("event: error\n") for e in events)


# ---- bare tool intent: discard invariants (#25) -----------------------------

_DEAD_ROUND = "All five votes are in. Let me read them, one read per terminal, batched."
_INTENT_TOOL = {
    "type": "function",
    "function": {
        "name": "terminal__extract",
        "parameters": {
            "type": "object",
            "additionalProperties": False,
            "required": ["terminalIds"],
            "properties": {"terminalIds": {"type": "array", "items": {"type": "string"}}},
        },
    },
}


def _intent_stream_request(session_id: str = "bi-session") -> RespondRequest:
    """A streaming turn that offers a tool — the only shape the gate arms for."""
    return RespondRequest.model_validate(
        {
            "protocol_version": 2,
            "session": {"id": session_id, "turn_id": "turn-1"},
            "input": {
                "messages": [{"role": "user", "content": "read the votes"}],
                "tools": [_INTENT_TOOL],
            },
            "startup": {},
            "generation": {"stream": True},
        }
    )


def _nudged(request: object) -> bool:
    return any(
        "previous draft was discarded" in (m.get("content") or "")
        for m in request.messages  # type: ignore[attr-defined]
    )


def _priced_selection(cost: float = 0.0004) -> ProviderChatResult:
    """A selector run that reported its cost.

    The default fake selector fails open and reports no cost, which alone clears
    `cost.complete`. Cost assertions about the MAIN slot need the selector out of
    the way, or every one of them passes for the wrong reason.
    """
    return selection_result([], usage=ProviderUsage(prompt_tokens=3000, cost=cost))


@pytest.mark.asyncio
async def test_stream_discarded_draft_never_reaches_skill_learning(
    orchestrator: ChatOrchestrator,
    fake_provider: FakeProvider,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Learning must see the workflow that actually happened, not the dead draft.

    Feeding it the discarded announcement would teach a skill from a round that
    performed nothing — the exact behavior this repair exists to erase.
    """
    seen: list[str] = []
    monkeypatch.setattr(
        orchestrator,
        "_maybe_learn_after_turn",
        lambda **kwargs: seen.append(kwargs["assistant_text"]),
    )
    fake_provider.stream_chunks = lambda request: (  # type: ignore[assignment]
        make_text_chunks("Read five votes; here is the tally.")
        if _nudged(request)
        else make_text_chunks(_DEAD_ROUND)
    )
    outcome = await orchestrator.complete(
        _intent_stream_request(), upstream_model="deepseek-v4-flash", api_key=TEST_API_KEY
    )
    assert outcome.stream is not None
    await _drain(outcome.stream)
    assert seen == ["Read five votes; here is the tally. "]
    assert _DEAD_ROUND not in "".join(seen)


@pytest.mark.asyncio
async def test_stream_discarded_attempt_is_closed_before_the_reroll_opens(
    orchestrator: ChatOrchestrator,
    fake_provider: FakeProvider,
) -> None:
    """The discarded producer must not be left to the outer teardown.

    A speculative first attempt holds an overlap slot until it closes, so the
    replacement open would otherwise queue behind capacity nobody still needs.
    """
    streams: list[FakeStream] = []
    # Recorded, never asserted, INSIDE the factory: an AssertionError raised there
    # is not a DaintreeError, so it unwinds into _stream_with_heartbeat's broad
    # handler and becomes an SSE `error` event instead of failing the test.
    opens: list[tuple[bool, list[bool]]] = []

    def stream_factory(request: object) -> FakeStream:
        nudged = _nudged(request)
        opens.append((nudged, [s.closed for s in streams]))
        stream = FakeStream(make_text_chunks("Tallied." if nudged else _DEAD_ROUND))
        streams.append(stream)
        return stream

    fake_provider.stream_factory = stream_factory  # type: ignore[assignment]
    outcome = await orchestrator.complete(
        _intent_stream_request("bi-close"), upstream_model="deepseek-v4-flash",
        api_key=TEST_API_KEY,
    )
    assert outcome.stream is not None
    events = await _drain(outcome.stream)
    assert not any(e.startswith("event: error\n") for e in events)
    assert [nudged for nudged, _ in opens] == [False, True]
    # The re-roll opened only after the draft it replaces had already been closed.
    assert opens[0][1] == []
    assert opens[1][1] == [True]
    assert all(s.closed for s in streams)


@pytest.mark.asyncio
async def test_stream_reroll_that_half_reported_cost_is_not_labelled_complete(
    orchestrator: ChatOrchestrator,
    fake_provider: FakeProvider,
) -> None:
    """A re-roll bills TWICE; one silent attempt makes the reported total a floor.

    The discarded draft is summed into the accepted one, so `cost.main` is
    non-null and looks answerable. It is short by whatever the silent attempt
    charged, and `complete: true` would present that as an exact receipt — the
    one place the flag said "complete" over a number that was not.
    """
    fake_provider.selection_result = _priced_selection()
    fake_provider.stream_chunks = lambda request: (  # type: ignore[assignment]
        make_text_chunks("Tallied.")
        if _nudged(request)
        else make_text_chunks(_DEAD_ROUND, cost=0.002)
    )
    outcome = await orchestrator.complete(
        _intent_stream_request("bi-cost"), upstream_model="deepseek-v4-flash",
        api_key=TEST_API_KEY,
    )
    assert outcome.stream is not None
    events = await _drain(outcome.stream)
    done = json.loads(events[-1].split("data: ", 1)[1])
    assert done["cost"]["main"] == pytest.approx(0.002)
    assert done["cost"]["complete"] is False


@pytest.mark.asyncio
async def test_stream_reroll_with_both_attempts_reported_stays_complete(
    orchestrator: ChatOrchestrator,
    fake_provider: FakeProvider,
) -> None:
    """The floor rule must not swallow an ordinary re-roll that reported twice."""
    fake_provider.selection_result = _priced_selection()
    fake_provider.stream_chunks = lambda request: (  # type: ignore[assignment]
        make_text_chunks("Tallied.", cost=0.003)
        if _nudged(request)
        else make_text_chunks(_DEAD_ROUND, cost=0.002)
    )
    outcome = await orchestrator.complete(
        _intent_stream_request("bi-cost-ok"), upstream_model="deepseek-v4-flash",
        api_key=TEST_API_KEY,
    )
    assert outcome.stream is not None
    events = await _drain(outcome.stream)
    done = json.loads(events[-1].split("data: ", 1)[1])
    assert done["cost"]["main"] == pytest.approx(0.005)
    assert done["cost"]["complete"] is True


@pytest.mark.asyncio
async def test_stream_single_attempt_cost_is_still_complete(
    orchestrator: ChatOrchestrator,
    fake_provider: FakeProvider,
) -> None:
    """The accumulator starts empty; that must not read as a silent first call.

    If the empty accumulator were summed as a real record, EVERY streamed turn
    would report `complete: false` — the fix would have traded a rare wrong
    "complete" for a universal wrong "floor".
    """
    fake_provider.selection_result = _priced_selection()
    fake_provider.stream_chunks = lambda _request: make_text_chunks(  # type: ignore[assignment]
        "Read five votes; here is the tally.", cost=0.004
    )
    outcome = await orchestrator.complete(
        _intent_stream_request("bi-cost-single"), upstream_model="deepseek-v4-flash",
        api_key=TEST_API_KEY,
    )
    assert outcome.stream is not None
    events = await _drain(outcome.stream)
    done = json.loads(events[-1].split("data: ", 1)[1])
    assert done["cost"]["main"] == pytest.approx(0.004)
    assert done["cost"]["complete"] is True


def _intent_body_request(session_id: str = "bi-body") -> RespondRequest:
    """The same tool-offering turn, NON-streaming: the re-roll lives in
    `_repair_bare_intent` rather than in the stream loop, and its summed usage
    reaches the response body instead of a `done` event."""
    return RespondRequest.model_validate(
        {
            "protocol_version": 2,
            "session": {"id": session_id, "turn_id": "turn-1"},
            "input": {
                "messages": [{"role": "user", "content": "read the votes"}],
                "tools": [_INTENT_TOOL],
            },
            "startup": {},
            "generation": {"stream": False},
        }
    )


@pytest.mark.asyncio
async def test_body_reroll_that_half_reported_cost_is_not_labelled_complete(
    orchestrator: ChatOrchestrator,
    fake_provider: FakeProvider,
) -> None:
    """The non-streaming half of the same defect.

    `_repair_bare_intent` sums the discarded draft into the accepted reply on a
    plain `ProviderChatResult`, a different code path from the stream loop's
    accumulator — so the completeness bit has to survive that assignment too.
    """
    fake_provider.selection_result = _priced_selection()
    fake_provider.complete_result = [
        make_result(content=_DEAD_ROUND, usage=ProviderUsage(cost=0.002)),
        make_result(content="Read five votes; here is the tally."),
    ]
    outcome = await orchestrator.complete(
        _intent_body_request("bi-body-cost"), upstream_model="deepseek-v4-flash",
        api_key=TEST_API_KEY,
    )
    assert outcome.body is not None
    assert len(fake_provider.requests) == 2, "the draft must actually have been re-rolled"
    assert outcome.body["cost"]["main"] == pytest.approx(0.002)
    assert outcome.body["cost"]["complete"] is False


@pytest.mark.asyncio
async def test_body_reroll_with_both_attempts_reported_stays_complete(
    orchestrator: ChatOrchestrator,
    fake_provider: FakeProvider,
) -> None:
    """Positive control for the path above: a fully-reported re-roll is exact."""
    fake_provider.selection_result = _priced_selection()
    fake_provider.complete_result = [
        make_result(content=_DEAD_ROUND, usage=ProviderUsage(cost=0.002)),
        make_result(
            content="Read five votes; here is the tally.", usage=ProviderUsage(cost=0.003)
        ),
    ]
    outcome = await orchestrator.complete(
        _intent_body_request("bi-body-ok"), upstream_model="deepseek-v4-flash",
        api_key=TEST_API_KEY,
    )
    assert outcome.body is not None
    assert outcome.body["cost"]["main"] == pytest.approx(0.005)
    assert outcome.body["cost"]["complete"] is True


@pytest.mark.asyncio
async def test_a_repaired_selection_that_half_reported_makes_the_turn_a_floor(
    orchestrator: ChatOrchestrator,
    fake_provider: FakeProvider,
) -> None:
    """The selector bills twice when its first JSON is malformed.

    Its partial sum never reaches the wire as its own flag — `cost.complete` is
    the single field a client is told to trust — so this proves the fact travels
    the internal route (`_RespondMeta.selector_cost_complete`) all the way there.
    """
    fake_provider.selection_result = [
        make_result(content="not json at all", usage=ProviderUsage(cost=0.0004)),
        selection_result([], usage=ProviderUsage(prompt_tokens=3000)),
    ]
    fake_provider.complete_result = make_result(
        content="Read five votes; here is the tally.", usage=ProviderUsage(cost=0.002)
    )
    outcome = await orchestrator.complete(
        _intent_body_request("sel-cost"), upstream_model="deepseek-v4-flash",
        api_key=TEST_API_KEY,
    )
    assert outcome.body is not None
    assert len(fake_provider.selection_requests) == 2, "the selection must have been repaired"
    assert outcome.body["cost"]["selector"] == pytest.approx(0.0004)
    assert outcome.body["cost"]["complete"] is False


@pytest.mark.asyncio
async def test_a_repaired_selection_that_reported_twice_stays_complete(
    orchestrator: ChatOrchestrator,
    fake_provider: FakeProvider,
) -> None:
    """Positive control: a repair is not by itself a reason to report a floor."""
    fake_provider.selection_result = [
        make_result(content="not json at all", usage=ProviderUsage(cost=0.0004)),
        selection_result([], usage=ProviderUsage(prompt_tokens=3000, cost=0.0005)),
    ]
    fake_provider.complete_result = make_result(
        content="Read five votes; here is the tally.", usage=ProviderUsage(cost=0.002)
    )
    outcome = await orchestrator.complete(
        _intent_body_request("sel-cost-ok"), upstream_model="deepseek-v4-flash",
        api_key=TEST_API_KEY,
    )
    assert outcome.body is not None
    assert outcome.body["cost"]["selector"] == pytest.approx(0.0009)
    assert outcome.body["cost"]["complete"] is True


@pytest.mark.asyncio
@pytest.mark.parametrize("priced_first", [True, False])
async def test_a_selector_that_never_parsed_still_reports_what_it_billed(
    orchestrator: ChatOrchestrator,
    fake_provider: FakeProvider,
    priced_first: bool,
) -> None:
    """Both selector calls were billed; neither produced a usable selection.

    `degraded_open` carries the summed usage out of a selection that failed —
    the turn still owes the caller that money — and the summed figure is short
    whichever of the two attempts went unreported, so the order is parameterized
    rather than assumed.
    """
    priced, silent = ProviderUsage(cost=0.0004), ProviderUsage()
    fake_provider.selection_result = [
        make_result(content="not json at all", usage=priced if priced_first else silent),
        make_result(content="still not json", usage=silent if priced_first else priced),
    ]
    fake_provider.complete_result = make_result(
        content="Read five votes; here is the tally.", usage=ProviderUsage(cost=0.002)
    )
    outcome = await orchestrator.complete(
        _intent_body_request(f"sel-degraded-{priced_first}"),
        upstream_model="deepseek-v4-flash", api_key=TEST_API_KEY,
    )
    assert outcome.body is not None
    assert len(fake_provider.selection_requests) == 2
    assert outcome.body["skills"]["selector"]["degraded"] is True
    assert outcome.body["cost"]["selector"] == pytest.approx(0.0004)
    assert outcome.body["cost"]["total"] == pytest.approx(0.0024)
    assert outcome.body["cost"]["complete"] is False


@pytest.mark.asyncio
async def test_a_degraded_selector_that_reported_both_calls_stays_complete(
    orchestrator: ChatOrchestrator,
    fake_provider: FakeProvider,
) -> None:
    """Failing to parse is not the same as failing to measure.

    A selection that degraded still knows exactly what it spent when both calls
    reported, and marking that turn a floor would be its own wrong answer.
    """
    fake_provider.selection_result = [
        make_result(content="not json at all", usage=ProviderUsage(cost=0.0004)),
        make_result(content="still not json", usage=ProviderUsage(cost=0.0005)),
    ]
    fake_provider.complete_result = make_result(
        content="Read five votes; here is the tally.", usage=ProviderUsage(cost=0.002)
    )
    outcome = await orchestrator.complete(
        _intent_body_request("sel-degraded-ok"), upstream_model="deepseek-v4-flash",
        api_key=TEST_API_KEY,
    )
    assert outcome.body is not None
    assert outcome.body["skills"]["selector"]["degraded"] is True
    assert outcome.body["cost"]["selector"] == pytest.approx(0.0009)
    assert outcome.body["cost"]["complete"] is True


@pytest.mark.asyncio
async def test_a_reported_zero_survives_the_stream_as_an_exact_total(
    orchestrator: ChatOrchestrator,
    fake_provider: FakeProvider,
) -> None:
    """`0.0` is falsy, and it travels through parsing, summing and narrowing.

    A fully-cached or promotional call genuinely costs nothing. Any truthiness
    check on the way to the wire would turn that measurement into "unreported"
    and put a floor on a turn we know exactly.
    """
    fake_provider.selection_result = _priced_selection(0.0)
    fake_provider.stream_chunks = lambda _request: make_text_chunks(  # type: ignore[assignment]
        "Read five votes; here is the tally.", cost=0.0
    )
    outcome = await orchestrator.complete(
        _intent_stream_request("zero-cost"), upstream_model="deepseek-v4-flash",
        api_key=TEST_API_KEY,
    )
    assert outcome.stream is not None
    done = json.loads((await _drain(outcome.stream))[-1].split("data: ", 1)[1])
    assert done["usage"]["cost"] == 0.0
    assert done["cost"]["main"] == 0.0
    assert done["cost"]["total"] == 0.0
    assert done["cost"]["complete"] is True


@pytest.mark.asyncio
async def test_stream_bare_intent_holds_content_until_the_reply_ends(
    orchestrator: ChatOrchestrator,
    fake_provider: FakeProvider,
) -> None:
    """No byte of a discardable draft may go out before the discard decision.

    This is the invariant the whole gate exists for: once a content delta is on
    the wire it cannot be unsent, so a round still eligible for discard must have
    emitted none.
    """
    emitted: list[str] = []
    fake_provider.stream_chunks = lambda request: (  # type: ignore[assignment]
        make_text_chunks("Tallied.") if _nudged(request) else make_text_chunks(_DEAD_ROUND)
    )
    outcome = await orchestrator.complete(
        _intent_stream_request("bi-hold"), upstream_model="deepseek-v4-flash",
        api_key=TEST_API_KEY,
    )
    assert outcome.stream is not None
    async for event in outcome.stream:  # type: ignore[attr-defined]
        emitted.append(event)
        # Before the second stream opens, the only public events are meta/heartbeat.
        if len(fake_provider.stream_requests) < 2:
            assert not event.startswith("event: delta\n")
    content = _streamed_content(emitted)
    assert content.strip() == "Tallied."
