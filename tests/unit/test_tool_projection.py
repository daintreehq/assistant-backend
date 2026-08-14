"""Skill-aware tool projection — shadow mode.

Tool schemas are ~61% of a request, and filtering them to the active skills'
declared needs is the largest prompt saving available. It is not enabled, and
these tests are mostly about *why not*.

The projection is computed every turn and applied to nothing. What decides
whether it can be applied is `outside_proposed` in the shadow log: how often the
model calls a tool the projection would have withheld. Until that is durably
empty in real usage, filtering would break turns in a way that reads as model
failure rather than configuration.

`test_declared_tools_cover_what_skill_bodies_actually_use` is the offline half of
that measurement. Read what it proves narrowly: it finds backticked registry
names in skill bodies and checks them against the frontmatter. It cannot see a
tool the model reaches for on its own, and it cannot tell an instruction from a
prohibition — which is why its exceptions are a hand-classified set rather than a
count.

Two failures shaped this file, and both were invisible to a green suite:

* the projection compared dotted names to wire names and matched NOTHING, while
  every test passed because the helpers built dotted names on both sides;
* the "20 of 26 skills have undeclared tools" figure it was built to defend was
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
from daintree_assistant_server.skills.catalog import load_catalog
from daintree_assistant_server.skills.tool_projection import (
    ALWAYS_AVAILABLE,
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
    have withheld all 85 tools.
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
    """A turn with NO skill loaded must still be able to orient, find a
    capability it was not given, and ask the user a question — the base prompt
    instructs it to do all three."""
    projection = project_tools(
        offered=_tools(*ALWAYS_AVAILABLE, "git.push"),
        active_skills=[],
        catalog=catalog,
        messages=_user(),
    )
    assert {wire_name(n) for n in ALWAYS_AVAILABLE} <= projection.proposed
    assert wire_name("git.push") in projection.withheld


def test_an_active_skills_declared_tools_are_admitted(catalog) -> None:  # type: ignore[no-untyped-def]
    skill = catalog.get("daintree.terminals.rename")
    assert skill is not None
    declared = list(skill.meta.required_tools or [])
    assert declared, "fixture skill declares no tools"

    projection = project_tools(
        offered=_tools(*declared, "forge.getPR"),
        active_skills=[skill],
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
        active_skills=[specific],
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
        active_skills=[],
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
        active_skills=[],
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
        active_skills=[],
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
        active_skills=[],
        catalog=catalog,
        messages=messages,
    )
    assert wire_name("forge.getPR") in projection.proposed


def test_a_turn_with_no_tools_projects_nothing(catalog) -> None:  # type: ignore[no-untyped-def]
    projection = project_tools(offered=None, active_skills=[], catalog=catalog, messages=_user())
    assert projection.proposed == frozenset()
    assert projection.withheld == frozenset()


def test_the_projection_never_invents_a_tool(catalog) -> None:  # type: ignore[no-untyped-def]
    """It can only ever admit from what the client offered — a proposal naming a
    tool that was not offered would be unfulfillable."""
    skill = catalog.get("daintree.terminals.rename")
    assert skill is not None
    projection = project_tools(
        offered=_tools("context.snapshot"),
        active_skills=[skill],
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
    projection = project_tools(
        offered=_tools("context.snapshot", "git.push", "forge.getPR"),
        active_skills=[],
        catalog=catalog,
        messages=_user(),
    )
    assert projection.outside([wire_name("context.snapshot")]) == []
    assert projection.outside([wire_name("git.push"), wire_name("context.snapshot")]) == [
        wire_name("git.push")
    ]
    assert projection.outside([wire_name("git.push"), wire_name("forge.getPR")]) == [
        wire_name("forge.getPR"),
        wire_name("git.push"),
    ]
    # A tool that was never offered is not a projection failure.
    assert projection.outside(["never.offered"]) == []


# ---------------------------------------------------------------------------
# Stage A: is the metadata trustworthy enough to filter on?
# ---------------------------------------------------------------------------


# Every dotted name in the CLI registry, from the captured inventory. This is the
# AUTHORITY on what a tool is. An earlier version derived it from the skills' own
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
# it in the skill's frontmatter instead.
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
    ("daintree.terminals.rename", "terminal.extract"),
}


def _undeclared_references(catalog) -> set[tuple[str, str]]:  # type: ignore[no-untyped-def]
    registry = _registry_dotted_names()
    found: set[tuple[str, str]] = set()
    for skill in catalog.skills:
        declared = set(skill.meta.required_tools or ())
        for token in _TICKED.findall(skill.body):
            if token in registry and token not in declared:
                found.add((skill.id, token))
    return found


def test_declared_tools_cover_what_skill_bodies_actually_use(catalog) -> None:  # type: ignore[no-untyped-def]
    """The Stage A measurement: is `requiredTools` an allowlist yet?

    Filtering treats it as one. A body that says "call `terminal.run.async`"
    while the frontmatter omits it would, under filtering, produce a turn
    instructed to use a tool it was not given — which reads as model failure, not
    as the configuration error it is.

    Exact set equality, so an improvement is reviewed as carefully as a
    regression: a pair that disappears must disappear because the skill now
    declares the tool, not because the detector stopped seeing it.
    """
    found = _undeclared_references(catalog)
    unexpected = sorted(found - _REVIEWED_UNDECLARED)
    stale = sorted(_REVIEWED_UNDECLARED - found)
    assert not unexpected, (
        "skill bodies reference tools they do not declare: "
        f"{unexpected}. If the body instructs the call, add it to that skill's "
        "requiredTools. If it only warns against or explains the tool, add the "
        "pair to _REVIEWED_UNDECLARED with its classification."
    )
    assert not stale, (
        f"_REVIEWED_UNDECLARED lists pairs that no longer occur: {stale}. "
        "Remove them so the set stays an exact record."
    )


def test_the_undeclared_detector_can_actually_see_a_missing_tool(catalog) -> None:  # type: ignore[no-untyped-def]
    """A test for the test. The previous detector was circular and could not.

    Take a skill that DOES declare its tools, strip one declaration, and the
    reference in its body must surface. Building `known` from declarations made
    this impossible: removing the declaration also removed the tool from the
    vocabulary, and the body reference went quiet.
    """
    from dataclasses import replace

    skill = next(
        s
        for s in catalog.skills
        if any(f"`{tool}" in s.body for tool in (s.meta.required_tools or ()))
    )
    tool = next(t for t in (skill.meta.required_tools or ()) if f"`{t}" in skill.body)
    stripped = replace(
        skill,
        meta=skill.meta.model_copy(
            update={"required_tools": [t for t in (skill.meta.required_tools or ()) if t != tool]}
        ),
    )

    class _OneSkill:
        skills = [stripped]

    assert (stripped.id, tool) in _undeclared_references(_OneSkill())


def test_the_core_set_is_small_and_real(catalog) -> None:  # type: ignore[no-untyped-def]
    """Every core entry is a permanent cost on every request, and a name that no
    longer exists in the registry would be dead weight nobody notices."""
    # Reuse the hygiene suite's registry loader and dotted->wire translation, so
    # the two cannot disagree about what "exists".
    from test_skill_catalog_hygiene import _known_tool_names, _wire_name

    known = _known_tool_names()
    assert len(ALWAYS_AVAILABLE) <= 8, "the always-on core is growing; each entry is permanent"
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

    Skills declare dotted names (`terminal.rename`); the CLI sends wire names
    (`terminal__rename`). Comparing them directly withheld all 85 tools — a total
    inversion that only showed up against a real inventory, because the synthetic
    helpers were building dotted names on both sides.
    """
    tools = _real_inventory()
    assert len(tools) > 50, "inventory fixture looks truncated"
    offered = {t.function.name for t in tools}

    skill = catalog.get("daintree.terminals.rename")
    assert skill is not None
    projection = project_tools(
        offered=tools, active_skills=[skill], catalog=catalog, messages=_user()
    )

    # The tool this runbook EXISTS to call, spelled literally. A numeric band
    # cannot express that: dropping `terminal.rename` from the frontmatter moves
    # the count by one and stays comfortably inside any range, while filtering
    # would withhold the one tool the skill is about.
    assert "terminal__rename" in projection.proposed

    # Exact equality, computed independently of the module under test — the
    # translation here is spelled out rather than borrowed from `wire_name`, so a
    # bug in that function cannot make both sides agree with each other.
    foundation_id = catalog.domain_foundation(skill.id)
    foundation = catalog.get(foundation_id) if foundation_id and foundation_id != skill.id else None
    expected_dotted = (
        set(ALWAYS_AVAILABLE)
        | set(skill.meta.required_tools or ())
        | set(foundation.meta.required_tools or () if foundation else ())
    )
    expected = offered & {name.replace(".", "__") for name in expected_dotted}
    assert projection.proposed == expected

    # And it must still be a real reduction, or there is nothing to gain.
    assert len(projection.proposed) < len(tools) * 0.8


@pytest.mark.parametrize(
    "skill_id",
    [
        "daintree.terminals.rename",
        "daintree.edits.spawn-visible-agent",
        "daintree.orchestration.supervise-fleet",
        "daintree.workflow.start-work-on-issue",
    ],
)
def test_a_typical_turn_would_shed_a_material_share_of_the_payload(catalog, skill_id: str) -> None:  # type: ignore[no-untyped-def]
    """The size of the prize, pinned so a metadata change that quietly admits
    everything shows up as a failed test rather than a lost saving."""
    import json

    tools = _real_inventory()
    by_name = {t.function.name: t for t in tools}
    skill = catalog.get(skill_id)
    assert skill is not None

    projection = project_tools(
        offered=tools, active_skills=[skill], catalog=catalog, messages=_user()
    )
    kept = [by_name[n].model_dump(exclude_none=True) for n in projection.proposed if n in by_name]
    full = json.dumps([t.model_dump(exclude_none=True) for t in tools], separators=(",", ":"))
    reduced = json.dumps(kept, separators=(",", ":"))
    saving = 1 - len(reduced) / len(full)
    assert saving > 0.3, f"{skill_id}: only {saving:.0%} of the tool payload would be shed"


# ---------------------------------------------------------------------------
# Degenerate catalog shapes. Each of these is a real skill file away.
# ---------------------------------------------------------------------------


def _skill_with(tools, skill_id="test.domain.thing", foundation=False):  # type: ignore[no-untyped-def]
    from daintree_assistant_server.skills.models import Skill, SkillMeta

    fields = {
        "id": skill_id,
        "title": "T",
        "summary": "S",
        "whenToUse": "W",
        "foundation": foundation,
    }
    # `requiredTools` is OPTIONAL, not nullable: omitting the key and passing []
    # are the two reachable shapes. An explicit null is rejected by the schema.
    if tools is not None:
        fields["requiredTools"] = tools
    return Skill(meta=SkillMeta(**fields), body="body")


@pytest.mark.parametrize("declared", [None, []], ids=["key-absent", "empty-list"])
def test_a_skill_declaring_no_tools_still_gets_the_core(catalog, declared) -> None:  # type: ignore[no-untyped-def]
    """`requiredTools` is optional, so both absent and empty must behave.

    The turn is left with the core alone — correct, and the reason the core
    exists: a skill that declares nothing still has to be able to orient and
    reach `tool.search` for what it needs.
    """
    tools = _tools("context.snapshot", "tool.search", "git.push")
    projection = project_tools(
        offered=tools,
        active_skills=[_skill_with(declared)],
        catalog=catalog,
        messages=_user(),
    )
    assert wire_name("context.snapshot") in projection.proposed
    assert wire_name("git.push") in projection.withheld


def test_a_declared_tool_the_client_does_not_offer_is_not_invented(catalog) -> None:  # type: ignore[no-untyped-def]
    """A skill outliving the tool it names must not conjure it back.

    `proposed` is a subset of `offered` by construction; if it ever were not, the
    filtered request would name a tool the client cannot execute.
    """
    tools = _tools("context.snapshot")
    projection = project_tools(
        offered=tools,
        active_skills=[_skill_with(["tool.that.was.removed"])],
        catalog=catalog,
        messages=_user(),
    )
    assert projection.proposed <= projection.offered
    assert wire_name("tool.that.was.removed") not in projection.proposed


def test_a_skill_that_is_its_own_foundation_does_not_recurse(catalog) -> None:  # type: ignore[no-untyped-def]
    """A foundation selected directly is its own `domain_foundation`."""
    foundations = [s for s in catalog.skills if s.meta.foundation]
    assert foundations, "catalog has no foundation skill to exercise this with"
    foundation = foundations[0]
    declared = list(foundation.meta.required_tools or ()) or ["context.snapshot"]

    projection = project_tools(
        offered=_tools(*declared),
        active_skills=[foundation],
        catalog=catalog,
        messages=_user(),
    )
    assert {wire_name(d) for d in declared} <= projection.proposed
    # Admitted once, under its own id — not doubled as its own foundation.
    for name in declared:
        assert not projection.reasons[wire_name(name)].startswith("foundation:")


def test_a_skill_in_no_known_domain_is_not_a_crash(catalog) -> None:  # type: ignore[no-untyped-def]
    """An id whose domain has no foundation must degrade, not raise."""
    projection = project_tools(
        offered=_tools("context.snapshot", "git.push"),
        active_skills=[_skill_with(["git.push"], skill_id="nowhere.at.all")],
        catalog=catalog,
        messages=_user(),
    )
    assert wire_name("git.push") in projection.proposed


def test_a_conversation_with_no_user_turn_is_handled(catalog) -> None:  # type: ignore[no-untyped-def]
    """Nothing in the projection requires a user message to exist."""
    projection = project_tools(
        offered=_tools("context.snapshot"),
        active_skills=[],
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
    from this same constant, so deleting an entry leaves it green. These are the
    capabilities the base prompt tells the model it always has; losing one
    silently would strand a skill-less turn with no way to orient or ask.
    """
    assert (
        frozenset(
            {
                "context.snapshot",
                "daintree.status",
                "tool.search",
                "daintree.listTools",
                "user.askMultipleChoice",
            }
        )
        == ALWAYS_AVAILABLE
    )
