"""Respond orchestrator unit tests (active-set resolution + selector cadence)."""

from __future__ import annotations

import asyncio
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
    merge_active_skills,
    resolve_active_skills,
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


# ---- endpoint attribution for cache_ratio (issue #41) -------------------
#
# `cache_ratio` was logged without saying which of OpenRouter's ~24 endpoints
# produced it. Each keeps its own node-local prefix cache, so a prompt layout
# regression and a routing switch were the same number. These tests pin the
# attribution end-to-end through both response paths.


def _turn_usage(logs: list[dict[str, Any]]) -> dict[str, Any]:
    entries = [entry for entry in logs if entry["event"] == "turn_usage"]
    assert len(entries) == 1, f"expected exactly one turn_usage record, got {len(entries)}"
    return entries[0]


@pytest.mark.asyncio
async def test_body_turn_usage_names_the_endpoint_that_served_it(
    orchestrator: ChatOrchestrator,
    fake_provider: FakeProvider,
) -> None:
    """The main and selector ratios each carry their own host.

    They are different prompt prefixes on separately-routed calls, so they share
    no cache and regress independently — one endpoint field could only ever be
    right about one of them.
    """
    from structlog.testing import capture_logs

    fake_provider.selection_result = selection_result(
        [],
        usage=ProviderUsage(prompt_tokens=3000, cached_tokens=2400, cost=0.0004),
        endpoint="novita",
    )
    fake_provider.complete_result = make_result(
        content="Read five votes; here is the tally.",
        usage=ProviderUsage(prompt_tokens=1000, completion_tokens=50, cached_tokens=750),
        endpoint="deepinfra",
    )
    with capture_logs() as logs:
        await orchestrator.complete(
            _intent_body_request("bi-endpoint"), upstream_model="deepseek-v4-flash",
            api_key=TEST_API_KEY,
        )
    entry = _turn_usage(logs)
    assert entry["endpoint"] == "deepinfra"
    assert entry["cache_ratio"] == 0.75
    assert entry["selector_endpoint"] == "novita"
    assert entry["selector_cache_ratio"] == 0.8
    # The selector's host is NOT folded into the main count: it lands elsewhere
    # routinely under throughput sorting, and counting it would make a switch
    # look like it happened on every turn.
    assert entry["distinct_endpoints"] == 1
    # The key that lets a query span the turn's later continuation rounds, which
    # are separate requests this stateless server cannot count across.
    assert entry["turn_id"] == "turn-1"
    assert entry["round"] == 0


@pytest.mark.asyncio
async def test_body_reroll_keeps_the_discarded_drafts_endpoint(
    orchestrator: ChatOrchestrator,
    fake_provider: FakeProvider,
) -> None:
    """The trap this change exists to avoid.

    `_repair_bare_intent` returns the repaired result in the original's place,
    carrying only the summed usage across. The discarded draft's host is
    unrecoverable after that assignment — and a re-roll is exactly where a
    routing switch blends two caches into the one ratio being logged, so losing
    it would lose the measurement on the turns that need it most.
    """
    from structlog.testing import capture_logs

    fake_provider.selection_result = _priced_selection()
    fake_provider.complete_result = [
        make_result(
            content=_DEAD_ROUND,
            usage=ProviderUsage(prompt_tokens=1000, cached_tokens=900),
            endpoint="deepinfra",
        ),
        make_result(
            content="Read five votes; here is the tally.",
            usage=ProviderUsage(prompt_tokens=1000, cached_tokens=0),
            endpoint="novita",
        ),
    ]
    with capture_logs() as logs:
        await orchestrator.complete(
            _intent_body_request("bi-endpoint-reroll"), upstream_model="deepseek-v4-flash",
            api_key=TEST_API_KEY,
        )
    assert len(fake_provider.requests) == 2, "the draft must actually have been re-rolled"
    # The re-roll is built with `dataclasses.replace`, so it inherits the affinity
    # key — and it must. A re-roll that dropped it would send the SAME prompt under
    # a different sticky key, splitting one turn across two cohorts and evicting the
    # endpoint that just served the draft: a guaranteed cold prefix on the retry.
    keys = [r.cache_session_id for r in fake_provider.requests]
    assert keys[0] is not None
    assert keys[0] == keys[1]
    entry = _turn_usage(logs)
    assert entry["distinct_endpoints"] == 2
    # The accepted attempt is the attribution; the count is what says the ratio
    # beside it averages two caches that were never the same cache.
    assert entry["endpoint"] == "novita"
    assert entry["cache_ratio"] == 0.45


@pytest.mark.asyncio
async def test_body_reroll_onto_the_same_endpoint_is_not_a_switch(
    orchestrator: ChatOrchestrator,
    fake_provider: FakeProvider,
) -> None:
    """Positive control: a re-roll that landed back on the same host blended
    nothing, and must not read as a routing switch."""
    from structlog.testing import capture_logs

    fake_provider.selection_result = _priced_selection()
    fake_provider.complete_result = [
        make_result(content=_DEAD_ROUND, endpoint="deepinfra"),
        make_result(content="Read five votes; here is the tally.", endpoint="deepinfra"),
    ]
    with capture_logs() as logs:
        await orchestrator.complete(
            _intent_body_request("bi-endpoint-same"), upstream_model="deepseek-v4-flash",
            api_key=TEST_API_KEY,
        )
    entry = _turn_usage(logs)
    assert entry["distinct_endpoints"] == 1
    assert entry["endpoint"] == "deepinfra"


@pytest.mark.asyncio
async def test_stream_turn_usage_names_the_endpoint_that_served_it(
    orchestrator: ChatOrchestrator,
    fake_provider: FakeProvider,
) -> None:
    """The streaming half: the accepted stream's host reaches the log line."""
    from structlog.testing import capture_logs

    fake_provider.selection_result = selection_result(
        [], usage=ProviderUsage(prompt_tokens=3000, cost=0.0004), endpoint="novita"
    )
    fake_provider.stream_factory = lambda _request: FakeStream(
        make_text_chunks("Read five votes; here is the tally."), endpoint="deepinfra"
    )
    with capture_logs() as logs:
        outcome = await orchestrator.complete(
            _intent_stream_request("bi-endpoint-stream"),
            upstream_model="deepseek-v4-flash",
            api_key=TEST_API_KEY,
        )
        assert outcome.stream is not None
        await _drain(outcome.stream)
    entry = _turn_usage(logs)
    assert entry["endpoint"] == "deepinfra"
    assert entry["distinct_endpoints"] == 1
    assert entry["selector_endpoint"] == "novita"
    assert entry["stream"] is True


@pytest.mark.asyncio
async def test_stream_reroll_counts_both_endpoints(
    orchestrator: ChatOrchestrator,
    fake_provider: FakeProvider,
) -> None:
    """The stream loop's accumulator tracks the same attempts its usage does.

    Both folds update it, so the discarded draft's host survives the re-roll the
    way its tokens do.
    """
    from structlog.testing import capture_logs

    fake_provider.selection_result = _priced_selection()
    fake_provider.stream_factory = lambda request: FakeStream(
        make_text_chunks("Read five votes; here is the tally.")
        if _nudged(request)
        else make_text_chunks(_DEAD_ROUND),
        endpoint="novita" if _nudged(request) else "deepinfra",
    )
    with capture_logs() as logs:
        outcome = await orchestrator.complete(
            _intent_stream_request("bi-endpoint-stream-reroll"),
            upstream_model="deepseek-v4-flash",
            api_key=TEST_API_KEY,
        )
        assert outcome.stream is not None
        await _drain(outcome.stream)
    assert len(fake_provider.stream_requests) == 2, "the draft must have been re-rolled"
    entry = _turn_usage(logs)
    assert entry["distinct_endpoints"] == 2
    assert entry["endpoint"] == "novita"


@pytest.mark.asyncio
async def test_a_provider_that_names_no_endpoint_reports_none_not_a_switch(
    orchestrator: ChatOrchestrator,
    fake_provider: FakeProvider,
) -> None:
    """`None` is "not reported", not a host.

    A single-host provider never sets it, and counting the absence would invent
    a routing switch out of a provider that simply stayed quiet.
    """
    from structlog.testing import capture_logs

    fake_provider.selection_result = _priced_selection()
    fake_provider.complete_result = make_result(content="Read five votes; here is the tally.")
    with capture_logs() as logs:
        await orchestrator.complete(
            _intent_body_request("bi-endpoint-silent"), upstream_model="deepseek-v4-flash",
            api_key=TEST_API_KEY,
        )
    entry = _turn_usage(logs)
    assert entry["endpoint"] is None
    assert entry["distinct_endpoints"] == 0
    assert entry["selector_endpoint"] is None


@pytest.mark.asyncio
async def test_an_accepted_reroll_that_named_no_host_does_not_inherit_the_drafts(
    orchestrator: ChatOrchestrator,
    fake_provider: FakeProvider,
) -> None:
    """The stale-attribution trap on the accepted fold.

    `main_endpoint = opened_stream.endpoint or main_endpoint` reads as harmless
    and is not: when the accepted re-roll names no host, the OR silently labels
    the ratio with the DISCARDED draft's endpoint — a node that served none of
    the tokens being reported. `None` is the honest answer.
    """
    from structlog.testing import capture_logs

    fake_provider.selection_result = _priced_selection()
    fake_provider.stream_factory = lambda request: FakeStream(
        make_text_chunks("Read five votes; here is the tally.")
        if _nudged(request)
        else make_text_chunks(_DEAD_ROUND),
        endpoint=None if _nudged(request) else "deepinfra",
    )
    with capture_logs() as logs:
        outcome = await orchestrator.complete(
            _intent_stream_request("bi-endpoint-stale"),
            upstream_model="deepseek-v4-flash",
            api_key=TEST_API_KEY,
        )
        assert outcome.stream is not None
        await _drain(outcome.stream)
    assert len(fake_provider.stream_requests) == 2, "the draft must have been re-rolled"
    entry = _turn_usage(logs)
    assert entry["endpoint"] is None, "the accepted attempt named no host"
    # The draft's host is still KNOWN to have contributed tokens, so it is still
    # counted — the count and the attribution answer different questions.
    assert entry["distinct_endpoints"] == 1


@pytest.mark.asyncio
async def test_a_reroll_that_never_opened_keeps_the_drafts_endpoint(
    orchestrator: ChatOrchestrator,
    fake_provider: FakeProvider,
) -> None:
    """The fail-open path, and the reason the discarded fold assigns at all.

    When the re-roll's stream never opens, the turn falls open to the draft
    already in hand — so the draft's host IS the honest attribution for the
    tokens being reported, and must survive the loop breaking early.
    """
    from structlog.testing import capture_logs

    opens = itertools.count()

    def factory(request: object) -> ProviderStream:
        if next(opens) > 0:
            raise upstream_error("the re-roll never opened")
        return FakeStream(make_text_chunks(_DEAD_ROUND), endpoint="deepinfra")

    fake_provider.selection_result = _priced_selection()
    fake_provider.stream_factory = factory  # type: ignore[assignment]
    with capture_logs() as logs:
        outcome = await orchestrator.complete(
            _intent_stream_request("bi-endpoint-failopen"),
            upstream_model="deepseek-v4-flash",
            api_key=TEST_API_KEY,
        )
        assert outcome.stream is not None
        events = await _drain(outcome.stream)
    assert any(e.startswith("event: done") for e in events), (
        "the turn must still answer from the draft it already has"
    )
    # Pin the branch, not just the outcome: without these the test would still
    # pass if the re-roll stopped happening at all, which is a different bug
    # wearing the same log line.
    assert len(fake_provider.stream_requests) == 2, "the re-roll must have been attempted"
    assert any(
        entry["event"] == "main_bare_intent_retry" and entry.get("phase") == "failed_open"
        for entry in logs
    ), "the fail-open branch must be the one that ran"
    entry = _turn_usage(logs)
    assert entry["endpoint"] == "deepinfra"
    assert entry["distinct_endpoints"] == 1


@pytest.mark.asyncio
async def test_a_repaired_selection_that_named_no_host_reports_none(
    orchestrator: ChatOrchestrator,
    fake_provider: FakeProvider,
) -> None:
    """The same stale-attribution rule, on the selector's own repair.

    A malformed selection is re-asked once. If the repair names no host, the
    answer's endpoint is unknown — labelling it with the discarded attempt's
    host would put a node beside a selector cache ratio it did not produce.
    """
    from structlog.testing import capture_logs

    fake_provider.selection_result = [
        make_result(content="not json at all", endpoint="deepinfra"),
        selection_result([], usage=ProviderUsage(prompt_tokens=3000, cost=0.0004)),
    ]
    fake_provider.complete_result = make_result(content="Read five votes; here is the tally.")
    with capture_logs() as logs:
        await orchestrator.complete(
            _intent_body_request("bi-endpoint-sel-repair"),
            upstream_model="deepseek-v4-flash",
            api_key=TEST_API_KEY,
        )
    assert len(fake_provider.selection_requests) == 2, "the selection must have been repaired"
    entry = _turn_usage(logs)
    assert entry["selector_endpoint"] is None


@pytest.mark.asyncio
async def test_a_repaired_selection_reports_the_host_that_answered(
    orchestrator: ChatOrchestrator,
    fake_provider: FakeProvider,
) -> None:
    """Positive control: when the repair DOES name a host, that is the answer's
    attribution — not the discarded first attempt's."""
    from structlog.testing import capture_logs

    fake_provider.selection_result = [
        make_result(content="not json at all", endpoint="deepinfra"),
        selection_result(
            [], usage=ProviderUsage(prompt_tokens=3000, cost=0.0004), endpoint="novita"
        ),
    ]
    fake_provider.complete_result = make_result(content="Read five votes; here is the tally.")
    with capture_logs() as logs:
        await orchestrator.complete(
            _intent_body_request("bi-endpoint-sel-repair-ok"),
            upstream_model="deepseek-v4-flash",
            api_key=TEST_API_KEY,
        )
    entry = _turn_usage(logs)
    assert entry["selector_endpoint"] == "novita"


@pytest.mark.asyncio
async def test_the_turn_key_reaches_the_log_from_a_real_continuation_round(
    orchestrator: ChatOrchestrator,
    fake_provider: FakeProvider,
) -> None:
    """The key is only useful if the CONTINUATION rounds carry it too.

    Those are the separate requests the grouping has to span, and they are the
    ones that skip selection — so this also pins that a skipped selector reports
    no cache figures rather than zeros.
    """
    from structlog.testing import capture_logs

    # A state token whose turn matches the request's IS the continuation shape:
    # selection is skipped, which is exactly why these rounds need the key to be
    # groupable with the round that did select.
    state = orchestrator.state_codec.issue(
        session_id="sess-cont",
        active_skills=[],
        catalog_revision=orchestrator.catalog.revision,
        turn_id="turn-cont",
        instruction_revision=0,
    )
    request = RespondRequest.model_validate(
        {
            "protocol_version": 2,
            "session": {"id": "sess-cont", "turn_id": "turn-cont", "round": 2},
            "input": {"messages": [{"role": "user", "content": "read the votes"}]},
            "startup": {},
            "generation": {"stream": False},
            "state": state,
        }
    )
    fake_provider.complete_result = make_result(content="Tallied.", endpoint="deepinfra")
    with capture_logs() as logs:
        await orchestrator.complete(
            request, upstream_model="deepseek-v4-flash", api_key=TEST_API_KEY
        )
    entry = _turn_usage(logs)
    assert entry["session_id"] == "sess-cont"
    assert entry["turn_id"] == "turn-cont"
    assert entry["round"] == 2
    assert entry["endpoint"] == "deepinfra"
    assert entry["selector_ran"] is False
    assert entry["selector_cache_ratio"] is None
    assert entry["selector_prompt_tokens"] is None


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


# ---- upstream cache affinity -------------------------------------------------


def _affinity_request(
    session_id: str = "affinity-session",
    *,
    turn_id: str = "turn-1",
    json_mode: bool = False,
) -> RespondRequest:
    generation: dict[str, Any] = {"stream": False}
    if json_mode:
        generation["response_format"] = "json_object"
    return RespondRequest.model_validate(
        {
            "protocol_version": 2,
            "session": {"id": session_id, "turn_id": turn_id},
            "input": {"messages": [{"role": "user", "content": "rename my terminals"}]},
            "startup": {},
            "generation": generation,
        }
    )


@pytest.mark.asyncio
async def test_main_call_carries_a_pseudonymous_cache_session_key(
    orchestrator: ChatOrchestrator, fake_provider: FakeProvider
) -> None:
    # The key exists to keep a conversation on the endpoint holding its prefix, so
    # it has to reach the wire at all — and it must NOT be the client's own session
    # id, which is opaque local text this server makes no promises about and has no
    # business handing to a third party.
    skill_id = sorted(orchestrator.catalog.known_ids)[0]
    fake_provider.selection_result = selection_result([skill_id])
    fake_provider.complete_result = make_result(content="done")

    request = _affinity_request()
    await orchestrator.complete(
        request, upstream_model="deepseek-v4-flash", api_key=TEST_API_KEY
    )

    sent = fake_provider.requests[-1]
    assert sent.cache_session_id is not None
    assert sent.cache_session_id.startswith("daintree-main-")
    assert "affinity-session" not in sent.cache_session_id
    # OpenRouter caps the field at 256 characters; a key it rejects is a key that
    # silently does nothing.
    assert len(sent.cache_session_id) <= 256
    # And it is the key the derivation actually produces, not merely a
    # plausibly-shaped string — this is what ties the wire value to
    # _cache_session_key, so a future edit cannot quietly attach a different one.
    assert sent.cache_session_id == orchestrator._cache_session_key(request)


@pytest.mark.asyncio
async def test_cache_session_key_is_stable_across_turns_and_unique_per_session(
    orchestrator: ChatOrchestrator, fake_provider: FakeProvider
) -> None:
    # Stability across turns IS the feature: a key that changed per turn would pin
    # nothing. And it must not be shared, or two conversations would be told they
    # are one — herding unrelated prefixes onto a single endpoint.
    skill_id = sorted(orchestrator.catalog.known_ids)[0]
    fake_provider.selection_result = selection_result([skill_id])
    fake_provider.complete_result = make_result(content="done")

    for turn in ("turn-1", "turn-2"):
        await orchestrator.complete(
            _affinity_request(turn_id=turn),
            upstream_model="deepseek-v4-flash",
            api_key=TEST_API_KEY,
        )
    await orchestrator.complete(
        _affinity_request("other-session"),
        upstream_model="deepseek-v4-flash",
        api_key=TEST_API_KEY,
    )

    # Exactly the three MAIN calls: a configured selector result routes every
    # json_object call to `selection_requests` instead (tests/fakes.py), so this
    # list holds no selector traffic. Asserted so a future change to that
    # discriminator cannot silently shift the indices below.
    keys = [r.cache_session_id for r in fake_provider.requests]
    assert len(keys) == 3
    assert keys[0] is not None
    assert keys[0] == keys[1]
    assert keys[-1] != keys[0]


@pytest.mark.asyncio
async def test_selector_call_is_keyed_by_catalog_not_by_session(
    orchestrator: ChatOrchestrator, fake_provider: FakeProvider
) -> None:
    """The selector's affinity key must not carry the session — asserted from the
    orchestrator's side, where the session key is in scope and the mistake is
    therefore available to make.

    Both halves matter. Keyed per session, the selector would splinter a prefix
    whose entire value is that every caller shares it. Keyed with nothing, the
    router hashes the user's own words and affinity never engages at all
    (docs/PROVIDER_POLICY.md section 3a).
    """
    skill_id = sorted(orchestrator.catalog.known_ids)[0]
    fake_provider.selection_result = selection_result([skill_id])
    fake_provider.complete_result = make_result(content="done")

    request = _affinity_request()
    await orchestrator.complete(
        request, upstream_model="deepseek-v4-flash", api_key=TEST_API_KEY
    )

    session_key = orchestrator._cache_session_key(request)
    assert session_key is not None
    assert fake_provider.selection_requests
    for req in fake_provider.selection_requests:
        assert req.cache_session_id == "daintree-selector-" + (
            orchestrator.catalog.revision.rpartition(":")[2][:16]
        )
        assert req.cache_session_id != session_key


def test_json_mode_main_call_uses_its_own_affinity_namespace(
    orchestrator: ChatOrchestrator,
) -> None:
    # A JSON-mode main call carries no tool schemas (validation rejects the
    # combination) and routes through the tail-bounded structured policy into a
    # different pool. Sharing one key across both shapes would let each evict the
    # other's sticky endpoint for a prefix that endpoint does not hold.
    #
    # Derivation only; the wire attachment for JSON mode is pinned by the test
    # below, which has to discriminate main from selector traffic by model id.
    plain = orchestrator._cache_session_key(_affinity_request())
    json_mode = orchestrator._cache_session_key(_affinity_request(json_mode=True))
    assert plain is not None and json_mode is not None
    assert json_mode != plain
    assert json_mode.startswith("daintree-main-json-")
    # The longer namespace must still clear OpenRouter's 256-character cap.
    assert len(json_mode) <= 256


@pytest.mark.asyncio
async def test_json_mode_main_request_reaches_the_wire_with_its_key(
    orchestrator: ChatOrchestrator, fake_provider: FakeProvider
) -> None:
    # The derivation test above proves the JSON namespace is COMPUTED; this proves
    # a JSON main call actually carries it. Without this, a future edit that
    # attached the key only on the non-JSON branch would pass both the derivation
    # test and the ordinary-path attachment test while shipping JSON turns unkeyed.
    #
    # Selection is left unconfigured on purpose: `FakeProvider` routes a
    # json_object call to `selection_requests` only when a selection result IS set
    # (tests/fakes.py), which would swallow the JSON main call too. With it unset
    # both land in `requests`, and the model id separates them — the selector
    # always calls OPENROUTER_MODEL, while the main call uses the upstream model
    # passed to `complete`. The selector then fails to parse "done" and degrades
    # open, which is its documented posture and does not stop the main call.
    fake_provider.complete_result = make_result(content="done")

    request = _affinity_request(json_mode=True)
    await orchestrator.complete(
        request, upstream_model="deepseek-v4-flash", api_key=TEST_API_KEY
    )

    main_calls = [r for r in fake_provider.requests if r.model == "deepseek-v4-flash"]
    assert len(main_calls) == 1
    sent = main_calls[0]
    assert sent.response_format == {"type": "json_object"}
    assert sent.cache_session_id == orchestrator._cache_session_key(request)
    assert sent.cache_session_id is not None
    assert sent.cache_session_id.startswith("daintree-main-json-")


# ---------------------------------------------------------------------------
# Phase timings.
# ---------------------------------------------------------------------------


def _timing_request(*, stream: bool) -> RespondRequest:
    return RespondRequest.model_validate(
        {
            "protocol_version": 2,
            "session": {"id": "timing-session", "turn_id": "turn-1"},
            "input": {"messages": [{"role": "user", "content": "rename my terminals"}]},
            "startup": {},
            "generation": {"stream": stream},
        }
    )


@pytest.mark.asyncio
async def test_nonstreaming_timings_are_measured_not_merely_present(
    orchestrator: ChatOrchestrator, fake_provider: FakeProvider
) -> None:
    """A timing block full of zeroes would pass a shape assertion and be useless.

    So this asserts the numbers RELATE: preparation covers the selector call that
    happened inside it, first output cannot precede preparation, and the total
    cannot be less than any phase it contains. Those orderings are what make the
    block diagnostic rather than decorative.
    """
    skill_id = sorted(orchestrator.catalog.known_ids)[0]
    fake_provider.selection_result = selection_result([skill_id])
    fake_provider.complete_result = make_result(content="done")

    outcome = await orchestrator.complete(
        _timing_request(stream=False), upstream_model="m", api_key=TEST_API_KEY
    )

    t = outcome.body["timings"]
    assert "selection_ms" in t, "the selector ran; its time must be reported"
    assert t["selection_ms"] <= t["preparation_ms"], (
        "selection happens inside preparation, so it cannot be the larger number"
    )
    assert t["preparation_ms"] <= t["first_output_ms"] <= t["total_ms"]
    # A non-streamed call cannot separate opening the model from generating with
    # it, so it reports neither rather than inventing a split. Absent, not zero —
    # both paths serialize with exclude_none, so "did not happen" is a missing
    # key and a client can never read it as an instant phase.
    assert "upstream_open_ms" not in t
    # Thinking is off across the whole interactive surface.
    assert "thinking_ms" not in t


@pytest.mark.asyncio
async def test_streaming_timings_ride_the_terminal_event(
    orchestrator: ChatOrchestrator, fake_provider: FakeProvider
) -> None:
    """`done`, not `meta` — and the reason is structural.

    `meta` is emitted before the model is opened, which is exactly what makes it
    useful and exactly why it cannot know how long generation took. A client
    logging a turn's phases reads the terminal event.
    """
    skill_id = sorted(orchestrator.catalog.known_ids)[0]
    fake_provider.selection_result = selection_result([skill_id])
    fake_provider.stream_result = FakeStream(make_text_chunks("hello there"))

    outcome = await orchestrator.complete(
        _timing_request(stream=True), upstream_model="m", api_key=TEST_API_KEY
    )
    assert outcome.stream is not None
    events = await _drain(outcome.stream)

    meta = json.loads(events[0].split("data: ", 1)[1])
    assert "timings" not in meta

    done = json.loads(events[-1].split("data: ", 1)[1])
    t = done["timings"]
    assert "selection_ms" in t
    assert "upstream_open_ms" in t, "a streamed turn observes its own open"
    # upstream_open_ms is a PHASE duration (open -> first event), not a
    # cumulative mark — it cannot be ordered against the cumulative fields,
    # only bounded by the whole turn.
    assert 0 <= t["upstream_open_ms"] <= t["total_ms"]
    assert t["preparation_ms"] <= t["first_output_ms"] <= t["total_ms"]
    # Generation is derived from the two numbers it sits between, so it can never
    # contradict them.
    assert t["generation_ms"] == t["total_ms"] - t["first_output_ms"]


@pytest.mark.asyncio
async def test_a_skipped_selector_reports_no_selection_time(
    orchestrator: ChatOrchestrator, fake_provider: FakeProvider
) -> None:
    """`None`, never `0`. A selector that did not run and one that answered
    instantly are different facts, and a zero would merge them — which is the
    whole reason someone reading these numbers would be misled."""
    fake_provider.complete_result = make_result(content="Hello.")

    request = RespondRequest.model_validate(
        {
            "protocol_version": 2,
            "session": {"id": "timing-session", "turn_id": "turn-1"},
            "input": {"messages": [{"role": "user", "content": "hey"}]},
            "startup": {},
            "generation": {"stream": False},
        }
    )
    outcome = await orchestrator.complete(request, upstream_model="m", api_key=TEST_API_KEY)

    assert outcome.body["skills"]["selector"]["ran"] is False
    assert "selection_ms" not in outcome.body["timings"]
    # ...while the phases that always happen are always reported.
    assert outcome.body["timings"]["total_ms"] >= 0
    assert outcome.body["timings"]["preparation_ms"] >= 0


def test_thinking_is_derived_from_marks_not_a_flag() -> None:
    """The chain-of-thought phase, tested where its arithmetic lives.

    Exercised directly rather than through a fake stream because the derivation
    is the part that was wrong: an earlier version read a `thinking_signaled`
    flag, which the bare-intent re-roll resets — so on the one path that HOLDS
    output back, the flag was already False by the time the held deltas flushed
    and the attribution vanished. Two marks and a subtraction cannot have that
    bug, and this pins it.
    """
    timings = orchestrator_module._Timings(
        preparation_ms=100, upstream_open_ms=150, reasoning_at_ms=200, first_output_ms=900
    )
    wire = timings.to_wire(None).model_dump()
    assert wire["thinking_ms"] == 700, "first visible token minus the first CoT fragment"

    # No reasoning streamed — the shipped posture — reports no thinking phase at
    # all rather than a zero a reader could mistake for a measurement.
    quiet = orchestrator_module._Timings(preparation_ms=100, first_output_ms=900)
    assert quiet.to_wire(None).thinking_ms is None


def test_generation_cannot_contradict_the_numbers_it_sits_between() -> None:
    """Derived, not measured, so the three can never disagree on the wire."""
    timings = orchestrator_module._Timings(first_output_ms=300)

    class _Budget:
        @staticmethod
        def elapsed() -> float:
            return 1.2

    wire = timings.to_wire(_Budget())  # type: ignore[arg-type]
    assert wire.total_ms == 1200
    assert wire.generation_ms == 900
    assert wire.first_output_ms is not None
    assert wire.first_output_ms + wire.generation_ms == wire.total_ms


# ---- runaway generations -----------------------------------------------------
#
# A degenerating endpoint does not stop on its own: it streams to the token
# ceiling and only then reports finish_reason=length, by which point the whole
# dump has been delivered to the user AND billed. Observed twice on 2026-08-20
# (~33.5k characters each, both on the same upstream endpoint), which is what
# these gates exist to make impossible.


def _plain_stream_request(session_id: str = "runaway-session") -> RespondRequest:
    """A streaming turn offering NO tools — the shape a runaway reply takes."""
    return RespondRequest.model_validate(
        {
            "protocol_version": 2,
            "session": {"id": session_id, "turn_id": "turn-1"},
            "input": {"messages": [{"role": "user", "content": "say something"}]},
            "startup": {},
            "generation": {"stream": True},
        }
    )


def _with_runaway_limit(orchestrator: ChatOrchestrator, limit: int) -> ChatOrchestrator:
    """The same orchestrator with a different runaway ceiling.

    Settings are frozen, so this rebuilds rather than mutating — which also keeps
    each test's threshold local instead of leaking into the shared fixture.
    """
    return ChatOrchestrator(
        settings=orchestrator.settings.model_copy(
            update={"main_stream_runaway_chars": limit}
        ),
        catalog_manager=orchestrator.catalog_manager,
        selector=orchestrator.selector,
        state_codec=orchestrator.state_codec,
        assembler=orchestrator.assembler,
        provider=orchestrator.provider,
    )


def _events_of(events: list[str], kind: str) -> list[dict[str, Any]]:
    return [
        json.loads(e.split("data: ", 1)[1]) for e in events if e.startswith(f"event: {kind}\n")
    ]


def _exact_content_chunks(pieces: list[str]) -> list[dict[str, Any]]:
    """Content chunks carrying EXACTLY the given strings, plus a stop chunk.

    `make_text_chunks` appends a space to every piece, which is fine for tests
    about behaviour but makes a boundary test off by one per chunk. This one is
    byte-exact, so an assertion about the character AT the limit means it.
    """
    head: list[dict[str, Any]] = [
        {
            "id": "chatcmpl-fake",
            "object": "chat.completion.chunk",
            "created": 1700000000,
            "model": "deepseek-v4-flash",
            "choices": [{"index": 0, "delta": {"role": "assistant"}, "finish_reason": None}],
        }
    ]
    for piece in pieces:
        head.append(
            {
                "id": "chatcmpl-fake",
                "object": "chat.completion.chunk",
                "created": 1700000000,
                "model": "deepseek-v4-flash",
                "choices": [{"index": 0, "delta": {"content": piece}, "finish_reason": None}],
            }
        )
    head.append(
        {
            "id": "chatcmpl-fake",
            "object": "chat.completion.chunk",
            "created": 1700000000,
            "model": "deepseek-v4-flash",
            "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}],
        }
    )
    return head


def _tool_call_chunk(arguments: str = "{}") -> dict[str, Any]:
    """One tool-call fragment, in the wire shape the fold expects."""
    return {
        "id": "chatcmpl-fake",
        "object": "chat.completion.chunk",
        "created": 1700000000,
        "model": "deepseek-v4-flash",
        "choices": [
            {
                "index": 0,
                "delta": {
                    "tool_calls": [
                        {
                            "index": 0,
                            "id": "call_1",
                            "type": "function",
                            "function": {
                                "name": "terminal__extract",
                                "arguments": arguments,
                            },
                        }
                    ]
                },
                "finish_reason": None,
            }
        ],
    }


@pytest.mark.asyncio
async def test_stream_runaway_is_cut_off_and_reported_as_an_error(
    orchestrator: ChatOrchestrator,
    fake_provider: FakeProvider,
) -> None:
    limit = orchestrator.settings.main_stream_runaway_chars
    assert limit, "the guard must ship on; a 0 default would make this test vacuous"
    stream = FakeStream(make_text_chunks("degenerate " * limit))
    fake_provider.stream_factory = lambda _request: stream
    outcome = await orchestrator.complete(
        _plain_stream_request(), upstream_model="deepseek-v4-flash", api_key=TEST_API_KEY
    )
    assert outcome.stream is not None
    events = await _drain(outcome.stream)

    errors = _events_of(events, "error")
    assert len(errors) == 1
    assert errors[0]["error"]["code"] == "upstream_runaway_output"
    # Terminal, never dressed up as a finished turn: a `done` invites the client
    # to commit the garbage to history, which is the prefix that degenerates the
    # NEXT turn too.
    assert not _events_of(events, "done")
    # The upstream is hung up on rather than drained — the whole point is not
    # paying for the rest of the ceiling.
    assert stream.closed
    assert stream._index < len(stream._chunks)


@pytest.mark.asyncio
async def test_stream_runaway_delivers_nothing_past_the_limit(
    orchestrator: ChatOrchestrator,
    fake_provider: FakeProvider,
) -> None:
    """The point of the guard is what it does NOT deliver or pay for."""
    limit = orchestrator.settings.main_stream_runaway_chars
    stream = FakeStream(make_text_chunks("degenerate " * limit))  # ~11x the limit
    fake_provider.stream_factory = lambda _request: stream
    outcome = await orchestrator.complete(
        _plain_stream_request(), upstream_model="deepseek-v4-flash", api_key=TEST_API_KEY
    )
    assert outcome.stream is not None
    delivered = _streamed_content(await _drain(outcome.stream))
    # Nothing past the line reaches the client at all: the guard raises on the
    # delta that crosses it, BEFORE that delta is yielded. Asserted as an upper
    # bound only — how much arrives under it depends on chunk sizes, and a single
    # large crossing delta legitimately delivers nothing.
    assert len(delivered) <= limit
    # And the source had far more left to give, so this is the guard stopping it
    # rather than the stream simply running short.
    assert stream._index < len(stream._chunks)


@pytest.mark.asyncio
async def test_a_tool_call_does_not_exempt_the_rest_of_the_reply(
    orchestrator: ChatOrchestrator,
    fake_provider: FakeProvider,
) -> None:
    """REGRESSION. The guard once skipped any round that had emitted a tool call,
    on the theory that a tool-calling round is converging.

    That was a hole rather than a safeguard, and it pointed the wrong way: only
    visible content is counted, so a real multi-spawn batch already costs nothing
    against this limit however large its arguments are — while the exemption let
    ONE junk tool-call fragment, which is exactly what a degenerating endpoint
    also emits, buy unbounded prose for the rest of the token ceiling.
    """
    limit = orchestrator.settings.main_stream_runaway_chars
    chunks = make_text_chunks("degenerate " * limit)
    chunks.insert(1, _tool_call_chunk())  # a tool call BEFORE the prose
    fake_provider.stream_factory = lambda _request: FakeStream(chunks)
    outcome = await orchestrator.complete(
        _intent_stream_request("runaway-after-tool-call"),
        upstream_model="deepseek-v4-flash",
        api_key=TEST_API_KEY,
    )
    assert outcome.stream is not None
    events = await _drain(outcome.stream)
    assert [e["error"]["code"] for e in _events_of(events, "error")] == [
        "upstream_runaway_output"
    ]


@pytest.mark.asyncio
async def test_large_tool_call_arguments_do_not_count_toward_the_prose_ceiling(
    orchestrator: ChatOrchestrator,
    fake_provider: FakeProvider,
) -> None:
    """The invariant that makes the unconditional prose ceiling safe.

    A legitimate multi-spawn batch carries its payload in tool-call ARGUMENTS. The
    counter tracks `delta.content` only, so such a round survives arguments many
    times the size of the ceiling — which is why exempting tool rounds bought
    nothing worth the hole it opened.
    """
    limit = orchestrator.settings.main_stream_runaway_chars
    payload = json.dumps({"terminalIds": ["t" * 64 for _ in range(limit // 32)]})
    assert len(payload) > limit * 1.5
    chunks = make_text_chunks("Spawning the batch now.")
    chunks.insert(1, _tool_call_chunk(payload))
    fake_provider.stream_factory = lambda _request: FakeStream(chunks)
    outcome = await orchestrator.complete(
        _intent_stream_request("big-tool-args"),
        upstream_model="deepseek-v4-flash",
        api_key=TEST_API_KEY,
    )
    assert outcome.stream is not None
    events = await _drain(outcome.stream)
    assert not _events_of(events, "error")
    assert _events_of(events, "done")


@pytest.mark.asyncio
async def test_an_ordinary_long_reply_is_not_treated_as_a_runaway(
    orchestrator: ChatOrchestrator,
    fake_provider: FakeProvider,
) -> None:
    """The false-positive gate. The threshold is deliberately well above any real
    answer, so a merely long one must stream through untouched."""
    limit = orchestrator.settings.main_stream_runaway_chars
    body = ("a real answer. " * (limit // 30)).strip()  # ~half the limit
    fake_provider.stream_factory = lambda _request: FakeStream(make_text_chunks(body))
    outcome = await orchestrator.complete(
        _plain_stream_request(), upstream_model="deepseek-v4-flash", api_key=TEST_API_KEY
    )
    assert outcome.stream is not None
    events = await _drain(outcome.stream)
    assert not _events_of(events, "error")
    assert _events_of(events, "done")
    assert _streamed_content(events).strip() == body


@pytest.mark.asyncio
async def test_the_limit_itself_is_allowed_and_one_character_more_is_not(
    orchestrator: ChatOrchestrator,
    fake_provider: FakeProvider,
) -> None:
    """The boundary, asserted on both sides with single-character deltas so the
    outcome cannot hinge on where a chunk happens to split."""
    limit = 64
    small = _with_runaway_limit(orchestrator, limit)

    fake_provider.stream_factory = lambda _request: FakeStream(
        _exact_content_chunks(["x"] * limit)
    )
    at_limit = await small.complete(
        _plain_stream_request("at-limit"), upstream_model="deepseek-v4-flash",
        api_key=TEST_API_KEY,
    )
    assert at_limit.stream is not None
    at_limit_events = await _drain(at_limit.stream)
    assert not _events_of(at_limit_events, "error")
    assert len(_streamed_content(at_limit_events)) == limit

    fake_provider.stream_factory = lambda _request: FakeStream(
        _exact_content_chunks(["x"] * (limit + 1))
    )
    over = await small.complete(
        _plain_stream_request("over-limit"), upstream_model="deepseek-v4-flash",
        api_key=TEST_API_KEY,
    )
    assert over.stream is not None
    over_events = await _drain(over.stream)
    assert [e["error"]["code"] for e in _events_of(over_events, "error")] == [
        "upstream_runaway_output"
    ]
    # The crossing character is suppressed, not delivered-then-regretted.
    assert len(_streamed_content(over_events)) == limit


@pytest.mark.asyncio
async def test_a_zero_limit_disables_the_guard(
    orchestrator: ChatOrchestrator,
    fake_provider: FakeProvider,
) -> None:
    """0 is the off switch, not a ceiling of zero — the distinction a truthiness
    check gets right and a `>= 0` comparison would get catastrophically wrong."""
    disabled = _with_runaway_limit(orchestrator, 0)
    body = "degenerate " * 4000
    fake_provider.stream_factory = lambda _request: FakeStream(make_text_chunks(body))
    outcome = await disabled.complete(
        _plain_stream_request("guard-off"), upstream_model="deepseek-v4-flash",
        api_key=TEST_API_KEY,
    )
    assert outcome.stream is not None
    events = await _drain(outcome.stream)
    assert not _events_of(events, "error")
    assert _events_of(events, "done")
    assert len(_streamed_content(events)) > orchestrator.settings.main_stream_runaway_chars
