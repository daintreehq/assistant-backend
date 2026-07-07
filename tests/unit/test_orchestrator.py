"""Respond orchestrator unit tests (merge + selector cadence)."""

from __future__ import annotations

from daintree_assistant_server.contracts.daintree_api import RespondRequest
from daintree_assistant_server.services.chat_orchestrator import (
    ChatOrchestrator,
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
            "protocol_version": 1,
            "session": {"id": "s", "turn_id": "t"},
            "input": {"messages": [{"role": "user", "content": "go"}]},
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
