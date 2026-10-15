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
"""

from __future__ import annotations

import re

from daintree_mcp_surfaces import (
    CALLER_POLICY,
    HUB_TOOLS,
    LANE_TOOLS,
    MESSAGING_TOOLS,
    PANE_CALLERS,
    SURFACES,
    action_ids,
    baseline_callers,
    baseline_violation,
    capture,
    known_action_ids,
)

from daintree_assistant_server.runbooks.catalog import default_runbooks_dir, load_catalog
from daintree_assistant_server.runbooks.local.documents import (
    runbook_document,
    split_when_to_use,
)


def test_the_captures_are_one_app_build_and_not_truncated() -> None:
    """Every surface file, one capture. A half refresh — one surface re-captured from a
    newer app, the others left behind — would let a runbook validate against a caller
    that never existed. The floor catches a truncated capture that would make every check
    below pass vacuously."""
    commits = set()
    for surface in SURFACES:
        recorded = capture(surface)["capture"]
        commits.add(recorded["app_commit"])
        assert len(action_ids(surface)) >= 20, f"{surface} capture looks truncated"
    assert len(commits) == 1, f"the surface captures come from different app builds: {commits}"
    # Every API-key tool is also a pane tool; external is curated, not different.
    assert action_ids("external") <= action_ids("pane-core")


def test_declared_tools_are_real_app_actions() -> None:
    known = known_action_ids()
    unknown: list[str] = []
    for runbook in load_catalog().runbooks:
        for tool in (*runbook.meta.required_tools, *runbook.meta.optional_tools):
            if tool not in known:
                unknown.append(f"{runbook.id}: {tool}")
    assert not unknown, (
        "requiredTools/optionalTools names that no captured Daintree caller is offered: "
        f"{unknown}. Use the app's action id (tests/fixtures/daintree_mcp), or refresh the "
        "capture if the app added the action."
    )


def test_required_tools_are_on_the_callers_surface() -> None:
    """The documented baseline path is the one its caller can take, checked against
    that caller's capture. The caller comes from the runbook's id namespace
    (`CALLER_POLICY`), never from its tool lists.

    Every runbook outside a listed domain is followed by Claude Code or Codex in an
    agent pane, so its `requiredTools` must be on `pane-core`. An action only the
    sidebar assistant is offered (the unscoped send and close) belongs in
    `optionalTools`, as the caller variant it is.

    `daintree.hosts` is the one sidebar-only domain: an agent pane is never a hub or
    an enrolled lane, and is never offered `host.*` or a lane's inbox. A hosts
    runbook's whole baseline must be offered to ONE of its callers, the hub
    (`hub-full`) or an enrolled lane (`lane-full`).
    """
    violations = [
        violation
        for runbook in load_catalog().runbooks
        if (violation := baseline_violation(runbook.id, runbook.meta.required_tools))
    ]
    assert not violations, f"required tools the runbook's caller is not offered: {violations}"


def test_supporting_runbooks_run_on_the_same_caller() -> None:
    """A runbook is delivered with its domain foundation and its `requires`, and the
    caller follows all of them, so the bundle's baselines together must still run on
    one of this runbook's callers. Otherwise a pane runbook could pull in hub-only
    instructions through `requires`, or a lane runbook inherit a hub-only foundation
    baseline."""
    catalog = load_catalog()
    violations = []
    for runbook in catalog.runbooks:
        bundle = [runbook, *catalog.supporting_for([runbook.id])]
        required = sorted({tool for member in bundle for tool in member.meta.required_tools})
        if violation := baseline_violation(runbook.id, required):
            violations.append(violation)
    assert not violations, f"bundles no single caller can follow: {violations}"
    # What this catches: a hub-only requirement reaching a lane or a pane bundle.
    assert baseline_violation("daintree.hosts.example", ["assistant.sendMessage", "host.list"])
    assert baseline_violation("daintree.release.example", ["terminal.list", "host.list"])


def test_the_caller_policy_names_one_sidebar_only_domain() -> None:
    """Pinned, so widening who escapes the pane check is a deliberate edit here."""
    assert PANE_CALLERS == ("pane-core",)
    assert CALLER_POLICY == {"daintree.hosts": ("hub-full", "lane-full")}


def test_every_other_runbook_is_still_checked_against_the_pane() -> None:
    """The policy exempts nothing it does not name: every runbook outside
    `daintree.hosts` keeps the pane check, whatever tools it declares."""
    catalog = load_catalog()
    off_pane = [rid for rid in catalog.known_ids if baseline_callers(rid) != PANE_CALLERS]
    assert all(rid.startswith("daintree.hosts.") for rid in off_pane), off_pane
    for rid in ("daintree.hostsx.ask", "daintree.orchestration.hosts", "daintree.host.ask"):
        assert baseline_callers(rid) == PANE_CALLERS, rid


def test_metadata_cannot_make_a_runbook_a_hub_or_lane_runbook() -> None:
    """Declaring hub or lane tools never moves a runbook off the pane check: outside
    `daintree.hosts` they can only ever be `optionalTools`, the sidebar hub variant
    of a pane runbook (like the unscoped send). And a hosts runbook's baseline cannot
    borrow a pane's owned tools, or span the hub and a lane."""
    for tool in sorted(MESSAGING_TOOLS):
        assert baseline_violation("daintree.release.ask-a-host", [tool]) is not None, tool
    hosts = "daintree.hosts.example"
    assert baseline_violation(hosts, ["host.list", "host.sendMessage"]) is None
    assert baseline_violation(hosts, ["host.list", "project.openOnHost"]) is None
    assert baseline_violation(hosts, sorted(LANE_TOOLS)) is None
    assert baseline_violation(hosts, ["assistant.reply", "terminal.sendKeys"]) is None
    assert baseline_violation(hosts, ["terminal.sendCommandOwned"]) is not None
    assert baseline_violation(hosts, ["host.sendMessage", "assistant.sendMessage"]) is not None
    assert baseline_violation(hosts, ["host.listAssistants", "assistant.sendMessage"]) is not None

    off_caller = [
        f"{runbook.id}: {tool}"
        for runbook in load_catalog().runbooks
        if baseline_callers(runbook.id) != PANE_CALLERS
        for tool in runbook.meta.optional_tools
        if not any(tool in action_ids(caller) for caller in baseline_callers(runbook.id))
    ]
    assert not off_caller, f"optional tools no caller of the runbook is offered: {off_caller}"


def test_pane_and_api_key_callers_are_never_offered_messaging() -> None:
    """Captured where messaging is live both ways (a Shell with a host that also
    listens as one), an agent pane and an API-key client are still offered none of
    it: they are not sidebar-assistant sessions. Nor is the ordinary sidebar
    assistant on a machine with no host and no listener."""
    for surface in ("pane-core", "pane-full", "external"):
        recorded = capture(surface)["capture"]["messaging"]
        assert recorded["machine_has_hosts"] and recorded["machine_listening_as_host"]
        assert not recorded["help_session"]
        assert not action_ids(surface) & MESSAGING_TOOLS, surface
    for surface in ("help-core", "help-full"):
        assert not action_ids(surface) & MESSAGING_TOOLS, surface


def test_the_hub_reads_on_core_and_writes_on_full() -> None:
    """The hub's sends, cancels and starts act on another machine, so the sidebar
    assistant is offered them only at Tool set = Full: the plain sidebar captures
    record what each tier's allowlist holds, and the hub and lane Core sessions what
    they list. The hub's own lane receives host-initiated messages but has no hub
    to write to; a lane on a host has no `host.*` reach."""

    def allowlisted_messaging(surface: str) -> set[str]:
        withheld = capture(surface)["capture"].get("allowlisted_but_not_listed", [])
        return {w["action_id"] for w in withheld} & MESSAGING_TOOLS

    reads = {"host.list", "host.listAssistants", "host.getTask", "host.listTasks"}
    writes = {"host.sendMessage", "host.cancelTask", "host.startAssistant"}
    inbox = {"assistant.getInbox", "assistant.reply"}
    assert allowlisted_messaging("help-core") == reads | LANE_TOOLS
    assert allowlisted_messaging("help-full") == reads | writes | LANE_TOOLS
    # The Core sessions themselves, captured with messaging reachable and recorded
    # as what they list beyond help-core.
    hub_core = capture("hub-full")["capture"]["core_listing"]
    lane_core = capture("lane-full")["capture"]["core_listing"]
    for listing in (hub_core, lane_core):
        assert listing["delta_of_surface_hash"] == capture("help-core")["capture"]["surface_hash"]
        assert not listing["removed"] and not listing["changed"]
    assert set(hub_core["added"]) == reads | inbox
    assert set(lane_core["added"]) == LANE_TOOLS
    assert action_ids("hub-full") & HUB_TOOLS == reads | writes
    assert action_ids("hub-full") & LANE_TOOLS == inbox
    assert action_ids("lane-full") & MESSAGING_TOOLS == LANE_TOOLS


def test_hub_and_lane_surfaces_are_the_sidebar_plus_messaging() -> None:
    """Stored as deltas against the sidebar surface of the same tier, from the same
    capture. Rebuilt, each matches the tool count the app listed, and differs from
    its base only by what the machine adds: messaging, and the host-gated actions a
    Shell with a Remote Host shows."""
    host_gated = {"host.switch", "project.openOnHost"}
    for surface in ("hub-full", "lane-full"):
        recorded = capture(surface)["capture"]
        base = recorded["delta_of"]
        assert capture(base)["capture"]["tier"] == recorded["tier"], surface
        assert capture(base)["capture"]["surface_hash"] == recorded["delta_of_surface_hash"]
        assert len(capture(surface)["tools"]) == recorded["tool_count"], surface
        assert action_ids(base) <= action_ids(surface), surface
        assert action_ids(surface) - action_ids(base) <= MESSAGING_TOOLS | host_gated, surface


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
_DEFERRAL = re.compile(r"\buse\s+(?:the\s+)?([a-z0-9][a-z0-9 ./_-]{2,60}?)(?=[,.;)]|\s+runbook\b|$)")

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


def _tool_namespace_names() -> set[str]:
    """App action ids, which are dotted like runbook ids and legitimately quoted in
    runbook prose."""
    return known_action_ids()


def test_every_dotted_reference_in_a_whenToUse_resolves() -> None:
    """A carve-out that points at nothing is an untestable claim.

    Three things legitimately wear this shape and all three are accepted: a real
    runbook id, a real NAMESPACE with children under it ("the relevant
    daintree.orchestration runbook"), and a CLI tool quoted by name
    (``terminal.getOutput``). Anything else is a dangling reference — a typo, or a
    runbook that was renamed or deleted, leaving a claim nothing can test.
    """
    catalog = load_catalog(default_runbooks_dir())
    known = set(catalog.known_ids)
    namespaces = {sid.rsplit(".", 1)[0] for sid in known}
    tools = _tool_namespace_names()

    dangling: list[str] = []
    for runbook in catalog.runbooks:
        for ref in _DOTTED.findall(runbook.meta.when_to_use):
            if ref in known or ref in namespaces or ref in tools:
                continue
            dangling.append(f"{runbook.id} -> {ref}")
    assert not dangling, (
        "whenToUse references that resolve to no runbook, namespace or tool: "
        + ", ".join(dangling)
    )


def test_every_carve_out_defers_to_a_fully_qualified_id() -> None:
    """"use competition" is invisible to the routing-trap gate; "use
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
    catalog = load_catalog(default_runbooks_dir())
    known = set(catalog.known_ids)
    leaves = {sid.rsplit(".", 1)[-1] for sid in known}
    namespaces = {sid.rsplit(".", 1)[0] for sid in known}
    tools = _tool_namespace_names()

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


def test_no_two_runbooks_share_a_routing_surface() -> None:
    """Two identical routing documents are one vector in two shortlist slots.

    The selector embeds title + summary + whenToUse and never reads a body, so
    two runbooks whose metadata matches are indistinguishable to it however
    different their procedures are — it will rank them together, and the
    cardinality head will be asked to choose between two things it cannot tell
    apart.
    """
    catalog = load_catalog(default_runbooks_dir())
    seen: dict[str, str] = {}
    clashes: list[str] = []
    for runbook in catalog.runbooks:
        document = runbook_document(runbook.meta)
        if document in seen:
            clashes.append(f"{seen[document]} == {runbook.id}")
        seen[document] = runbook.id
    assert not clashes, f"runbooks with identical routing surfaces: {clashes}"


def test_no_runbooks_positive_trigger_is_contained_in_anothers() -> None:
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
    catalog = load_catalog(default_runbooks_dir())
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
