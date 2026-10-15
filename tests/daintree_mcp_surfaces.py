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
SURFACES: tuple[str, ...] = (
    "pane-core",
    "pane-full",
    "help-core",
    "help-full",
    "hub-full",
    "lane-full",
    "external",
)


@cache
def capture(surface: str) -> dict[str, Any]:
    """One surface's capture as ``{"capture": ..., "tools": [...]}``.

    The hub and lane surfaces are stored as a delta against the sidebar surface of
    the same tier (``<name>.delta.json``: ``added``, ``changed``, ``removed``),
    because they are that surface plus a handful of messaging tools. They are
    rebuilt here, so every test reads every surface the same way.
    """
    whole = FIXTURES / f"{surface}.json"
    if whole.exists():
        data: dict[str, Any] = json.loads(whole.read_text(encoding="utf-8"))
        return data
    delta = json.loads((FIXTURES / f"{surface}.delta.json").read_text(encoding="utf-8"))
    base = capture(delta["capture"]["delta_of"])
    removed = set(delta["removed"])
    changed = {tool["action_id"]: tool for tool in delta["changed"]}
    tools = [changed.get(t["action_id"], t) for t in base["tools"] if t["action_id"] not in removed]
    tools = sorted([*tools, *delta["added"]], key=lambda t: str(t["action_id"]))
    return {"capture": delta["capture"], "tools": tools}


def input_schemas(surface: str) -> dict[str, Any]:
    return {tool["action_id"]: tool["inputSchema"] for tool in capture(surface)["tools"]}


def action_ids(surface: str) -> set[str]:
    return set(input_schemas(surface))


def known_action_ids() -> set[str]:
    return set().union(*(action_ids(s) for s in SURFACES))


# ---------------------------------------------------------------------------
# Caller policy: which captured caller a runbook's baseline path is written for.
#
# A runbook's `requiredTools` (its baseline path) and its untagged examples are
# checked against the surface of the caller that follows it. That caller is
# decided by the runbook's id namespace and by nothing else in its metadata, so
# a runbook cannot make itself a hub or lane runbook by declaring hub or lane
# tools, and no pane runbook's requirements stop being checked against the pane.
# ---------------------------------------------------------------------------

#: The default caller: Claude Code or Codex in a Daintree agent pane. A pane's
#: tier is an approval line, not a ceiling, so pane-core lists everything a pane
#: can ever be offered.
PANE_CALLERS: tuple[str, ...] = ("pane-core",)

#: Domains whose runbooks are followed by a caller other than an agent pane.
#:
#: `daintree.hosts` (cross-host assistant messaging) is sidebar-only by design:
#: the hub is the sidebar assistant on a Shell with a Remote Host configured, and
#: an enrolled lane is the sidebar assistant on a machine listening as a host.
#: An agent pane is never either. A hosts runbook's baseline must run on ONE of
#: those two callers, at Tool set = Full, because the hub's sends, cancels and
#: starts are Full-only; the hub-core and lane-core captures record what each
#: offers at Core.
CALLER_POLICY: dict[str, tuple[str, ...]] = {
    "daintree.hosts": ("hub-full", "lane-full"),
}

#: The cross-host messaging tools: the hub's `host.*` reach and a lane's inbox.
#: Offered only to sidebar-assistant sessions that messaging can reach.
HUB_TOOLS = frozenset(
    {
        "host.list",
        "host.listAssistants",
        "host.sendMessage",
        "host.getTask",
        "host.listTasks",
        "host.cancelTask",
        "host.startAssistant",
    }
)
LANE_TOOLS = frozenset({"assistant.getInbox", "assistant.reply", "assistant.sendMessage"})
MESSAGING_TOOLS = HUB_TOOLS | LANE_TOOLS


def baseline_callers(runbook_id: str) -> tuple[str, ...]:
    """The captured callers a runbook's baseline path may run on, by id namespace.

    A domain matches on whole dotted segments: `daintree.hosts.ask` is a hosts
    runbook; `daintree.hostsx.ask` and `daintree.orchestration.hosts` are not.
    """
    for domain, callers in CALLER_POLICY.items():
        if runbook_id == domain or runbook_id.startswith(domain + "."):
            return callers
    return PANE_CALLERS


def baseline_violation(runbook_id: str, required: tuple[str, ...] | list[str]) -> str | None:
    """Why `required` cannot be one caller's baseline path for `runbook_id`, or None.

    The whole set must be offered to a single caller in the runbook's policy: a
    baseline that needs both the hub's send and a lane's send runs nowhere.
    """
    callers = baseline_callers(runbook_id)
    if any(set(required) <= action_ids(caller) for caller in callers):
        return None
    missing = {caller: sorted(set(required) - action_ids(caller)) for caller in callers}
    return f"{runbook_id}: no single caller in {list(callers)} is offered all of it: {missing}"
