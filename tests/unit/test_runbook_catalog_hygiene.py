"""Catalog hygiene checks that the loader deliberately does NOT enforce.

``RunbookMeta`` validates ``requiredTools`` against a character regex only — nothing
checks a name is a *real* CLI tool, on either side of the wire (the CLI's startup
validation for runbook-declared tool names is gone). Two things make a typo persistent
rather than inert: the names are rendered into ``workflow_plan`` grounding outlines
(``task_runner.py``, ``_runbook_outlines``), and the runbook learner treats every name
already declared by a packaged runbook as an allowed name when it filters a proposal's
tools (``runbook_learning.py``, ``_admissible_tools``) — so one bad name in the catalog
launders itself into the next generated runbook. A deterministic guard has real value.

Severity, honestly: ``requiredTools`` is NOT a capability gate. Nothing dispatches
off it and nothing filters the offered toolset with it, so a bad name **misinforms a
prompt** — it does not break a tool call. That is why this lives in a test rather
than in ``runbooks/loader.py``: a runtime check against a pinned snapshot would fail
``/readyz`` the moment the CLI legitimately adds a tool, inverting the repo's
deliberate fail-open (selection) / fail-closed (malformed runbook) split.

Deliberately NOT added here — evaluated and rejected, do not re-propose:

* **A body-level tool-name lint** (scan runbook prose for tool names not in the
  registry). Measured 4/4 false positives on the catalog as it stood: every hit was
  either a raw MCP action correctly documented as reached *via* ``daintree.call``,
  or a name inside a NEGATIVE instruction telling the model not to use it. A lint
  that is wrong every time it fires is worse than no lint.
* **A duplicate-``requiredTools`` rejector in ``RunbookMeta``.** Zero duplicates exist,
  the harm is a few wasted tokens, and it would convert a harmless condition into a
  fail-closed load error that blocks ``/readyz``.
* **An id-shape validator on ``RunbookMeta``** (``daintree.<domain>.<name>``). It would
  immediately redden existing test fixtures whose ids (``a.one``, ``dom.zeta``,
  ``test.example``) are intentionally short.

Existing coverage this file does not duplicate: size/pipe-table/report-back budgets
live in ``test_prompt_budgets.py``; the eight house-style body checks live in
``services/runbook_learning.py`` and are swept over the packaged catalog by
``test_runbook_learning.py``.
"""

from __future__ import annotations

import json
import re
import subprocess
from pathlib import Path

import pytest

from daintree_assistant_server.runbooks.catalog import default_runbooks_dir, load_catalog
from daintree_assistant_server.runbooks.local.documents import (
    runbook_document,
    split_when_to_use,
)

# Snapshot of the CLI's tool registry. Never read cli/ at runtime: CI does not fetch
# submodules, so a submodule-reading test would silently no-op or hard-fail there.
# The file's own header carries the regeneration recipe.
TOOL_NAMES_FIXTURE = Path(__file__).resolve().parents[1] / "fixtures" / "cli_tool_names.txt"

# The full-schema capture of the SAME registry, which the name list above declares
# itself to be a pure projection of. Both are committed (no submodule involved), so
# that claim can be asserted literally instead of approximated by a count.
TOOL_INVENTORY_FIXTURE = Path(__file__).resolve().parents[1] / "fixtures" / "cli_tool_inventory.json"

# The registry projects an internal dotted name to its OpenAI wire name by replacing
# every "." with "__" (cli/internal/tools/registry.go, toWireName). Runbook frontmatter
# uses the dotted form, so normalize before comparing.
_WIRE_SEP = "__"


def _wire_name(dotted: str) -> str:
    return dotted.replace(".", _WIRE_SEP)


def _known_tool_names() -> set[str]:
    lines = TOOL_NAMES_FIXTURE.read_text(encoding="utf-8").splitlines()
    return {
        line.strip() for line in lines if line.strip() and not line.lstrip().startswith("#")
    }


def _captured_tool_names() -> set[str]:
    inventory = json.loads(TOOL_INVENTORY_FIXTURE.read_text(encoding="utf-8"))
    return {entry["function"]["name"] for entry in inventory}


def test_tool_name_fixture_is_an_exact_projection_of_the_registry_capture() -> None:
    # Guard the guard: a truncated or mangled regeneration must fail loudly rather
    # than making every check below pass vacuously. The fixture header states it IS
    # the name-only projection of the full-schema capture, so assert exactly that —
    # a floor ("at least 50 names") silently accepts losing a third of an 85-name
    # registry, which is the regeneration accident this is here to catch.
    names = _known_tool_names()
    captured = _captured_tool_names()
    # Equality alone still passes vacuously if BOTH files are regenerated from a
    # truncated capture — and the capture is the fragile end (it is dumped from the
    # cli/ submodule). Keep a floor on the source so that failure is visible too.
    assert len(captured) >= 50, f"registry capture looks truncated ({len(captured)} tools)"
    assert names == captured, (
        f"{TOOL_NAMES_FIXTURE.name} is out of sync with {TOOL_INVENTORY_FIXTURE.name}: "
        f"missing {sorted(captured - names)}, unexpected {sorted(names - captured)}. "
        "Regenerate it from the capture (recipe is in the fixture header)."
    )
    assert all(_WIRE_SEP in name for name in names), "fixture holds wire names (family__tool)"


def test_required_tools_exist_in_the_cli_registry() -> None:
    # ONE direction only. Registry tools that no runbook declares are the normal state
    # (a large minority of the registry today) — asserting the reverse would churn on
    # every CLI addition and every catalog edit.
    known = _known_tool_names()
    unknown: list[str] = []
    for runbook in load_catalog().runbooks:
        for tool in runbook.meta.required_tools:
            if _wire_name(tool) not in known:
                unknown.append(f"{runbook.id}: {tool}")
    assert not unknown, (
        "requiredTools names not found in the CLI tool registry snapshot "
        f"({TOOL_NAMES_FIXTURE.name}): {unknown}. Either fix the typo in the runbook "
        "frontmatter, or regenerate the snapshot if the CLI added the tool "
        "(recipe is in the fixture header)."
    )


# ---------------------------------------------------------------------------
# The two CLI fixtures are ONE capture. They must not drift apart.
# ---------------------------------------------------------------------------


def test_the_name_fixture_is_exactly_the_projection_of_the_inventory() -> None:
    """`cli_tool_names.txt` is the name-only view of `cli_tool_inventory.json`.

    They are refreshed by hand from the same dump, which makes a HALF refresh the
    obvious mistake — update the inventory, forget the names, and the hygiene
    check above starts validating runbooks against a registry that no longer
    exists. Nothing else would notice: both files parse, both look plausible, and
    the failure only appears as a runbook declaring a tool the CLI dropped.

    This is free and offline, which matters because CI does not fetch the `cli/`
    submodule and so cannot regenerate either file to compare.
    """
    inventory = json.loads(TOOL_INVENTORY_FIXTURE.read_text(encoding="utf-8"))
    projected = sorted(t["function"]["name"] for t in inventory)
    raw = TOOL_NAMES_FIXTURE.read_text(encoding="utf-8")
    recorded = [ln for ln in raw.splitlines() if ln.strip() and not ln.startswith("#")]

    only_names = sorted(set(recorded) - set(projected))
    only_inventory = sorted(set(projected) - set(recorded))
    assert not only_names and not only_inventory, (
        "the two CLI fixtures disagree — they are one capture and must be "
        f"regenerated together.\n  in cli_tool_names.txt only: {only_names}\n"
        f"  in cli_tool_inventory.json only: {only_inventory}\n"
        "Recipe: tests/fixtures/README.md."
    )
    # Compared as a LIST, not a set: set equality passes on a duplicated or
    # unsorted file, and both are the shape a hand-edit produces. The file is
    # meant to be the projection verbatim, so a reviewer can diff two captures
    # and read the change rather than re-derive it.
    assert recorded == projected, (
        "cli_tool_names.txt holds the right names in the wrong form — it must be "
        "the sorted projection with no duplicates. Regenerate it rather than "
        "editing by hand."
    )
    assert raw.endswith("\n") and not raw.endswith("\n\n"), (
        "cli_tool_names.txt must end with exactly one newline, so a refresh is an "
        "additive diff rather than a whitespace change on the last name"
    )


def test_the_fixtures_record_which_cli_commit_they_came_from() -> None:
    """A snapshot with no provenance cannot be checked against the submodule.

    The header names the commit the capture was taken at. Without it, "is this
    stale?" is unanswerable, and a pin that moved silently takes the fixtures
    with it.
    """
    header = TOOL_NAMES_FIXTURE.read_text(encoding="utf-8")
    match = re.search(r"cli/ @ ([0-9a-f]{7,40})", header)
    assert match, (
        "cli_tool_names.txt no longer records the cli/ commit it was captured "
        "at. Keep that line when regenerating — it is the only way to tell a "
        "current fixture from a stale one."
    )
    # ... and the recorded commit must be the one the submodule is pinned at.
    # Accepting any hash made the line decorative: it recorded that SOMEBODY once
    # captured against SOMETHING, which is exactly what it looked like after the
    # pin moved and the header did not.
    #
    # Read from the INDEX (`ls-files -s`), not a populated checkout and not HEAD.
    # The checkout is absent in CI, which never fetches the submodule. HEAD would
    # be the PREVIOUS pin during the very commit that moves it — so a correct
    # refresh would fail and the fix would be to weaken this test.
    recorded = match.group(1)
    ls = subprocess.run(
        ["git", "ls-files", "-s", "cli"],
        cwd=Path(__file__).resolve().parents[2],
        capture_output=True, text=True, check=False,
    )
    if ls.returncode != 0 or not ls.stdout.strip():
        pytest.skip("not a git checkout, or cli/ is not a submodule here")
    pinned = ls.stdout.split()[1]
    assert pinned.startswith(recorded), (
        f"cli_tool_names.txt says it was captured at cli/ @ {recorded}, but the "
        f"submodule is pinned at {pinned[:7]}. Either the pin moved without the "
        f"fixtures being refreshed — which is how a runbook ends up declaring a tool "
        f"the CLI no longer has — or the header was not updated with them."
    )


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
    """CLI tool names in their dotted spelling — ``daintree__status`` is
    ``daintree.status``, which is shaped exactly like a runbook id and is
    legitimately quoted in runbook prose."""
    return {name.replace("__", ".") for name in _known_tool_names()}


def test_every_dotted_reference_in_a_whenToUse_resolves() -> None:
    """A carve-out that points at nothing is an untestable claim.

    Three things legitimately wear this shape and all three are accepted: a real
    runbook id, a real NAMESPACE with children under it ("the relevant
    daintree.orchestration runbook"), and a CLI tool quoted by name
    (``daintree.status``). Anything else is a dangling reference — a typo, or a
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
