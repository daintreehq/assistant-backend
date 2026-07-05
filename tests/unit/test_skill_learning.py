"""Tests for the backend-internal skill-learning pipeline."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any

import pytest
from fakes import FakeProvider, make_result, make_text_chunks

from daintree_assistant_server.config import (
    DEEPSEEK_MODEL,
    SKILL_BUILDER_MODEL,
    SKILL_IMPROVER_MODEL,
    Settings,
)
from daintree_assistant_server.contracts.daintree_api import RespondRequest
from daintree_assistant_server.errors import upstream_error
from daintree_assistant_server.prompts.loader import PromptLoader
from daintree_assistant_server.services.skill_learning import (
    SkillLearner,
    _contains_secret,
    _house_style_problems,
)
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


def _settings(
    tmp_path: Path,
    *,
    mode: str = "apply",
    write_dir: Path | None = None,
    context_tokens: int | None = None,
) -> Settings:
    extra: dict[str, Any] = {}
    if context_tokens is not None:
        extra["SKILL_LEARNING_CONTEXT_TOKENS"] = context_tokens
    return Settings(
        DAINTREE_API_KEY="k",
        DEEPSEEK_API_KEY="fw",
        STATE_SIGNING_KEY_CURRENT="s",
        RETRY_BACKOFF_BASE_SECONDS=0.0,
        SKILL_LEARNING_MODE=mode,
        SKILL_LEARNING_DIR=str(write_dir) if write_dir else "",
        SKILL_LEARNING_AUDIT_DIR=str(tmp_path / "audit"),
        SKILL_LEARNING_MIN_MESSAGES=1,
        SKILL_LEARNING_MIN_CONFIDENCE=0.75,
        **extra,
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
    context_tokens: int | None = None,
) -> tuple[SkillLearner, CatalogManager, Path]:
    primary = _primary(tmp_path, with_target=with_target)
    write_dir = (tmp_path / "overlay") if overlay else primary
    overlay_dir = write_dir if overlay else None
    settings = _settings(
        tmp_path,
        mode=mode,
        write_dir=write_dir if overlay else None,
        context_tokens=context_tokens,
    )
    manager = CatalogManager(
        load_catalog(primary, overlay_dir), primary_dir=primary, overlay_dir=overlay_dir
    )
    learner = SkillLearner(
        settings=settings, provider=fake, prompt_loader=_loader(), catalog_manager=manager
    )
    return learner, manager, write_dir


def _request(
    messages: list[dict[str, Any]] | None = None, *, state: str | None = None
) -> RespondRequest:
    payload: dict[str, Any] = {
        "protocol_version": 1,
        "session": {"id": "s", "turn_id": "t"},
        "input": {"messages": messages or MESSAGES},
    }
    if state is not None:
        payload["state"] = state
    return RespondRequest.model_validate(payload)


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
async def test_create_uses_pro_builder_model_with_thinking(tmp_path: Path) -> None:
    # Regression guard for the DeepSeek model split (see config.py): the gate runs on
    # the small model NON-thinking, while the builder runs on deepseek-v4-pro WITH
    # thinking at the configured effort. The fake records every request, so a silent
    # regression back to the small model (or thinking off) for the builder is caught.
    assert SKILL_BUILDER_MODEL == "deepseek-v4-pro"
    fake = _provider(GATE_CREATE, PROPOSAL_CREATE)
    learner, _, _ = _learner(tmp_path, fake)
    await learner.run(request=_request(), active_ids=[])

    gate_reqs = [r for r in fake.requests if r.model == DEEPSEEK_MODEL]
    build_reqs = [r for r in fake.requests if r.model == SKILL_BUILDER_MODEL]
    assert gate_reqs and all(r.thinking_disabled for r in gate_reqs)
    assert build_reqs and all(r.thinking_disabled is False for r in build_reqs)
    assert all(r.reasoning_effort == "high" for r in build_reqs)


@pytest.mark.asyncio
async def test_improve_uses_pro_improver_model_with_thinking(tmp_path: Path) -> None:
    assert SKILL_IMPROVER_MODEL == "deepseek-v4-pro"
    fake = _provider(GATE_IMPROVE, PROPOSAL_IMPROVE)
    learner, _, _ = _learner(tmp_path, fake, with_target=True)
    await learner.run(request=_request(), active_ids=["daintree.test.target"])

    improve_reqs = [r for r in fake.requests if r.model == SKILL_IMPROVER_MODEL]
    assert improve_reqs and all(r.thinking_disabled is False for r in improve_reqs)
    assert all(r.reasoning_effort == "high" for r in improve_reqs)


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
async def test_gate_without_evidence_does_nothing(tmp_path: Path) -> None:
    # The gate prompt demands concrete evidence for a write; the code enforces it
    # too, so an evidence-free verdict never spends a builder call.
    gate = {**GATE_CREATE, "evidence": []}
    fake = _provider(gate, PROPOSAL_CREATE)
    learner, _, write_dir = _learner(tmp_path, fake)
    outcome = await learner.run(request=_request(), active_ids=[])
    assert outcome.changed is False
    assert not (write_dir / "daintree.test.created.md").exists()
    assert len(fake.requests) == 1  # gate only — no builder call


@pytest.mark.asyncio
async def test_out_of_range_priority_and_risk_case_normalized(tmp_path: Path) -> None:
    # Mechanical normalization: a clamped priority and a case-fixed risk must not
    # discard an otherwise sound proposal.
    proposal = {**PROPOSAL_CREATE, "priority": 5000, "risk": "Read"}
    fake = _provider(GATE_CREATE, proposal)
    learner, manager, _ = _learner(tmp_path, fake)
    outcome = await learner.run(request=_request(), active_ids=[])
    assert outcome.changed is True
    skill = manager.catalog.get("daintree.test.created")
    assert skill is not None
    assert skill.meta.priority == 1000
    assert skill.meta.risk == "read"


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
async def test_non_house_style_body_rejected(tmp_path: Path) -> None:
    # Passes the loader (valid frontmatter, non-empty body) but violates house style:
    # no "Use when:" opening. The learning-layer gate must reject it so the catalog
    # only gains runbooks a small model can actually follow.
    off_style = {
        **PROPOSAL_CREATE,
        "body": "Do the thing.\n\nProcedure:\n1. Step one.\n2. Step two.\n\nReport back: done.",
    }
    fake = _provider(GATE_CREATE, off_style)
    learner, _, write_dir = _learner(tmp_path, fake)
    outcome = await learner.run(request=_request(), active_ids=[])
    assert outcome.changed is False
    assert not (write_dir / "daintree.test.created.md").exists()


@pytest.mark.asyncio
async def test_hardcoded_agent_body_rejected(tmp_path: Path) -> None:
    # Skills must be agent-agnostic: a body that names a specific CLI agent is
    # rejected so the catalog transfers across agents/users.
    hardcoded = {
        **PROPOSAL_CREATE,
        "body": "Use when: fixing.\n\nProcedure:\n1. Spawn claude to make the edits.\n"
        "2. Wait for it.\n\nReport back: done.",
    }
    fake = _provider(GATE_CREATE, hardcoded)
    learner, _, write_dir = _learner(tmp_path, fake)
    outcome = await learner.run(request=_request(), active_ids=[])
    assert outcome.changed is False
    assert not (write_dir / "daintree.test.created.md").exists()


@pytest.mark.asyncio
async def test_audit_redacts_secretlike_evidence(tmp_path: Path) -> None:
    # The skill body is secret-scanned, but the audit ALSO stores model free-text
    # (the what-worked/what-failed diagnosis, gate evidence, diff). A secret-shaped
    # value copied there must be masked before the audit record hits disk.
    secret = "fw_" + "a" * 24
    proposal = {**PROPOSAL_IMPROVE, "what_worked": [f"reused the key {secret}"]}
    fake = _provider(GATE_IMPROVE, proposal)
    learner, _, _ = _learner(tmp_path, fake, with_target=True)
    outcome = await learner.run(request=_request(), active_ids=["daintree.test.target"])
    assert outcome.changed is True
    audits = list((tmp_path / "audit").glob("daintree.test.target.*.json"))
    assert len(audits) == 1
    raw = audits[0].read_text(encoding="utf-8")
    assert secret not in raw
    audit = json.loads(raw)
    assert audit["what_worked"] == ["reused the key [REDACTED-SECRET]"]


def test_house_style_accepts_every_packaged_skill() -> None:
    # The house-style gate also runs on the IMPROVE path over existing skills, so
    # every packaged skill must pass it — otherwise learning could never improve the
    # real catalog. This guards the "improve path is never blocked" invariant.
    for skill in load_catalog().skills:
        assert _house_style_problems(skill.body) == [], skill.id


def test_house_style_problems_flags_each_violation() -> None:
    # Each check fires in isolation on an otherwise-conforming body.
    good = "Use when: X happens.\n\nProcedure:\n1. A.\n2. B.\n3. C.\n\nReport back: done."
    assert _house_style_problems(good) == []
    no_opener = "Does a thing.\n\nProcedure:\n1. A.\n2. B.\n3. C.\n\nReport back: done."
    assert any("Use when" in p for p in _house_style_problems(no_opener))
    agent = "Use when: x.\n\nProcedure:\n1. spawn codex now.\n\nReport back: y."
    assert any("CLI agent" in p for p in _house_style_problems(agent))
    path = "Use when: x.\n\nProcedure:\n1. open /Users/greg/repo.\n\nReport back: y."
    assert any("path" in p for p in _house_style_problems(path))
    episodic = "Use when: x.\n\nProcedure:\n1. do it as seen in this workflow.\n\nReport back: y."
    assert any("self-contained" in p for p in _house_style_problems(episodic))
    # "Use whenever" is not the house opener; "pick the best-fit agent" is not a hardcode.
    assert any("Use when" in p for p in _house_style_problems("Use whenever.\n\nProcedure:\n1. a.\n2. b.\n3. c.\n\nReport back: y."))
    agnostic = "Use when: x.\n\nProcedure:\n1. pick the best-fit agent.\n2. b.\n3. c.\n\nReport back: y."
    assert _house_style_problems(agnostic) == []


@pytest.mark.asyncio
async def test_gate_transcript_includes_trailing_final_answer(tmp_path: Path) -> None:
    # Post-completion learning appends the just-generated final assistant answer to
    # the transcript, and gate.md now judges "the workflow ending at that final
    # answer". Lock that threading: the gate must SEE the final answer, positioned
    # AFTER the conversation's last user message (i.e. genuinely appended last).
    from daintree_assistant_server.contracts.openai import ChatMessage

    seen: dict[str, str] = {}
    fake = FakeProvider()

    def _complete(req: Any) -> Any:
        if "skill-learning gate" in req.messages[0]["content"]:
            seen["transcript"] = "\n".join(
                m["content"] for m in req.messages if isinstance(m.get("content"), str)
            )
            return make_result(content=json.dumps(GATE_NONE))
        return make_result(content="ok")

    fake.complete_result = _complete
    learner, _, _ = _learner(tmp_path, fake)
    final = "FINAL-ANSWER-SENTINEL-9c3f"
    await learner.run(
        request=_request(),
        active_ids=[],
        trailing_messages=[ChatMessage(role="assistant", content=final)],
    )
    transcript = seen["transcript"]
    assert final in transcript
    assert transcript.index(final) > transcript.index("what happened here?")


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
async def test_audit_records_diff_and_analysis(tmp_path: Path) -> None:
    # The audit record is the dogfooding review trail: it must carry the model's
    # what-worked/what-failed diagnosis and a reviewable old→new diff.
    proposal = {
        **PROPOSAL_IMPROVE,
        "what_worked": ["The original step ran fine."],
        "what_failed": ["No guardrail for the retry loop."],
    }
    fake = _provider(GATE_IMPROVE, proposal)
    learner, _, _ = _learner(tmp_path, fake, with_target=True)
    outcome = await learner.run(request=_request(), active_ids=["daintree.test.target"])
    assert outcome.changed is True
    audits = list((tmp_path / "audit").glob("daintree.test.target.*.json"))
    assert len(audits) == 1
    audit = json.loads(audits[0].read_text(encoding="utf-8"))
    assert audit["what_worked"] == ["The original step ran fine."]
    assert audit["what_failed"] == ["No guardrail for the retry loop."]
    assert "-summary: The original target summary." in audit["diff"]
    assert "+summary: An improved target summary." in audit["diff"]


@pytest.mark.asyncio
async def test_cancel_during_commit_keeps_disk_and_catalog_in_sync(tmp_path: Path) -> None:
    # The CLI cancels the in-flight request on Ctrl-C — most likely during a slow
    # learning turn. If that lands mid-commit, the write+reload must still finish
    # as one unit (shielded), so disk and the in-memory catalog never diverge.
    fake = _provider(GATE_CREATE, PROPOSAL_CREATE)
    learner, manager, write_dir = _learner(tmp_path, fake)
    real_reload = manager.reload
    started = asyncio.Event()

    async def slow_reload() -> Any:
        started.set()
        await asyncio.sleep(0.05)
        return await real_reload()

    manager.reload = slow_reload  # type: ignore[method-assign]
    task = asyncio.create_task(learner.run(request=_request(), active_ids=[]))
    await started.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    # The commit completed before the cancellation propagated: file AND catalog.
    assert (write_dir / "daintree.test.created.md").is_file()
    assert "daintree.test.created" in manager.catalog


@pytest.mark.asyncio
async def test_reload_failure_rolls_back_file(tmp_path: Path) -> None:
    # If the catalog reload blows up after the write, the file must be rolled
    # back so disk and the in-memory catalog stay in agreement — and the turn
    # still proceeds (fail-open).
    fake = _provider(GATE_CREATE, PROPOSAL_CREATE)
    learner, manager, write_dir = _learner(tmp_path, fake)
    before = manager.catalog.revision

    async def broken_reload() -> Any:
        raise RuntimeError("disk went away")

    manager.reload = broken_reload  # type: ignore[method-assign]
    outcome = await learner.run(request=_request(), active_ids=[])
    assert outcome.changed is False
    assert not (write_dir / "daintree.test.created.md").exists()
    assert manager.catalog.revision == before


@pytest.mark.asyncio
async def test_identical_improve_applies_once_then_skips(tmp_path: Path) -> None:
    # The completed workflow stays in history for many turns; a re-proposed,
    # byte-identical improve must not re-apply forever.
    fake = _provider(GATE_IMPROVE, PROPOSAL_IMPROVE)
    learner, manager, _ = _learner(tmp_path, fake, with_target=True)
    first = await learner.run(request=_request(), active_ids=["daintree.test.target"])
    assert first.changed is True
    revision = manager.catalog.revision
    second = await learner.run(request=_request(), active_ids=["daintree.test.target"])
    assert second.changed is False
    assert manager.catalog.revision == revision


def test_truncated_transcript_carries_marker(tmp_path: Path) -> None:
    # When the newest-biased budget drops older messages, the gate is told so it
    # doesn't mistake an abrupt opening for the start of the workflow.
    fake = _provider(GATE_NONE, None)
    learner, _, _ = _learner(tmp_path, fake, context_tokens=50)
    req = _request(
        [
            {"role": "user", "content": "old message " * 40},
            {"role": "assistant", "content": "old reply " * 40},
            {"role": "user", "content": "what happened here?"},
        ]
    )
    transcript = learner._transcript(req.input.messages)
    assert transcript.startswith("[transcript truncated: oldest messages dropped]")
    assert "what happened here?" in transcript


@pytest.mark.asyncio
async def test_gate_related_skills_become_builder_examples(tmp_path: Path) -> None:
    # The cheap gate names the catalog skills nearest the workflow's domain; the
    # The builder must see those first among its house-style exemplars.
    gate = {**GATE_CREATE, "related_skill_ids": ["daintree.test.target"]}
    fake = _provider(gate, PROPOSAL_CREATE)
    learner, _, _ = _learner(tmp_path, fake, with_target=True)
    outcome = await learner.run(request=_request(), active_ids=[])
    assert outcome.changed is True
    build_calls = [r for r in fake.requests if "skill builder" in r.messages[0]["content"]]
    assert len(build_calls) == 1
    user_msg = build_calls[0].messages[1]["content"]
    # The related skill leads the examples block (its full file, id included).
    assert user_msg.index("id: daintree.test.target") < user_msg.index(
        "id: daintree.test.example"
    )


@pytest.mark.asyncio
async def test_gate_related_skills_shown_to_improver(tmp_path: Path) -> None:
    # The improver gets gate-picked neighbors as style/scope reference — but never
    # its own target, and only genuinely related ids (no shortest-first filler).
    gate = {**GATE_IMPROVE, "related_skill_ids": ["daintree.test.example"]}
    fake = _provider(gate, PROPOSAL_IMPROVE)
    learner, _, _ = _learner(tmp_path, fake, with_target=True)
    outcome = await learner.run(request=_request(), active_ids=["daintree.test.target"])
    assert outcome.changed is True
    improve_calls = [r for r in fake.requests if "skill improver" in r.messages[0]["content"]]
    assert len(improve_calls) == 1
    user_msg = improve_calls[0].messages[1]["content"]
    assert "Related skills" in user_msg
    assert "id: daintree.test.example" in user_msg


def test_transcript_keeps_long_messages_and_tool_results(tmp_path: Path) -> None:
    # Learning-sized caps: the selector's excerpts (8000/2000 chars) are too small
    # for judging what actually happened; full tool output (up to the CLI's own
    # 8000-char wire cap) and long assistant replies must survive intact.
    fake = _provider(GATE_NONE, None)
    learner, _, _ = _learner(tmp_path, fake)
    long_assistant = "a" * 12_000
    long_tool = "t" * 5_000
    req = _request(
        [
            {"role": "user", "content": "start"},
            {"role": "assistant", "content": long_assistant},
            {"role": "tool", "content": long_tool, "tool_call_id": "c1", "name": "term.read"},
            {"role": "user", "content": "what happened here?"},
        ]
    )
    transcript = learner._transcript(req.input.messages)
    assert long_assistant in transcript
    assert long_tool in transcript
    assert "…[truncated]…" not in transcript


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


def _orchestrator_with_learner(
    tmp_path: Path, fake: FakeProvider
) -> tuple[Any, CatalogManager, Path]:
    """A wired orchestrator whose learner writes edit-in-place into `primary`."""
    from daintree_assistant_server.services.chat_orchestrator import ChatOrchestrator
    from daintree_assistant_server.services.prompt_assembler import PromptAssembler
    from daintree_assistant_server.skills.selector import Selector
    from daintree_assistant_server.state.codec import StateCodec

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
    return orch, manager, primary


@pytest.mark.asyncio
async def test_learns_after_completed_turn(tmp_path: Path) -> None:
    # A completed turn (final answer, no tool calls) triggers learning OFF the
    # response path: the main reply returns immediately and unchanged, and the
    # background pass then writes/reloads the skill so it is live for the next turn.
    fake = _provider(GATE_CREATE, PROPOSAL_CREATE)
    orch, manager, primary = _orchestrator_with_learner(tmp_path, fake)

    outcome = await orch.complete(_request(), upstream_model=DEEPSEEK_MODEL)
    assert outcome.body is not None
    # The main completion returned its normal reply straight away...
    assert outcome.body["message"]["content"] == "ok"
    # ...and the just-learned skill is NOT folded into THIS turn — you can't use a
    # skill to guide the very workflow that taught it. It lands for the next turn.
    active = [s["id"] for s in outcome.body["skills"]["active"]]
    assert "daintree.test.created" not in active
    # Fire-and-forget: complete() returned BEFORE the learner ran (no await between
    # spawning the task and returning), so nothing is written yet — this proves the
    # work is off the response path, not synchronously awaited before the reply.
    assert not (primary / "daintree.test.created.md").exists()

    await orch.drain_background_tasks()

    assert (primary / "daintree.test.created.md").is_file()
    assert "daintree.test.created" in manager.catalog


@pytest.mark.asyncio
async def test_learner_skipped_when_turn_ends_in_tool_calls(tmp_path: Path) -> None:
    # A turn that ends by calling tools is a half-finished workflow, not a
    # completion boundary: learning must NOT fire. It waits for the round that
    # returns a final answer.
    fake = _provider(GATE_CREATE, PROPOSAL_CREATE)

    def _complete(req: Any) -> Any:
        system = req.messages[0]["content"]
        if "skill-learning gate" in system:
            return make_result(content=json.dumps(GATE_CREATE))
        if "skill builder" in system or "skill improver" in system:
            return make_result(content=json.dumps(PROPOSAL_CREATE))
        # The main completion ends the round with a tool call, not a final answer.
        return make_result(
            content=None,
            tool_calls=[
                {
                    "id": "c1",
                    "type": "function",
                    "function": {"name": "terminal.read", "arguments": "{}"},
                }
            ],
            finish_reason="tool_calls",
        )

    fake.complete_result = _complete
    orch, _, primary = _orchestrator_with_learner(tmp_path, fake)

    outcome = await orch.complete(_request(), upstream_model=DEEPSEEK_MODEL)
    assert outcome.body is not None
    await orch.drain_background_tasks()  # nothing was spawned; a no-op

    gate_calls = [r for r in fake.requests if "skill-learning gate" in r.messages[0]["content"]]
    assert gate_calls == []  # learning never ran on the continuation round
    assert not (primary / "daintree.test.created.md").exists()


@pytest.mark.asyncio
async def test_learns_after_streamed_completion(tmp_path: Path) -> None:
    # The streaming path learns after a clean `done`, from the accumulated reply.
    fake = _provider(GATE_CREATE, PROPOSAL_CREATE)
    fake.stream_chunks = make_text_chunks("all done here")
    orch, manager, primary = _orchestrator_with_learner(tmp_path, fake)

    req = RespondRequest.model_validate(
        {
            "protocol_version": 1,
            "session": {"id": "s", "turn_id": "t"},
            "input": {"messages": MESSAGES},
            "generation": {"stream": True},
        }
    )
    outcome = await orch.complete(req, upstream_model=DEEPSEEK_MODEL)
    assert outcome.stream is not None
    async for _event in outcome.stream:  # drain the SSE stream to completion
        pass

    await orch.drain_background_tasks()

    assert (primary / "daintree.test.created.md").is_file()
    assert "daintree.test.created" in manager.catalog


@pytest.mark.asyncio
async def test_learner_skipped_when_streamed_turn_ends_in_tool_calls(tmp_path: Path) -> None:
    # The streaming path has its OWN tool-call detector (saw_tool_calls / finish_reason),
    # separate from the non-stream path: a stream whose deltas carry tool_calls and
    # finishes with reason "tool_calls" is a continuation round and must NOT learn.
    fake = _provider(GATE_CREATE, PROPOSAL_CREATE)
    fake.stream_chunks = [
        {"choices": [{"index": 0, "delta": {"role": "assistant"}, "finish_reason": None}]},
        {
            "choices": [
                {
                    "index": 0,
                    "delta": {
                        "tool_calls": [
                            {
                                "index": 0,
                                "id": "c1",
                                "type": "function",
                                "function": {"name": "terminal.read", "arguments": "{}"},
                            }
                        ]
                    },
                    "finish_reason": None,
                }
            ]
        },
        {
            "choices": [{"index": 0, "delta": {}, "finish_reason": "tool_calls"}],
            "usage": {"prompt_tokens": 5, "completion_tokens": 2, "total_tokens": 7},
        },
    ]
    orch, _, primary = _orchestrator_with_learner(tmp_path, fake)

    req = RespondRequest.model_validate(
        {
            "protocol_version": 1,
            "session": {"id": "s", "turn_id": "t"},
            "input": {"messages": MESSAGES},
            "generation": {"stream": True},
        }
    )
    outcome = await orch.complete(req, upstream_model=DEEPSEEK_MODEL)
    assert outcome.stream is not None
    async for _event in outcome.stream:
        pass
    await orch.drain_background_tasks()

    gate_calls = [r for r in fake.requests if "skill-learning gate" in r.messages[0]["content"]]
    assert gate_calls == []
    assert not (primary / "daintree.test.created.md").exists()


@pytest.mark.asyncio
async def test_drain_background_tasks_awaits_pending(tmp_path: Path) -> None:
    # drain with zero tasks is a no-op; with a pending task it awaits to completion
    # and the done-callback discards it from the set.
    fake = _provider(GATE_NONE, None)
    orch, _, _ = _orchestrator_with_learner(tmp_path, fake)
    await orch.drain_background_tasks()  # zero tasks: must not raise

    done = asyncio.Event()

    async def _work() -> None:
        await asyncio.sleep(0)
        done.set()

    task = asyncio.create_task(_work())
    orch._background_tasks.add(task)
    task.add_done_callback(orch._background_tasks.discard)

    await orch.drain_background_tasks()
    assert done.is_set()
    assert not orch._background_tasks  # discarded on completion


@pytest.mark.asyncio
async def test_drain_background_tasks_cancels_after_timeout(tmp_path: Path) -> None:
    # A pass still running past the grace period is cancelled, so shutdown can't
    # hang on a slow (minutes-long) learning call.
    fake = _provider(GATE_NONE, None)
    orch, _, _ = _orchestrator_with_learner(tmp_path, fake)

    cancelled = asyncio.Event()

    async def _slow() -> None:
        try:
            await asyncio.sleep(60)
        except asyncio.CancelledError:
            cancelled.set()
            raise

    task = asyncio.create_task(_slow())
    orch._background_tasks.add(task)
    task.add_done_callback(orch._background_tasks.discard)

    await orch.drain_background_tasks(timeout=0.05)
    assert cancelled.is_set()
    assert task.cancelled()
    assert not orch._background_tasks
