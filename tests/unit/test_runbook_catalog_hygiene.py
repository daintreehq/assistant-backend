"""Catalog hygiene checks that the loader deliberately does NOT enforce.

``RunbookMeta`` validates ``requiredTools`` and ``optionalTools`` against a character
regex only; nothing at load time checks that a name is a real Daintree app action.
That belongs here rather than in ``runbooks/loader.py``: a runtime check against a
pinned capture would fail ``/readyz`` the moment the app legitimately renames or adds
an action, inverting the repo's fail-open (selection) / fail-closed (malformed
runbook) split.

The authority is ``tests/fixtures/daintree_mcp/``: the tool surfaces the Daintree app
actually lists to each kind of caller, captured from one app commit (its README says
which, and how to refresh). The names in frontmatter are action ids, which are also
the wire names the app lists.

Every current runbook is checked against the current app capture. Archived inbox
procedures are never admitted through this contract.
"""

from __future__ import annotations

import re

import pytest
from daintree_mcp_surfaces import (
    CALLER_POLICY_BY_MODEL,
    GLOBAL_SURFACES,
    GLOBAL_TIER_SURFACE,
    HOST_CONTROL_TOOLS,
    MESSAGING_TOOLS,
    MODELS,
    PANE_CALLERS,
    RELAYED_SURFACE,
    SURFACES_BY_MODEL,
    Model,
    action_ids,
    baseline_callers,
    baseline_violation,
    capture,
    known_action_ids,
    listed_action_ids,
    rebuild_delta,
)

from daintree_assistant_server.runbooks.catalog import Catalog, load_catalog
from daintree_assistant_server.runbooks.local.documents import (
    runbook_document,
    split_when_to_use,
)


def _catalog(model: Model) -> Catalog:
    return load_catalog()


@pytest.mark.parametrize("model", MODELS)
def test_the_captures_are_one_app_build_and_not_truncated(model: Model) -> None:
    """Every surface file of a generation, one capture. A half refresh — one surface
    re-captured from a newer app, the others left behind — would let a runbook validate
    against a caller that never existed. The floor catches a truncated capture that
    would make every check below pass vacuously."""
    commits = set()
    for surface in SURFACES_BY_MODEL[model]:
        recorded = capture(surface, model)["capture"]
        commits.add(recorded["app_commit"])
        assert len(action_ids(surface, model)) >= 20, f"{surface} capture looks truncated"
    assert len(commits) == 1, f"the surface captures come from different app builds: {commits}"
    # Every API-key tool is also a pane tool; external is curated, not different.
    assert action_ids("external", model) <= action_ids("pane-core", model)


@pytest.mark.parametrize("model", MODELS)
def test_declared_tools_are_real_app_actions(model: Model) -> None:
    known = known_action_ids(model)
    unknown: list[str] = []
    for runbook in _catalog(model).runbooks:
        for tool in (*runbook.meta.required_tools, *runbook.meta.optional_tools):
            if tool not in known:
                unknown.append(f"{runbook.id}: {tool}")
    assert not unknown, (
        "requiredTools/optionalTools names that no captured Daintree caller is offered: "
        f"{unknown}. Use the app's action id (tests/fixtures/daintree_mcp), or refresh the "
        "capture if the app added the action."
    )


@pytest.mark.parametrize("model", MODELS)
def test_required_tools_are_on_the_callers_surface(model: Model) -> None:
    """The documented baseline path is the one its caller can take, checked against
    that caller's capture. The caller comes from the runbook's id namespace
    (`CALLER_POLICY`), never from its tool lists.

    Every runbook outside a listed domain is followed by Claude Code or Codex in an
    agent pane, so its `requiredTools` must be on `pane-core`. An action only the
    sidebar assistant is offered (the unscoped send and close) belongs in
    `optionalTools`, as the caller variant it is.

    `daintree.hosts` is the one sidebar-only domain: an agent pane is never offered
    `host.*`. A hosts runbook's whole baseline must be offered to ONE of its callers:
    the Shell's assistant at either Tool set.
    """
    violations = [
        violation
        for runbook in _catalog(model).runbooks
        if (violation := baseline_violation(runbook.id, runbook.meta.required_tools, model))
    ]
    assert not violations, f"required tools the runbook's caller is not offered: {violations}"


@pytest.mark.parametrize("model", MODELS)
def test_supporting_runbooks_run_on_the_same_caller(model: Model) -> None:
    """A runbook is delivered with its domain foundation and its `requires`, and the
    caller follows all of them, so the bundle's baselines together must still run on
    one of this runbook's callers. A pane runbook must not pull sidebar-only
    instructions into its baseline through `requires`. The control hosts foundation, which a connected caller gets with every
    runbook, therefore requires nothing at all: a pane follows it too."""
    catalog = _catalog(model)
    violations = []
    for runbook in catalog.runbooks:
        bundle = [runbook, *catalog.supporting_for([runbook.id])]
        required = sorted({tool for member in bundle for tool in member.meta.required_tools})
        if violation := baseline_violation(runbook.id, required, model):
            violations.append(violation)
    assert not violations, f"bundles no single caller can follow: {violations}"
    hosts_foundation = load_catalog().get("daintree.hosts.foundation")
    assert hosts_foundation is not None and not hosts_foundation.meta.required_tools
    assert baseline_violation("daintree.release.example", ["terminal.list", "host.list"])


def test_the_caller_policy_names_its_two_assistant_only_domains() -> None:
    """Pinned, so widening who escapes the pane check is a deliberate edit here: the
    hosts domain is the Shell assistant's, and the assistants domain is Daintree's own
    assistant's, the global one first."""
    assert PANE_CALLERS == ("pane-core",)
    assert CALLER_POLICY_BY_MODEL == {
        "control": {
            "daintree.hosts": ("help-core", "help-full"),
            "daintree.assistants": ("global-core", "global-full", "help-core", "help-full"),
        },
    }


@pytest.mark.parametrize("model", MODELS)
def test_every_other_runbook_is_still_checked_against_the_pane(model: Model) -> None:
    """The policy exempts nothing it does not name: every runbook outside
    `daintree.hosts` and `daintree.assistants` keeps the pane check, whatever tools it
    declares."""
    catalog = _catalog(model)
    off_pane = [rid for rid in catalog.known_ids if baseline_callers(rid, model) != PANE_CALLERS]
    assert all(rid.startswith(("daintree.hosts.", "daintree.assistants.")) for rid in off_pane), (
        off_pane
    )
    for rid in (
        "daintree.hostsx.ask",
        "daintree.orchestration.hosts",
        "daintree.host.ask",
        "daintree.assistant.resume",
        "daintree.assistantsx.resume",
        "daintree.orchestration.assistants",
    ):
        assert baseline_callers(rid, model) == PANE_CALLERS, rid


def test_metadata_cannot_make_a_runbook_a_hosts_runbook() -> None:
    """Declaring host-control tools never moves a runbook off the pane check: outside
    `daintree.hosts` they can only ever be `optionalTools`, the sidebar variant of a
    pane runbook. And a hosts runbook's baseline cannot borrow a pane's owned tools."""
    for tool in sorted(HOST_CONTROL_TOOLS | {"assistant.list", "assistant.start"}):
        assert baseline_violation("daintree.release.ask-a-host", [tool]) is not None, tool
        assert baseline_violation("daintree.release.resume-everywhere", [tool]) is not None
    hosts = "daintree.hosts.example"
    assert baseline_violation(hosts, ["host.list", "host.call"]) is None
    assert baseline_violation(hosts, ["host.list", "project.openOnHost"]) is None
    assert baseline_violation(hosts, ["host.call", "terminal.sendCommand"]) is None
    assert baseline_violation(hosts, ["terminal.sendCommandOwned"]) is not None

    off_caller = [
        f"{runbook.id}: {tool}"
        for runbook in load_catalog().runbooks
        if baseline_callers(runbook.id) != PANE_CALLERS
        for tool in runbook.meta.optional_tools
        if not any(tool in action_ids(caller) for caller in baseline_callers(runbook.id))
    ]
    assert not off_caller, f"optional tools no caller of the runbook is offered: {off_caller}"


def test_only_the_shells_own_assistant_controls_a_host() -> None:
    """`host.list` and `host.call` reach another machine, so only this machine's own
    assistant sessions are offered them: never a pane or an API-key client, and never
    the session a host opens for a Shell's call, so a call cannot chain. Neither
    model's inbox tool survives anywhere."""
    for surface in SURFACES_BY_MODEL["control"]:
        offered = action_ids(surface)
        assert not offered & MESSAGING_TOOLS - {"host.list"}, surface
        if surface in ("help-core", "help-full"):
            assert offered >= HOST_CONTROL_TOOLS, surface
            assert {"assistant.list", "assistant.start", "agent.readQuota"} <= offered
        else:
            assert not offered & HOST_CONTROL_TOOLS, surface
    for surface in ("relayed-core", "relayed-full"):
        offered = action_ids(surface)
        assert capture(surface)["capture"]["session"]["relay"] is True
        assert not {tool for tool in offered if tool.startswith("host.")}, surface
        assert {"assistant.list", "assistant.start", "terminal.sendCommandOwned"} <= offered
    # The command runner answers to its caller, not the tier: the Shell's own assistant
    # and a relay at either in-app tier, never a pane or an API-key client.
    for surface in ("help-core", "help-full", "relayed-core", "relayed-full"):
        assert "project.runCommand" in action_ids(surface), surface
    for surface in ("pane-core", "pane-full", "external"):
        assert "project.runCommand" not in action_ids(surface), surface


def test_a_host_call_reaches_the_relayed_surface_of_its_own_tier() -> None:
    """What `host.call` can name is what the host lists to a relay at the caller's
    Tool set: the same app build, and the tier the call carries."""
    for help_surface, relayed in (("help-core", "relayed-core"), ("help-full", "relayed-full")):
        assert capture(relayed)["capture"]["tier"] == capture(help_surface)["capture"]["tier"]
    schema = capture("help-core")["tools"]
    call = next(t for t in schema if t["action_id"] == "host.call")
    assert call["inputSchema"]["required"] == ["hostId", "projectId", "tool"]


def test_a_delta_that_does_not_apply_is_refused() -> None:
    """The rebuild refuses any delta it cannot apply exactly, so a hand edit to a
    delta file cannot drop or duplicate part of a surface unnoticed."""
    base = [{"action_id": "a.one"}, {"action_id": "a.two"}]
    ok = {
        "added": [{"action_id": "b.new"}],
        "changed": [{"action_id": "a.two", "x": 1}],
        "removed": ["a.one"],
    }
    assert [t["action_id"] for t in rebuild_delta(base, ok)] == ["a.two", "b.new"]
    bad = [
        {"added": [], "changed": [{"action_id": "z.gone"}], "removed": []},
        {"added": [], "changed": [], "removed": ["z.gone"]},
        {"added": [{"action_id": "a.one"}], "changed": [], "removed": []},
        {"added": [], "changed": [{"action_id": "a.one"}], "removed": ["a.one"]},
        {"added": [{"action_id": "b.new"}, {"action_id": "b.new"}], "changed": [], "removed": []},
    ]
    for delta in bad:
        with pytest.raises(ValueError):
            rebuild_delta(base, delta)


# ---------------------------------------------------------------------------
# Routing-graph references.
#
# This catalog's routing quality does not live in the positive clause of a
# whenToUse; it lives in the carve-outs after it — "do not use for X, use
# `daintree.<other>` instead". Each one is a CLAIM that a specific confusion
# exists and that the text resolves it, and `bench coverage` gates every such
# claim on having a routing trap that tests it.
#
# That gate can only see carve-outs that NAME AN ID. Two ways to slip past it,
# both silent:
#
#   * a reference to an id that does not exist — a typo, or a runbook that was
#     renamed or deleted. It resolves to nothing, the coverage gate does not
#     count it as a neighbour, and the runbook reads as owing no trap.
#   * a carve-out written in shorthand — "use the competition runbook". A human
#     reads it fine; the gate reads a runbook with no declared neighbours.
#
# Both leave an untested confusion claim in the catalog, and both look correct
# in review. These count them.
# ---------------------------------------------------------------------------

#: Anything shaped like a dotted Daintree identifier — INCLUDING malformed ones.
#:
#: ``[a-z0-9_-]`` rather than ``[a-z0-9-]`` on purpose. A typo like
#: ``daintree.orchestration.multi_agent`` would otherwise match only as far as
#: ``daintree.orchestration``, which is a real namespace, and the exemption below
#: would wave it through — a dangling reference hidden by the rule that exists to
#: allow legitimate namespace prose.
_DOTTED = re.compile(r"\bdaintree(?:\.[a-z0-9][a-z0-9_-]*)+\b")

#: A carve-out that DEFERS to another runbook: "…, use <target>".
#:
#: Matches whatever follows, so the target can be checked rather than assumed.
#: The first version of this looked only for the exact phrase "use the X runbook"
#: and reported zero once those were fixed — while ten more carve-outs said
#: "use poll-pinned-agents" in a spelling it could not see. A check that
#: recognises one wording measures that wording, not the property.
_DEFERRAL = re.compile(
    r"\buse\s+(?:the\s+)?([a-z0-9][a-z0-9 ./_-]{2,60}?)(?=[,.;)]|\s+runbook\b|$)"
)

#: Carve-out targets that are not a fully-qualified id. ZERO, and it stays there.
#:
#: It was 27 across two spellings, and `bench lint` showed why that mattered:
#: ranking every runbook pair by how alike the retriever finds them, the closest
#: pairs with no id-level edge were almost exactly these —
#: competition/runoff-tiebreaker (the closest pair in the catalog),
#: poll-pinned-agents/competition, conversation/multi-agent. The catalog's most
#: confusable neighbours were the ones written in a spelling its own tooling
#: could not parse.
#:
#: Rewriting them left routing untouched: every one of the 177 benchmark cases
#: returned a byte-identical selected-id list before and after.
UNQUALIFIED_DEFERRALS = 0


def _tool_namespace_names(model: Model) -> set[str]:
    """App action ids, which are dotted like runbook ids and legitimately quoted in
    runbook prose."""
    return known_action_ids(model)


@pytest.mark.parametrize("model", MODELS)
def test_every_dotted_reference_in_a_whenToUse_resolves(model: Model) -> None:
    """A carve-out that points at nothing is an untestable claim.

    Three things legitimately wear this shape and all three are accepted: a real
    runbook id, a real NAMESPACE with children under it ("the relevant
    daintree.orchestration runbook"), and a CLI tool quoted by name
    (``terminal.getOutput``). Anything else is a dangling reference — a typo, or a
    runbook that was renamed or deleted, leaving a claim nothing can test.
    """
    catalog = _catalog(model)
    known = set(catalog.known_ids)
    namespaces = {sid.rsplit(".", 1)[0] for sid in known}
    tools = _tool_namespace_names(model)

    dangling: list[str] = []
    for runbook in catalog.runbooks:
        for ref in _DOTTED.findall(runbook.meta.when_to_use):
            if ref in known or ref in namespaces or ref in tools:
                continue
            dangling.append(f"{runbook.id} -> {ref}")
    assert not dangling, (
        "whenToUse references that resolve to no runbook, namespace or tool: " + ", ".join(dangling)
    )


@pytest.mark.parametrize("model", MODELS)
def test_every_carve_out_defers_to_a_fully_qualified_id(model: Model) -> None:
    """ "use competition" is invisible to the routing-trap gate; "use
    daintree.orchestration.competition" is not.

    ``bench coverage`` parses declared neighbours by scanning a whenToUse for
    other catalog ids, so a carve-out written as a bare leaf name or as prose
    reads to it as a runbook claiming no confusion at all. The claim is still
    there, still asserted to a human reader, and now untestable.

    Bare namespaces and tool names are allowed as deferral targets only because
    they are legitimate prose ("the relevant daintree.orchestration runbook"); they
    create no coverage edge either, which is why they are counted in the message
    below rather than silently accepted.
    """
    catalog = _catalog(model)
    known = set(catalog.known_ids)
    leaves = {sid.rsplit(".", 1)[-1] for sid in known}
    namespaces = {sid.rsplit(".", 1)[0] for sid in known}
    tools = _tool_namespace_names(model)

    unqualified: list[str] = []
    for runbook in catalog.runbooks:
        for target in _DEFERRAL.findall(runbook.meta.when_to_use):
            target = target.strip()
            if target in known or target in namespaces or target in tools:
                continue
            # Only flag targets that plainly NAME a runbook: a bare leaf id, or a
            # phrase built from one. Ordinary English after "use" — "use one
            # agent", "use it for" — is prose, not a deferral.
            words = set(re.split(r"[ /-]+", target))
            if target in leaves or (words & leaves):
                unqualified.append(f"{runbook.id}: use {target}")

    assert len(unqualified) <= UNQUALIFIED_DEFERRALS, (
        f"{len(unqualified)} carve-outs defer to something other than a full "
        f"runbook id, so `bench coverage` sees no neighbour and demands no trap:\n  "
        + "\n  ".join(sorted(unqualified)[:12])
    )


@pytest.mark.parametrize("model", MODELS)
def test_no_two_runbooks_share_a_routing_surface(model: Model) -> None:
    """Two identical routing documents are one vector in two shortlist slots.

    The selector embeds title + summary + whenToUse and never reads a body, so
    two runbooks whose metadata matches are indistinguishable to it however
    different their procedures are — it will rank them together, and the
    cardinality head will be asked to choose between two things it cannot tell
    apart.
    """
    catalog = _catalog(model)
    seen: dict[str, str] = {}
    clashes: list[str] = []
    for runbook in catalog.runbooks:
        document = runbook_document(runbook.meta)
        if document in seen:
            clashes.append(f"{seen[document]} == {runbook.id}")
        seen[document] = runbook.id
    assert not clashes, f"runbooks with identical routing surfaces: {clashes}"


@pytest.mark.parametrize("model", MODELS)
def test_no_runbooks_positive_trigger_is_contained_in_anothers(model: Model) -> None:
    """A copy-and-extend detector, which is what it actually is.

    Compares the 5+-letter word sets of the POSITIVE clauses — the carve-outs
    after them are supposed to overlap, that is what makes them carve-outs — and
    fires when one is a strict subset of another. It is not vacuous: the smallest
    trigger in the catalog is seven words, so a runbook written by copying a
    neighbour's trigger and adding to it would trip it.

    Claimed narrowly on purpose. Word-set containment is not semantic
    containment: this drops the short words that often carry the distinction
    ("tie", "poll", "one", "not"), and three runbooks use an exclusion marker the
    splitter does not recognise ("Never load"), so their "positive" set includes
    routing prose. Read a failure as "these two triggers were probably written
    from each other", not as proof that one owns no requests.
    """
    catalog = _catalog(model)
    triggers = {
        runbook.id: set(
            re.findall(r"[a-z]{5,}", split_when_to_use(runbook.meta.when_to_use)[0].lower())
        )
        for runbook in catalog.runbooks
    }
    contained = [
        f"{a} ⊆ {b}"
        for a, wa in triggers.items()
        for b, wb in triggers.items()
        if a != b and wa and wa < wb
    ]
    assert not contained, f"runbooks with no unique positive region: {contained}"


def test_the_global_assistant_is_its_own_caller() -> None:
    """The global Assistant is captured from the same build, offered only its root
    tools, and reaches projects through `host.call` alone, under an allow-list that
    keeps the command runner, the session resume and every unscoped tool from it. An
    assistants runbook's baseline, its foundation included, must run on one of its
    callers: the global one counts what its `host.call` reaches, never a pane's tools."""
    commit = capture("help-core")["capture"]["app_commit"]
    for surface in GLOBAL_SURFACES:
        recorded = capture(surface)["capture"]
        assert recorded["app_commit"] == commit, surface
        assert recorded["caller_origin"] == "global", surface
        assert recorded["tier"] == capture(GLOBAL_TIER_SURFACE[surface])["capture"]["tier"]
        assert set(recorded["root_tools"]) == {"host.list", "host.call", "host.settings"}
        assert action_ids(surface) == set(recorded["root_tools"]), surface
        admits = set(recorded["host_call"]["admits"])
        assert not admits & set(recorded["host_call"]["withheld"]), surface
        assert {"project.runCommand", "project.runCheck", "agentSessionHistory.resume"} <= set(
            recorded["host_call"]["withheld"]
        )
        reached = action_ids(RELAYED_SURFACE[surface])
        assert reached <= admits and reached <= action_ids(
            {"global-core": "relayed-core", "global-full": "relayed-full"}[surface]
        )
        assert not reached & {"terminal.sendCommand", "terminal.close", "project.runCommand"}
        assert {"terminal.list", "terminal.sendCommandOwned", "agent.launch"} <= reached
    assistants = "daintree.assistants.example"
    assert baseline_violation(assistants, ["host.list", "host.call"]) is None
    # The foundation's terminal reads, through host.call.
    assert baseline_violation(assistants, ["host.call", "terminal.list"]) is None
    assert baseline_violation(assistants, ["terminal.sendCommandOwned"]) is None
    # A project Assistant runs the unscoped send in its own project; the global one
    # only the owned send, through host.call. A baseline needing both runs nowhere.
    assert baseline_violation(assistants, ["terminal.sendCommand"]) is None
    assert baseline_violation(assistants, ["terminal.sendCommand", "terminal.sendCommandOwned"])
    # And a pane runbook still cannot borrow what host.call reaches for the global caller.
    assert baseline_violation("daintree.release.example", ["host.call", "terminal.list"])


def test_the_global_assistants_listing_gap_stays_visible() -> None:
    """What the global Assistant lists is recorded apart from what it may call: at the
    captured commit its `tools/list` is empty, and the tests read its root tools'
    schemas from the help surface rather than pretend they were listed. When a capture
    lists them, `root_tools_listed` says so and the note goes."""
    for surface in GLOBAL_SURFACES:
        recorded = capture(surface)["capture"]
        listed = listed_action_ids(surface)
        assert listed <= set(recorded["root_tools"]), surface
        assert sorted(listed) == recorded["root_tools_listed"], surface
        unlisted = set(recorded["root_tools"]) - listed
        noted = any("does not carry" in note for note in recorded.get("notes", []))
        assert noted == bool(unlisted), surface
        offered = recorded["host_control"]
        assert offered["assistant_tools_offered"] is False, surface
        assert offered["command_runner_offered"] is False, surface


def test_the_runbooks_match_what_daintree_provisions_the_global_assistant() -> None:
    """Native setup, captured from the app: the runbook server and its headers, the
    instructions that drop the project Assistant's owned-to-unscoped translation, and
    the checkpoint folder the foundation names, which is not the scratch folder."""
    catalog = load_catalog()
    foundation = catalog.get("daintree.foundation")
    assert foundation is not None
    for surface in GLOBAL_SURFACES:
        setup = capture(surface)["capture"]["native_setup"]
        assert setup["runbooks_mcp_server"] == "daintree-runbooks"
        assert setup["runbooks_headers_connected"]["Daintree-Hosts"] == "connected"
        addendum = setup["runbooks_addendum"]
        assert "you have no project of your own" in addendum
        assert "`terminal.sendCommandOwned`" in addendum and "as written" in addendum
        assert "yours is `terminal.sendCommand`" not in addendum
        assert setup["checkpoint_bucket"] == "global"
        assert f"`${setup['checkpoint_env']}`" in foundation.body
        assert setup["scratch_env"] not in foundation.body
        assert setup["checkpoint_env"] in setup["checkpoint_addendum"]
