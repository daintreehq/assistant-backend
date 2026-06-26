"""Respond orchestrator unit tests (merge + selector cadence)."""

from __future__ import annotations

from daintree_assistant_server.services.chat_orchestrator import (
    ChatOrchestrator,
    merge_active_skills,
)


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
