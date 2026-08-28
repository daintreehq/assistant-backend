"""Runbook-aware tool projection — what a turn WOULD send.

Tool schemas are ~61% of a request, and filtering them to the active runbooks'
declared needs is the largest prompt saving available. These tests cover the
projector: which tools survive, and why each one did.

Whether the result is ever APPLIED is a separate question with its own suite
(`test_tool_projection_enforcement.py` for the gate,
`tests/integration/test_tool_projection_enforced.py` for the wiring). Nothing
here depends on the mode — the projection is computed identically in `shadow` and
`enforced`, and that is deliberate: the evidence that authorizes enforcement has
to come from the same computation enforcement will use.

What decides whether a stage may be promoted is `outside_proposed` in the result
log: how often the model calls a tool the projection would have withheld. Read it
from SHADOW traffic — an enforced turn cannot call a tool it was never offered,
so enforced evidence grades itself clean by construction.

`test_declared_tools_cover_what_runbook_bodies_actually_use` is the offline half of
that measurement. Read what it proves narrowly: it finds backticked registry
names in runbook bodies and checks them against the frontmatter. It cannot see a
tool the model reaches for on its own, and it cannot tell an instruction from a
prohibition — which is why its exceptions are a hand-classified set rather than a
count.

Two failures shaped this file, and both were invisible to a green suite:

* the projection compared dotted names to wire names and matched NOTHING, while
  every test passed because the helpers built dotted names on both sides;
* the "20 of 26 runbooks have undeclared tools" figure it was built to defend was
  never real — the detector derived its vocabulary from the declarations it was
  auditing, so removing a declaration also removed the evidence against it.

Hence the rule here: assert against literals and against the captured registry,
never against the code under test.
"""

from __future__ import annotations

import re

import pytest

from daintree_assistant_server.contracts.openai import (
    ChatMessage,
    FunctionCall,
    FunctionDef,
    ToolCall,
    ToolDef,
)
from daintree_assistant_server.runbooks.catalog import load_catalog
from daintree_assistant_server.runbooks.tool_projection import (
    ALWAYS_AVAILABLE,
    RUNBOOK_SCAFFOLD_TOOLS,
    project_tools,
    wire_name,
)


@pytest.fixture(scope="module")
def catalog():  # type: ignore[no-untyped-def]
    return load_catalog()


def _tools(*names: str) -> list[ToolDef]:
    """Build an offered inventory the way the CLI does — in WIRE form.

    Building these in dotted form is what hid the name-format bug: the tests all
    passed while the projection, fed a real inventory, matched nothing and would
    have withheld the whole captured inventory.
    """
    return [
        ToolDef(
            type="function",
            function=FunctionDef(name=wire_name(n), parameters={"type": "object"}),
        )
        for n in names
    ]


def _user(text: str = "do the thing") -> list[ChatMessage]:
    return [ChatMessage(role="user", content=text)]


# ---------------------------------------------------------------------------
# The projection admits what a turn needs.
# ---------------------------------------------------------------------------


def test_the_core_is_always_admitted(catalog) -> None:  # type: ignore[no-untyped-def]
    """A turn with NO runbook loaded must still be able to orient, find a
    capability it was not given, and ask the user a question — the base prompt
    instructs it to do all three."""
    projection = project_tools(
        offered=_tools(*ALWAYS_AVAILABLE, "forge.getPR"),
        active_runbooks=[],
        catalog=catalog,
        messages=_user(),
    )
    assert {wire_name(n) for n in ALWAYS_AVAILABLE} <= projection.proposed
    assert wire_name("forge.getPR") in projection.withheld


def test_an_active_runbooks_declared_tools_are_admitted(catalog) -> None:  # type: ignore[no-untyped-def]
    runbook = catalog.get("daintree.terminals.rename")
    assert runbook is not None
    declared = list(runbook.meta.required_tools or [])
    assert declared, "fixture runbook declares no tools"

    projection = project_tools(
        offered=_tools(*declared, "forge.getPR"),
        active_runbooks=[runbook],
        catalog=catalog,
        messages=_user(),
    )
    assert {wire_name(d) for d in declared} <= projection.proposed
    assert wire_name("forge.getPR") in projection.withheld


def test_the_domain_foundations_tools_come_with_a_specific(catalog) -> None:  # type: ignore[no-untyped-def]
    """A foundation is the base judgment its specifics build on, so its tools are
    needed even on a turn where the foundation itself did not make the cap."""
    specific = catalog.get("daintree.terminals.rename")
    foundation_id = catalog.domain_foundation("daintree.terminals.rename")
    assert specific is not None and foundation_id
    foundation = catalog.get(foundation_id)
    assert foundation is not None

    # Exclude the always-on core: a tool admitted by BOTH rules reports the
    # first, and "core" is the more informative reason.
    foundation_only = (
        set(foundation.meta.required_tools or [])
        - set(specific.meta.required_tools or [])
        - ALWAYS_AVAILABLE
    )
    assert foundation_only, "fixture foundation adds no tools of its own"

    projection = project_tools(
        offered=_tools(*foundation_only),
        active_runbooks=[specific],
        catalog=catalog,
        messages=_user(),
    )
    assert {wire_name(n) for n in foundation_only} <= projection.proposed
    assert all(
        projection.reasons[wire_name(name)].startswith("foundation:") for name in foundation_only
    )


def test_a_pinned_tool_choice_is_never_withheld(catalog) -> None:  # type: ignore[no-untyped-def]
    """Withholding a tool the client pinned is an immediate hard error from the
    provider, not a degraded turn."""
    projection = project_tools(
        offered=_tools("forge.getPR"),
        active_runbooks=[],
        catalog=catalog,
        messages=_user(),
        forced_tool="forge.getPR",
    )
    assert wire_name("forge.getPR") in projection.proposed
    assert projection.reasons[wire_name("forge.getPR")] == "forced_tool_choice"


def test_an_unresolved_call_keeps_its_tool_available(catalog) -> None:  # type: ignore[no-untyped-def]
    """A continuation round must always be able to finish work already started."""
    messages = [
        ChatMessage(role="user", content="check the PR"),
        ChatMessage(
            role="assistant",
            content=None,
            tool_calls=[
                ToolCall(
                    id="c1",
                    type="function",
                    function=FunctionCall(name=wire_name("forge.getPR"), arguments="{}"),
                )
            ],
        ),
    ]
    projection = project_tools(
        offered=_tools("forge.getPR"),
        active_runbooks=[],
        catalog=catalog,
        messages=messages,
    )
    assert projection.reasons[wire_name("forge.getPR")] == "unresolved_call"


def test_a_resolved_call_does_not_keep_its_tool(catalog) -> None:  # type: ignore[no-untyped-def]
    messages = [
        ChatMessage(role="user", content="check the PR"),
        ChatMessage(
            role="assistant",
            content=None,
            tool_calls=[
                ToolCall(
                    id="c1",
                    type="function",
                    function=FunctionCall(name=wire_name("forge.getPR"), arguments="{}"),
                )
            ],
        ),
        ChatMessage(role="tool", tool_call_id="c1", content="{}"),
    ]
    projection = project_tools(
        offered=_tools("forge.getPR"),
        active_runbooks=[],
        catalog=catalog,
        messages=messages,
    )
    assert wire_name("forge.getPR") in projection.withheld


def test_two_calls_to_one_tool_need_both_answered(catalog) -> None:  # type: ignore[no-untyped-def]
    """Matched by call id, not by name: answering one of two calls to the same
    tool must not strand the other."""
    messages = [
        ChatMessage(role="user", content="check both"),
        ChatMessage(
            role="assistant",
            content=None,
            tool_calls=[
                ToolCall(
                    id=cid,
                    type="function",
                    function=FunctionCall(name=wire_name("forge.getPR"), arguments="{}"),
                )
                for cid in ("c1", "c2")
            ],
        ),
        ChatMessage(role="tool", tool_call_id="c1", content="{}"),
    ]
    projection = project_tools(
        offered=_tools("forge.getPR"),
        active_runbooks=[],
        catalog=catalog,
        messages=messages,
    )
    assert wire_name("forge.getPR") in projection.proposed


def test_a_turn_with_no_tools_projects_nothing(catalog) -> None:  # type: ignore[no-untyped-def]
    projection = project_tools(offered=None, active_runbooks=[], catalog=catalog, messages=_user())
    assert projection.proposed == frozenset()
    assert projection.withheld == frozenset()


def test_the_projection_never_invents_a_tool(catalog) -> None:  # type: ignore[no-untyped-def]
    """It can only ever admit from what the client offered — a proposal naming a
    tool that was not offered would be unfulfillable."""
    runbook = catalog.get("daintree.terminals.rename")
    assert runbook is not None
    projection = project_tools(
        offered=_tools("context.snapshot"),
        active_runbooks=[runbook],
        catalog=catalog,
        messages=_user(),
        forced_tool="something.notOffered",
    )
    assert projection.proposed <= projection.offered
    assert "something.notOffered" not in projection.proposed


# ---------------------------------------------------------------------------
# The `outside` metric — the gate on ever enabling this.
# ---------------------------------------------------------------------------


def test_outside_reports_exactly_the_withheld_calls(catalog) -> None:  # type: ignore[no-untyped-def]
    # Both withheld examples must be tools the catalog DECLARES: one it has no
    # opinion about passes through as unclassified and is never outside anything.
    projection = project_tools(
        offered=_tools("context.snapshot", "recipe.run", "forge.getPR"),
        active_runbooks=[],
        catalog=catalog,
        messages=_user(),
    )
    assert projection.outside([wire_name("context.snapshot")]) == []
    assert projection.outside([wire_name("recipe.run"), wire_name("context.snapshot")]) == [
        wire_name("recipe.run")
    ]
    assert projection.outside([wire_name("recipe.run"), wire_name("forge.getPR")]) == [
        wire_name("forge.getPR"),
        wire_name("recipe.run"),
    ]
    # A tool that was never offered is not a projection failure.
    assert projection.outside(["never.offered"]) == []


# ---------------------------------------------------------------------------
# Stage A: is the metadata trustworthy enough to filter on?
# ---------------------------------------------------------------------------


# Every dotted name in the CLI registry, from the captured inventory. This is the
# AUTHORITY on what a tool is. An earlier version derived it from the runbooks' own
# declarations, which is circular: deleting the last declaration of a real tool
# made every body reference to it invisible, so the check certified the very
# regression it existed to catch.
def _registry_dotted_names() -> frozenset[str]:
    import json
    from pathlib import Path

    path = (
        Path(__file__).resolve().parents[2]
        / "benchmarks"
        / "_harness"
        / "fixtures"
        / "cli_tool_inventory.json"
    )
    inv = json.loads(path.read_text(encoding="utf-8"))
    return frozenset(t["function"]["name"].replace("__", ".") for t in inv)


# Backticked only: a tool name in running prose is discussion, a backticked one is
# the runbook naming a call site. Still imperfect — see the reviewed set below.
_TICKED = re.compile(r"`([a-zA-Z][a-zA-Z0-9.]*)")

# References a body makes to a tool it does not declare, REVIEWED ONE BY ONE and
# pinned as an exact set.
#
# This is not a ratchet on a number. A count-based ceiling was worse than useless
# here: it sat at 20 while the real figure was 7, so it permitted thirteen new
# offenders without failing, and nothing ever pushed it down. Worse, the detector
# cannot tell a call site from a warning — most entries below are the runbook
# telling the model NOT to call something, which is the opposite of an undeclared
# dependency.
#
# So each pair is classified by hand. Adding or removing one fails this test and
# demands the same review. A genuine undeclared USE does not belong here — declare
# it in the runbook's frontmatter instead.
_REVIEWED_UNDECLARED = {
    # PROHIBITIONS — the body names the tool to forbid it. Declaring these would
    # be actively wrong: it would offer the model exactly what the runbook is
    # telling it not to reach for.
    ("daintree.orchestration.fan-out-work", "terminal.awaitAll"),
    ("daintree.orchestration.multi-agent", "terminal.extract.json"),
    ("daintree.orchestration.verify-agent-work", "fs.read"),
    ("daintree.orchestration.verify-agent-work", "terminal.await.async"),
    # EXPLANATORY — the body names the tool to describe how the SYSTEM behaves
    # (what opens or closes a ledger row, what can never be granted, how a
    # sibling tool differs), not to instruct a call.
    ("daintree.orchestration.status-and-resume", "watcher.cancel"),
    ("daintree.orchestration.status-and-resume", "workflow.startWorkOnIssue"),
    ("daintree.orchestration.watch-outcome", "daintree.call"),
    ("daintree.forge.land-pull-request", "watcher.watchPR"),
    ("daintree.terminals.rename", "terminal.extract"),
    # EXPLANATORY + handoff. The find runbook names the CI read only to forbid
    # trusting `ciStatus` on a searched PR row and to say where the real verdict
    # comes from (daintree.workflow.follow-up-on-issue, which declares it). This
    # runbook selects work; it never reads CI itself.
    ("daintree.workflow.find-issue-work", "forge.getChecks"),
    # PROHIBITION. This runbook exists because a session reached for raw git in the
    # user's own terminal when the typed wrapper's schema gave it no argument
    # names; its step 5 forbids both the raw `daintree.call` and improvised
    # `git checkout`/`git worktree add`. Declaring it would be a false statement
    # about the runbook's dependencies — `requiredTools` says what a runbook NEEDS,
    # and this one needs the tool not to be used. (It would not actually withhold
    # anything: the foundation declares `daintree.call` and is projected alongside
    # every specific. The reason is metadata truth, not enforcement.)
    ("daintree.worktree.create-plain-worktree", "daintree.call"),
    # PROHIBITION. `terminal.moveToWorktree` gained a typed wrapper, so its step 6
    # names `daintree.call` only to forbid routing the move through it — the raw
    # action now returns USE_TYPED_WRAPPER and does nothing, which would ask the
    # user to approve a call that cannot run. Same reasoning as the entry above:
    # the runbook needs this tool NOT to be used.
    ("daintree.worktree.relocate-agents", "daintree.call"),
    # PROHIBITIONS. The durable-graph runbook says graph operations are CLI-local
    # and must not be routed through the raw MCP escape hatch. The resource
    # runbook says that raw MCP lifecycle calls cannot be grant-authorized.
    ("daintree.orchestration.execute-durable-workflow", "daintree.call"),
    ("daintree.worktree.manage-resource", "grant.create"),
    # PROHIBITION. The relocate runbook's step 10 forbids closing the very
    # terminals it was asked to preserve, including as a fallback when the
    # cross-worktree move is refused. Declaring it would offer the model the one
    # tool the runbook exists to keep it away from.
    ("daintree.worktree.relocate-agents", "terminal.close"),
}


def _undeclared_references(catalog) -> set[tuple[str, str]]:  # type: ignore[no-untyped-def]
    registry = _registry_dotted_names()
    found: set[tuple[str, str]] = set()
    for runbook in catalog.runbooks:
        declared = set(runbook.meta.required_tools or ())
        for token in _TICKED.findall(runbook.body):
            if token in registry and token not in declared:
                found.add((runbook.id, token))
    return found


def test_declared_tools_cover_what_runbook_bodies_actually_use(catalog) -> None:  # type: ignore[no-untyped-def]
    """The Stage A measurement: is `requiredTools` an allowlist yet?

    Filtering treats it as one. A body that says "call `terminal.run.async`"
    while the frontmatter omits it would, under filtering, produce a turn
    instructed to use a tool it was not given — which reads as model failure, not
    as the configuration error it is.

    Exact set equality, so an improvement is reviewed as carefully as a
    regression: a pair that disappears must disappear because the runbook now
    declares the tool, not because the detector stopped seeing it.
    """
    found = _undeclared_references(catalog)
    unexpected = sorted(found - _REVIEWED_UNDECLARED)
    stale = sorted(_REVIEWED_UNDECLARED - found)
    assert not unexpected, (
        "runbook bodies reference tools they do not declare: "
        f"{unexpected}. If the body instructs the call, add it to that runbook's "
        "requiredTools. If it only warns against or explains the tool, add the "
        "pair to _REVIEWED_UNDECLARED with its classification."
    )
    assert not stale, (
        f"_REVIEWED_UNDECLARED lists pairs that no longer occur: {stale}. "
        "Remove them so the set stays an exact record."
    )


def test_the_undeclared_detector_can_actually_see_a_missing_tool(catalog) -> None:  # type: ignore[no-untyped-def]
    """A test for the test. The previous detector was circular and could not.

    Take a runbook that DOES declare its tools, strip one declaration, and the
    reference in its body must surface. Building `known` from declarations made
    this impossible: removing the declaration also removed the tool from the
    vocabulary, and the body reference went quiet.
    """
    from dataclasses import replace

    runbook = next(
        s
        for s in catalog.runbooks
        if any(f"`{tool}" in s.body for tool in (s.meta.required_tools or ()))
    )
    tool = next(t for t in (runbook.meta.required_tools or ()) if f"`{t}" in runbook.body)
    stripped = replace(
        runbook,
        meta=runbook.meta.model_copy(
            update={"required_tools": [t for t in (runbook.meta.required_tools or ()) if t != tool]}
        ),
    )

    class _OneRunbook:
        runbooks = [stripped]

    assert (stripped.id, tool) in _undeclared_references(_OneRunbook())


def test_the_core_set_is_bounded_and_real(catalog) -> None:  # type: ignore[no-untyped-def]
    """Every core entry is a permanent cost on every request, and a name that no
    longer exists in the registry would be dead weight nobody notices.

    The core is not hand-chosen — `test_prompt_hygiene_tools.py` derives it from
    what the always-present base prompt instructs, and that test is what stops it
    shrinking below correctness. This one guards the other direction: the core
    grows only because the PROMPT grew, so a jump well past the number of tools
    the prompt names means something was added here without an instruction behind
    it, and the saving is being given away for nothing.
    """
    # Reuse the hygiene suite's registry loader and dotted->wire translation, so
    # the two cannot disagree about what "exists".
    from test_runbook_catalog_hygiene import _known_tool_names, _wire_name

    known = _known_tool_names()
    assert len(ALWAYS_AVAILABLE) <= 30, "the always-on core is growing; each entry is permanent"
    unknown = sorted(n for n in ALWAYS_AVAILABLE if _wire_name(n) not in known)
    assert not unknown, f"core tools missing from the CLI registry: {unknown}"


# ---------------------------------------------------------------------------
# Against the REAL inventory. This is the test that would have caught the bug.
# ---------------------------------------------------------------------------


def _real_inventory() -> list[ToolDef]:
    import json
    from pathlib import Path

    path = (
        Path(__file__).resolve().parents[2]
        / "benchmarks"
        / "_harness"
        / "fixtures"
        / "cli_tool_inventory.json"
    )
    return [ToolDef.model_validate(t) for t in json.loads(path.read_text(encoding="utf-8"))]


def test_the_projection_matches_the_real_captured_inventory(catalog) -> None:  # type: ignore[no-untyped-def]
    """Every unit test above passed while the projection matched NOTHING.

    Runbooks declare dotted names (`terminal.rename`); the CLI sends wire names
    (`terminal__rename`). Comparing them directly withheld the whole captured inventory — a total
    inversion that only showed up against a real inventory, because the synthetic
    helpers were building dotted names on both sides.
    """
    tools = _real_inventory()
    assert len(tools) > 50, "inventory fixture looks truncated"
    offered = {t.function.name for t in tools}

    runbook = catalog.get("daintree.terminals.rename")
    assert runbook is not None
    projection = project_tools(
        offered=tools, active_runbooks=[runbook], catalog=catalog, messages=_user()
    )

    # The tool this runbook EXISTS to call, spelled literally. A numeric band
    # cannot express that: dropping `terminal.rename` from the frontmatter moves
    # the count by one and stays comfortably inside any range, while filtering
    # would withhold the one tool the runbook is about.
    assert "terminal__rename" in projection.proposed

    # Exact equality, computed independently of the module under test — the
    # translation here is spelled out rather than borrowed from `wire_name`, so a
    # bug in that function cannot make both sides agree with each other.
    foundation_id = catalog.domain_foundation(runbook.id)
    foundation = catalog.get(foundation_id) if foundation_id and foundation_id != runbook.id else None
    expected_dotted = (
        set(ALWAYS_AVAILABLE)
        # A runbook is active, so the loaded-runbooks block renders and instructs the
        # run scaffold. No runbook declares these — they belong to the template.
        | set(RUNBOOK_SCAFFOLD_TOOLS)
        | set(runbook.meta.required_tools or ())
        | set(foundation.meta.required_tools or () if foundation else ())
    )
    expected = offered & {name.replace(".", "__") for name in expected_dotted}
    # Plus everything the catalog has no opinion about. Withholding argues from a
    # runbook's metadata, and there is no metadata about a tool no runbook declares.
    expected |= offered - catalog.declared_tools
    assert projection.proposed == expected

    # And it must still be a real reduction, or there is nothing to gain.
    assert len(projection.proposed) < len(tools) * 0.8


@pytest.mark.parametrize(
    "runbook_id",
    [
        "daintree.terminals.rename",
        "daintree.edits.spawn-visible-agent",
        "daintree.orchestration.supervise-fleet",
        "daintree.workflow.start-work-on-issue",
    ],
)
def test_a_typical_turn_would_shed_a_material_share_of_the_payload(catalog, runbook_id: str) -> None:  # type: ignore[no-untyped-def]
    """The size of the prize, pinned so a metadata change that quietly admits
    everything shows up as a failed test rather than a lost saving."""
    import json

    tools = _real_inventory()
    by_name = {t.function.name: t for t in tools}
    runbook = catalog.get(runbook_id)
    assert runbook is not None

    projection = project_tools(
        offered=tools, active_runbooks=[runbook], catalog=catalog, messages=_user()
    )
    kept = [by_name[n].model_dump(exclude_none=True) for n in projection.proposed if n in by_name]
    full = json.dumps([t.model_dump(exclude_none=True) for t in tools], separators=(",", ":"))
    reduced = json.dumps(kept, separators=(",", ":"))
    saving = 1 - len(reduced) / len(full)
    assert saving > 0.3, f"{runbook_id}: only {saving:.0%} of the tool payload would be shed"


# ---------------------------------------------------------------------------
# Degenerate catalog shapes. Each of these is a real runbook file away.
# ---------------------------------------------------------------------------


def _runbook_with(tools, runbook_id="test.domain.thing", foundation=False):  # type: ignore[no-untyped-def]
    from daintree_assistant_server.runbooks.models import Runbook, RunbookMeta

    fields = {
        "id": runbook_id,
        "title": "T",
        "summary": "S",
        "whenToUse": "W",
        "foundation": foundation,
    }
    # `requiredTools` is OPTIONAL, not nullable: omitting the key and passing []
    # are the two reachable shapes. An explicit null is rejected by the schema.
    if tools is not None:
        fields["requiredTools"] = tools
    return Runbook(meta=RunbookMeta(**fields), body="body")


@pytest.mark.parametrize("declared", [None, []], ids=["key-absent", "empty-list"])
def test_a_runbook_declaring_no_tools_still_gets_the_core(catalog, declared) -> None:  # type: ignore[no-untyped-def]
    """`requiredTools` is optional, so both absent and empty must behave.

    The turn is left with the core alone — correct, and the reason the core
    exists: a runbook that declares nothing still has to be able to orient and
    reach `tool.search` for what it needs.
    """
    tools = _tools("context.snapshot", "tool.search", "forge.getPR")
    projection = project_tools(
        offered=tools,
        active_runbooks=[_runbook_with(declared)],
        catalog=catalog,
        messages=_user(),
    )
    assert wire_name("context.snapshot") in projection.proposed
    assert wire_name("forge.getPR") in projection.withheld


def test_a_declared_tool_the_client_does_not_offer_is_not_invented(catalog) -> None:  # type: ignore[no-untyped-def]
    """A runbook outliving the tool it names must not conjure it back.

    `proposed` is a subset of `offered` by construction; if it ever were not, the
    filtered request would name a tool the client cannot execute.
    """
    tools = _tools("context.snapshot")
    projection = project_tools(
        offered=tools,
        active_runbooks=[_runbook_with(["tool.that.was.removed"])],
        catalog=catalog,
        messages=_user(),
    )
    assert projection.proposed <= projection.offered
    assert wire_name("tool.that.was.removed") not in projection.proposed


def test_a_runbook_that_is_its_own_foundation_does_not_recurse(catalog) -> None:  # type: ignore[no-untyped-def]
    """A foundation selected directly is its own `domain_foundation`."""
    foundations = [s for s in catalog.runbooks if s.meta.foundation]
    assert foundations, "catalog has no foundation runbook to exercise this with"
    foundation = foundations[0]
    declared = list(foundation.meta.required_tools or ()) or ["context.snapshot"]

    projection = project_tools(
        offered=_tools(*declared),
        active_runbooks=[foundation],
        catalog=catalog,
        messages=_user(),
    )
    assert {wire_name(d) for d in declared} <= projection.proposed
    # Admitted once, under its own id — not doubled as its own foundation.
    for name in declared:
        assert not projection.reasons[wire_name(name)].startswith("foundation:")


def test_a_runbook_in_no_known_domain_is_not_a_crash(catalog) -> None:  # type: ignore[no-untyped-def]
    """An id whose domain has no foundation must degrade, not raise."""
    projection = project_tools(
        offered=_tools("context.snapshot", "forge.getPR"),
        active_runbooks=[_runbook_with(["forge.getPR"], runbook_id="nowhere.at.all")],
        catalog=catalog,
        messages=_user(),
    )
    assert wire_name("forge.getPR") in projection.proposed


def test_a_conversation_with_no_user_turn_is_handled(catalog) -> None:  # type: ignore[no-untyped-def]
    """Nothing in the projection requires a user message to exist."""
    projection = project_tools(
        offered=_tools("context.snapshot"),
        active_runbooks=[],
        catalog=catalog,
        messages=[ChatMessage(role="assistant", content="hello")],
    )
    assert wire_name("context.snapshot") in projection.proposed


# ---------------------------------------------------------------------------
# The name translation, against literals rather than against itself.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("dotted", "wire"),
    [
        ("terminal.rename", "terminal__rename"),
        ("terminal.extract.json", "terminal__extract__json"),  # multi-dot
        ("workflow.startWorkOnIssue", "workflow__startWorkOnIssue"),  # camelCase tail
        ("terminal__rename", "terminal__rename"),  # already wire — idempotent
        ("noDotsAtAll", "noDotsAtAll"),
        ("agentTask.spawnForEdits", "agentTask__spawnForEdits"),  # camelCase head
    ],
)
def test_wire_name_translation_is_pinned_to_literals(dotted: str, wire: str) -> None:
    """Hand-written expectations, so the test cannot agree with a broken oracle.

    Every synthetic test in this file builds its offered inventory by calling
    `wire_name`, which means a localized translation bug — one that mangles only
    `terminal.extract.json`, say — leaves both sides of those comparisons equally
    wrong and equally green. These pairs are the independent anchor.
    """
    assert wire_name(dotted) == wire
    assert wire_name(wire_name(dotted)) == wire  # idempotent under repetition


def test_wire_name_matches_the_cli_registrys_own_projection() -> None:
    """The CLI's `toWireName` replaces EVERY dot. Prove ours does too, over the
    whole captured registry rather than over examples."""
    registry = _registry_dotted_names()
    inventory = {t.function.name for t in _real_inventory()}
    assert {wire_name(name) for name in registry} == inventory


def test_the_mandated_core_is_pinned_by_name() -> None:
    """`ALWAYS_AVAILABLE` asserted against literals.

    The other core test derives both the offered inventory and the expectation
    from this same constant, so deleting an entry leaves it green. Every name
    below is instructed by an ALWAYS-PRESENT layer — the base prompt, or the turn
    footer — which is why losing one silently is not a lost optimisation but a
    turn told to call something it was never given.
    `test_prompt_hygiene_tools.py` re-derives this list from those layers' text;
    this one is the human-readable record of what that derivation currently
    yields.
    """
    assert (
        frozenset(
            {
                # Orient.
                "context.snapshot",
                "daintree.status",
                # Read the workspace.
                "fs.read",
                "fs.list",
                "fs.search",
                # Page a large result, from any call.
                "artifact.read",
                # Delegate, and make file changes the only sanctioned way.
                "subagent.run",
                "agentTask.spawnForEdits",
                # Discover and reach an unprojected capability.
                "tool.search",
                "daintree.listTools",
                "daintree.call",
                # Talk to the user; retire what was reported.
                "user.askMultipleChoice",
                "queue.resolve",
                # Speak to work already running.
                "terminal.sendCommand",
                "terminal.close",
                # Instructed in PROSE, so the hygiene scanner cannot see them —
                # `_PROSE_INSTRUCTED` there carries the quote for each.
                "terminal.read",
                "queue.digest",
                # See standing supervision.
                "watcher.list",
                "timer.list",
                "worktree.list",
                # Stop it — instructed by the TURN FOOTER, not the base prompt.
                "async.cancel",
                "watcher.cancel",
                # Also the footer: the workflow-digest block's "call
                # workflow.reconcile", which renders on client-supplied
                # `turn_context.workflow_state` and so is gated by no runbook.
                "workflow.reconcile",
                # Remember.
                "memory.save",
            }
        )
        == ALWAYS_AVAILABLE
    )


def test_the_scaffold_is_admitted_only_with_a_runbook_active(catalog) -> None:  # type: ignore[no-untyped-def]
    """`runbook.step.advance` / `runbook.run.get` follow the block that instructs them.

    No runbook declares either — they belong to `loaded_runbooks.md.j2` — so nothing
    that reads `requiredTools` could admit them, and a runbook-less turn never sees
    the paragraph that names them.

    The WITHHOLDING half is the load-bearing assertion, and it is the one that
    nearly did not work: the unclassified pass-through admits anything the catalog
    has no opinion about, and it has no opinion about these two either. So they
    are classified explicitly, and without that this test would have asserted an
    activation rule that changed nothing on the wire.
    """
    offered = _tools(*RUNBOOK_SCAFFOLD_TOOLS, "terminal.rename")
    runbook = catalog.get("daintree.terminals.rename")
    assert runbook is not None

    with_runbook = project_tools(
        offered=offered, active_runbooks=[runbook], catalog=catalog, messages=_user()
    )
    assert {wire_name(n) for n in RUNBOOK_SCAFFOLD_TOOLS} <= with_runbook.proposed
    assert all(with_runbook.reasons[wire_name(n)] == "runbook_scaffold" for n in RUNBOOK_SCAFFOLD_TOOLS)

    without = project_tools(offered=offered, active_runbooks=[], catalog=catalog, messages=_user())
    assert {wire_name(n) for n in RUNBOOK_SCAFFOLD_TOOLS} <= without.withheld, (
        "a runbook-less turn renders no scaffold paragraph, so it must not pay for "
        "the schemas either — and `unclassified` must not smuggle them back in"
    )


def test_a_tool_no_runbook_declares_is_never_withheld(catalog) -> None:  # type: ignore[no-untyped-def]
    """Silence is not evidence that a turn does not need something.

    Withholding argues from metadata, and the catalog has no metadata about a
    tool nothing declares — a CLI release that ships a tool ahead of the runbook
    using it, or an integration contributing its own. The alternative is a tool
    that is withheld by default and stays withheld until someone notices.
    """
    projection = project_tools(
        offered=_tools("forge.getPR", "nobody.declaresThis"),
        active_runbooks=[],
        catalog=catalog,
        messages=_user(),
    )
    assert projection.reasons[wire_name("nobody.declaresThis")] == "unclassified"
    # And the rule does not swallow the point: a tool the catalog DOES know, that
    # no active runbook needs, is still withheld.
    assert wire_name("forge.getPR") in projection.withheld


# --------------------------------------------------------------------------
# The invariant this module DEPENDS ON, and where it is enforced.
#
# `_unresolved_tool_calls` keys `{call_id: tool_name}` to decide which tools a
# continuation round still needs offered. A dict cannot hold two entries for one
# id, so the function is only correct while a call id is unique among the calls
# that are still OPEN — otherwise the later name overwrites the earlier, and
# under enforcement the projection withholds the tool a pending call needs.
#
# Nothing in this module can repair that: by the time the messages arrive the
# first call's name is gone. The guarantee lives in
# `services/validation._validate_message_sequence`, whose state machine refuses a
# transcript that reuses an open id (pinned in `tests/unit/test_validation.py`).
#
# What is pinned HERE is the positive half — that distinct open calls all
# survive — because that is the property this module actually needs and it stays
# true however the validator is rewritten. An earlier draft asserted the
# corruption itself as the expected result, which made a bug into a contract: a
# future hardening of `_unresolved_tool_calls` would have failed a test for
# improving it.
# --------------------------------------------------------------------------


def test_every_distinct_open_call_keeps_its_tool() -> None:
    """A parallel batch with several calls unanswered must surface EVERY tool, not
    the last one. This is the property enforcement depends on: a name lost here is
    a tool withheld from a model that has already committed to calling it."""
    from daintree_assistant_server.runbooks.tool_projection import _unresolved_tool_calls

    messages = [
        ChatMessage(role="user", content="go"),
        ChatMessage(
            role="assistant",
            tool_calls=[
                ToolCall(id="c1", function=FunctionCall(name="first__tool", arguments="{}")),
                ToolCall(id="c2", function=FunctionCall(name="second__tool", arguments="{}")),
                ToolCall(id="c3", function=FunctionCall(name="third__tool", arguments="{}")),
            ],
        ),
        ChatMessage(role="tool", content="ok", tool_call_id="c2"),
    ]
    assert _unresolved_tool_calls(messages) == {"first__tool", "third__tool"}


def test_the_same_tool_called_twice_survives_one_answer() -> None:
    """Matched by id rather than by name, so answering one call does not strand
    its namesake."""
    from daintree_assistant_server.runbooks.tool_projection import _unresolved_tool_calls

    messages = [
        ChatMessage(role="user", content="go"),
        ChatMessage(
            role="assistant",
            tool_calls=[
                ToolCall(id="c1", function=FunctionCall(name="same__tool", arguments="{}")),
                ToolCall(id="c2", function=FunctionCall(name="same__tool", arguments="{}")),
            ],
        ),
        ChatMessage(role="tool", content="ok", tool_call_id="c1"),
    ]
    assert _unresolved_tool_calls(messages) == {"same__tool"}


def test_request_validation_refuses_a_transcript_that_reuses_an_open_id() -> None:
    """The guarantee, asserted from this side.

    Duplicated deliberately from `tests/unit/test_validation.py`: that suite owns
    the validator's rules, and this one records that THIS module is a consumer of
    one of them. Deleting the rule as "over-strict validation" would fail a test
    that names the mechanism it protects.
    """
    from daintree_assistant_server.config import Settings
    from daintree_assistant_server.contracts.daintree_api import RespondRequest
    from daintree_assistant_server.errors import DaintreeError
    from daintree_assistant_server.services.validation import validate_respond_request

    call = {"id": "c1", "type": "function", "function": {"name": "t", "arguments": "{}"}}
    request = RespondRequest.model_validate({
        "protocol_version": 3,
        "session": {"id": "s", "turn_id": "t"},
        "startup": {},
        "input": {"messages": [
            {"role": "user", "content": "go"},
            {"role": "assistant", "content": None, "tool_calls": [call]},
            {"role": "assistant", "content": None, "tool_calls": [call]},
        ]},
    })
    settings = Settings(BASETEN_API_KEY="sk", STATE_SIGNING_KEY_CURRENT="s")
    with pytest.raises(DaintreeError) as exc:
        validate_respond_request(request, settings)
    assert exc.value.code == "unresolved_tool_calls"
