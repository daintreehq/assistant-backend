"""Chat orchestrator unit tests (merge, cadence, metadata)."""

from __future__ import annotations

import pytest

from daintree_assistant_server.config import Settings
from daintree_assistant_server.contracts.extensions import (
    DaintreeExtension,
    Prelude,
    ResponseMeta,
    Selection,
)
from daintree_assistant_server.errors import DaintreeError
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


def _ext(**kwargs: object) -> DaintreeExtension:
    base: dict[str, object] = {"protocol_version": 1, "session_id": "s", "turn_id": "t1"}
    base.update(kwargs)
    return DaintreeExtension.model_validate(base)


def test_cadence_stateless_runs(orchestrator: ChatOrchestrator) -> None:
    assert orchestrator._should_run_selector(None, False, None, None, None) is True


def test_cadence_force_runs(orchestrator: ChatOrchestrator) -> None:
    ext = _ext(selection=Selection(force=True).model_dump())
    assert orchestrator._should_run_selector(ext, True, orchestrator.catalog.revision, "t1", 0) is True


def test_cadence_always_runs(orchestrator: ChatOrchestrator) -> None:
    ext = _ext(selection={"policy": "always"})
    assert orchestrator._should_run_selector(ext, True, orchestrator.catalog.revision, "t1", 0) is True


def test_cadence_invalid_state_runs(orchestrator: ChatOrchestrator) -> None:
    ext = _ext()
    assert orchestrator._should_run_selector(ext, False, None, None, None) is True


def test_cadence_catalog_mismatch_runs(orchestrator: ChatOrchestrator) -> None:
    ext = _ext()
    assert orchestrator._should_run_selector(ext, True, "sha256:old", "t1", 0) is True


def test_cadence_new_turn_runs(orchestrator: ChatOrchestrator) -> None:
    ext = _ext(turn_id="t2")
    assert orchestrator._should_run_selector(ext, True, orchestrator.catalog.revision, "t1", 0) is True


def test_cadence_instruction_change_runs(orchestrator: ChatOrchestrator) -> None:
    ext = _ext(instruction_revision=1)
    assert orchestrator._should_run_selector(ext, True, orchestrator.catalog.revision, "t1", 0) is True


def test_cadence_same_turn_does_not_run(orchestrator: ChatOrchestrator) -> None:
    ext = _ext(round=5)
    assert orchestrator._should_run_selector(ext, True, orchestrator.catalog.revision, "t1", 0) is False


def _meta() -> ResponseMeta:
    return ResponseMeta(
        request_id="req_1",
        state="dst1.xxx.yyy",
        catalog_revision="sha256:rev",
        prompt_version="main-2026-06-26.1",
        prelude=Prelude(),
    )


def test_encode_meta_roundtrips(orchestrator: ChatOrchestrator) -> None:
    import base64
    import json

    encoded = orchestrator._encode_meta(_meta())
    pad = "=" * (-len(encoded) % 4)
    decoded = json.loads(base64.urlsafe_b64decode(encoded + pad))
    assert decoded["request_id"] == "req_1"
    assert decoded["state"] == "dst1.xxx.yyy"


def test_encode_meta_too_large_raises(catalog, fake_provider, prompt_loader, state_codec, assembler) -> None:
    tiny = Settings(
        DAINTREE_API_KEY="k", DEEPSEEK_API_KEY="d", STATE_SIGNING_KEY_CURRENT="s",
        MAX_META_HEADER_BYTES=10,
    )
    from daintree_assistant_server.skills.selector import Selector

    orch = ChatOrchestrator(
        settings=tiny,
        catalog=catalog,
        selector=Selector(fake_provider, prompt_loader, catalog, tiny),
        state_codec=state_codec,
        assembler=assembler,
        provider=fake_provider,
    )
    with pytest.raises(DaintreeError) as exc:
        orch._encode_meta(_meta())
    assert exc.value.code == "daintree_metadata_too_large"
