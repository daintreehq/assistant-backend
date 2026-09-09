"""Discovery stays callable without adding individual agent commands to startup."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from daintree_assistant_server.contracts.openai import ChatMessage, ToolDef
from daintree_assistant_server.runbooks.catalog import load_catalog
from daintree_assistant_server.runbooks.tool_projection import project_tools

ROOT = Path(__file__).resolve().parents[2]


@pytest.mark.parametrize(
    "runbook_id",
    [
        None,
        "daintree.foundation",
        "daintree.orchestration.fan-out-work",
        "daintree.edits.spawn-visible-agent",
        "daintree.workflow.start-work-on-issue",
    ],
)
def test_discovery_survives_projection_with_the_captured_cli_inventory(
    runbook_id: str | None,
) -> None:
    inventory = json.loads(
        (ROOT / "benchmarks/_harness/fixtures/cli_tool_inventory.json").read_text()
    )
    offered = [ToolDef.model_validate(entry) for entry in inventory]
    catalog = load_catalog()
    active = []
    if runbook_id:
        runbook = catalog.get(runbook_id)
        assert runbook is not None
        active.append(runbook)
    projection = project_tools(
        offered=offered,
        active_runbooks=active,
        catalog=catalog,
        messages=[ChatMessage(role="user", content="Look up $work-issue and run it for issue 42")],
    )
    assert "tool__search" in projection.proposed
    if active:
        assert {"tool__schema", "daintree__invoke"} <= projection.proposed
    # Dynamic host actions are discovered through the existing dispatcher.
    assert not any(name.startswith("agentCapabilities__") for name in projection.offered)


def test_runbook_lookup_names_match_the_pinned_host_contract() -> None:
    fixture = ROOT / "cli/internal/tools/mcpx/testdata/agent_capabilities.json"
    assert fixture.is_file(), "Initialize the pinned CLI submodule before testing synchronization"
    contract = {entry["name"]: entry for entry in json.loads(fixture.read_text())}
    assert set(contract) == {"agentCapabilities.search", "agentCapabilities.get"}
    foundation = load_catalog().get("daintree.foundation")
    assert foundation is not None
    for name, entry in contract.items():
        assert name in foundation.body
        assert entry["inputSchema"]["additionalProperties"] is False
        assert {"terminalId", "agentId", "worktreeId", "worktreePath"} <= entry["inputSchema"][
            "properties"
        ].keys()
    assert (
        contract["agentCapabilities.search"]["inputSchema"]["properties"]["limit"]["maximum"] == 50
    )
    assert "sourceRevision" in contract["agentCapabilities.get"]["outputSchema"]["properties"]
