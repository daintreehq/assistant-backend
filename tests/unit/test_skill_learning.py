"""Tests for the backend-internal skill-learning pipeline."""

from __future__ import annotations

import asyncio
import json
import re
from pathlib import Path
from typing import Any

import pytest
from fakes import FakeProvider, make_result, make_text_chunks
from pydantic import SecretStr

from daintree_assistant_server.config import (
    OPENROUTER_MODEL,
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
from daintree_assistant_server.skills.loader import parse_skill

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

_TARGET_RICH_META = (
    "---\n"
    "id: daintree.test.target\n"
    "title: Target\n"
    "summary: The original target summary.\n"
    "whenToUse: Use when improving the target.\n"
    "risk: git\n"
    "requiredTools:\n"
    "  - context.snapshot\n"
    "  - terminal.awaitAll\n"
    "---\n"
    "Use when: the original condition.\n\nProcedure:\n1. Original step.\n\nReport back: original.\n"
)

_TARGET_FOUNDATION = (
    "---\n"
    "id: daintree.test.target\n"
    "title: Target\n"
    "summary: The original target summary.\n"
    "whenToUse: Use when improving the target.\n"
    "foundation: true\n"
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
    "risk": "read",
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
    "risk": "read",
    "requiredTools": [],
    "body": "Use when: the improved condition.\n\nProcedure:\n1. Improved step.\n2. New guardrail.\n\n"
    "Report back: improved.",
    "change_summary": ["Added a guardrail."],
    "confidence": 0.88,
}

TEST_API_KEY = SecretStr("test-caller-key")

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
    allow_foundation: bool = False,
) -> Settings:
    extra: dict[str, Any] = {}
    if context_tokens is not None:
        extra["SKILL_LEARNING_CONTEXT_TOKENS"] = context_tokens
    return Settings(
        OPENROUTER_API_KEY="fw",
        STATE_SIGNING_KEY_CURRENT="s",
        RETRY_BACKOFF_BASE_SECONDS=0.0,
        SKILL_LEARNING_MODE=mode,
        SKILL_LEARNING_DIR=str(write_dir) if write_dir else "",
        SKILL_LEARNING_AUDIT_DIR=str(tmp_path / "audit"),
        SKILL_LEARNING_MIN_MESSAGES=1,
        SKILL_LEARNING_MIN_CONFIDENCE=0.75,
        SKILL_LEARNING_ALLOW_FOUNDATION_CREATE=allow_foundation,
        **extra,
    )


def _primary(
    tmp_path: Path, *, with_target: bool = False, target_text: str = _TARGET
) -> Path:
    primary = tmp_path / "skills"
    primary.mkdir()
    (primary / "daintree.test.example.md").write_text(_EXAMPLE, encoding="utf-8")
    if with_target:
        (primary / "daintree.test.target.md").write_text(target_text, encoding="utf-8")
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
    target_text: str = _TARGET,
    overlay: bool = False,
    context_tokens: int | None = None,
    allow_foundation: bool = False,
) -> tuple[SkillLearner, CatalogManager, Path]:
    primary = _primary(tmp_path, with_target=with_target, target_text=target_text)
    write_dir = (tmp_path / "overlay") if overlay else primary
    overlay_dir = write_dir if overlay else None
    settings = _settings(
        tmp_path,
        mode=mode,
        write_dir=write_dir if overlay else None,
        context_tokens=context_tokens,
        allow_foundation=allow_foundation,
    )
    manager = CatalogManager(
        load_catalog(primary, overlay_dir), primary_dir=primary, overlay_dir=overlay_dir
    )
    learner = SkillLearner(
        settings=settings, provider=fake, prompt_loader=_loader(), catalog_manager=manager
    )
    return learner, manager, write_dir


def _request(
    messages: list[dict[str, Any]] | None = None,
    *,
    state: str | None = None,
    session_id: str = "s",
    startup: dict[str, Any] | None = None,
    tools: list[str] | None = None,
) -> RespondRequest:
    inp: dict[str, Any] = {"messages": messages or MESSAGES}
    if tools is not None:
        inp["tools"] = [
            {"type": "function", "function": {"name": name, "parameters": {}}} for name in tools
        ]
    payload: dict[str, Any] = {
        "protocol_version": 2,
        "session": {"id": session_id, "turn_id": "t"},
        "input": inp,
        "startup": startup or {},
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
    outcome = await learner.run(api_key=TEST_API_KEY, request=_request(), active_ids=[])
    assert outcome.changed is True
    assert outcome.created_id == "daintree.test.created"
    assert (write_dir / "daintree.test.created.md").is_file()
    assert "daintree.test.created" in manager.catalog


@pytest.mark.asyncio
async def test_create_overlay_dir(tmp_path: Path) -> None:
    fake = _provider(GATE_CREATE, PROPOSAL_CREATE)
    learner, manager, write_dir = _learner(tmp_path, fake, overlay=True)
    outcome = await learner.run(api_key=TEST_API_KEY, request=_request(), active_ids=[])
    assert outcome.changed is True
    # Written to the overlay dir, and the packaged source is untouched.
    assert (write_dir / "daintree.test.created.md").is_file()
    assert not (manager.primary_dir / "daintree.test.created.md").exists()
    assert "daintree.test.created" in manager.catalog


@pytest.mark.asyncio
async def test_create_uses_stronger_builder_model_with_reasoning(tmp_path: Path) -> None:
    # Regression guard for the model split (see config.py): the gate runs on the
    # small main model (thinking-high by default now), while the builder runs on the
    # stronger reasoning model at the configured effort. The fake records every
    # request, so a silent regression back to the small model (or reasoning off) for
    # the builder is caught.
    assert SKILL_BUILDER_MODEL == "openai/gpt-5.6-sol"
    fake = _provider(GATE_CREATE, PROPOSAL_CREATE)
    learner, _, _ = _learner(tmp_path, fake)
    await learner.run(api_key=TEST_API_KEY, request=_request(), active_ids=[])

    gate_reqs = [r for r in fake.requests if r.model == OPENROUTER_MODEL]
    build_reqs = [r for r in fake.requests if r.model == SKILL_BUILDER_MODEL]
    assert gate_reqs and all(r.thinking_disabled is False for r in gate_reqs)
    assert all(r.reasoning_effort == "high" for r in gate_reqs)
    assert build_reqs and all(r.thinking_disabled is False for r in build_reqs)
    assert all(r.reasoning_effort == "high" for r in build_reqs)


@pytest.mark.asyncio
async def test_improve_uses_stronger_improver_model_with_reasoning(tmp_path: Path) -> None:
    assert SKILL_IMPROVER_MODEL == "openai/gpt-5.6-sol"
    fake = _provider(GATE_IMPROVE, PROPOSAL_IMPROVE)
    learner, _, _ = _learner(tmp_path, fake, with_target=True)
    await learner.run(api_key=TEST_API_KEY, request=_request(), active_ids=["daintree.test.target"])

    improve_reqs = [r for r in fake.requests if r.model == SKILL_IMPROVER_MODEL]
    assert improve_reqs and all(r.thinking_disabled is False for r in improve_reqs)
    assert all(r.reasoning_effort == "high" for r in improve_reqs)


@pytest.mark.asyncio
async def test_gate_and_builder_share_one_provider_and_differ_by_model(
    tmp_path: Path,
) -> None:
    # Both tiers reach OpenRouter over the SAME client; the only thing that differs
    # is the model each names. Assert one provider sees both, in order — a second
    # adapter creeping back in would break this.
    fake = _provider(GATE_CREATE, PROPOSAL_CREATE)
    learner, _, _ = _learner(tmp_path, fake)
    outcome = await learner.run(api_key=TEST_API_KEY, request=_request(), active_ids=[])

    assert outcome.changed is True
    assert [r.model for r in fake.requests] == [OPENROUTER_MODEL, SKILL_BUILDER_MODEL]


@pytest.mark.asyncio
async def test_builder_sends_no_temperature(tmp_path: Path) -> None:
    # The builder model accepts only its default temperature, and it does not
    # advertise the parameter at all — with OpenRouter's require_parameters on, a
    # request carrying one matches NO endpoint and hard-fails the routing rather
    # than degrading. The gate (small model) is unaffected and keeps its 0.0.
    fake = _provider(GATE_CREATE, PROPOSAL_CREATE)
    learner, _, _ = _learner(tmp_path, fake)
    await learner.run(api_key=TEST_API_KEY, request=_request(), active_ids=[])

    build_reqs = [r for r in fake.requests if r.model == SKILL_BUILDER_MODEL]
    assert build_reqs and all(r.temperature is None for r in build_reqs)
    gate_reqs = [r for r in fake.requests if r.model == OPENROUTER_MODEL]
    assert gate_reqs and all(r.temperature == 0.0 for r in gate_reqs)


def test_render_markdown_keeps_long_scalars_on_one_line() -> None:
    # yaml.dump folds scalars past 80 columns by default, which split long summary/
    # whenToUse values across lines in generated skills. The renderer disables that so
    # values stay single-line (matching hand-authored skills); folding is only cosmetic
    # but the on-disk form should not wrap. Guard both: no fold, and still loads clean.
    long_summary = (
        "Run a focused tie-breaker round for an existing multi-agent competition by "
        "collecting new entries from tied agents, polling the original voters, and "
        "tallying the runoff cleanly."
    )
    long_when = "Use when a multi-agent competition ends in a tie and " + "x " * 40
    meta = {
        "id": "daintree.test.longscalars",
        "title": "Resolve a tied multi-agent vote across a long descriptive title here",
        "summary": long_summary,
        "whenToUse": long_when.strip(),
        "risk": "read",
        "requiredTools": [],
    }
    body = "Use when: demonstrating.\n\nProcedure:\n1. Show it.\n\nReport back: shown."
    markdown = SkillLearner._render_markdown(meta, body)

    # The long values appear verbatim on a single line (no continuation lines).
    assert f"summary: {long_summary}\n" in markdown
    assert f"whenToUse: {long_when.strip()}\n" in markdown
    # And it still round-trips through the real loader to single-line strings.
    skill = parse_skill(markdown, expected_id="daintree.test.longscalars")
    assert "\n" not in skill.summary
    assert "\n" not in skill.when_to_use


@pytest.mark.asyncio
async def test_improve_rewrites_target(tmp_path: Path) -> None:
    fake = _provider(GATE_IMPROVE, PROPOSAL_IMPROVE)
    learner, manager, _ = _learner(tmp_path, fake, with_target=True)
    outcome = await learner.run(api_key=TEST_API_KEY, request=_request(), active_ids=["daintree.test.target"])
    assert outcome.changed is True
    assert outcome.created_id is None  # improve is not a new load
    skill = manager.catalog.get("daintree.test.target")
    assert skill is not None
    assert skill.summary == "An improved target summary."
    assert "New guardrail." in skill.body


@pytest.mark.asyncio
async def test_improve_preserves_foundation_flag(tmp_path: Path) -> None:
    # The improver's proposal never carries `foundation`, so re-rendering an improved
    # skill must preserve the target's existing flag — otherwise an apply-mode improve
    # would silently demote a foundation skill (e.g. daintree.foundation).
    fake = _provider(GATE_IMPROVE, PROPOSAL_IMPROVE)
    learner, manager, _ = _learner(
        tmp_path, fake, with_target=True, target_text=_TARGET_FOUNDATION
    )
    outcome = await learner.run(api_key=TEST_API_KEY, request=_request(), active_ids=["daintree.test.target"])
    assert outcome.changed is True
    skill = manager.catalog.get("daintree.test.target")
    assert skill is not None
    assert skill.summary == "An improved target summary."  # the improve landed
    assert skill.meta.foundation is True  # ...and foundation survived the rewrite


@pytest.mark.asyncio
async def test_improve_preserves_omitted_frontmatter(tmp_path: Path) -> None:
    # An improve proposal is a PARTIAL edit. SkillProposal defaults risk to "read" and
    # requiredTools to empty, so a proposal that simply omits them must NOT reset the
    # target's real values — requiredTools feeds the workflow-plan skill outlines and
    # risk is the reviewer's signal for what tier of mutation the runbook authorizes.
    proposal = {k: v for k, v in PROPOSAL_IMPROVE.items()
                if k not in {"risk", "requiredTools"}}
    fake = _provider(GATE_IMPROVE, proposal)
    learner, manager, _ = _learner(
        tmp_path, fake, with_target=True, target_text=_TARGET_RICH_META
    )
    outcome = await learner.run(api_key=TEST_API_KEY, request=_request(), active_ids=["daintree.test.target"])
    assert outcome.changed is True
    skill = manager.catalog.get("daintree.test.target")
    assert skill is not None
    assert skill.summary == "An improved target summary."  # the improve landed
    assert skill.meta.risk == "git"  # ...and neither of these was reset
    assert skill.meta.required_tools == ["context.snapshot", "terminal.awaitAll"]


@pytest.mark.asyncio
async def test_improve_applies_explicitly_set_frontmatter(tmp_path: Path) -> None:
    # The carry-over must not freeze metadata: a field the proposal actually SETS still
    # wins, so the improver can still sharpen it after a diagnosed selection miss.
    proposal = dict(PROPOSAL_IMPROVE)
    # requiredTools is supplied under its camelCase ALIAS: the carry-over keys off
    # `model_fields_set`, which records the FIELD name, so the alias must still win.
    proposal |= {"risk": "project", "requiredTools": ["context.snapshot"]}
    fake = _provider(GATE_IMPROVE, proposal)
    learner, manager, _ = _learner(
        tmp_path, fake, with_target=True, target_text=_TARGET_RICH_META
    )
    outcome = await learner.run(api_key=TEST_API_KEY, request=_request(), active_ids=["daintree.test.target"])
    assert outcome.changed is True
    skill = manager.catalog.get("daintree.test.target")
    assert skill is not None
    assert skill.meta.risk == "project"
    assert skill.meta.required_tools == ["context.snapshot"]  # camelCase alias won


@pytest.mark.asyncio
async def test_create_foundation_suppressed_without_optin(tmp_path: Path) -> None:
    # A create that asks for foundation:true lands as a NON-foundation skill unless the
    # opt-in is set — minting a foundation is a human curation decision by default.
    proposal = {**PROPOSAL_CREATE, "foundation": True}
    fake = _provider(GATE_CREATE, proposal)
    learner, manager, _ = _learner(tmp_path, fake, allow_foundation=False)
    outcome = await learner.run(api_key=TEST_API_KEY, request=_request(), active_ids=[])
    assert outcome.changed is True
    skill = manager.catalog.get("daintree.test.created")
    assert skill is not None
    assert skill.meta.foundation is False  # requested, but suppressed


@pytest.mark.asyncio
async def test_create_foundation_allowed_with_optin(tmp_path: Path) -> None:
    proposal = {**PROPOSAL_CREATE, "foundation": True}
    fake = _provider(GATE_CREATE, proposal)
    learner, manager, _ = _learner(tmp_path, fake, allow_foundation=True)
    outcome = await learner.run(api_key=TEST_API_KEY, request=_request(), active_ids=[])
    assert outcome.changed is True
    skill = manager.catalog.get("daintree.test.created")
    assert skill is not None
    assert skill.meta.foundation is True


@pytest.mark.asyncio
async def test_audit_records_suppressed_foundation_request(tmp_path: Path) -> None:
    # A requested-but-suppressed foundation must be reviewable: the audit records the
    # landed tier (foundation=false) alongside what the model asked for (requested=true).
    proposal = {**PROPOSAL_CREATE, "foundation": True}
    fake = _provider(GATE_CREATE, proposal)
    learner, _, _ = _learner(tmp_path, fake, allow_foundation=False)
    outcome = await learner.run(api_key=TEST_API_KEY, request=_request(), active_ids=[])
    assert outcome.changed is True
    audits = list((tmp_path / "audit").glob("daintree.test.created.*.json"))
    assert len(audits) == 1
    audit = json.loads(audits[0].read_text(encoding="utf-8"))
    assert audit["foundation_requested"] is True
    assert audit["foundation"] is False


@pytest.mark.asyncio
async def test_improve_cannot_promote_specific_without_optin(tmp_path: Path) -> None:
    # Improving a NON-foundation skill can never turn it into one without the opt-in,
    # even if the improver proposes foundation:true.
    proposal = {**PROPOSAL_IMPROVE, "foundation": True}
    fake = _provider(GATE_IMPROVE, proposal)
    learner, manager, _ = _learner(tmp_path, fake, with_target=True, allow_foundation=False)
    outcome = await learner.run(api_key=TEST_API_KEY, request=_request(), active_ids=["daintree.test.target"])
    assert outcome.changed is True
    skill = manager.catalog.get("daintree.test.target")
    assert skill is not None and skill.meta.foundation is False


@pytest.mark.asyncio
async def test_improve_promotes_specific_with_optin(tmp_path: Path) -> None:
    proposal = {**PROPOSAL_IMPROVE, "foundation": True}
    fake = _provider(GATE_IMPROVE, proposal)
    learner, manager, _ = _learner(tmp_path, fake, with_target=True, allow_foundation=True)
    outcome = await learner.run(api_key=TEST_API_KEY, request=_request(), active_ids=["daintree.test.target"])
    assert outcome.changed is True
    skill = manager.catalog.get("daintree.test.target")
    assert skill is not None and skill.meta.foundation is True


@pytest.mark.asyncio
async def test_gate_none_does_nothing(tmp_path: Path) -> None:
    fake = _provider(GATE_NONE, PROPOSAL_CREATE)
    learner, manager, write_dir = _learner(tmp_path, fake)
    before = manager.catalog.revision
    outcome = await learner.run(api_key=TEST_API_KEY, request=_request(), active_ids=[])
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
    outcome = await learner.run(api_key=TEST_API_KEY, request=_request(), active_ids=[])
    assert outcome.changed is False
    assert not (write_dir / "daintree.test.created.md").exists()
    assert len(fake.requests) == 1  # gate only — no builder call


@pytest.mark.asyncio
async def test_risk_case_normalized(tmp_path: Path) -> None:
    # Mechanical normalization: a case-mismatched risk token must not discard an
    # otherwise sound proposal.
    proposal = {**PROPOSAL_CREATE, "risk": "Read"}
    fake = _provider(GATE_CREATE, proposal)
    learner, manager, _ = _learner(tmp_path, fake)
    outcome = await learner.run(api_key=TEST_API_KEY, request=_request(), active_ids=[])
    assert outcome.changed is True
    skill = manager.catalog.get("daintree.test.created")
    assert skill is not None
    assert skill.meta.risk == "read"


@pytest.mark.asyncio
async def test_low_confidence_gate_does_nothing(tmp_path: Path) -> None:
    gate = {**GATE_CREATE, "confidence": 0.5}
    fake = _provider(gate, PROPOSAL_CREATE)
    learner, _, write_dir = _learner(tmp_path, fake)
    outcome = await learner.run(api_key=TEST_API_KEY, request=_request(), active_ids=[])
    assert outcome.changed is False
    assert not (write_dir / "daintree.test.created.md").exists()


@pytest.mark.asyncio
async def test_non_general_lesson_does_nothing(tmp_path: Path) -> None:
    gate = {**GATE_CREATE, "lesson_type": "project_specific"}
    fake = _provider(gate, PROPOSAL_CREATE)
    learner, _, write_dir = _learner(tmp_path, fake)
    outcome = await learner.run(api_key=TEST_API_KEY, request=_request(), active_ids=[])
    assert outcome.changed is False
    assert not (write_dir / "daintree.test.created.md").exists()


@pytest.mark.asyncio
async def test_propose_mode_writes_json_not_skill(tmp_path: Path) -> None:
    fake = _provider(GATE_CREATE, PROPOSAL_CREATE)
    learner, manager, write_dir = _learner(tmp_path, fake, mode="propose")
    outcome = await learner.run(api_key=TEST_API_KEY, request=_request(), active_ids=[])
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
    outcome = await learner.run(api_key=TEST_API_KEY, request=_request(), active_ids=[])
    assert outcome.changed is False
    assert not (write_dir / "daintree.test.created.md").exists()


@pytest.mark.asyncio
async def test_bad_new_id_rejected(tmp_path: Path) -> None:
    bad = {**PROPOSAL_CREATE, "skill_id": "not-house-style"}
    fake = _provider(GATE_CREATE, bad)
    learner, _, _ = _learner(tmp_path, fake)
    outcome = await learner.run(api_key=TEST_API_KEY, request=_request(), active_ids=[])
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
    outcome = await learner.run(api_key=TEST_API_KEY, request=_request(), active_ids=[])
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
    outcome = await learner.run(api_key=TEST_API_KEY, request=_request(), active_ids=[])
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
    outcome = await learner.run(api_key=TEST_API_KEY, request=_request(), active_ids=[])
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
    outcome = await learner.run(api_key=TEST_API_KEY, request=_request(), active_ids=["daintree.test.target"])
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
        api_key=TEST_API_KEY,
        request=_request(),
        active_ids=[],
        trailing_messages=[ChatMessage(role="assistant", content=final)],
    )
    transcript = seen["transcript"]
    assert final in transcript
    assert transcript.index(final) > transcript.index("what happened here?")


@pytest.mark.asyncio
async def test_gate_transcript_excludes_structured_startup_context(tmp_path: Path) -> None:
    marker = "STARTUP-MUST-NOT-BECOME-LEARNING-EVIDENCE-7f3c"
    seen: dict[str, str] = {}
    fake = FakeProvider()

    def _complete(req: Any) -> Any:
        if "skill-learning gate" in req.messages[0]["content"]:
            seen["request"] = "\n".join(
                m["content"] for m in req.messages if isinstance(m.get("content"), str)
            )
        return make_result(content=json.dumps(GATE_NONE))

    fake.complete_result = _complete
    learner, _, _ = _learner(tmp_path, fake)
    await learner.run(
        api_key=TEST_API_KEY,
        request=_request(
            messages=[*MESSAGES, {"role": "user", "content": "VISIBLE-LEARNING-EVIDENCE"}],
            startup={
                "project": {"id": marker, "name": marker, "path": f"/{marker}"},
                "agent_roster": {
                    "agents": [{"id": marker, "source": "user"}],
                    "total_count": 1,
                },
                "project_instructions": marker,
            },
        ),
        active_ids=[],
    )
    assert "VISIBLE-LEARNING-EVIDENCE" in seen["request"]
    assert marker not in seen["request"]


@pytest.mark.asyncio
async def test_create_id_collision_rejected(tmp_path: Path) -> None:
    # Proposal reuses an existing catalog id: a create must not clobber it.
    collide = {**PROPOSAL_CREATE, "skill_id": "daintree.test.example"}
    fake = _provider(GATE_CREATE, collide)
    learner, manager, _ = _learner(tmp_path, fake)
    outcome = await learner.run(api_key=TEST_API_KEY, request=_request(), active_ids=[])
    assert outcome.changed is False
    assert manager.catalog.get("daintree.test.example").summary == "An example skill for tests."


@pytest.mark.asyncio
async def test_improve_wrong_target_rejected(tmp_path: Path) -> None:
    # Gate targets one skill; the improver returns a different (valid) id. It must be
    # rejected so an unrelated skill is never overwritten.
    proposal = {**PROPOSAL_IMPROVE, "skill_id": "daintree.test.example"}
    fake = _provider(GATE_IMPROVE, proposal)
    learner, manager, _ = _learner(tmp_path, fake, with_target=True)
    outcome = await learner.run(api_key=TEST_API_KEY, request=_request(), active_ids=["daintree.test.target"])
    assert outcome.changed is False
    assert manager.catalog.get("daintree.test.target").summary == "The original target summary."
    assert manager.catalog.get("daintree.test.example").summary == "An example skill for tests."


@pytest.mark.asyncio
async def test_create_with_update_action_rejected(tmp_path: Path) -> None:
    # A create gate with an "update_skill" proposal action is inconsistent → reject.
    proposal = {**PROPOSAL_CREATE, "action": "update_skill"}
    fake = _provider(GATE_CREATE, proposal)
    learner, _, write_dir = _learner(tmp_path, fake)
    outcome = await learner.run(api_key=TEST_API_KEY, request=_request(), active_ids=[])
    assert outcome.changed is False
    assert not (write_dir / "daintree.test.created.md").exists()


@pytest.mark.asyncio
async def test_no_change_proposal_does_nothing(tmp_path: Path) -> None:
    fake = _provider(GATE_IMPROVE, {"action": "no_change", "skill_id": "daintree.test.target"})
    learner, manager, _ = _learner(tmp_path, fake, with_target=True)
    outcome = await learner.run(api_key=TEST_API_KEY, request=_request(), active_ids=["daintree.test.target"])
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
    outcome = await learner.run(api_key=TEST_API_KEY, request=_request(), active_ids=["daintree.test.target"])
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
    task = asyncio.create_task(learner.run(api_key=TEST_API_KEY, request=_request(), active_ids=[]))
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
    outcome = await learner.run(api_key=TEST_API_KEY, request=_request(), active_ids=[])
    assert outcome.changed is False
    assert not (write_dir / "daintree.test.created.md").exists()
    assert manager.catalog.revision == before


@pytest.mark.asyncio
async def test_identical_improve_applies_once_then_skips(tmp_path: Path) -> None:
    # The completed workflow stays in history for many turns; a re-proposed,
    # byte-identical improve must not re-apply forever.
    fake = _provider(GATE_IMPROVE, PROPOSAL_IMPROVE)
    learner, manager, _ = _learner(tmp_path, fake, with_target=True)
    first = await learner.run(api_key=TEST_API_KEY, request=_request(), active_ids=["daintree.test.target"])
    assert first.changed is True
    revision = manager.catalog.revision
    second = await learner.run(api_key=TEST_API_KEY, request=_request(), active_ids=["daintree.test.target"])
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
    outcome = await learner.run(api_key=TEST_API_KEY, request=_request(), active_ids=[])
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
    outcome = await learner.run(api_key=TEST_API_KEY, request=_request(), active_ids=["daintree.test.target"])
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
    outcome = await learner.run(api_key=TEST_API_KEY, request=_request(), active_ids=[])
    assert outcome.changed is False
    assert fake.requests == []


@pytest.mark.asyncio
async def test_ineligible_short_conversation_skips_gate(tmp_path: Path) -> None:
    fake = _provider(GATE_CREATE, PROPOSAL_CREATE)
    learner, _, _ = _learner(tmp_path, fake)
    # No assistant message → not eligible → gate never runs.
    req = _request([{"role": "user", "content": "rename my terminals"}])
    outcome = await learner.run(api_key=TEST_API_KEY, request=req, active_ids=[])
    assert outcome.changed is False
    assert fake.requests == []


@pytest.mark.asyncio
async def test_provider_error_fails_open(tmp_path: Path) -> None:
    fake = _provider(GATE_CREATE, PROPOSAL_CREATE)
    fake.complete_error = upstream_error("boom", retryable=False)
    learner, _, write_dir = _learner(tmp_path, fake)
    # Must not raise; the turn proceeds untouched.
    outcome = await learner.run(api_key=TEST_API_KEY, request=_request(), active_ids=[])
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

    outcome = await orch.complete(_request(), upstream_model=OPENROUTER_MODEL, api_key=TEST_API_KEY)
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

    outcome = await orch.complete(_request(), upstream_model=OPENROUTER_MODEL, api_key=TEST_API_KEY)
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
            "protocol_version": 2,
            "session": {"id": "s", "turn_id": "t"},
            "input": {"messages": MESSAGES},
            "startup": {},
            "generation": {"stream": True},
        }
    )
    outcome = await orch.complete(req, upstream_model=OPENROUTER_MODEL, api_key=TEST_API_KEY)
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
            "protocol_version": 2,
            "session": {"id": "s", "turn_id": "t"},
            "input": {"messages": MESSAGES},
            "startup": {},
            "generation": {"stream": True},
        }
    )
    outcome = await orch.complete(req, upstream_model=OPENROUTER_MODEL, api_key=TEST_API_KEY)
    assert outcome.stream is not None
    async for _event in outcome.stream:
        pass
    await orch.drain_background_tasks()

    gate_calls = [r for r in fake.requests if "skill-learning gate" in r.messages[0]["content"]]
    assert gate_calls == []
    assert not (primary / "daintree.test.created.md").exists()


# Verbatim from session ses_659b0fc9 (issue #26): finish_reason "stop", zero tool
# calls, 20/43/32 completion tokens — each announced a read and emitted no call.
# The last is scripted by our own prompt (main/base/10-hard-rules.md).
DEAD_ROUND_REPLIES = [
    "All five votes are in. Let me read them, one read per terminal, batched.",
    (
        "You're right, my previous reply ended with a tool call intent but I only "
        "emitted the reads in the next sentence without actually calling them. Let "
        "me read all five votes now, in one batched reply."
    ),
    (
        "I have the live roster, all five agents are waiting at a prompt, meaning "
        "they finished their votes. Let me read each vote now in one batch."
    ),
    "This needs file changes, so I'll spawn a visible agent to do them.",
]


@pytest.mark.asyncio
@pytest.mark.parametrize("reply", DEAD_ROUND_REPLIES)
async def test_learner_skipped_when_turn_only_announced_an_action(
    tmp_path: Path, reply: str
) -> None:
    # A "dead round" reaches the trigger looking exactly like a final answer — no
    # tool calls, finish_reason "stop", non-empty text — but it accomplished nothing.
    # The response path re-rolls it once; the fake answers the re-roll with the same
    # reply, which is precisely the case this guard exists for: a dead round the
    # re-roll did not fix must still not be learned from as a completed workflow.
    fake = _provider(GATE_CREATE, PROPOSAL_CREATE)

    def _complete(req: Any) -> Any:
        system = req.messages[0]["content"]
        if "skill-learning gate" in system:
            return make_result(content=json.dumps(GATE_CREATE))
        if "skill builder" in system or "skill improver" in system:
            return make_result(content=json.dumps(PROPOSAL_CREATE))
        return make_result(content=reply)

    fake.complete_result = _complete
    orch, _, primary = _orchestrator_with_learner(tmp_path, fake)

    # Tools MUST be offered: a round with nothing callable cannot have dropped a
    # call, so the predicate would short-circuit and the test would pass vacuously.
    outcome = await orch.complete(
        _request(tools=["terminal.read"]), upstream_model=OPENROUTER_MODEL, api_key=TEST_API_KEY
    )
    assert outcome.body is not None
    # The reply itself still reaches the client — this guard only gates LEARNING.
    assert outcome.body["message"]["content"] == reply
    await orch.drain_background_tasks()

    gate_calls = [r for r in fake.requests if "skill-learning gate" in r.messages[0]["content"]]
    assert gate_calls == []
    assert not (primary / "daintree.test.created.md").exists()


@pytest.mark.asyncio
@pytest.mark.parametrize("reply", DEAD_ROUND_REPLIES)
async def test_learner_skipped_when_streamed_turn_only_announced_an_action(
    tmp_path: Path, reply: str
) -> None:
    # The streaming path accumulates its text and funnels through the same trigger,
    # so a dead round streamed to a clean `done` must be skipped there too.
    fake = _provider(GATE_CREATE, PROPOSAL_CREATE)
    fake.stream_chunks = make_text_chunks(reply)
    orch, _, primary = _orchestrator_with_learner(tmp_path, fake)

    req = RespondRequest.model_validate(
        {
            "protocol_version": 2,
            "session": {"id": "s", "turn_id": "t"},
            "input": {
                "messages": MESSAGES,
                "tools": [
                    {"type": "function", "function": {"name": "terminal.read", "parameters": {}}}
                ],
            },
            "startup": {},
            "generation": {"stream": True},
        }
    )
    outcome = await orch.complete(req, upstream_model=OPENROUTER_MODEL, api_key=TEST_API_KEY)
    assert outcome.stream is not None
    events = [event async for event in outcome.stream]  # drain the SSE stream
    await orch.drain_background_tasks()

    # Self-authenticating: prove the stream actually REACHED a clean `done` carrying
    # the whole reply, BEFORE asserting the absence of learning. Otherwise this test
    # would still pass if the stream errored, or accumulated nothing and tripped the
    # older blank-text guard — passing for a reason that is not this guard.
    assert not [e for e in events if e.startswith("event: error\n")]
    assert events[-1].startswith("event: done\n")
    streamed = "".join(
        json.loads(e.split("data: ", 1)[1]).get("content", "") or ""
        for e in events
        if e.startswith("event: delta\n")
    )
    # `make_text_chunks` emits word-wise deltas, so the accumulation carries a
    # trailing space — which is exactly why the predicate normalizes before matching.
    assert streamed.strip() == reply

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


# ---- per-session learned-skill damp -----------------------------------------


@pytest.mark.asyncio
async def test_improve_lands_once_per_session(tmp_path: Path) -> None:
    fake = _provider(GATE_IMPROVE, PROPOSAL_IMPROVE)
    learner, _, _ = _learner(tmp_path, fake, with_target=True)
    first = await learner.run(api_key=TEST_API_KEY, request=_request(), active_ids=[])
    assert first.changed is True
    after_first = len(fake.requests)  # gate + improver
    second = await learner.run(api_key=TEST_API_KEY, request=_request(), active_ids=[])
    assert second.changed is False
    # The damp skips BEFORE the improver call: only the gate ran the second time.
    assert len(fake.requests) == after_first + 1
    # A completed pass always releases its in-flight reservation.
    assert not learner._improves_in_flight


@pytest.mark.asyncio
async def test_improve_damp_is_per_session(tmp_path: Path) -> None:
    fake = _provider(GATE_IMPROVE, PROPOSAL_IMPROVE)
    learner, _, _ = _learner(tmp_path, fake, with_target=True)
    await learner.run(api_key=TEST_API_KEY, request=_request(), active_ids=[])
    after_first = len(fake.requests)
    # A different session is entitled to its own change for the same skill: the
    # improver runs again (gate + builder), even though the identical render then
    # lands nothing new.
    outcome = await learner.run(api_key=TEST_API_KEY, request=_request(session_id="s2"), active_ids=[])
    assert len(fake.requests) == after_first + 2
    assert outcome.changed is False  # identical to the already-applied improvement


@pytest.mark.asyncio
async def test_create_marks_new_skill_for_session_damp(tmp_path: Path) -> None:
    # After a create lands, a same-session improve verdict targeting the freshly
    # minted skill (the same workflow re-gated on a later turn) spends no builder call.
    gate_improve_created = {
        **GATE_IMPROVE,
        "target_skill_id": "daintree.test.created",
    }
    calls = {"gate": 0}
    fake = FakeProvider()

    def _complete(req: Any) -> Any:
        system = req.messages[0]["content"]
        if "skill-learning gate" in system:
            calls["gate"] += 1
            verdict = GATE_CREATE if calls["gate"] == 1 else gate_improve_created
            return make_result(content=json.dumps(verdict))
        return make_result(content=json.dumps(PROPOSAL_CREATE))

    fake.complete_result = _complete
    learner, _, _ = _learner(tmp_path, fake)
    first = await learner.run(api_key=TEST_API_KEY, request=_request(), active_ids=[])
    assert first.created_id == "daintree.test.created"
    after_first = len(fake.requests)
    second = await learner.run(api_key=TEST_API_KEY, request=_request(), active_ids=[])
    assert second.changed is False
    assert len(fake.requests) == after_first + 1  # gate only


def test_session_damp_registry_is_bounded(tmp_path: Path) -> None:
    fake = _provider(GATE_NONE, None)
    learner, _, _ = _learner(tmp_path, fake)
    limit = SkillLearner._MAX_COOLDOWN_SESSIONS
    for i in range(limit + 5):
        learner._mark_landed(f"session-{i}", "daintree.test.target")
    assert len(learner._session_landed) == limit
    assert "session-0" not in learner._session_landed  # oldest evicted
    assert learner._already_landed(f"session-{limit + 4}", "daintree.test.target")
    assert not learner._already_landed("session-0", "daintree.test.target")


@pytest.mark.asyncio
async def test_no_change_verdict_consumes_session_damp(tmp_path: Path) -> None:
    # The improver examined the skill and declared it settled — the same session
    # must not re-spend the improver on the same evidence every later turn.
    no_change = {"action": "no_change", "skill_id": "daintree.test.target", "confidence": 0}
    fake = _provider(GATE_IMPROVE, no_change)
    learner, _, _ = _learner(tmp_path, fake, with_target=True)
    first = await learner.run(api_key=TEST_API_KEY, request=_request(), active_ids=[])
    assert first.changed is False
    after_first = len(fake.requests)  # gate + improver
    await learner.run(api_key=TEST_API_KEY, request=_request(), active_ids=[])
    assert len(fake.requests) == after_first + 1  # gate only — improver not re-spent


@pytest.mark.asyncio
async def test_inflight_improve_reservation_blocks_overlapping_pass(tmp_path: Path) -> None:
    fake = _provider(GATE_IMPROVE, PROPOSAL_IMPROVE)
    learner, _, _ = _learner(tmp_path, fake, with_target=True)
    # Simulate a concurrent pass parked inside its builder call for the same skill.
    learner._improves_in_flight.add(("s", "daintree.test.target"))
    outcome = await learner.run(api_key=TEST_API_KEY, request=_request(), active_ids=[])
    assert outcome.changed is False
    assert len(fake.requests) == 1  # gate only — no double builder spend
    # The reservation belongs to the (simulated) first pass — not consumed here.
    assert ("s", "daintree.test.target") in learner._improves_in_flight


# ---- create damping ----------------------------------------------------------


@pytest.mark.asyncio
async def test_session_mints_at_most_one_skill(tmp_path: Path) -> None:
    # Observed failure: two passes over the same workflow minted twin skills ~50s
    # apart, both gate verdicts asserting "no existing skill covers this" (the first
    # had not landed yet). Exact-id equality cannot see a differently-NAMED twin, so
    # a session gets one create, whatever the second one would have been called.
    second = {**PROPOSAL_CREATE, "skill_id": "daintree.test.twin"}
    calls = {"build": 0}
    fake = FakeProvider()

    def _complete(req: Any) -> Any:
        system = req.messages[0]["content"]
        if "skill-learning gate" in system:
            return make_result(content=json.dumps(GATE_CREATE))
        calls["build"] += 1
        return make_result(content=json.dumps(PROPOSAL_CREATE if calls["build"] == 1 else second))

    fake.complete_result = _complete
    learner, manager, write_dir = _learner(tmp_path, fake)

    first = await learner.run(api_key=TEST_API_KEY, request=_request(), active_ids=[])
    assert first.created_id == "daintree.test.created"
    after_first = len(fake.requests)

    outcome = await learner.run(api_key=TEST_API_KEY, request=_request(), active_ids=[])
    assert outcome.changed is False
    assert len(fake.requests) == after_first + 1  # gate only — no second builder spend
    assert not (write_dir / "daintree.test.twin.md").exists()
    assert "daintree.test.twin" not in manager.catalog


@pytest.mark.asyncio
async def test_create_damp_is_per_session(tmp_path: Path) -> None:
    # The create damp is a per-session budget, not a global one: a DIFFERENT session
    # may still mint a skill (the catalog it gates against now contains the first).
    # The second session proposes a DISTINCT id, so the assertion is that a real
    # second skill lands — not merely that two more model calls were spent.
    second = {**PROPOSAL_CREATE, "skill_id": "daintree.test.later", "title": "Later skill"}
    calls = {"build": 0}
    fake = FakeProvider()

    def _complete(req: Any) -> Any:
        if "skill-learning gate" in req.messages[0]["content"]:
            return make_result(content=json.dumps(GATE_CREATE))
        calls["build"] += 1
        return make_result(content=json.dumps(PROPOSAL_CREATE if calls["build"] == 1 else second))

    fake.complete_result = _complete
    learner, manager, write_dir = _learner(tmp_path, fake)

    first = await learner.run(api_key=TEST_API_KEY, request=_request(), active_ids=[])
    assert first.created_id == "daintree.test.created"
    after_first = len(fake.requests)

    outcome = await learner.run(api_key=TEST_API_KEY, request=_request(session_id="s2"), active_ids=[])
    assert len(fake.requests) == after_first + 2  # gate + builder ran again
    assert outcome.changed is True
    assert outcome.created_id == "daintree.test.later"
    assert (write_dir / "daintree.test.later.md").is_file()
    assert "daintree.test.later" in manager.catalog
    assert "daintree.test.created" in manager.catalog  # the first one survived the reload


@pytest.mark.asyncio
async def test_inflight_create_reservation_is_process_wide(tmp_path: Path) -> None:
    # The reservation is process-wide, not per-session: while one create is between
    # its builder call and its write, ANY other session's create is dropped. That is
    # what serializes creates and closes the duplicate race.
    #
    # Driven as two genuinely overlapping `run` calls rather than by flipping the
    # private flag: the first pass is held INSIDE its builder call until the second
    # pass has run its whole gate → damp path, which is the interleaving the flag
    # exists for. The second pass would propose a differently-NAMED skill, so if the
    # reservation failed to hold, a twin would land on disk.
    twin = {**PROPOSAL_CREATE, "skill_id": "daintree.test.twin", "title": "Twin skill"}
    calls = {"build": 0}
    first_in_builder = asyncio.Event()
    second_finished = asyncio.Event()

    def _complete(req: Any) -> Any:
        if "skill-learning gate" in req.messages[0]["content"]:
            return make_result(content=json.dumps(GATE_CREATE))
        calls["build"] += 1
        return make_result(content=json.dumps(PROPOSAL_CREATE if calls["build"] == 1 else twin))

    class OverlappingProvider(FakeProvider):
        async def complete(self, request: Any) -> Any:
            is_builder = "skill-learning gate" not in request.messages[0]["content"]
            if is_builder and not first_in_builder.is_set():
                first_in_builder.set()
                await second_finished.wait()  # park pass one between build and write
            return await super().complete(request)

    fake = OverlappingProvider()
    fake.complete_result = _complete
    learner, manager, write_dir = _learner(tmp_path, fake)

    async def second_pass() -> Any:
        await first_in_builder.wait()
        try:
            return await learner.run(api_key=TEST_API_KEY, request=_request(session_id="other"), active_ids=[])
        finally:
            second_finished.set()  # let pass one out of its builder call

    first, second = await asyncio.wait_for(
        asyncio.gather(
            learner.run(api_key=TEST_API_KEY, request=_request(session_id="first"), active_ids=[]),
            second_pass(),
        ),
        timeout=10,
    )

    assert second.changed is False
    assert len(fake.requests) == 3  # first: gate + builder; second: gate only
    assert not (write_dir / "daintree.test.twin.md").exists()
    assert "daintree.test.twin" not in manager.catalog
    # The holder's own create still lands, and the reservation is released, not leaked.
    assert first.created_id == "daintree.test.created"
    assert (write_dir / "daintree.test.created.md").is_file()
    assert learner._create_in_flight is False


@pytest.mark.asyncio
async def test_create_reservation_released_after_pass(tmp_path: Path) -> None:
    fake = _provider(GATE_CREATE, PROPOSAL_CREATE)
    learner, _, _ = _learner(tmp_path, fake)
    await learner.run(api_key=TEST_API_KEY, request=_request(), active_ids=[])
    assert learner._create_in_flight is False


@pytest.mark.asyncio
async def test_create_abandoned_when_the_catalog_moved_under_the_gate(tmp_path: Path) -> None:
    # The in-flight reservation alone does NOT close the duplicate-skill race: it only
    # covers the window between builder call and write. Two passes can both capture
    # catalog C0, the first can create + reload to C1 and RELEASE the flag, and the
    # second — still holding C0 — then finds the flag clear and mints a twin, because
    # its verdict ("no existing skill covers this") and its collision check both run
    # against a catalog that no longer exists. This is the shape that produced two real
    # duplicate pairs, ~50s apart. A revision comparison at reservation time closes it.
    fake = _provider(GATE_CREATE, PROPOSAL_CREATE)
    learner, manager, write_dir = _learner(tmp_path, fake)

    original_gate = learner._gate

    async def gate_then_move_the_catalog(*args: object, **kwargs: object) -> object:
        verdict = await original_gate(*args, **kwargs)  # type: ignore[arg-type]
        # Simulate the other pass landing its skill and reloading WHILE we gated.
        (manager.primary_dir / "daintree.test.rival.md").write_text(
            _TARGET.replace("daintree.test.target", "daintree.test.rival"), encoding="utf-8"
        )
        manager.reload_sync()
        return verdict

    learner._gate = gate_then_move_the_catalog  # type: ignore[method-assign]
    outcome = await learner.run(api_key=TEST_API_KEY, request=_request(), active_ids=[])

    assert outcome.changed is False
    assert len(fake.requests) == 1  # gate only — no builder spend
    assert not (write_dir / "daintree.test.created.md").exists()
    assert learner._create_in_flight is False  # reservation never taken, nothing leaked


# ---- disk-drift guard --------------------------------------------------------


@pytest.mark.asyncio
async def test_improve_aborts_when_file_changed_on_disk(tmp_path: Path) -> None:
    # The owner's actual loop: ./dev is running in apply mode and they hand-edit a
    # skill file. Nothing reloads the catalog except a learning write, so the
    # in-memory staleness check cannot see that edit — and the improver was working
    # from the pre-edit body anyway. The write must be abandoned, not applied.
    fake = _provider(GATE_IMPROVE, PROPOSAL_IMPROVE)
    learner, manager, write_dir = _learner(tmp_path, fake, with_target=True)
    path = write_dir / "daintree.test.target.md"
    hand_edited = _TARGET.replace("Original step.", "A hand-edited step.")
    path.write_text(hand_edited, encoding="utf-8")

    outcome = await learner.run(api_key=TEST_API_KEY, request=_request(), active_ids=["daintree.test.target"])
    assert outcome.changed is False
    assert path.read_text(encoding="utf-8") == hand_edited  # the hand edit survived
    assert not (write_dir / "daintree.test.target.md.tmp").exists()  # temp discarded
    assert "New guardrail." not in manager.catalog.get("daintree.test.target").body


@pytest.mark.asyncio
async def test_improve_tolerates_cosmetic_yaml_reflow(tmp_path: Path) -> None:
    # The guard compares PARSED content hashes, never raw bytes: re-ordering or
    # re-quoting frontmatter changes the file without changing the skill, and must
    # not be mistaken for a hand edit.
    fake = _provider(GATE_IMPROVE, PROPOSAL_IMPROVE)
    learner, manager, write_dir = _learner(tmp_path, fake, with_target=True)
    path = write_dir / "daintree.test.target.md"
    reflowed = (
        "---\n"
        'summary: "The original target summary."\n'
        "id: daintree.test.target\n"
        "whenToUse: Use when improving the target.\n"
        "title: Target\n"
        "---\n"
        "Use when: the original condition.\n\nProcedure:\n1. Original step.\n\n"
        "Report back: original.\n"
    )
    path.write_text(reflowed, encoding="utf-8")
    assert path.read_bytes() != _TARGET.encode()  # different bytes...
    assert parse_skill(reflowed).content_hash == parse_skill(_TARGET).content_hash  # same skill

    outcome = await learner.run(api_key=TEST_API_KEY, request=_request(), active_ids=["daintree.test.target"])
    assert outcome.changed is True
    assert manager.catalog.get("daintree.test.target").summary == "An improved target summary."


@pytest.mark.asyncio
async def test_create_is_not_blocked_by_drift_check(tmp_path: Path) -> None:
    # The drift check is improve-only — a create has no prior body to protect, and
    # an existing file at that path is already caught by the id-collision guard.
    fake = _provider(GATE_CREATE, PROPOSAL_CREATE)
    learner, _, write_dir = _learner(tmp_path, fake)
    outcome = await learner.run(api_key=TEST_API_KEY, request=_request(), active_ids=[])
    assert outcome.changed is True
    assert (write_dir / "daintree.test.created.md").is_file()


# ---- body-growth control -----------------------------------------------------


_STATED_MAX_RE = re.compile(r"hard max ~(\d+)\s*KB")


def test_house_ceiling_matches_the_prompts_stated_max() -> None:
    # Read the ceiling out of the PROMPTS, not out of a second copy of the constant:
    # the drift this guards against is prose and code disagreeing, and a test that
    # never opens build.md / improve.md cannot see that happen. A code ceiling looser
    # than the number the model was told backs nothing.
    from daintree_assistant_server.services import skill_learning as sl

    loader = _loader()
    for name in ("build", "improve"):
        match = _STATED_MAX_RE.search(loader.skill_learning_prompt(name))
        assert match is not None, f"{name}.md no longer states a 'hard max ~N KB' body size"
        stated = int(match.group(1)) * 1024
        assert stated == sl._HOUSE_MAX_BODY_BYTES, (
            f"{name}.md tells the model {match.group(1)} KB but the code ceiling is "
            f"{sl._HOUSE_MAX_BODY_BYTES / 1024:g} KB"
        )

    # ...and the constant is actually enforced, not merely declared.
    oversized = "Use when: x.\n\nProcedure:\n1. a.\n2. b.\n3. c.\n" + ("filler. " * 2500)
    assert len(oversized.encode()) > sl._HOUSE_MAX_BODY_BYTES
    assert any("size ceiling" in p for p in _house_style_problems(oversized + "\nReport back: y."))


@pytest.mark.asyncio
async def test_improve_rejects_runaway_body_growth(tmp_path: Path) -> None:
    # Every landed improve to date has been net-additive; one skill grew 4x in five
    # days. A single improve may not add more than the growth allowance.
    bloated = {
        **PROPOSAL_IMPROVE,
        "body": "Use when: the improved condition.\n\nProcedure:\n1. Improved step.\n"
        + "".join(f"{i}. Another step that keeps piling on prose.\n" for i in range(2, 200))
        + "\nReport back: improved.",
    }
    fake = _provider(GATE_IMPROVE, bloated)
    learner, manager, _ = _learner(tmp_path, fake, with_target=True)
    outcome = await learner.run(api_key=TEST_API_KEY, request=_request(), active_ids=["daintree.test.target"])
    assert outcome.changed is False
    assert manager.catalog.get("daintree.test.target").summary == "The original target summary."


@pytest.mark.asyncio
async def test_improve_allows_growth_within_the_allowance(tmp_path: Path) -> None:
    # A real lesson still fits: the flat slack is generous enough for a new step plus
    # its guardrail on a small skill.
    from daintree_assistant_server.services import skill_learning as sl

    target_body = parse_skill(_TARGET).body
    added = "x" * (sl._IMPROVE_GROWTH_FLAT_BYTES - 200)
    grown = {
        **PROPOSAL_IMPROVE,
        "body": f"Use when: the improved condition.\n\nProcedure:\n1. Improved step.\n"
        f"2. New guardrail: {added}\n\nReport back: improved.",
    }
    assert len(grown["body"].encode()) > len(target_body.encode())
    assert len(grown["body"].encode()) <= sl._growth_limit(len(target_body.encode()))
    fake = _provider(GATE_IMPROVE, grown)
    learner, manager, _ = _learner(tmp_path, fake, with_target=True)
    outcome = await learner.run(api_key=TEST_API_KEY, request=_request(), active_ids=["daintree.test.target"])
    assert outcome.changed is True
    assert added in manager.catalog.get("daintree.test.target").body


def test_growth_limit_uses_the_larger_of_flat_and_proportional() -> None:
    from daintree_assistant_server.services import skill_learning as sl

    # Small skill: the flat slack wins (proportional would be a few hundred bytes).
    assert sl._growth_limit(1_000) == 1_000 + sl._IMPROVE_GROWTH_FLAT_BYTES
    # Large skill: the proportional slack wins.
    assert sl._growth_limit(40_000) == int(40_000 * sl._IMPROVE_GROWTH_RATIO)


# ---- requiredTools admission -------------------------------------------------


@pytest.mark.asyncio
async def test_invented_required_tool_is_stripped_not_fatal(tmp_path: Path) -> None:
    # SkillMeta only checks a tool name's CHARACTERS, so an invented-but-plausible
    # tool id would otherwise land in catalog metadata the selector reads. Unknown
    # names are dropped; the skill itself still lands (the lesson is not thrown away).
    proposal = {
        **PROPOSAL_CREATE,
        "requiredTools": ["context.snapshot", "terminal.teleport", "agent__spawn"],
    }
    fake = _provider(GATE_CREATE, proposal)
    learner, manager, _ = _learner(tmp_path, fake)
    outcome = await learner.run(
        api_key=TEST_API_KEY,
        request=_request(tools=["context__snapshot", "agent__spawn"]), active_ids=[]
    )
    assert outcome.changed is True
    skill = manager.catalog.get("daintree.test.created")
    assert skill is not None
    # Wire "__" normalized to the dotted catalog form; the invented one is gone.
    assert skill.meta.required_tools == ["context.snapshot", "agent.spawn"]


@pytest.mark.asyncio
async def test_required_tool_already_in_catalog_survives(tmp_path: Path) -> None:
    # The allow-set is (this turn's tools) UNION (every requiredTools name already in
    # the catalog), so a real tool that simply wasn't offered on the teaching turn is
    # not stripped out of a skill.
    proposal = {**PROPOSAL_CREATE, "requiredTools": ["context.snapshot"]}
    fake = _provider(GATE_CREATE, proposal)
    primary = _primary(tmp_path)
    (primary / "daintree.test.tooled.md").write_text(
        "---\nid: daintree.test.tooled\ntitle: Tooled\nsummary: Has a tool.\n"
        "whenToUse: Use when tooled.\nrequiredTools:\n  - context.snapshot\n---\n"
        "Use when: tooled.\n\nProcedure:\n1. Do it.\n\nReport back: done.\n",
        encoding="utf-8",
    )
    manager = CatalogManager(load_catalog(primary), primary_dir=primary)
    learner = SkillLearner(
        settings=_settings(tmp_path),
        provider=fake,
        prompt_loader=_loader(),
        catalog_manager=manager,
    )
    outcome = await learner.run(api_key=TEST_API_KEY, request=_request(tools=["terminal__read"]), active_ids=[])
    assert outcome.changed is True
    assert manager.catalog.get("daintree.test.created").meta.required_tools == [
        "context.snapshot"
    ]


@pytest.mark.asyncio
async def test_required_tools_deduped_after_normalization(tmp_path: Path) -> None:
    # Two spellings of ONE tool collapse to the same logical name — they must not
    # both land in the file's requiredTools (SkillMeta never dedupes tool names, so
    # the dedupe has to happen in _admissible_tools).
    proposal = {**PROPOSAL_CREATE, "requiredTools": ["agent__spawn", "agent.spawn"]}
    fake = _provider(GATE_CREATE, proposal)
    learner, manager, _ = _learner(tmp_path, fake)
    outcome = await learner.run(api_key=TEST_API_KEY, request=_request(tools=["agent__spawn"]), active_ids=[])
    assert outcome.changed is True
    assert manager.catalog.get("daintree.test.created").meta.required_tools == ["agent.spawn"]


@pytest.mark.asyncio
async def test_no_tools_reported_narrows_to_the_catalog_rather_than_admitting_anything(
    tmp_path: Path,
) -> None:
    # An empty tool list is NOT permission to admit any name. A builder that saw no
    # tool inventory has no evidence for anything outside the catalog, so an invented
    # name is stripped — while a name the catalog already uses still survives.
    proposal = {
        **PROPOSAL_CREATE,
        "requiredTools": ["something.unheard-of", "context.snapshot"],
    }
    # Put a real tool in the catalog so the surviving half of the contract is testable:
    # the allow-set is the catalog's declared tools, not "nothing".
    target_with_tools = _TARGET.replace(
        "whenToUse: Use when improving the target.\n",
        "whenToUse: Use when improving the target.\nrequiredTools:\n  - context.snapshot\n",
    )
    fake = _provider(GATE_CREATE, proposal)
    learner, manager, _ = _learner(
        tmp_path, fake, with_target=True, target_text=target_with_tools
    )
    outcome = await learner.run(api_key=TEST_API_KEY, request=_request(), active_ids=[])
    assert outcome.changed is True
    kept = manager.catalog.get("daintree.test.created").meta.required_tools
    assert "something.unheard-of" not in kept
    assert kept == ["context.snapshot"]


# ---- gate verdict shape ------------------------------------------------------


def test_lesson_kind_parses_and_degrades_to_none() -> None:
    from daintree_assistant_server.contracts.skill_learning import GateVerdict

    assert GateVerdict.model_validate({"lesson_kind": "trigger"}).lesson_kind == "trigger"
    assert GateVerdict.model_validate({"lesson_kind": "Procedure"}).lesson_kind == "procedure"
    # An off-enum value must never cost the whole verdict — it is an ADVISORY field.
    assert GateVerdict.model_validate({"lesson_kind": "both"}).lesson_kind is None
    assert GateVerdict.model_validate({"lesson_kind": None}).lesson_kind is None
    assert GateVerdict.model_validate({}).lesson_kind is None


@pytest.mark.asyncio
async def test_lesson_kind_reaches_the_improver(tmp_path: Path) -> None:
    gate = {**GATE_IMPROVE, "lesson_kind": "trigger"}
    fake = _provider(gate, PROPOSAL_IMPROVE)
    learner, _, _ = _learner(tmp_path, fake, with_target=True)
    await learner.run(api_key=TEST_API_KEY, request=_request(), active_ids=["daintree.test.target"])
    improve_calls = [r for r in fake.requests if "skill improver" in r.messages[0]["content"]]
    assert len(improve_calls) == 1
    assert "Gate lesson kind: trigger" in improve_calls[0].messages[1]["content"]


# ---- product-scope fence -----------------------------------------------------


def test_learning_prompts_carry_the_orchestration_scope_fence() -> None:
    # The only generality constraint the prompts used to carry was PORTABILITY, never
    # SUBJECT MATTER — so a perfectly portable "how to resolve a rebase conflict"
    # runbook passed every check. All three prompts must fence on Daintree's job.
    loader = _loader()
    for name in ("gate", "build", "improve"):
        text = loader.skill_learning_prompt(name).lower()
        assert "spawn" in text and "supervis" in text, name
        assert "who does the work" in text, name
