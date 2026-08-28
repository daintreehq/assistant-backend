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
from daintree_assistant_server.runbooks.catalog import Catalog, load_catalog
from daintree_assistant_server.runbooks.load_events import RunbookLoadExecution
from daintree_assistant_server.services.docs_mcp import DocsLookup, DocsSection
from daintree_assistant_server.services.fast_response import FastResponseService
from daintree_assistant_server.services.prompt_assembler import (
    PromptAssembler,
)
from daintree_assistant_server.services.task_runner import build_profiles

SNAPSHOTS = Path(__file__).parent / "snapshots"

# The catalog-dependent snapshots render against a PINNED fixture catalog, not the
# live one, so the self-evolving runbook catalog (the local runbook-learning harness
# writing/improving real runbook files) never reddens these goldens. What they still
# protect is the prompt/assembler behavior: template wording, field layout, the
# active marker, and message ordering. Real-catalog coverage lives in behavioral
# tests plus the structural smoke test below.
FIXTURE_RUNBOOKS = Path(__file__).parent / "fixtures" / "runbooks"


def _fixture_catalog() -> Catalog:
    return load_catalog(FIXTURE_RUNBOOKS)


def _settings() -> Settings:
    return Settings(BASETEN_API_KEY="tgp_v1_test", STATE_SIGNING_KEY_CURRENT="s")


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


def test_loaded_runbooks_empty(assembler: PromptAssembler) -> None:
    assert_golden("loaded_runbooks_empty.md", assembler.render_loaded_runbooks([]))


def test_loaded_runbooks_one(assembler: PromptAssembler) -> None:
    runbook = _fixture_catalog().get("daintree.example.alpha")
    assert_golden("loaded_runbooks_alpha.md", assembler.render_loaded_runbooks([runbook]))


def test_loaded_runbooks_foundation_and_workflow(assembler: PromptAssembler) -> None:
    """Two runbooks, which is the ONLY rendering that emits the layering paragraph.

    That paragraph — how a domain foundation and a workflow runbook combine, and
    the rule against interleaving two procedures — is guarded by `{% if
    loaded_runbooks | length > 1 %}` and so appears in neither the empty nor the
    one-runbook snapshot. It was edited (the instruction-precedence clause moved to
    `main/base/30-runbooks.md`) with no golden covering a single byte of it, and
    the only other test touching it asserts the literal string "Layering:" is
    present. The fixture catalog already carries the right pair: alpha is
    `foundation: true`, beta is an ordinary workflow runbook.
    """
    catalog = _fixture_catalog()
    pair = [catalog.get("daintree.example.alpha"), catalog.get("daintree.example.beta")]
    assert_golden("loaded_runbooks_pair.md", assembler.render_loaded_runbooks(pair))


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
            "protocol_version": 3,
            "session": {"id": "s", "turn_id": "t"},
            "input": {"messages": [{"role": "user", "content": "hi"}]},
            "startup": _startup_payload(),
            "runtime": {
                "permission_tier": "operator",
                "mcp_servers": [{"name": "daintree"}],
            },
        }
    )
    # Runbooks AND integrations are supplied deliberately: with both absent the
    # count-of-two assertion below holds under the OLD layout too, and would prove
    # nothing. These are exactly the two blocks issue #39 moved out of the system
    # region, so the request has to contain them for the check to bite.
    pr = assembler.build_main_request(
        request=req, runbooks=[catalog.runbooks[0]], upstream_model="m"
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


# Driven off the REAL profile registry, not a hardcoded list: a task added without
# a snapshot (or with a missing/misnamed prompt file) previously passed the whole
# suite and only failed the first time a client called it.
@pytest.mark.parametrize("task_id", sorted(build_profiles()))
def test_task_prompts(loader: PromptLoader, task_id: str) -> None:
    assert_golden(f"task_{task_id}.md", loader.task_system_prompt(task_id))


def test_full_upstream_messages(assembler: PromptAssembler) -> None:
    runbook = _fixture_catalog().get("daintree.example.alpha")
    req = RespondRequest.model_validate(
        {
            "protocol_version": 3,
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
        runbooks=[runbook],
        upstream_model="deepseek-v4-flash",
    )
    snapshot = json.dumps(pr.messages, indent=2, ensure_ascii=False)
    assert_golden("upstream_messages.json", snapshot)


def test_fast_response_messages(assembler: PromptAssembler) -> None:
    """Everything the fast model is given, as one reviewable array.

    The system prompt, the runbook bodies, and up to three recent USER messages —
    nothing else. Worth a snapshot precisely because the value of this stage is a
    judgement call the prompt has to carry on its own (two or three sentences, or
    silence), and because the thing most likely to go wrong is context creeping
    back in. The assistant's turns, the tool inventory and the startup block were
    all in here once; a diff that reintroduces any of them is visible as an extra
    row.
    """
    del assembler  # the fast stage builds its own request, from the same loader
    catalog = _fixture_catalog()
    req = RespondRequest.model_validate(
        {
            "protocol_version": 3,
            "session": {"id": "s", "turn_id": "t"},
            "generation": {"stream": True},
            "input": {
                "messages": [
                    {"role": "user", "content": "Take a look at the release branch."},
                    {"role": "assistant", "content": "An assistant turn, never sent."},
                    {"role": "user", "content": "Prepare this branch for review."},
                ]
            },
            "startup": _startup_payload(),
            "turn": {"goal": "Prepare this branch for review."},
        }
    )
    service = FastResponseService(
        settings=_settings(),
        prompt_loader=PromptLoader(),
        provider=None,
    )
    messages = service.build_messages(
        req, active_runbooks=[catalog.get("daintree.example.alpha")]
    )
    assert messages is not None
    snapshot = json.dumps(messages, indent=2, ensure_ascii=False)
    assert_golden("fast_response_messages.json", snapshot)


def test_full_upstream_messages_subagent(assembler: PromptAssembler) -> None:
    """The sub-agent profile's assembled array — the layout, not just its pieces.

    This is the assertion the unit tests cannot make: that the ONLY system row is
    the persona (anything else would hoist above the tool schemas and bust their
    cache), that the runbook body and startup rows sit below it in that order, and
    that the orchestrator's integrations, runtime, turn-footer and response-contract
    rows are absent rather than merely untested. Same request shape as the
    assistant snapshot above, so the two diff against each other by eye.
    """
    runbook = _fixture_catalog().get("daintree.example.alpha")
    req = RespondRequest.model_validate(
        {
            "protocol_version": 3,
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
        runbooks=[runbook],
        upstream_model="deepseek-v4-flash",
    )
    snapshot = json.dumps(pr.messages, indent=2, ensure_ascii=False)
    assert_golden("upstream_messages_subagent.json", snapshot)


def test_full_upstream_messages_with_open_terminals(assembler: PromptAssembler) -> None:
    # The open-terminal inventory must reach the assembled upstream request in the
    # volatile per-request tail (runtime context), not just render in isolation.
    runbook = _fixture_catalog().get("daintree.example.alpha")
    req = RespondRequest.model_validate(
        {
            "protocol_version": 3,
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
        runbooks=[runbook],
        upstream_model="deepseek-v4-flash",
    )
    snapshot = json.dumps(pr.messages, indent=2, ensure_ascii=False)
    assert_golden("upstream_messages_with_terminals.json", snapshot)


def test_synthetic_execution_shape() -> None:
    runbook = _fixture_catalog().get("daintree.example.alpha")
    ex = RunbookLoadExecution.from_runbook("call_srv_GOLDEN", runbook)
    prelude = ex.prelude_execution().model_dump()
    snapshot = json.dumps(prelude, indent=2, ensure_ascii=False)
    assert_golden("prelude_execution.json", snapshot)
