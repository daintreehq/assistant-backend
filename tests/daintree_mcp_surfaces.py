"""The Daintree app's captured MCP surfaces (``tests/fixtures/daintree_mcp``), shared.

Runbook tests read the app's action ids and input schemas from here rather than
from a hand-written list, so a refreshed capture changes what every test accepts
at once.

The current app capture is the authority. Retired captures are offline in archive/.
"""

from __future__ import annotations

import json
from functools import cache
from pathlib import Path
from typing import Any, Literal

Model = Literal["control"]
MODELS: tuple[Model, ...] = ("control",)

FIXTURES = Path(__file__).resolve().parent / "fixtures" / "daintree_mcp"
GENERATIONS: dict[Model, Path] = {"control": FIXTURES}

#: Every captured caller, per generation. Also the default order an untagged
#: example is read in: the agent pane first, because that is the path runbooks are
#: written for.
SURFACES_BY_MODEL: dict[Model, tuple[str, ...]] = {
    "control": (
        "pane-core",
        "pane-full",
        "help-core",
        "help-full",
        "relayed-core",
        "relayed-full",
        "external",
    ),
}
SURFACES = SURFACES_BY_MODEL["control"]


def rebuild_delta(base_tools: list[dict[str, Any]], delta: dict[str, Any]) -> list[dict[str, Any]]:
    """A delta surface's tools: the base's, minus ``removed``, with ``changed`` entries
    replacing theirs, plus ``added``. Refuses a delta that does not apply cleanly (an id
    removed or changed that the base lacks, one added that it has, an id named twice),
    so a hand edit cannot quietly drop part of a surface."""
    base_ids = [str(t["action_id"]) for t in base_tools]
    removed = [str(i) for i in delta["removed"]]
    changed = {str(t["action_id"]): t for t in delta["changed"]}
    added = [str(t["action_id"]) for t in delta["added"]]
    named = [*removed, *changed, *added]
    problems = []
    if len(set(named)) != len(named) or len(changed) != len(delta["changed"]):
        problems.append("an id is named more than once")
    if missing := sorted((set(removed) | set(changed)) - set(base_ids)):
        problems.append(f"removed or changed ids the base lacks: {missing}")
    if present := sorted(set(added) & set(base_ids)):
        problems.append(f"added ids the base already has: {present}")
    if problems:
        raise ValueError("delta does not apply: " + "; ".join(problems))
    kept = [
        changed.get(str(t["action_id"]), t) for t in base_tools if t["action_id"] not in removed
    ]
    return sorted([*kept, *delta["added"]], key=lambda t: str(t["action_id"]))


@cache
def capture(surface: str, model: Model = "control") -> dict[str, Any]:
    """One current surface's complete capture."""
    path = GENERATIONS[model] / f"{surface}.json"
    return json.loads(path.read_text(encoding="utf-8"))


def input_schemas(surface: str, model: Model = "control") -> dict[str, Any]:
    return {tool["action_id"]: tool["inputSchema"] for tool in capture(surface, model)["tools"]}


def action_ids(surface: str, model: Model = "control") -> set[str]:
    return set(input_schemas(surface, model))


def known_action_ids(model: Model = "control") -> set[str]:
    return set().union(*(action_ids(s, model) for s in SURFACES_BY_MODEL[model]))


# ---------------------------------------------------------------------------
# Caller policy: which captured caller a runbook's baseline path is written for.
#
# A runbook's `requiredTools` (its baseline path) and its untagged examples are
# checked against the surface of the caller that follows it. That caller is
# decided by the runbook's id namespace and by nothing else in its metadata, so
# a runbook cannot make itself a hosts runbook by declaring host tools, and no
# pane runbook's requirements stop being checked against the pane.
# ---------------------------------------------------------------------------

#: The default caller: Claude Code or Codex in a Daintree agent pane. A pane's
#: tier is an approval line, not a ceiling, so pane-core lists everything a pane
#: can ever be offered.
PANE_CALLERS: tuple[str, ...] = ("pane-core",)

#: Domains whose runbooks are followed by a caller other than an agent pane.
#:
#: `daintree.hosts` is sidebar-only: the Shell's own assistant is offered
#: `host.list` and `host.call`, at either Tool set; an agent pane never is.
CALLER_POLICY_BY_MODEL: dict[Model, dict[str, tuple[str, ...]]] = {
    "control": {"daintree.hosts": ("help-core", "help-full")},
}
CALLER_POLICY = CALLER_POLICY_BY_MODEL["control"]

#: Direct host control: the Shell assistant's reach into a host.
HOST_CONTROL_TOOLS = frozenset({"host.list", "host.call"})
#: The surfaces a host offers inside `host.call`, by the calling surface's tier.
RELAYED_SURFACE = {"help-core": "relayed-core", "help-full": "relayed-full"}

#: Retired action ids must not reappear in current instructions or captures.
MESSAGING_TOOLS = frozenset(
    {
        "host.sendMessage",
        "host.listAssistants",
        "host.getTask",
        "host.listTasks",
        "host.cancelTask",
        "host.startAssistant",
        "host.readQuota",
        "assistant.getInbox",
        "assistant.reply",
        "assistant.sendMessage",
    }
)
NON_PANE_SURFACES_BY_MODEL: dict[Model, tuple[str, ...]] = {
    "control": ("relayed-core", "relayed-full"),
}


def baseline_callers(runbook_id: str, model: Model = "control") -> tuple[str, ...]:
    """The captured callers a runbook's baseline path may run on, by id namespace.

    A domain matches on whole dotted segments: `daintree.hosts.ask` is a hosts
    runbook; `daintree.hostsx.ask` and `daintree.orchestration.hosts` are not.
    """
    for domain, callers in CALLER_POLICY_BY_MODEL[model].items():
        if runbook_id == domain or runbook_id.startswith(domain + "."):
            return callers
    return PANE_CALLERS


def baseline_violation(
    runbook_id: str, required: tuple[str, ...] | list[str], model: Model = "control"
) -> str | None:
    """Why `required` cannot be one caller's baseline path for `runbook_id`, or None.

    The whole set must be offered to a single caller in the runbook's policy: a
    baseline that needs both the hub's send and a lane's send runs nowhere.
    """
    callers = baseline_callers(runbook_id, model)
    if any(set(required) <= action_ids(caller, model) for caller in callers):
        return None
    missing = {caller: sorted(set(required) - action_ids(caller, model)) for caller in callers}
    return f"{runbook_id}: no single caller in {list(callers)} is offered all of it: {missing}"


def untagged_example_surfaces(runbook_id: str, model: Model = "control") -> tuple[str, ...]:
    """Where an untagged example in this runbook is validated: the first of these
    offering the action. A hosts runbook's own callers only; for any other runbook
    every surface but the non-pane ones, pane first, so a pane runbook's sidebar-branch
    example must name its surface (``surface=help-full``) to validate at all."""
    callers = baseline_callers(runbook_id, model)
    if callers != PANE_CALLERS:
        return callers
    return tuple(
        surface
        for surface in SURFACES_BY_MODEL[model]
        if surface not in NON_PANE_SURFACES_BY_MODEL[model]
    )
