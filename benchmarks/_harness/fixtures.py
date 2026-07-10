"""Realistic request fixtures — the captured CLI tool inventory + RespondRequest
builders every request-driving suite reuses.

``cli_tool_inventory.json`` is the real 72-tool projection the Go CLI sends every
turn (``Registry.OpenAITools(nil)``, ~75 KB / ~18k tokens), captured once so the
prompt-cost, tool-repair, and injection suites exercise the true tool-schema
region rather than a toy stub. Regenerate it by dumping the CLI registry (see the
benchmark README) if the CLI's tool surface changes.
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
        "project_path": "/Users/dev/Projects/acme/web",
        "project_id": "acme-web",
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
        "configured_agent_ids": ["claude-code", "codex", "gemini"],
        "scheduler_active": True,
        "active_worktree": "feature/checkout-refactor",
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


def build_respond_request(
    *,
    messages: list[dict[str, Any]],
    with_tools: bool = True,
    runtime: dict[str, Any] | None = "default",  # type: ignore[assignment]
    turn: dict[str, Any] | None = None,
    session_id: str = "bench-session",
    turn_id: str = "turn-1",
    instruction_revision: int = 0,
    stream: bool = False,
) -> RespondRequest:
    """Build a strict, valid ``RespondRequest`` against the committed contract."""
    payload: dict[str, Any] = {
        "protocol_version": 1,
        "session": {
            "id": session_id,
            "turn_id": turn_id,
            "instruction_revision": instruction_revision,
        },
        "input": {"messages": messages},
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
