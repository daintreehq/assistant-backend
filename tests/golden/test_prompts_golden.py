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
    AgentRosterSnapshot,
    AgentSnapshot,
    CurrentWorktreeSnapshot,
    McpInfo,
    McpServer,
    Memories,
    OpenTerminal,
    ProjectSnapshot,
    RuntimeContext,
    StartupContext,
    TurnContext,
    WorkflowDigest,
    WorktreeSnapshot,
)
from daintree_assistant_server.prompts.loader import PromptLoader
from daintree_assistant_server.services.docs_mcp import DocsLookup, DocsSection
from daintree_assistant_server.services.prompt_assembler import PromptAssembler
from daintree_assistant_server.services.task_runner import build_profiles
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
    return Settings(OPENROUTER_API_KEY="d", STATE_SIGNING_KEY_CURRENT="s")


def _startup_payload() -> dict[str, object]:
    return {
        "project": {
            "id": "project_123",
            "name": "Daintree Example",
            "path": "/home/user/project",
            "status": "active",
            "daintree_config_present": True,
            "in_repo_settings": True,
        },
        "agent_roster": {
            "agents": [
                {
                    "id": "claude",
                    "display_name": "Claude",
                    "source": "built-in",
                    "availability": "ready",
                    "installed": True,
                    "launchable": True,
                    "pinned": True,
                    "toolbar_visible": True,
                },
                {
                    "id": "plugin:example/custom-agent",
                    "display_name": "Custom Agent",
                    "source": "plugin",
                    "availability": "unauthenticated",
                    "installed": True,
                    "launchable": True,
                },
            ],
            "complete": True,
            "availability_complete": True,
            "total_count": 2,
        },
        "project_instructions": "Always run the linter.",
    }


def assert_golden(name: str, content: str) -> None:
    SNAPSHOTS.mkdir(exist_ok=True)
    path = SNAPSHOTS / name
    if os.getenv("DAINTREE_UPDATE_GOLDEN"):
        path.write_text(content, encoding="utf-8")
        return
    # A MISSING snapshot must fail, not self-create. Auto-writing it meant a newly
    # registered task (or any new golden) silently minted its own baseline and went
    # green on the very first run — the snapshot diff is supposed to BE the
    # human-reviewable record, and CI does not check for a dirty worktree.
    assert path.exists(), (
        f"No golden snapshot for {name}. Review the rendered output, then create it "
        "with DAINTREE_UPDATE_GOLDEN=1 pytest tests/golden/"
    )
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


def test_subagent_prompt(loader: PromptLoader) -> None:
    """The second persona gets the same reviewable record as the first.

    It is a whole system prompt that ships on every delegated turn, assembled
    from ordered partials exactly like the base one — so a reordered or dropped
    partial has to show up as a snapshot diff here, not as behaviour someone
    notices later in a run.
    """
    assert_golden("subagent_prompt.md", loader.subagent_prompt())


def test_runtime_context(assembler: PromptAssembler) -> None:
    rc = RuntimeContext(
        permission_tier="operator",
        mcp=McpInfo(connected=True, transport="stdio", tool_count=42),
        scheduler_active=True,
        worktree=CurrentWorktreeSnapshot(
            current=WorktreeSnapshot(
                id="wt-feature-login",
                path="/home/user/project/.worktrees/feature-login",
                branch="feature/login",
                issue_number=12,
                issue_title="Add login form",
                pr_number=34,
                pr_title="Implement login form",
                pr_url="https://example.test/pull/34",
                status="dirty",
                last_commit="abc1234 Add login form",
            )
        ),
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


def test_startup_context(assembler: PromptAssembler) -> None:
    startup = StartupContext(
        project=ProjectSnapshot(
            id="project_123",
            name="Daintree Example",
            path="/home/user/project",
            status="active",
            daintree_config_present=True,
            in_repo_settings=True,
        ),
        agent_roster=AgentRosterSnapshot(
            agents=[
                AgentSnapshot(
                    id="claude",
                    display_name="Claude",
                    source="built-in",
                    availability="ready",
                    installed=True,
                    launchable=True,
                    pinned=True,
                    toolbar_visible=True,
                ),
                AgentSnapshot(
                    id="plugin:example/custom-agent",
                    display_name="Custom Agent",
                    source="plugin",
                    availability="unauthenticated",
                    installed=True,
                    launchable=True,
                ),
            ],
            complete=True,
            availability_complete=True,
            total_count=2,
        ),
        project_instructions="Always run the linter.",
    )
    assert_golden("startup_context.md", assembler.render_startup_context(startup))


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
    assert_golden("integrations.md", assembler.render_integrations(rc))


def test_integrations_empty_when_nothing_supplied(assembler: PromptAssembler) -> None:
    assert assembler.render_integrations(RuntimeContext()) == ""


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
    assert_golden("turn_footer.md", assembler.render_turn_footer(turn))


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


def test_display_context(assembler: PromptAssembler) -> None:
    """The volatile geometry block the contract's rules defer to (issue #38).

    Snapshotted at a WIDE measured width: that exercises both derived values
    (the narrow/wide verdict and the 5-column cap) away from the server default,
    so a drift in either shows up in the diff.
    """
    runtime = RuntimeContext.model_validate({"display": {"columns": 180, "content_width": 100}})
    assert_golden("display_context.md", assembler.render_display_context(runtime))


def test_docs_context(assembler: PromptAssembler) -> None:
    """What the model sees when the server answered a usage question for it.

    Snapshotted like every other prompt layer: this text tells the model to
    prefer these pages over its own memory and how to turn a path into a
    citation, so a change to it is a change to what gets cited.
    """
    lookup = DocsLookup(
        query="create worktree from pull request",
        sections=[
            DocsSection(
                path="/docs/worktrees",
                title="Worktrees",
                section_title="PR Checkout",
                text="You can create a worktree straight from a pull request with the "
                "**Create Worktree** button in the Code Forge panel's PR list.",
            ),
            DocsSection(
                path="/docs/code-forge",
                title="Code Forge",
                section_title="Pull requests",
                text="The PR list shows open pull requests for the current repository.",
            ),
        ],
    )
    assert_golden("docs_context.md", assembler.render_docs_context(lookup))


def test_docs_context_is_empty_without_a_lookup(assembler: PromptAssembler) -> None:
    """No lookup and no matches render nothing at all — not an empty block.

    An "I searched and found nothing" preamble would spend tokens on every
    non-docs turn and invite the model to remark on a search the user never
    asked for.
    """
    assert assembler.render_docs_context(None) == ""
    assert assembler.render_docs_context(DocsLookup(query="x", sections=[])) == ""


def test_response_contract_is_final_system_message(
    assembler: PromptAssembler, catalog: Catalog
) -> None:
    # The output-shape contract is the FINAL system message — so the tool schemas
    # serialize directly behind it (system content anywhere else in the array is
    # hoisted into DeepSeek's system region and busts the tools+conversation cache).
    req = RespondRequest.model_validate(
        {
            "protocol_version": 2,
            "session": {"id": "s", "turn_id": "t"},
            "input": {"messages": [{"role": "user", "content": "hi"}]},
            "startup": _startup_payload(),
            "runtime": {
                "permission_tier": "operator",
                "mcp_servers": [{"name": "daintree"}],
            },
        }
    )
    # Skills AND integrations are supplied deliberately: with both absent the
    # count-of-two assertion below holds under the OLD layout too, and would prove
    # nothing. These are exactly the two blocks issue #39 moved out of the system
    # region, so the request has to contain them for the check to bite.
    pr = assembler.build_main_request(
        request=req, skills=[catalog.skills[0]], upstream_model="m"
    )
    system_msgs = [m for m in pr.messages if m["role"] == "system"]
    last_system = system_msgs[-1]
    assert last_system["content"].startswith("# Response contract")
    # Exactly two: base + contract. Anything else in the system region would sit
    # ahead of the schemas — "last system message" alone would not catch that.
    assert len(system_msgs) == 2
    # The contract's rules defer to the tail rather than quoting a number, so the
    # width appears ONLY in the volatile tail (issue #38). A number here would sit
    # ahead of the tool schemas and re-encode them on every resize.
    width = str(assembler.settings.main_terminal_width)
    assert "Display context" in last_system["content"]
    assert width not in last_system["content"]
    assert width in pr.messages[-1]["content"]
    # And no system message may follow the conversation.
    convo_idx = next(i for i, m in enumerate(pr.messages) if m["content"] == "hi")
    assert all(m["role"] != "system" for m in pr.messages[convo_idx:])


def test_selector_system(loader: PromptLoader) -> None:
    catalog = _fixture_catalog()
    rendered = loader.render(
        "selector/system.md.j2",
        catalog=catalog.selector_metadata(),
        active_ids=["daintree.example.alpha"],
        max_active=3,
    )
    assert_golden("selector_system.md", rendered)


def test_selector_system_cold(loader: PromptLoader) -> None:
    """The no-active-skills render — the branch every opening message takes.

    The reason budget is still conditional on ``active_ids``: with nothing loaded there
    is no keep-vs-evict judgement to work through, so the prompt asks for a terser
    reason and the selector emits fewer tokens on the path that gates a cold turn's
    first byte. What changed is WHERE that condition renders — it trails the catalog
    now instead of leading it, so both cohorts share one cacheable prefix (issue #43).
    The warm snapshot above renders the 300-character branch and would stay green
    through any change to the cold one, so this is the only byte-level coverage the
    cold branch's wording has. Its POSITION is guarded separately, on bytes, by
    ``tests/unit/test_prompt_cache_stability.py``.
    """
    catalog = _fixture_catalog()
    rendered = loader.render(
        "selector/system.md.j2",
        catalog=catalog.selector_metadata(),
        active_ids=[],
        max_active=3,
    )
    assert_golden("selector_system_cold.md", rendered)


def test_selector_prompt_renders_real_catalog(loader: PromptLoader) -> None:
    # Not a byte snapshot (the live catalog evolves) — a structural guard that the
    # selector template renders the REAL catalog without error and lists every
    # skill id. This keeps real-catalog coverage after the goldens were pinned to
    # a fixture catalog above.
    catalog = load_catalog()
    assert catalog.skills, "real catalog is empty"
    metadata = catalog.selector_metadata()
    rendered = loader.render(
        "selector/system.md.j2",
        catalog=metadata,
        active_ids=[],
        max_active=3,
    )
    # Every SELECTABLE skill. `selector_metadata` omits the server-pinned
    # documentation runbook (config.DOCS_SKILL_ID) — it is loaded off the turn's
    # docs_query rather than chosen, so its absence from the classifier's menu is
    # the intent, not a rendering bug.
    selectable = {m["id"] for m in metadata}
    assert selectable, "the selector sees no skills at all"
    for skill in [s for s in catalog.skills if s.id in selectable]:
        # exact list-item line, plus the trigger text — catches the template
        # dropping a field, not just an id substring coincidence.
        assert f"- id: {skill.id}" in rendered
        assert skill.when_to_use in rendered
        # foundation skills carry the marker the pairing rule keys off of.
        if skill.meta.foundation:
            assert f"- id: {skill.id} [foundation]" in rendered


# Driven off the REAL profile registry, not a hardcoded list: a task added without
# a snapshot (or with a missing/misnamed prompt file) previously passed the whole
# suite and only failed the first time a client called it.
@pytest.mark.parametrize("task_id", sorted(build_profiles()))
def test_task_prompts(loader: PromptLoader, task_id: str) -> None:
    assert_golden(f"task_{task_id}.md", loader.task_system_prompt(task_id))


@pytest.mark.parametrize("name", ["gate", "build", "improve"])
def test_skill_learning_prompts(loader: PromptLoader, name: str) -> None:
    assert_golden(f"skill_learning_{name}.md", loader.skill_learning_prompt(name))


def test_full_upstream_messages(assembler: PromptAssembler) -> None:
    skill = _fixture_catalog().get("daintree.example.alpha")
    req = RespondRequest.model_validate(
        {
            "protocol_version": 2,
            "session": {"id": "s", "turn_id": "t"},
            "input": {"messages": [{"role": "user", "content": "Prepare this branch for review."}]},
            "startup": _startup_payload(),
            "runtime": {
                "permission_tier": "operator",
                "worktree": {
                    "current": {
                        "id": "wt-review",
                        "path": "/home/user/project/.worktrees/review",
                        "branch": "feature/review",
                        "is_main": False,
                        "status": "clean",
                        "last_commit": "def5678 Prepare branch",
                    }
                },
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
    assert_golden("upstream_messages.json", snapshot)


def test_full_upstream_messages_subagent(assembler: PromptAssembler) -> None:
    """The sub-agent profile's assembled array — the layout, not just its pieces.

    This is the assertion the unit tests cannot make: that the ONLY system row is
    the persona (anything else would hoist above the tool schemas and bust their
    cache), that the skill body and startup rows sit below it in that order, and
    that the orchestrator's integrations, runtime, turn-footer and response-contract
    rows are absent rather than merely untested. Same request shape as the
    assistant snapshot above, so the two diff against each other by eye.
    """
    skill = _fixture_catalog().get("daintree.example.alpha")
    req = RespondRequest.model_validate(
        {
            "protocol_version": 2,
            "profile": "subagent",
            "session": {"id": "s", "turn_id": "t"},
            "input": {
                "messages": [
                    {"role": "user", "content": "Find where the retry budget is enforced."}
                ]
            },
            "startup": _startup_payload(),
            "runtime": {
                "permission_tier": "operator",
                "worktree": {
                    "current": {
                        "id": "wt-review",
                        "path": "/home/user/project/.worktrees/review",
                        "branch": "feature/review",
                        "is_main": False,
                        "status": "clean",
                        "last_commit": "def5678 Prepare branch",
                    }
                },
            },
            "turn": {"goal": "Find where the retry budget is enforced."},
        }
    )
    pr = assembler.build_main_request(
        request=req,
        skills=[skill],
        upstream_model="deepseek-v4-flash",
    )
    snapshot = json.dumps(pr.messages, indent=2, ensure_ascii=False)
    assert_golden("upstream_messages_subagent.json", snapshot)


def test_full_upstream_messages_with_open_terminals(assembler: PromptAssembler) -> None:
    # The open-terminal inventory must reach the assembled upstream request in the
    # volatile per-request tail (runtime context), not just render in isolation.
    skill = _fixture_catalog().get("daintree.example.alpha")
    req = RespondRequest.model_validate(
        {
            "protocol_version": 2,
            "session": {"id": "s", "turn_id": "t"},
            "input": {"messages": [{"role": "user", "content": "Prepare this branch for review."}]},
            "startup": _startup_payload(),
            "runtime": {
                "permission_tier": "operator",
                "worktree": {
                    "current": {
                        "id": "wt-feature-login",
                        "path": "/home/user/project/.worktrees/feature-login",
                        "branch": "feature/login",
                        "issue_number": 12,
                        "issue_title": "Add login form",
                        "status": "dirty",
                        "last_commit": "abc1234 Add login form",
                    }
                },
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
