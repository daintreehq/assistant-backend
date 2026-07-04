"""Tests for the backend-internal skill-learning pipeline."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
from fakes import FakeProvider, make_result

from daintree_assistant_server.config import FIREWORKS_SMALL_MODEL, Settings
from daintree_assistant_server.contracts.daintree_api import RespondRequest
from daintree_assistant_server.errors import upstream_error
from daintree_assistant_server.prompts.loader import PromptLoader
from daintree_assistant_server.services.skill_learning import SkillLearner, _contains_secret
from daintree_assistant_server.skills.catalog import CatalogManager, load_catalog

# --------------------------------------------------------------------------
# Fixtures / helpers
# --------------------------------------------------------------------------

_EXAMPLE = (
    "---\n"
    "id: daintree.test.example\n"
    "title: Example\n"
    "summary: An example skill for tests.\n"
    "whenToUse: Use when demonstrating the example.\n"
    "---\n"
    "Use when: demonstrating.\n\nProcedure:\n1. Show it.\n\nReport back: shown.\n"
)

_TARGET = (
    "---\n"
    "id: daintree.test.target\n"
    "title: Target\n"
    "summary: The original target summary.\n"
    "whenToUse: Use when improving the target.\n"
    "---\n"
    "Use when: the original condition.\n\nProcedure:\n1. Original step.\n\nReport back: original.\n"
)

GATE_CREATE = {
    "workflow_complete": True,
    "learnable": True,
    "action": "create_new_skill",
    "target_skill_id": None,
    "lesson_type": "general_daintree_workflow",
    "confidence": 0.92,
    "reason": "A repeatable workflow with no covering skill.",
    "evidence": ["The user asked to do it and it worked end to end."],
}

GATE_IMPROVE = {
    "workflow_complete": True,
    "learnable": True,
    "action": "improve_existing_skill",
    "target_skill_id": "daintree.test.target",
    "lesson_type": "general_daintree_workflow",
    "confidence": 0.9,
    "reason": "The user said this shouldn't have happened.",
    "evidence": ["User: 'what happened here?'"],
}

GATE_NONE = {
    "workflow_complete": True,
    "learnable": False,
    "action": "none",
    "target_skill_id": None,
    "lesson_type": "none",
    "confidence": 0.1,
    "reason": "Nothing reusable.",
    "evidence": [],
}

PROPOSAL_CREATE = {
    "action": "create_skill",
    "skill_id": "daintree.test.created",
    "title": "Created skill",
    "summary": "A freshly created skill.",
    "whenToUse": "Use when the created workflow recurs.",
    "priority": 100,
    "risk": "read",
    "tags": ["daintree", "test"],
    "requiredTools": ["context.snapshot"],
    "body": "Use when: the created workflow recurs.\n\nProcedure:\n1. Step one.\n2. Step two.\n\n"
    "Report back: what was done.",
    "change_summary": ["Created the skill."],
    "confidence": 0.9,
}

PROPOSAL_IMPROVE = {
    "action": "update_skill",
    "skill_id": "daintree.test.target",
    "title": "Target",
    "summary": "An improved target summary.",
    "whenToUse": "Use when improving the target.",
    "priority": 100,
    "risk": "read",
    "tags": ["daintree", "test"],
    "requiredTools": [],
    "body": "Use when: the improved condition.\n\nProcedure:\n1. Improved step.\n2. New guardrail.\n\n"
    "Report back: improved.",
    "change_summary": ["Added a guardrail."],
    "confidence": 0.88,
}

MESSAGES = [
    {"role": "user", "content": "Run the multi-agent workflow across three agents."},
    {"role": "assistant", "content": "Done, but one agent was mishandled."},
    {"role": "user", "content": "what happened here? this shouldn't have happened."},
]


def _provider(gate: dict[str, Any], proposal: dict[str, Any] | None) -> FakeProvider:
    fake = FakeProvider()

    def _complete(req: Any) -> Any:
        system = req.messages[0]["content"]
        if "skill-learning gate" in system:
            return make_result(content=json.dumps(gate))
        if "skill builder" in system or "skill improver" in system:
            return make_result(content=json.dumps(proposal or {}))
        return make_result(content="ok")

    fake.complete_result = _complete
    return fake


def _settings(tmp_path: Path, *, mode: str = "apply", write_dir: Path | None = None) -> Settings:
    return Settings(
        DAINTREE_API_KEY="k",
        FIREWORKS_API_KEY="fw",
        STATE_SIGNING_KEY_CURRENT="s",
        RETRY_BACKOFF_BASE_SECONDS=0.0,
        SKILL_LEARNING_MODE=mode,
        SKILL_LEARNING_DIR=str(write_dir) if write_dir else "",
        SKILL_LEARNING_AUDIT_DIR=str(tmp_path / "audit"),
        SKILL_LEARNING_MIN_MESSAGES=1,
        SKILL_LEARNING_MIN_CONFIDENCE=0.75,
    )


def _primary(tmp_path: Path, *, with_target: bool = False) -> Path:
    primary = tmp_path / "skills"
    primary.mkdir()
    (primary / "daintree.test.example.md").write_text(_EXAMPLE, encoding="utf-8")
    if with_target:
        (primary / "daintree.test.target.md").write_text(_TARGET, encoding="utf-8")
    return primary


def _loader() -> PromptLoader:
    loader = PromptLoader()
    loader.validate()
    return loader


def _learner(
    tmp_path: Path,
    fake: FakeProvider,
    *,
    mode: str = "apply",
    with_target: bool = False,
    overlay: bool = False,
) -> tuple[SkillLearner, CatalogManager, Path]:
    primary = _primary(tmp_path, with_target=with_target)
    write_dir = (tmp_path / "overlay") if overlay else primary
    overlay_dir = write_dir if overlay else None
    settings = _settings(tmp_path, mode=mode, write_dir=write_dir if overlay else None)
    manager = CatalogManager(
        load_catalog(primary, overlay_dir), primary_dir=primary, overlay_dir=overlay_dir
    )
    learner = SkillLearner(
        settings=settings, provider=fake, prompt_loader=_loader(), catalog_manager=manager
    )
    return learner, manager, write_dir


def _request(messages: list[dict[str, Any]] | None = None) -> RespondRequest:
    return RespondRequest.model_validate(
        {
            "protocol_version": 1,
            "session": {"id": "s", "turn_id": "t"},
            "input": {"messages": messages or MESSAGES},
        }
    )


# --------------------------------------------------------------------------
# Tests
# --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_create_writes_and_reloads(tmp_path: Path) -> None:
    fake = _provider(GATE_CREATE, PROPOSAL_CREATE)
    learner, manager, write_dir = _learner(tmp_path, fake)
    outcome = await learner.run(request=_request(), active_ids=[])
    assert outcome.changed is True
    assert outcome.created_id == "daintree.test.created"
    assert (write_dir / "daintree.test.created.md").is_file()
    assert "daintree.test.created" in manager.catalog


@pytest.mark.asyncio
async def test_create_overlay_dir(tmp_path: Path) -> None:
    fake = _provider(GATE_CREATE, PROPOSAL_CREATE)
    learner, manager, write_dir = _learner(tmp_path, fake, overlay=True)
    outcome = await learner.run(request=_request(), active_ids=[])
    assert outcome.changed is True
    # Written to the overlay dir, and the packaged source is untouched.
    assert (write_dir / "daintree.test.created.md").is_file()
    assert not (manager.primary_dir / "daintree.test.created.md").exists()
    assert "daintree.test.created" in manager.catalog


@pytest.mark.asyncio
async def test_improve_rewrites_target(tmp_path: Path) -> None:
    fake = _provider(GATE_IMPROVE, PROPOSAL_IMPROVE)
    learner, manager, _ = _learner(tmp_path, fake, with_target=True)
    outcome = await learner.run(request=_request(), active_ids=["daintree.test.target"])
    assert outcome.changed is True
    assert outcome.created_id is None  # improve is not a new load
    skill = manager.catalog.get("daintree.test.target")
    assert skill is not None
    assert skill.summary == "An improved target summary."
    assert "New guardrail." in skill.body


@pytest.mark.asyncio
async def test_gate_none_does_nothing(tmp_path: Path) -> None:
    fake = _provider(GATE_NONE, PROPOSAL_CREATE)
    learner, manager, write_dir = _learner(tmp_path, fake)
    before = manager.catalog.revision
    outcome = await learner.run(request=_request(), active_ids=[])
    assert outcome.changed is False
    assert not (write_dir / "daintree.test.created.md").exists()
    assert manager.catalog.revision == before


@pytest.mark.asyncio
async def test_low_confidence_gate_does_nothing(tmp_path: Path) -> None:
    gate = {**GATE_CREATE, "confidence": 0.5}
    fake = _provider(gate, PROPOSAL_CREATE)
    learner, _, write_dir = _learner(tmp_path, fake)
    outcome = await learner.run(request=_request(), active_ids=[])
    assert outcome.changed is False
    assert not (write_dir / "daintree.test.created.md").exists()


@pytest.mark.asyncio
async def test_non_general_lesson_does_nothing(tmp_path: Path) -> None:
    gate = {**GATE_CREATE, "lesson_type": "project_specific"}
    fake = _provider(gate, PROPOSAL_CREATE)
    learner, _, write_dir = _learner(tmp_path, fake)
    outcome = await learner.run(request=_request(), active_ids=[])
    assert outcome.changed is False
    assert not (write_dir / "daintree.test.created.md").exists()


@pytest.mark.asyncio
async def test_propose_mode_writes_json_not_skill(tmp_path: Path) -> None:
    fake = _provider(GATE_CREATE, PROPOSAL_CREATE)
    learner, manager, write_dir = _learner(tmp_path, fake, mode="propose")
    outcome = await learner.run(request=_request(), active_ids=[])
    assert outcome.changed is False
    assert not (write_dir / "daintree.test.created.md").exists()
    assert "daintree.test.created" not in manager.catalog
    proposals = list((tmp_path / "audit" / "proposals").glob("daintree.test.created.*.md"))
    assert len(proposals) == 1


@pytest.mark.asyncio
async def test_invalid_proposal_rejected(tmp_path: Path) -> None:
    bad = {**PROPOSAL_CREATE, "body": ""}  # empty body fails the skill loader
    fake = _provider(GATE_CREATE, bad)
    learner, _, write_dir = _learner(tmp_path, fake)
    outcome = await learner.run(request=_request(), active_ids=[])
    assert outcome.changed is False
    assert not (write_dir / "daintree.test.created.md").exists()


@pytest.mark.asyncio
async def test_bad_new_id_rejected(tmp_path: Path) -> None:
    bad = {**PROPOSAL_CREATE, "skill_id": "not-house-style"}
    fake = _provider(GATE_CREATE, bad)
    learner, _, _ = _learner(tmp_path, fake)
    outcome = await learner.run(request=_request(), active_ids=[])
    assert outcome.changed is False


@pytest.mark.asyncio
async def test_secret_in_body_rejected(tmp_path: Path) -> None:
    leaky = {
        **PROPOSAL_CREATE,
        "body": "Use when: leaking.\n\nProcedure:\n1. export TOKEN=abcd1234efgh5678.\n\n"
        "Report back: done.",
    }
    fake = _provider(GATE_CREATE, leaky)
    learner, _, write_dir = _learner(tmp_path, fake)
    outcome = await learner.run(request=_request(), active_ids=[])
    assert outcome.changed is False
    assert not (write_dir / "daintree.test.created.md").exists()


@pytest.mark.asyncio
async def test_create_id_collision_rejected(tmp_path: Path) -> None:
    # Proposal reuses an existing catalog id: a create must not clobber it.
    collide = {**PROPOSAL_CREATE, "skill_id": "daintree.test.example"}
    fake = _provider(GATE_CREATE, collide)
    learner, manager, _ = _learner(tmp_path, fake)
    outcome = await learner.run(request=_request(), active_ids=[])
    assert outcome.changed is False
    assert manager.catalog.get("daintree.test.example").summary == "An example skill for tests."


@pytest.mark.asyncio
async def test_improve_wrong_target_rejected(tmp_path: Path) -> None:
    # Gate targets one skill; the improver returns a different (valid) id. It must be
    # rejected so an unrelated skill is never overwritten.
    proposal = {**PROPOSAL_IMPROVE, "skill_id": "daintree.test.example"}
    fake = _provider(GATE_IMPROVE, proposal)
    learner, manager, _ = _learner(tmp_path, fake, with_target=True)
    outcome = await learner.run(request=_request(), active_ids=["daintree.test.target"])
    assert outcome.changed is False
    assert manager.catalog.get("daintree.test.target").summary == "The original target summary."
    assert manager.catalog.get("daintree.test.example").summary == "An example skill for tests."


@pytest.mark.asyncio
async def test_create_with_update_action_rejected(tmp_path: Path) -> None:
    # A create gate with an "update_skill" proposal action is inconsistent → reject.
    proposal = {**PROPOSAL_CREATE, "action": "update_skill"}
    fake = _provider(GATE_CREATE, proposal)
    learner, _, write_dir = _learner(tmp_path, fake)
    outcome = await learner.run(request=_request(), active_ids=[])
    assert outcome.changed is False
    assert not (write_dir / "daintree.test.created.md").exists()


@pytest.mark.asyncio
async def test_no_change_proposal_does_nothing(tmp_path: Path) -> None:
    fake = _provider(GATE_IMPROVE, {"action": "no_change", "skill_id": "daintree.test.target"})
    learner, manager, _ = _learner(tmp_path, fake, with_target=True)
    outcome = await learner.run(request=_request(), active_ids=["daintree.test.target"])
    assert outcome.changed is False
    assert manager.catalog.get("daintree.test.target").summary == "The original target summary."


@pytest.mark.asyncio
async def test_disabled_when_mode_off_never_calls_provider(tmp_path: Path) -> None:
    fake = _provider(GATE_CREATE, PROPOSAL_CREATE)
    learner, _, _ = _learner(tmp_path, fake, mode="off")
    outcome = await learner.run(request=_request(), active_ids=[])
    assert outcome.changed is False
    assert fake.requests == []


@pytest.mark.asyncio
async def test_ineligible_short_conversation_skips_gate(tmp_path: Path) -> None:
    fake = _provider(GATE_CREATE, PROPOSAL_CREATE)
    learner, _, _ = _learner(tmp_path, fake)
    # No assistant message → not eligible → gate never runs.
    req = _request([{"role": "user", "content": "hi"}])
    outcome = await learner.run(request=req, active_ids=[])
    assert outcome.changed is False
    assert fake.requests == []


@pytest.mark.asyncio
async def test_provider_error_fails_open(tmp_path: Path) -> None:
    fake = _provider(GATE_CREATE, PROPOSAL_CREATE)
    fake.complete_error = upstream_error("boom", retryable=False)
    learner, _, write_dir = _learner(tmp_path, fake)
    # Must not raise; the turn proceeds untouched.
    outcome = await learner.run(request=_request(), active_ids=[])
    assert outcome.changed is False
    assert not (write_dir / "daintree.test.created.md").exists()


@pytest.mark.parametrize(
    "text",
    [
        "GITHUB_TOKEN=ghp_abcdefghijklmnopqrstuvwxyz012345",
        "export AWS_SECRET_ACCESS_KEY=wJalrXUtnFEMIabcdefghij1234567890",
        "token: ghp_abcdefghijklmnopqrstuvwxyz012345",
        "Authorization: Bearer abcdefghijklmnopqrstuvwx",
        "-----BEGIN RSA PRIVATE KEY-----",
        "anthropic=sk-ant-abcdefghijklmnopqrstuvwx",
        "eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxMjM0NTY3ODkwIn0.abcdef",
    ],
)
def test_secret_scan_catches(text: str) -> None:
    assert _contains_secret(text) is True


@pytest.mark.parametrize(
    "text",
    [
        "Use when: reading the .env file for local config.",
        "Pass a token to the API and await the result.",
        "Procedure:\n1. Run terminal.awaitAll and read the output.",
        "requiredTools: context.snapshot, terminal.read",
        # Schema/runbook prose describing arguments must not be flagged (no digit value).
        "token: optional",
        "password: required",
        "apiKey: placeholder",
    ],
)
def test_secret_scan_allows_normal_prose(text: str) -> None:
    assert _contains_secret(text) is False


@pytest.mark.asyncio
async def test_orchestrator_surfaces_created_skill_as_load(tmp_path: Path) -> None:
    # End-to-end: a created skill is folded into THIS turn's active set and renders
    # to the CLI as an ordinary dynamic skill load (newly_loaded), and the main
    # completion still returns normally.
    from daintree_assistant_server.services.chat_orchestrator import ChatOrchestrator
    from daintree_assistant_server.services.prompt_assembler import PromptAssembler
    from daintree_assistant_server.skills.selector import Selector
    from daintree_assistant_server.state.codec import StateCodec

    fake = _provider(GATE_CREATE, PROPOSAL_CREATE)
    primary = _primary(tmp_path)
    settings = _settings(tmp_path)  # edit-in-place into `primary`
    manager = CatalogManager(load_catalog(primary), primary_dir=primary)
    loader = _loader()
    learner = SkillLearner(
        settings=settings, provider=fake, prompt_loader=loader, catalog_manager=manager
    )
    selector = Selector(fake, loader, manager, settings)
    codec = StateCodec(current_key=b"k" * 16, current_kid="k1", salt=b"salt", ttl_seconds=3600)
    orch = ChatOrchestrator(
        settings=settings,
        catalog_manager=manager,
        selector=selector,
        state_codec=codec,
        assembler=PromptAssembler(loader, settings),
        provider=fake,
        learner=learner,
    )

    outcome = await orch.complete(_request(), upstream_model=FIREWORKS_SMALL_MODEL)
    assert outcome.body is not None
    active = [s["id"] for s in outcome.body["skills"]["active"]]
    newly = [s["id"] for s in outcome.body["skills"]["newly_loaded"]]
    assert "daintree.test.created" in active
    assert "daintree.test.created" in newly
    assert (primary / "daintree.test.created.md").is_file()
    # The main completion still ran and produced its normal reply.
    assert outcome.body["message"]["content"] == "ok"
