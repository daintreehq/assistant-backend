"""Golden tests for server-owned prompt text.

Prompt bytes are behavior. These tests snapshot rendered prompts and the
assembled upstream message array; a change requires an intentional update
(run with ``DAINTREE_UPDATE_GOLDEN=1`` to regenerate snapshots).
"""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from daintree_assistant_server.config import Settings
from daintree_assistant_server.contracts.daintree_api import RespondRequest
from daintree_assistant_server.contracts.extensions import (
    McpInfo,
    McpServer,
    Memories,
    OpenTerminal,
    RuntimeContext,
    TurnContext,
    WorkflowDigest,
)
from daintree_assistant_server.prompts.loader import PromptLoader
from daintree_assistant_server.services.prompt_assembler import PromptAssembler
from daintree_assistant_server.skills.catalog import Catalog, load_catalog
from daintree_assistant_server.skills.load_events import SkillLoadExecution

SNAPSHOTS = Path(__file__).parent / "snapshots"

# The catalog-dependent snapshots render against a PINNED fixture catalog, not the
# live one, so the self-evolving skill catalog (the local skill-learning harness
# writing/improving real skill files) never reddens these goldens. What they still
# protect is the prompt/assembler behavior: template wording, field layout, the
# active marker, and message ordering. Real-catalog coverage lives in behavioral
# tests plus the structural smoke test below.
FIXTURE_SKILLS = Path(__file__).parent / "fixtures" / "skills"


def _fixture_catalog() -> Catalog:
    return load_catalog(FIXTURE_SKILLS)


def _settings() -> Settings:
    return Settings(DAINTREE_API_KEY="k", DEEPSEEK_API_KEY="d", STATE_SIGNING_KEY_CURRENT="s")


def assert_golden(name: str, content: str) -> None:
    SNAPSHOTS.mkdir(exist_ok=True)
    path = SNAPSHOTS / name
    if os.getenv("DAINTREE_UPDATE_GOLDEN") or not path.exists():
        path.write_text(content, encoding="utf-8")
        return
    expected = path.read_text(encoding="utf-8")
    assert content == expected, f"Golden mismatch for {name}; run with DAINTREE_UPDATE_GOLDEN=1 to update."


@pytest.fixture
def assembler() -> PromptAssembler:
    loader = PromptLoader()
    loader.validate()
    return PromptAssembler(loader, _settings())


@pytest.fixture
def loader() -> PromptLoader:
    loader = PromptLoader()
    loader.validate()
    return loader


def test_base_prompt(loader: PromptLoader) -> None:
    assert_golden("base_prompt.md", loader.base_prompt())


def test_runtime_context(assembler: PromptAssembler) -> None:
    rc = RuntimeContext(
        permission_tier="operator",
        project_path="/home/user/project",
        project_id="project_123",
        mcp=McpInfo(connected=True, transport="stdio", tool_count=42),
        configured_agent_ids=["claude", "codex"],
        scheduler_active=True,
        project_instructions="Always run the linter.",
        open_terminals=[
            OpenTerminal(
                id="terminal-5284bfef-3d11-424c-90cb-136f24046295",
                kind="agent",
                worktree_id="wt-feature-login",
                title="Implement login form",
                agent_id="claude",
                agent_state="working",
            ),
            OpenTerminal(
                id="terminal-9a1c0b77-2f4e-4d8a-8c1b-7e0f5a2d3c64",
                kind="shell",
                exit_code=0,
            ),
        ],
    )
    assert_golden("runtime_context.md", assembler.render_runtime_context(rc))


def test_integrations(assembler: PromptAssembler) -> None:
    rc = RuntimeContext(
        mcp_servers=[
            McpServer(
                name="daintree",
                transport="http",
                status="connected",
                tool_count=12,
                description="Daintree orchestration surface.",
                instructions="Use terminal.list before sendCommand.\nNever invent state.",
            ),
            McpServer(name="filesystem", status="connected", tool_count=5),
        ],
    )
    tools = RespondRequest.model_validate(
        {
            "protocol_version": 1,
            "session": {"id": "s", "turn_id": "t"},
            "input": {
                "messages": [{"role": "user", "content": "hi"}],
                "tools": [
                    {"type": "function", "function": {"name": "git__status", "description": "Show git status."}},
                    {"type": "function", "function": {"name": "fs__read", "description": "Read a file."}},
                ],
            },
        }
    ).input.tools
    assert_golden("integrations.md", assembler.render_integrations(rc, tools))


def test_integrations_empty_when_nothing_supplied(assembler: PromptAssembler) -> None:
    assert assembler.render_integrations(RuntimeContext(), None) == ""


def test_loaded_skills_empty(assembler: PromptAssembler) -> None:
    assert_golden("loaded_skills_empty.md", assembler.render_loaded_skills([]))


def test_loaded_skills_one(assembler: PromptAssembler) -> None:
    skill = _fixture_catalog().get("daintree.example.alpha")
    assert_golden("loaded_skills_alpha.md", assembler.render_loaded_skills([skill]))


def test_turn_footer(assembler: PromptAssembler) -> None:
    turn = TurnContext(
        goal="Prepare the branch for review",
        workflow_runs=["wfr_1 — preparing branch"],
        memories=Memories(pinned=["uses pnpm"], relevant=["last run failed on lint"]),
        resumed_watchers=["watcher-1"],
    )
    assert_golden("turn_footer.md", assembler.render_turn_footer(turn, active_worktree="feature/x"))


def test_turn_footer_workflow_state(assembler: PromptAssembler) -> None:
    turn = TurnContext(
        goal="Fix the watcher tests",
        workflow_state=[
            WorkflowDigest(
                id="wf_123",
                goal="Fix watcher tests and verify branch",
                status="active",
                progress="2/5 nodes done; current: verify tests",
                active_nodes=["n_verify: Run watcher tests (ready)"],
                resources=["terminal term_8: Claude repair watcher tests", "async asy_77 done"],
                next_action="Run tests in the repair worktree",
                last_event="Agent reported fixes complete",
            ),
            WorkflowDigest(
                id="wf_124",
                goal="Prepare PR for issue 286",
                status="blocked",
                blockers=["waiting on user: choose branch strategy"],
            ),
        ],
    )
    assert_golden("turn_footer_workflow_state.md", assembler.render_turn_footer(turn))


def test_response_contract(assembler: PromptAssembler) -> None:
    assert_golden("response_contract.md", assembler.render_response_contract())


def test_response_contract_is_final_system_message(assembler: PromptAssembler) -> None:
    # The output-shape contract must land LAST (nearest generation). Assemble a full
    # request and assert the trailing message is the contract, carrying the width.
    req = RespondRequest.model_validate(
        {
            "protocol_version": 1,
            "session": {"id": "s", "turn_id": "t"},
            "input": {"messages": [{"role": "user", "content": "hi"}]},
            "runtime": {"permission_tier": "operator", "project_path": "/p"},
        }
    )
    pr = assembler.build_main_request(request=req, skills=[], upstream_model="m")
    last = pr.messages[-1]
    assert last["role"] == "system"
    assert last["content"].startswith("# Response contract")
    assert str(assembler.settings.main_terminal_width) in last["content"]


def test_selector_system(loader: PromptLoader) -> None:
    catalog = _fixture_catalog()
    rendered = loader.render(
        "selector/system.md.j2",
        catalog=catalog.selector_metadata(["daintree.example.alpha"]),
        max_active=3,
    )
    assert_golden("selector_system.md", rendered)


def test_selector_prompt_renders_real_catalog(loader: PromptLoader) -> None:
    # Not a byte snapshot (the live catalog evolves) — a structural guard that the
    # selector template renders the REAL catalog without error and lists every
    # skill id. This keeps real-catalog coverage after the goldens were pinned to
    # a fixture catalog above.
    catalog = load_catalog()
    assert catalog.skills, "real catalog is empty"
    rendered = loader.render(
        "selector/system.md.j2",
        catalog=catalog.selector_metadata([]),
        max_active=3,
    )
    for skill in catalog.skills:
        # exact list-item line, plus the trigger text — catches the template
        # dropping a field, not just an id substring coincidence.
        assert f"- id: {skill.id}" in rendered
        assert skill.when_to_use in rendered
        # foundation skills carry the marker the pairing rule keys off of.
        if skill.meta.foundation:
            assert f"- id: {skill.id} [foundation]" in rendered


@pytest.mark.parametrize(
    "task_id",
    [
        "checkpoint",
        "memory_distill",
        "watcher_classify",
        "terminal_judge",
        "terminal_summarize",
        "terminal_extract_text",
        "terminal_extract_json",
        "extraction_verdict",
        "skill_step_consistency",
        "workflow_plan",
        "workflow_reconcile",
        "workflow_resume_digest",
    ],
)
def test_task_prompts(loader: PromptLoader, task_id: str) -> None:
    assert_golden(f"task_{task_id}.md", loader.task_system_prompt(task_id))


@pytest.mark.parametrize("name", ["gate", "build", "improve"])
def test_skill_learning_prompts(loader: PromptLoader, name: str) -> None:
    assert_golden(f"skill_learning_{name}.md", loader.skill_learning_prompt(name))


def test_full_upstream_messages(assembler: PromptAssembler) -> None:
    skill = _fixture_catalog().get("daintree.example.alpha")
    req = RespondRequest.model_validate(
        {
            "protocol_version": 1,
            "session": {"id": "s", "turn_id": "t"},
            "input": {"messages": [{"role": "user", "content": "Prepare this branch for review."}]},
            "runtime": {"permission_tier": "operator", "project_path": "/p"},
            "turn": {"goal": "Prepare this branch for review."},
        }
    )
    pr = assembler.build_main_request(
        request=req,
        skills=[skill],
        upstream_model="deepseek-v4-flash",
    )
    snapshot = json.dumps(pr.messages, indent=2, ensure_ascii=False)
    assert_golden("upstream_messages.json", snapshot)


def test_full_upstream_messages_with_open_terminals(assembler: PromptAssembler) -> None:
    # The open-terminal inventory must reach the assembled upstream request in the
    # volatile per-request tail (runtime context), not just render in isolation.
    skill = _fixture_catalog().get("daintree.example.alpha")
    req = RespondRequest.model_validate(
        {
            "protocol_version": 1,
            "session": {"id": "s", "turn_id": "t"},
            "input": {"messages": [{"role": "user", "content": "Prepare this branch for review."}]},
            "runtime": {
                "permission_tier": "operator",
                "project_path": "/p",
                "open_terminals": [
                    {
                        "id": "terminal-5284bfef-3d11-424c-90cb-136f24046295",
                        "kind": "agent",
                        "worktree_id": "wt-feature-login",
                        "title": "Implement login form",
                        "agent_id": "claude",
                        "agent_state": "working",
                    },
                    {"id": "terminal-9a1c0b77-2f4e-4d8a-8c1b-7e0f5a2d3c64", "kind": "shell", "exit_code": 0},
                ],
            },
            "turn": {"goal": "Prepare this branch for review."},
        }
    )
    pr = assembler.build_main_request(
        request=req,
        skills=[skill],
        upstream_model="deepseek-v4-flash",
    )
    snapshot = json.dumps(pr.messages, indent=2, ensure_ascii=False)
    assert_golden("upstream_messages_with_terminals.json", snapshot)


def test_synthetic_execution_shape() -> None:
    skill = _fixture_catalog().get("daintree.example.alpha")
    ex = SkillLoadExecution.from_skill("call_srv_GOLDEN", skill)
    prelude = ex.prelude_execution().model_dump()
    snapshot = json.dumps(prelude, indent=2, ensure_ascii=False)
    assert_golden("prelude_execution.json", snapshot)
