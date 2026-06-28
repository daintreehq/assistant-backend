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
)
from daintree_assistant_server.prompts.loader import PromptLoader
from daintree_assistant_server.services.prompt_assembler import PromptAssembler
from daintree_assistant_server.skills.catalog import load_catalog
from daintree_assistant_server.skills.load_events import SkillLoadExecution

SNAPSHOTS = Path(__file__).parent / "snapshots"


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
    catalog = load_catalog()
    skill = catalog.get("daintree.terminals.rename")
    assert_golden("loaded_skills_rename.md", assembler.render_loaded_skills([skill]))


def test_turn_footer(assembler: PromptAssembler) -> None:
    turn = TurnContext(
        goal="Prepare the branch for review",
        workflow_runs=["wfr_1 — preparing branch"],
        memories=Memories(pinned=["uses pnpm"], relevant=["last run failed on lint"]),
        session_ended_watchers=["watcher-1"],
    )
    assert_golden("turn_footer.md", assembler.render_turn_footer(turn, active_worktree="feature/x"))


def test_selector_system(loader: PromptLoader) -> None:
    catalog = load_catalog()
    rendered = loader.render(
        "selector/system.md.j2",
        catalog=catalog.selector_metadata(["daintree.terminals.rename"]),
        max_active=3,
    )
    assert_golden("selector_system.md", rendered)


@pytest.mark.parametrize(
    "task_id",
    [
        "checkpoint.v1",
        "memory_distill.v1",
        "watcher_classify.v1",
        "terminal_judge.v1",
        "terminal_summarize.v1",
        "terminal_extract_text.v1",
        "terminal_extract_json.v1",
        "extraction_verdict.v1",
        "skill_step_consistency.v1",
    ],
)
def test_task_prompts(loader: PromptLoader, task_id: str) -> None:
    assert_golden(f"task_{task_id}.md", loader.task_system_prompt(task_id))


def test_full_upstream_messages(assembler: PromptAssembler) -> None:
    catalog = load_catalog()
    skill = catalog.get("daintree.workflow.prep-branch-for-review")
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
    catalog = load_catalog()
    skill = catalog.get("daintree.workflow.prep-branch-for-review")
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
    catalog = load_catalog()
    skill = catalog.get("daintree.workflow.prep-branch-for-review")
    ex = SkillLoadExecution.from_skill("call_srv_GOLDEN", skill)
    prelude = ex.prelude_execution().model_dump()
    snapshot = json.dumps(prelude, indent=2, ensure_ascii=False)
    assert_golden("prelude_execution.json", snapshot)
