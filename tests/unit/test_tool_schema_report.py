"""The per-tool schema cost report.

Tool schemas are the largest region of a request and every measurement of them
was an aggregate, which answers "how big is the tool region" and refuses the
only question an author can act on. This is the report that names the tools —
and the reason it needs tests is that its whole value is the RANKING, which one
spelling mismatch silently inverted.
"""

from __future__ import annotations

import json

from benchmarks.prompt_cost.tool_schemas import _INVENTORY, run_report
from daintree_assistant_server.contracts.openai import FunctionDef, ToolDef
from daintree_assistant_server.runbooks.catalog import load_catalog
from daintree_assistant_server.runbooks.tool_projection import (
    ALWAYS_AVAILABLE,
    RUNBOOK_SCAFFOLD_TOOLS,
    project_tools,
    tools_bytes,
    wire_name,
)

#: Written out HERE rather than imported from the module under test. Importing
#: the implementation's own `_TIERS` made every ordering assertion circular:
#: reordering the constant and letting the sorter follow it passed unchanged,
#: which is exactly the reclassification these tests exist to catch.
EXPECTED_TIERS = ("always", "scaffold", "undeclared", "foundation", "runbook")


def test_every_core_tool_is_classified_as_core() -> None:
    """The spelling bug that motivated this test.

    Both sets are written dotted (`fs.read`) because that is the form a runbook's
    `requiredTools` uses; the CLI inventory carries the wire form (`fs__read`).
    Compared raw, EVERY core tool fell through to the `runbook` tier — which put
    the one tier paid on every single request at the bottom of the audit order
    and reported the never-withholdable floor as 4.2% of the inventory instead
    of ~28%. Nothing failed; the report simply pointed the wrong way.
    """
    doc = run_report()
    tiers = {row["name"]: row["tier"] for row in doc["tools"]}

    for dotted in ALWAYS_AVAILABLE:
        name = wire_name(dotted)
        if name in tiers:  # a core tool the pinned inventory predates
            assert tiers[name] == "always", name
    for dotted in RUNBOOK_SCAFFOLD_TOOLS:
        name = wire_name(dotted)
        if name in tiers:
            assert tiers[name] == "scaffold", name

    assert doc["summary"]["by_tier"]["always"]["tools"] > 0, (
        "no tool classified as core — the dotted/wire mismatch is back"
    )


def test_the_report_ranks_by_reach_before_size() -> None:
    """A tool's cost is its size times how often it is offered, and a TIER order
    does not express the second factor.

    Two labels can mean identical exposure — `always` and `undeclared` reach
    every request by opposite arguments, and `scaffold` and `foundation` both
    reach every runbook-bearing turn — while one tier (`runbook`) spans everything
    from one declarer to twenty. Ranking on the label put a tool offered on
    every request below one that needs an active runbook.
    """
    rows = run_report()["tools"]
    keys = [
        (not row["offered_on_runbook_less_turns"], -row["reach"], -row["schema_bytes"])
        for row in rows
    ]
    assert keys == sorted(keys), "rows are not in reach-then-size order"

    universal = [r for r in rows if r["offered_on_runbook_less_turns"]]
    assert {r["tier"] for r in universal} == {"always", "undeclared"}, (
        "the widest rank must be exactly the tiers projection can never withhold"
    )
    assert rows[: len(universal)] == universal, "a narrower tool outranks a universal one"


def test_reach_counts_the_runbooks_that_would_actually_admit_a_tool() -> None:
    """Derived from the admission rules, not assumed from a label.

    A foundation's tools are admitted alongside every specific in its namespace,
    so their reach is the namespace, not the one runbook that declares them.
    """
    rows = {row["name"]: row for row in run_report()["tools"]}
    catalog = load_catalog()
    foundation = next((s for s in catalog.runbooks if s.meta.foundation), None)
    assert foundation is not None, "no foundation — this test needs rewriting"

    paired = sum(
        1
        for runbook in catalog.runbooks
        if catalog.domain_foundation(runbook.id) == foundation.id and runbook.id != foundation.id
    )
    assert paired > 1, "the foundation pairs with nothing; reach cannot be checked"

    for dotted in foundation.meta.required_tools or ():
        row = rows.get(wire_name(dotted))
        if row is None or row["tier"] != "foundation":
            continue  # core tools outrank the foundation label; see `_tier`
        assert row["reach"] >= paired, (
            f"{row['name']} reaches {row['reach']} runbooks but its foundation pairs "
            f"with {paired}"
        )
        assert row["reach"] > row["declaring_runbooks"]


def test_the_universal_rows_are_exactly_what_a_runbook_less_turn_proposes() -> None:
    """Cross-checked against `project_tools` itself rather than re-derived here.

    The report reimplements the admission rules to rank by them, and a
    reimplementation that drifted from the real projection would rank confidently
    and wrongly — which is the one failure mode a report has.
    """
    inventory = json.loads(_INVENTORY.read_text(encoding="utf-8"))
    catalog = load_catalog()
    # `project_tools` takes typed `ToolDef`s, not the raw fixture dicts — the
    # inventory is the CLI's OpenAI-shaped capture, so it is converted here the
    # way the request boundary converts it.
    offered = [
        ToolDef(
            type="function",
            function=FunctionDef(
                name=(entry.get("function") or {}).get("name", ""),
                description=(entry.get("function") or {}).get("description"),
                parameters=(entry.get("function") or {}).get("parameters"),
            ),
        )
        for entry in inventory
    ]
    projected = project_tools(
        active_runbooks=[], catalog=catalog, offered=offered, messages=[]
    )

    doc = run_report()
    universal = {r["name"] for r in doc["tools"] if r["offered_on_runbook_less_turns"]}
    assert universal == set(projected.proposed), (
        "the report's runbook-less set disagrees with project_tools"
    )
    assert doc["summary"]["runbook_less_floor_bytes"] == tools_bytes(
        [t for t in inventory if (t.get("function") or {}).get("name") in universal]
    )


def test_the_totals_agree_with_the_aggregate_everything_else_measures() -> None:
    """A per-tool report that summed to a different number than `tools_bytes`
    would be a second, quietly disagreeing account of the same array."""
    doc = run_report()
    per_tool = sum(row["schema_bytes"] for row in doc["tools"])
    assert doc["summary"]["total_bytes"] == per_tool

    # `tools_bytes` measures the ARRAY, so it carries punctuation this sum does
    # not: N-1 commas and two brackets. Read through the module's own constant
    # rather than a hand-built path, so the two cannot come to disagree about
    # which capture they describe.
    inventory = json.loads(_INVENTORY.read_text(encoding="utf-8"))
    assert len(inventory) >= 1, "the formula below assumes a non-empty array"
    assert tools_bytes(inventory) == per_tool + (len(inventory) - 1) + 2


def test_a_foundation_tool_outranks_a_single_runbook_tool() -> None:
    """Admission pairs every selected specific with its domain foundation, so a
    foundation's declared tools ride nearly every runbook-bearing turn — 38 of 39
    runbooks point at one foundation today. Ranking them beside a tool one runbook
    declares understates them by an order of magnitude, which is exactly the
    mis-aim this report exists to prevent."""
    doc = run_report()
    tiers = [row["tier"] for row in doc["tools"]]
    assert "foundation" in tiers, "no foundation tools — the pairing rule changed"
    assert tiers.index("foundation") < (
        tiers.index("runbook") if "runbook" in tiers else len(tiers)
    )
    assert doc["summary"]["by_tier"]["foundation"]["bytes"] > 0


def test_every_tier_is_accounted_for() -> None:
    """A tool falling into no tier would vanish from the ranking silently."""
    doc = run_report()
    assert sum(stats["tools"] for stats in doc["summary"]["by_tier"].values()) == len(
        doc["tools"]
    )
    assert {row["tier"] for row in doc["tools"]} <= set(EXPECTED_TIERS)


def test_declaring_runbooks_is_the_count_of_declared_by() -> None:
    """The count is what the table prints and the list is what a reader follows;
    they are two views of one fact and must not drift."""
    for row in run_report()["tools"]:
        assert row["declaring_runbooks"] == len(row["declared_by"])
        assert row["declared_by"] == sorted(row["declared_by"])
        # An undeclared tool is undeclared by definition — that is the tier's
        # whole meaning, and projection never withholds one.
        if row["tier"] == "undeclared":
            assert row["declared_by"] == []
