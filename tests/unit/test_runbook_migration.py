"""No active runbook may teach the retired Assistant CLI's execution model.

The catalog was written for the Daintree Assistant's own CLI: a `context.snapshot`
injected every turn, `agentTask.spawnForEdits`, `terminal.awaitAll`, async handles,
watchers, timers, grants, scratch stores and an attention queue. None of that exists
for Claude Code or Codex. They act through the Daintree app's MCP, whose surfaces
are captured in ``tests/fixtures/daintree_mcp/``.

A name shaped like an action — ``family.action``, where the family is one the app
uses or the CLI used — must therefore be an action some captured caller is offered,
or a runbook id. This is surface-aware rather than a blanket ban on old spellings:
``workflow.startWorkOnIssue`` is a real app action even though ``workflow.update``
is retired, and ``terminal.sendCommand`` is real for the sidebar assistant even
though an agent pane uses its owned twin.

The current catalog cannot name any of the inbox tools Daintree removed.

The only exemption is the hidden legacy docs runbook, which is never served, by
either tool.
"""

from __future__ import annotations

import re

import pytest
from daintree_mcp_surfaces import MODELS, Model, known_action_ids

from daintree_assistant_server.runbooks.catalog import DOCS_RUNBOOK_ID, load_catalog

#: Families the retired CLI exposed and the app does not. A dotted name in one of
#: these is always a retired construct.
RETIRED_FAMILIES = frozenset(
    {
        "agentTask",
        "async",
        "context",
        "fs",
        "grant",
        "memory",
        "queue",
        "scratch",
        "subagent",
        "timer",
        "tool",
        "watcher",
    }
)

_ACTION_SHAPED = re.compile(
    r"(?<![\w./-])([a-z][A-Za-z]*)\.([a-z][\w-]*(?:\.[a-z][\w-]*)*)(?![\w-])"
)


def _families() -> set[str]:
    """Every family either generation names: a name in one the other lost (the
    inbox's `host.sendMessage` read against the control capture) is still caught."""
    return {
        action.split(".", 1)[0] for model in MODELS for action in known_action_ids(model)
    } | RETIRED_FAMILIES


def _unknown_actions(
    text: str, runbook_ids: set[str], namespaces: set[str], model: Model = "control"
) -> set[str]:
    known = known_action_ids(model)
    families = _families()
    found: set[str] = set()
    for family, rest in _ACTION_SHAPED.findall(text):
        name = f"{family}.{rest}"
        if family == "daintree":
            # Runbook ids and their namespaces; `daintree.call`/`.invoke`/`.status`
            # were CLI tools and are neither.
            if name not in runbook_ids and name not in namespaces:
                found.add(name)
            continue
        if family in families and name not in known:
            found.add(name)
    return found


@pytest.mark.parametrize("model", MODELS)
def test_active_runbooks_name_only_real_actions(model: Model) -> None:
    catalog = load_catalog()
    runbook_ids = set(catalog.known_ids)
    namespaces = {
        ".".join(rid.split(".")[:i]) for rid in runbook_ids for i in range(1, rid.count(".") + 1)
    }
    offenders: dict[str, list[str]] = {}
    for runbook in catalog.runbooks:
        if runbook.id == DOCS_RUNBOOK_ID:
            continue
        text = "\n".join([runbook.summary, runbook.when_to_use, runbook.body])
        unknown = _unknown_actions(text, runbook_ids, namespaces, model)
        if unknown:
            offenders[runbook.id] = sorted(unknown)
    assert not offenders, (
        "runbooks naming actions no captured Daintree caller is offered (retired CLI "
        f"tools, or typos): {offenders}"
    )


def test_the_scanner_catches_what_it_exists_for() -> None:
    """Guard the guard against a regex that silently matches nothing."""
    ids = {"daintree.foundation", "daintree.terminals.rename"}
    namespaces = {"daintree", "daintree.terminals"}
    text = (
        "Call `agentTask.spawnForEdits`, then terminal.awaitAll and daintree.call; "
        "queue.resolve it. Use terminal.getOutput and daintree.terminals.rename. "
        "Read package.json and .daintree/runbook-runs/x.json."
    )
    assert _unknown_actions(text, ids, namespaces) == {
        "agentTask.spawnForEdits",
        "terminal.awaitAll",
        "daintree.call",
        "queue.resolve",
    }


@pytest.mark.parametrize("model", MODELS)
def test_notice_driven_runbooks_say_how_they_resume_and_stop(model: Model) -> None:
    """A procedure that ends its turn on a Daintree notice must say what to do when
    the notice (or a resume) arrives, and where it stops — or the next activation
    has nothing to follow but the original launch."""
    arranges_notice = re.compile(r'"notify"\s*:\s*true|terminal\.notifyWhenIdle')
    missing: list[str] = []
    for runbook in load_catalog().runbooks:
        if runbook.meta.foundation or runbook.id == DOCS_RUNBOOK_ID:
            continue
        if not arranges_notice.search(runbook.body):
            continue
        for section in ("On notice", "Stop and recovery"):
            if section not in runbook.body:
                missing.append(f"{runbook.id}: no '{section}' section")
    assert not missing, missing
