"""The always-on core must cover what the ALWAYS-PRESENT prompt instructs.

Skill-aware tool projection withholds tools no active skill declares. That is an
argument from a skill's metadata, and it has no answer for a tool the SERVER'S
OWN permanent prompt names: no skill is loaded to explain the absence, and on a
skill-less turn there is no metadata in play at all. A projection that withholds
`artifact.read` while `20-tool-discipline.md` tells the model to page every large
result through it hands the model an instruction it cannot follow.

So the core is DERIVED, not chosen, and this is where that is enforced. The
scanner reads backticked dotted names out of the prompt text and keeps the ones
the CLI actually registers as tools — prose like `recoverable: false` and ids
like `sha256:...` are not tools and must not be treated as gaps.

When this fails, the fix is usually to add the tool to `ALWAYS_AVAILABLE`. The
other fix is to stop instructing it in the always-present prompt and let a skill
own it instead — but that is a prompt decision, not a projection one, and it has
to be made deliberately rather than discovered as a withheld call in production.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

from daintree_assistant_server.prompts.loader import PromptLoader
from daintree_assistant_server.skills.catalog import load_catalog
from daintree_assistant_server.skills.tool_projection import (
    ALWAYS_AVAILABLE,
    SKILL_SCAFFOLD_TOOLS,
    wire_name,
)

# A backticked dotted identifier: `fs.read`, `terminal.await.async`. Anchored on
# the backticks so a bare mention in prose is not mistaken for a call site.
_BACKTICKED = re.compile(r"`([a-z][a-zA-Z0-9]*(?:\.[a-zA-Z0-9]+)+)`")

_INVENTORY = (
    Path(__file__).resolve().parents[2]
    / "benchmarks"
    / "_harness"
    / "fixtures"
    / "cli_tool_inventory.json"
)


def _registry_names() -> frozenset[str]:
    """Wire names the CLI actually registers, from the pinned inventory fixture."""
    with _INVENTORY.open(encoding="utf-8") as handle:
        return frozenset(entry["function"]["name"] for entry in json.load(handle))


def _tools_named_in(text: str) -> set[str]:
    registry = _registry_names()
    return {wire_name(match) for match in _BACKTICKED.findall(text) if wire_name(match) in registry}


def test_the_base_prompt_names_tools_the_scanner_can_see() -> None:
    """Guard the guard: a scanner that matched nothing would pass everything."""
    named = _tools_named_in(PromptLoader().base_prompt())
    assert len(named) >= 10, (
        "the base prompt names fewer real tools than expected — the scanner has "
        f"probably stopped matching (found {sorted(named)})"
    )


def test_every_tool_the_base_prompt_instructs_survives_projection() -> None:
    core = {wire_name(name) for name in ALWAYS_AVAILABLE}
    missing = sorted(_tools_named_in(PromptLoader().base_prompt()) - core)
    assert not missing, (
        "the always-present base prompt instructs these tools, but the projection "
        f"would withhold them on a turn no skill covers: {missing}. Add them to "
        "ALWAYS_AVAILABLE, or stop naming them in the permanent prompt."
    )


def test_the_skill_scaffold_tools_are_admitted_when_a_skill_is_active() -> None:
    """`loaded_skills.md.j2` instructs these and no skill declares them.

    They belong to the TEMPLATE, so no `requiredTools` audit over skill bodies
    could ever see the gap — which is exactly why it needs its own assertion.

    The template names one further tool, `terminal.await.async`, as the SCENARIO
    that makes a checkpoint worth recording rather than as a call to make. Any
    skill that actually starts such a wait declares it, so the skill path covers
    it — which is the general rule asserted below: a tool the scaffold mentions
    is either scaffold, core, or something some skill owns.
    """
    template = (
        Path(__file__).resolve().parents[2]
        / "src"
        / "daintree_assistant_server"
        / "prompts"
        / "main"
        / "loaded_skills.md.j2"
    )
    named = _tools_named_in(template.read_text(encoding="utf-8"))
    scaffold = {wire_name(name) for name in SKILL_SCAFFOLD_TOOLS}
    assert named, "the loaded-skills template names no tools — scanner drift?"
    assert scaffold <= named, (
        "SKILL_SCAFFOLD_TOOLS claims to mirror what the template instructs, but "
        f"the template no longer names {sorted(scaffold - named)}"
    )

    core = {wire_name(name) for name in ALWAYS_AVAILABLE}
    unowned = sorted(
        name for name in named - scaffold - core if name not in load_catalog().declared_tools
    )
    assert not unowned, (
        "the loaded-skills scaffold names tools that neither the scaffold set, the "
        f"core, nor any skill's requiredTools would admit: {unowned}"
    )
