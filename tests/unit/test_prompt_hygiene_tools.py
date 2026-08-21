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

**"The prompt" is two files, not one.** `prompts/main/base/*.md` is the obvious
half; `prompts/main/turn_footer.md.j2` is the other, and it went unscanned long
enough to open a real hole (`async.cancel`, `watcher.cancel`). It is a
server-owned layer that renders on every assistant turn carrying turn context,
skill or no skill, so a tool it names has exactly the same claim on the core as
one the base prompt names — and no skill's `requiredTools` can stand in for that,
because the footer renders when nothing is loaded.

When this fails, the fix is usually to add the tool to `ALWAYS_AVAILABLE`. The
other fix is to stop instructing it in the always-present prompt and let a skill
own it instead — but that is a prompt decision, not a projection one, and it has
to be made deliberately rather than discovered as a withheld call in production.

**The scanner does only half the job, and the half it cannot do is written down
rather than left implicit.** It reads names; the prompt also instructs
capabilities in prose ("confirm a finish with a bounded tail read per terminal"
names no tool at all), and it names `memory.save` inside a PROHIBITION, which
reads identically to an instruction. So `_PROSE_INSTRUCTED` below records the
prose cases by hand with their justification, and a third test bounds the core
from the other side — nothing may sit in it that none of the three sources
explains. Between them the set is pinned in both directions without pretending a
regex understood the prompt.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

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

# The turn footer writes its tool names BARE — "async.cancel stops one" — so the
# backticked scanner reads it as naming nothing at all. This one drops the
# anchors, which on its own would also match `wf.goal`, `memories.pinned` and
# every other Jinja attribute access in that template: the registry intersection
# in `_registry_names_in` is what makes it a scanner rather than noise, and it is
# why this pattern is only ever used through that helper.
_BARE_DOTTED = re.compile(r"\b([a-z][a-zA-Z0-9]*(?:\.[a-zA-Z0-9]+)+)\b")

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


def _registry_names_in(text: str) -> set[str]:
    """Every registry tool named in `text`, backticked or bare."""
    registry = _registry_names()
    return {
        wire_name(match) for match in _BARE_DOTTED.findall(text) if wire_name(match) in registry
    }


def _turn_footer() -> str:
    path = (
        Path(__file__).resolve().parents[2]
        / "src"
        / "daintree_assistant_server"
        / "prompts"
        / "main"
        / "turn_footer.md.j2"
    )
    return path.read_text(encoding="utf-8")


# Every registry tool the base prompt currently names, reviewed by hand. Pinned as
# a SET rather than a count: a floor lets today's references disappear one by one
# without a word, and this list's whole job is to notice when the prompt and the
# core stop agreeing about what a turn can do.
_NAMED_IN_BASE_PROMPT: frozenset[str] = frozenset(
    {
        "agentTask__spawnForEdits",
        "artifact__read",
        "context__snapshot",
        "daintree__call",
        "fs__list",
        "fs__read",
        "fs__search",
        "memory__save",
        "queue__resolve",
        "subagent__run",
        "terminal__close",
        "terminal__sendCommand",
        "timer__list",
        "user__askMultipleChoice",
        "watcher__list",
        "worktree__list",
    }
)


def test_the_scanner_still_sees_what_the_prompt_says() -> None:
    """Guard the guard, in both directions.

    A scanner that quietly stopped matching would make every assertion in this
    file pass over an empty set, and the feature it guards silently withholds
    tools — so "found nothing" must never read as "found no problem". Pinning the
    exact set also catches the opposite drift: a NEW tool named in the permanent
    prompt shows up here as a diff to review rather than as a core entry someone
    added without anyone asking why.
    """
    named = _tools_named_in(PromptLoader().base_prompt())
    assert named == _NAMED_IN_BASE_PROMPT, (
        "the tools the base prompt names have changed.\n"
        f"  newly named: {sorted(named - _NAMED_IN_BASE_PROMPT)}\n"
        f"  no longer named: {sorted(_NAMED_IN_BASE_PROMPT - named)}\n"
        "Review whether ALWAYS_AVAILABLE should follow, then update this set."
    )


@pytest.mark.parametrize(
    "text",
    [
        "call `fs.read` to read it",
        "- `terminal.sendCommand` is always there",
        "use `fs.read`, then `fs.list`",
    ],
)
def test_the_scanner_matches_the_shapes_the_prompt_actually_uses(text: str) -> None:
    """The regex, tested directly rather than only through the prompt.

    Every assertion above depends on this matching; none of them would fail
    informatively if it stopped. A shape the prompt uses today and the scanner
    misses is a false all-clear, and this is where that gets caught.
    """
    assert _tools_named_in(text), f"scanner missed a tool in: {text!r}"


def test_every_tool_the_base_prompt_instructs_survives_projection() -> None:
    core = {wire_name(name) for name in ALWAYS_AVAILABLE}
    missing = sorted(_tools_named_in(PromptLoader().base_prompt()) - core)
    assert not missing, (
        "the always-present base prompt instructs these tools, but the projection "
        f"would withhold them on a turn no skill covers: {missing}. Add them to "
        "ALWAYS_AVAILABLE, or stop naming them in the permanent prompt."
    )


# Every registry tool the turn footer names, reviewed by hand — the same pin as
# `_NAMED_IN_BASE_PROMPT`, and it earns it twice over here: the bare-dotted
# scanner has no backticks to anchor on, so a template edit that broke matching
# would leave the assertion below passing over an empty set.
_NAMED_IN_TURN_FOOTER: frozenset[str] = frozenset(
    {
        "async__cancel",
        "watcher__cancel",
        "watcher__list",
    }
)


def test_every_tool_the_turn_footer_instructs_is_in_the_core() -> None:
    """The footer is the other always-present layer, and it gets no skill.

    `turn_footer.md.j2` renders on any assistant turn carrying turn context,
    whatever the classifier selected — so "some skill declares it" is not an
    answer here the way it is for the loaded-skills scaffold below. Both cancel
    tools ARE declared by `daintree.foundation`, which is precisely the trap:
    that makes the catalog classify them, the unclassified pass-through skip
    them, and an enforced skill-less turn render "async.cancel stops one" over a
    request that never offered it. Withholding is final, so `tool.search` cannot
    walk it back either.
    """
    named = _registry_names_in(_turn_footer())
    assert named == _NAMED_IN_TURN_FOOTER, (
        "the tools the turn footer names have changed.\n"
        f"  newly named: {sorted(named - _NAMED_IN_TURN_FOOTER)}\n"
        f"  no longer named: {sorted(_NAMED_IN_TURN_FOOTER - named)}\n"
        "Review whether ALWAYS_AVAILABLE should follow, then update this set."
    )

    core = {wire_name(name) for name in ALWAYS_AVAILABLE}
    missing = sorted(named - core)
    assert not missing, (
        "the turn footer instructs these tools, but the projection would withhold "
        f"them on a turn no skill covers: {missing}. Add them to ALWAYS_AVAILABLE, "
        "or stop naming them in the footer."
    )


# Tools the base prompt instructs in PROSE, with no registry name to match. The
# scanner is blind to every one of these, so they are recorded here by hand — the
# alternative is a check that silently covers less of the prompt than it appears
# to, which is worse than no check because it stops the next reader looking.
#
# The CLI reaches the same conclusion independently: all of these are in its own
# `coreToolNames` (internal/agent/session.go), the set it asserts at boot BECAUSE
# the base prompt advertises them as always available. Two sources, one list.
_PROSE_INSTRUCTED: dict[str, str] = {
    # 20-tool-discipline: "Confirm a finish with a bounded tail read per terminal,
    # or a watcher's confirmed completion". A specific act, named as the way to
    # establish a specific fact — and there is no backticked name to match.
    "terminal.read": "20-tool-discipline: 'a bounded tail read per terminal'",
    # 20-tool-discipline: "Keep the main conversation clean: ... surface queue
    # items", alongside the backticked `queue.resolve`. Resolving presupposes
    # reading, and 30-skills lists what the injected context carries — the queue
    # is not on it, so reading it is a call.
    "queue.digest": "20-tool-discipline: 'surface queue items'",
}

# Four more were here and came out. `terminal.extract` rested on "read output only
# when you need the content"; `terminal.awaitAll` on nothing in this prompt at
# all; `memory.recall`/`memory.list` on the assumption that memory is searched,
# when 30-skills says "pinned and recalled memories" ARRIVE in the injected
# context and to "read the answer straight out of whatever is present".
#
# They were added from the CLI's `coreToolNames`, which is a fine cross-check and
# a bad authority — it is written against the CLI's own view. Under the reading
# that admitted `terminal.extract`, `terminal.summarize` and every watcher and
# async tool qualify too, and "derived from the prompt" stops constraining
# anything. The bar is a specific instruction naming the capability.


def test_the_prose_instructed_tools_are_in_the_core() -> None:
    """The half of the derivation no regex can do.

    Deleting an entry from `ALWAYS_AVAILABLE` that the prompt only describes would
    otherwise be invisible: the scanner finds no name, so the test above stays
    green while an enforced skill-less turn loses the ability to confirm that a
    terminal finished.
    """
    core = {wire_name(name) for name in ALWAYS_AVAILABLE}
    registry = _registry_names()
    for dotted, why in _PROSE_INSTRUCTED.items():
        assert wire_name(dotted) in registry, f"{dotted} is not a real CLI tool"
        assert wire_name(dotted) in core, f"{dotted} dropped from the core — {why}"


def test_the_core_holds_nothing_the_prompt_does_not_reach() -> None:
    """Every core entry is a permanent per-request cost, so each needs a reason.

    Equality against the scanner is deliberately NOT the assertion — the core is
    legitimately wider than what a regex over the base prompt can see, in exactly
    three ways, and all of them are enumerated here so a future addition cannot
    hide among them. Anything outside all four sources is dead weight: bytes on
    every request with no instruction behind them.
    """
    named = _tools_named_in(PromptLoader().base_prompt()) | _registry_names_in(_turn_footer())
    prose = {wire_name(n) for n in _PROSE_INSTRUCTED}
    # The discovery escape hatch. These predate enforcement and are not instructed
    # anywhere in `base/` — they exist so a turn can find and reach a capability it
    # was not projected, which is the one thing withholding must never make
    # unrecoverable.
    escape_hatch = {
        wire_name(n)
        for n in ("tool.search", "daintree.listTools", "daintree.call", "daintree.status")
    }
    unexplained = sorted({wire_name(n) for n in ALWAYS_AVAILABLE} - named - prose - escape_hatch)
    assert not unexplained, (
        "these core tools are not named in the base prompt or the turn footer, "
        f"not recorded in _PROSE_INSTRUCTED, and not part of the escape hatch: "
        f"{unexplained}"
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
