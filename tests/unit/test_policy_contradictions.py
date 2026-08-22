"""Rules stated in more than one model-visible place must agree.

A contradiction between two prompt layers is worse than a wrong rule in one of
them. The model receives both in the same request, cannot tell which is
authoritative, and resolves the conflict differently from turn to turn — so the
failure is intermittent and reads like flakiness rather than a prompt bug.

This file exists because exactly that shipped. `skill.step.advance` was
described as a PER-STEP progress record by the CLI tool schema, the utility task
that audits it, and that task's whole benchmark corpus — while
`loaded_skills.md.j2` told the model the opposite: to call it ONLY at a turn
boundary and explicitly "do NOT call it per step". Both were in every
skill-loaded assistant turn.

The implementation settled it. `skill.go`'s `upsertStep` records ONLY the step
named in the call and backfills nothing, so turn-boundary checkpointing leaves
holes in the progress array the tool exists to produce: complete steps 1-5 in
one turn, checkpoint at 5, and a later `skill.run.get` reads 2-4 as never done.
The per-step contract is the one the data model supports, so the scaffold was
the surface that moved. (Turn-boundary checkpointing is a defensible design and
costs fewer calls — it just needs CLI-side backfill first. See TODO.md.)
"""

from __future__ import annotations

import json
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
PROMPTS = REPO_ROOT / "src" / "daintree_assistant_server" / "prompts"
SCAFFOLD = PROMPTS / "main" / "loaded_skills.md.j2"
CONSISTENCY_TASK = PROMPTS / "tasks" / "skill_step_consistency.md"
TOOL_INVENTORY = REPO_ROOT / "benchmarks" / "_harness" / "fixtures" / "cli_tool_inventory.json"

#: Phrases that assert the RETIRED turn-boundary contract. Any of them appearing
#: in a model-visible surface reopens the contradiction.
_TURN_BOUNDARY_CLAIMS = (
    "only when a skill run may continue beyond this turn",
    "do not call it per step",
    "about to end the turn mid-skill",
    "only the last completed step",
    "record only the last",
    "at the turn boundary",
)

#: The per-step instruction the scaffold must actually carry. Forbidding the old
#: claim is not enough on its own: the paragraph could be rewritten to say
#: "record the last completed step before yielding" and trip none of the phrases
#: above while meaning exactly what they meant.
_PER_STEP_INSTRUCTION = "as you complete"

CLI_SUBMODULE = REPO_ROOT / "cli"
_CLI_SKILL_TOOL = CLI_SUBMODULE / "internal" / "tools" / "skill" / "skill.go"


def _tool_description(name: str) -> str:
    tools = json.loads(TOOL_INVENTORY.read_text(encoding="utf-8"))
    for tool in tools:
        fn = tool.get("function", {})
        if fn.get("name") == name:
            return str(fn.get("description", ""))
    raise AssertionError(
        f"{name} is not in the captured CLI inventory. If the CLI dropped the tool, "
        "the scaffold paragraph that instructs it must go too."
    )


def test_the_scaffold_does_not_contradict_the_tool_it_instructs() -> None:
    """The scaffold and the tool schema are read together; they must agree.

    Asserted as the ABSENCE of the retired claim rather than the presence of a
    blessed sentence: the wording will be edited for other reasons, and a test
    that pins prose exactly gets updated reflexively without anyone rereading
    what it protects.
    """
    scaffold = SCAFFOLD.read_text(encoding="utf-8").lower()
    assert _PER_STEP_INSTRUCTION in scaffold, (
        "loaded_skills.md.j2 no longer instructs a per-step record. Removing the "
        "instruction is how the contradiction comes back without reusing any of its "
        "old wording."
    )
    for claim in _TURN_BOUNDARY_CLAIMS:
        assert claim not in scaffold, (
            f"loaded_skills.md.j2 reasserts the turn-boundary contract ({claim!r}), which "
            "the CLI tool description and the skill_step_consistency task both contradict. "
            "Only the step named in a call is recorded — see this module's docstring."
        )


def test_the_cli_tool_still_describes_a_per_step_record() -> None:
    """The other half of the pair, so a CLI bump that flips it is not silent.

    Without this, aligning the scaffold would look permanently correct while the
    authority it was aligned TO had moved underneath it.
    """
    description = _tool_description("skill__step__advance").lower()
    assert "as you complete each step" in description, (
        "The CLI's skill.step.advance description no longer describes a per-step "
        "record. If the CLI moved to turn-boundary checkpointing (and taught "
        "upsertStep to backfill), the scaffold and skill_step_consistency must move "
        "with it — and this test's expectation is what changes last, deliberately."
    )


def test_the_consistency_auditor_assumes_the_same_contract() -> None:
    """The task that JUDGES advances must judge them under the live contract.

    Under turn-boundary semantics a jump from step 1 to step 5 is a normal
    multi-step turn; under per-step semantics it is the illegitimate jump this
    task exists to catch. Auditing with the wrong contract does not merely
    weaken the check — it inverts it.
    """
    task = CONSISTENCY_TASK.read_text(encoding="utf-8").lower()
    assert "one step at a time" in task
    for claim in _TURN_BOUNDARY_CLAIMS:
        assert claim not in task


def test_the_gate_reads_the_cli_source_when_the_submodule_is_present() -> None:
    """The fixture is a CAPTURE; the submodule is the authority.

    ``cli_tool_inventory.json`` refreshes by hand, so a CLI that changed this
    tool's description under an unrefreshed fixture is invisible to the test
    above — the exact drift the fixture exists to make visible everywhere else.
    CI checks the submodule out, so when it is present the real source is read.
    Skipped rather than failed when it is not: a bare clone has an empty `cli/`,
    and a developer who has not run `git submodule update --init` should not see
    a failure that says nothing about their change.
    """
    if not _CLI_SKILL_TOOL.exists():
        import pytest

        pytest.skip("cli/ submodule not initialised — run `git submodule update --init cli`")
    source = _CLI_SKILL_TOOL.read_text(encoding="utf-8").lower()
    assert "as you complete each step" in source, (
        "The CLI's skill.step.advance description changed. If it moved to "
        "turn-boundary checkpointing, the scaffold, skill_step_consistency and this "
        "gate move with it — see this module's docstring and TODO.md."
    )


#: The base partial the container omits when no compaction producer is configured.
_DECLARATION_PARTIAL = PROMPTS / "main" / "base" / "15-response-declaration.md"


def test_the_declaration_partial_still_exists_to_be_omitted() -> None:
    """Omission is by FILENAME, so a rename silently un-omits the rule.

    `PromptLoader.base_prompt(response_declaration=False)` drops the partial whose
    stem matches `DECLARATION_PART`. It stays quiet when the stem is absent,
    because a prompt directory legitimately varies — the variant benchmarks ship a
    single template, and tests ship a single partial. That tolerance is right
    there and wrong here: rename this file and every deployment starts shipping
    the control-line rule again, with the flag still reading False and no error
    anywhere.
    """
    from daintree_assistant_server.prompts.loader import PromptLoader

    assert _DECLARATION_PARTIAL.exists(), (
        f"{_DECLARATION_PARTIAL.name} is gone. If the declaration rule was deleted "
        "outright, delete PromptLoader.DECLARATION_PART and the assembler flag with "
        "it — leaving them behind means the off switch silently does nothing."
    )
    assert _DECLARATION_PARTIAL.stem == PromptLoader.DECLARATION_PART

    loader = PromptLoader()
    with_rule = loader.base_prompt(response_declaration=True)
    without_rule = loader.base_prompt(response_declaration=False)
    assert "[[DAINTREE:FINAL]]" in with_rule
    assert "[[DAINTREE:" not in without_rule
    assert len(with_rule) > len(without_rule)
