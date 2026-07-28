"""Realistic request fixtures — the captured CLI tool inventory + RespondRequest
builders every request-driving suite reuses.

``cli_tool_inventory.json`` is the captured CLI tool projection the Go CLI sends
every turn (``(*tools.Registry).OpenAITools(nil)``), captured once so the
prompt-cost, tool-repair, and injection suites exercise the true tool-schema
region — the largest and most cache-critical part of a request — rather than a
toy stub. The tool count is data in that file and is deliberately not restated
here, so a stale capture shows up as a measurement change rather than a wrong
comment. The CLI lives in a pinned submodule CI never fetches, so the fixture
only refreshes by hand: see ``fixtures/README.md`` for the regeneration recipe.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from daintree_assistant_server.contracts.daintree_api import RespondRequest

FIXTURES = Path(__file__).resolve().parent / "fixtures"
_TOOL_INVENTORY = FIXTURES / "cli_tool_inventory.json"


def load_tool_inventory() -> list[dict[str, Any]]:
    """The raw wire tool list (OpenAI function-spec dicts) the CLI sends."""
    return json.loads(_TOOL_INVENTORY.read_text(encoding="utf-8"))


def tool_names() -> list[str]:
    return [t["function"]["name"] for t in load_tool_inventory()]


def _base_runtime() -> dict[str, Any]:
    """A representative CLI runtime block (permission tier, MCP, terminals)."""
    return {
        "permission_tier": "operator",
        "mcp": {"connected": True, "transport": "http", "tool_count": 24},
        "mcp_servers": [
            {
                "name": "daintree",
                "transport": "http",
                "status": "connected",
                "tool_count": 24,
                "description": "Daintree orchestration surface.",
                "instructions": "Use terminal.list before sendCommand.",
            }
        ],
        "scheduler_active": True,
        "worktree": {
            "current": {
                "id": "wt-checkout-refactor",
                "path": "/Users/dev/Projects/acme/web/.worktrees/checkout-refactor",
                "branch": "feature/checkout-refactor",
                "is_main": False,
                "issue_number": 286,
                "issue_title": "Expose structured startup context",
                "pr_number": 418,
                "pr_title": "Add startup and worktree snapshots",
                "pr_url": "https://example.test/acme/web/pull/418",
                "status": "dirty",
                "last_commit": "abc1234 Add request context",
            }
        },
        "open_terminals": [
            {
                "id": "term-1",
                "kind": "agent",
                "title": "refactor checkout",
                "agent_id": "claude-code",
                "agent_state": "working",
            }
        ],
    }


def _base_startup() -> dict[str, Any]:
    """Representative session-stable context sent before visible conversation."""
    return {
        "project": {
            "id": "acme-web",
            "name": "Acme Web",
            "path": "/Users/dev/Projects/acme/web",
            "status": "active",
            "daintree_config_present": True,
            "in_repo_settings": True,
        },
        "agent_roster": {
            "agents": [
                {
                    "id": "claude-code",
                    "display_name": "Claude Code",
                    "source": "user",
                    "availability": "ready",
                    "installed": True,
                    "launchable": True,
                },
                {
                    "id": "codex",
                    "display_name": "Codex",
                    "source": "built-in",
                    "availability": "ready",
                    "installed": True,
                    "launchable": True,
                    "pinned": True,
                    "toolbar_visible": True,
                },
                {
                    "id": "gemini",
                    "display_name": "Gemini",
                    "source": "built-in",
                    "availability": "missing",
                    "installed": False,
                    "launchable": False,
                    "pinned": False,
                    "toolbar_visible": False,
                },
            ],
            "complete": True,
            "availability_complete": True,
            "total_count": 3,
        },
        "project_instructions": "Run project checks before handing work back.",
    }


def build_respond_request(
    *,
    messages: list[dict[str, Any]],
    with_tools: bool = True,
    startup: dict[str, Any] | None = None,
    runtime: dict[str, Any] | None = "default",  # type: ignore[assignment]
    turn: dict[str, Any] | None = None,
    session_id: str = "bench-session",
    turn_id: str = "turn-1",
    instruction_revision: int = 0,
    stream: bool = False,
) -> RespondRequest:
    """Build a strict, valid ``RespondRequest`` against the committed contract."""
    payload: dict[str, Any] = {
        "protocol_version": 2,
        "session": {
            "id": session_id,
            "turn_id": turn_id,
            "instruction_revision": instruction_revision,
        },
        "input": {"messages": messages},
        "startup": startup if startup is not None else _base_startup(),
        "generation": {"stream": stream},
    }
    if with_tools:
        payload["input"]["tools"] = load_tool_inventory()
    if runtime == "default":
        runtime = _base_runtime()
    if runtime is not None:
        payload["runtime"] = runtime
    if turn is not None:
        payload["turn"] = turn
    return RespondRequest.model_validate(payload)
