"""The Daintree app's captured MCP surfaces (``tests/fixtures/daintree_mcp``), shared.

Runbook tests read the app's action ids and input schemas from here rather than
from a hand-written list, so a refreshed capture changes what every test accepts
at once.
"""

from __future__ import annotations

import json
from functools import cache
from pathlib import Path
from typing import Any

FIXTURES = Path(__file__).resolve().parent / "fixtures" / "daintree_mcp"
#: Every captured caller. Also the default order an untagged example is read in:
#: the agent pane first, because that is the path runbooks are written for.
SURFACES: tuple[str, ...] = ("pane-core", "pane-full", "help-core", "help-full", "external")


@cache
def capture(surface: str) -> dict[str, Any]:
    data: dict[str, Any] = json.loads((FIXTURES / f"{surface}.json").read_text(encoding="utf-8"))
    return data


def input_schemas(surface: str) -> dict[str, Any]:
    return {tool["action_id"]: tool["inputSchema"] for tool in capture(surface)["tools"]}


def action_ids(surface: str) -> set[str]:
    return set(input_schemas(surface))


def known_action_ids() -> set[str]:
    return set().union(*(action_ids(s) for s in SURFACES))
