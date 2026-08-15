"""Catalog hygiene checks that the loader deliberately does NOT enforce.

``SkillMeta`` validates ``requiredTools`` against a character regex only — nothing
checks a name is a *real* CLI tool, on either side of the wire (the CLI's startup
validation for skill-declared tool names is gone). Two things make a typo persistent
rather than inert: the names are rendered into ``workflow_plan`` grounding outlines
(``task_runner.py``, ``_skill_outlines``), and the skill learner treats every name
already declared by a packaged skill as an allowed name when it filters a proposal's
tools (``skill_learning.py``, ``_admissible_tools``) — so one bad name in the catalog
launders itself into the next generated skill. A deterministic guard has real value.

Severity, honestly: ``requiredTools`` is NOT a capability gate. Nothing dispatches
off it and nothing filters the offered toolset with it, so a bad name **misinforms a
prompt** — it does not break a tool call. That is why this lives in a test rather
than in ``skills/loader.py``: a runtime check against a pinned snapshot would fail
``/readyz`` the moment the CLI legitimately adds a tool, inverting the repo's
deliberate fail-open (selection) / fail-closed (malformed skill) split.

Deliberately NOT added here — evaluated and rejected, do not re-propose:

* **A body-level tool-name lint** (scan skill prose for tool names not in the
  registry). Measured 4/4 false positives on the catalog as it stood: every hit was
  either a raw MCP action correctly documented as reached *via* ``daintree.call``,
  or a name inside a NEGATIVE instruction telling the model not to use it. A lint
  that is wrong every time it fires is worse than no lint.
* **A duplicate-``requiredTools`` rejector in ``SkillMeta``.** Zero duplicates exist,
  the harm is a few wasted tokens, and it would convert a harmless condition into a
  fail-closed load error that blocks ``/readyz``.
* **An id-shape validator on ``SkillMeta``** (``daintree.<domain>.<name>``). It would
  immediately redden existing test fixtures whose ids (``a.one``, ``dom.zeta``,
  ``test.example``) are intentionally short.

Existing coverage this file does not duplicate: size/pipe-table/report-back budgets
live in ``test_prompt_budgets.py``; the eight house-style body checks live in
``services/skill_learning.py`` and are swept over the packaged catalog by
``test_skill_learning.py``.
"""

from __future__ import annotations

import json
import re
import subprocess
from pathlib import Path

import pytest

from daintree_assistant_server.skills.catalog import load_catalog

# Snapshot of the CLI's tool registry. Never read cli/ at runtime: CI does not fetch
# submodules, so a submodule-reading test would silently no-op or hard-fail there.
# The file's own header carries the regeneration recipe.
TOOL_NAMES_FIXTURE = Path(__file__).resolve().parents[1] / "fixtures" / "cli_tool_names.txt"

# The full-schema capture of the SAME registry, which the name list above declares
# itself to be a pure projection of. Both are committed (no submodule involved), so
# that claim can be asserted literally instead of approximated by a count.
TOOL_INVENTORY_FIXTURE = (
    Path(__file__).resolve().parents[2]
    / "benchmarks"
    / "_harness"
    / "fixtures"
    / "cli_tool_inventory.json"
)

# The registry projects an internal dotted name to its OpenAI wire name by replacing
# every "." with "__" (cli/internal/tools/registry.go, toWireName). Skill frontmatter
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
    # ONE direction only. Registry tools that no skill declares are the normal state
    # (a large minority of the registry today) — asserting the reverse would churn on
    # every CLI addition and every catalog edit.
    known = _known_tool_names()
    unknown: list[str] = []
    for skill in load_catalog().skills:
        for tool in skill.meta.required_tools:
            if _wire_name(tool) not in known:
                unknown.append(f"{skill.id}: {tool}")
    assert not unknown, (
        "requiredTools names not found in the CLI tool registry snapshot "
        f"({TOOL_NAMES_FIXTURE.name}): {unknown}. Either fix the typo in the skill "
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
    check above starts validating skills against a registry that no longer
    exists. Nothing else would notice: both files parse, both look plausible, and
    the failure only appears as a skill declaring a tool the CLI dropped.

    This is free and offline, which matters because CI does not fetch the `cli/`
    submodule and so cannot regenerate either file to compare.
    """
    inventory = json.loads(
        (
            Path(__file__).resolve().parents[2]
            / "benchmarks"
            / "_harness"
            / "fixtures"
            / "cli_tool_inventory.json"
        ).read_text(encoding="utf-8")
    )
    projected = sorted(t["function"]["name"] for t in inventory)
    raw = TOOL_NAMES_FIXTURE.read_text(encoding="utf-8")
    recorded = [ln for ln in raw.splitlines() if ln.strip() and not ln.startswith("#")]

    only_names = sorted(set(recorded) - set(projected))
    only_inventory = sorted(set(projected) - set(recorded))
    assert not only_names and not only_inventory, (
        "the two CLI fixtures disagree — they are one capture and must be "
        f"regenerated together.\n  in cli_tool_names.txt only: {only_names}\n"
        f"  in cli_tool_inventory.json only: {only_inventory}\n"
        "Recipe: benchmarks/_harness/fixtures/README.md."
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
        f"fixtures being refreshed — which is how a skill ends up declaring a tool "
        f"the CLI no longer has — or the header was not updated with them."
    )
