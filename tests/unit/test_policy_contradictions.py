"""Rules stated in more than one model-visible place must agree.

A contradiction between two prompt layers is worse than a wrong rule in one of
them. The model receives both in the same request, cannot tell which is
authoritative, and resolves the conflict differently from turn to turn — so the
failure is intermittent and reads like flakiness rather than a prompt bug.

This file exists because exactly that shipped. `runbook.step.advance` was
described as a PER-STEP progress record by the CLI tool schema, the utility task
that audits it, and that task's whole benchmark corpus — while
`loaded_runbooks.md.j2` told the model the opposite: to call it ONLY at a turn
boundary and explicitly "do NOT call it per step". Both were in every
runbook-loaded assistant turn.

The implementation settled it. `skill.go`'s `upsertStep` records ONLY the step
named in the call and backfills nothing, so turn-boundary checkpointing leaves
holes in the progress array the tool exists to produce: complete steps 1-5 in
one turn, checkpoint at 5, and a later `runbook.run.get` reads 2-4 as never done.
The per-step contract is the one the data model supports, so the scaffold was
the surface that moved. (Turn-boundary checkpointing is a defensible design and
costs fewer calls — it just needs CLI-side backfill first. See TODO.md.)
"""

from __future__ import annotations

import json
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
PROMPTS = REPO_ROOT / "src" / "daintree_assistant_server" / "prompts"
SCAFFOLD = PROMPTS / "main" / "loaded_runbooks.md.j2"
CONSISTENCY_TASK = PROMPTS / "tasks" / "runbook_step_consistency.md"
BASE_DIR = PROMPTS / "main" / "base"
TOOL_INVENTORY = REPO_ROOT / "benchmarks" / "_harness" / "fixtures" / "cli_tool_inventory.json"

#: Phrases that assert the RETIRED turn-boundary contract. Any of them appearing
#: in a model-visible surface reopens the contradiction.
_TURN_BOUNDARY_CLAIMS = (
    "only when a runbook run may continue beyond this turn",
    # The pre-rename wording of the same two claims. These are QUOTES of retired
    # prompt text, so the strings to forbid are the ones that were actually written.
    "only when a skill run may continue beyond this turn",
    "do not call it per step",
    "about to end the turn mid-runbook",
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
# The CLI's own path, and deliberately still `tools/skill/skill.go`: the Go side
# has not been renamed yet, and a path spelled the way we WANT it does not read
# the file — it just makes this gate skip, which is precisely the silent pass the
# test below exists to prevent. Move it when the CLI rename is pinned here.
_CLI_RUNBOOK_TOOL = CLI_SUBMODULE / "internal" / "tools" / "skill" / "skill.go"


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

    Asserted mostly as the ABSENCE of the retired claim rather than the presence
    of a blessed sentence: the wording will be edited for other reasons, and a
    test that pins prose exactly gets updated reflexively without anyone
    rereading what it protects.

    One positive assertion is unavoidable, though — that the paragraph still says
    "as you complete". Absence alone is satisfiable by saying nothing, and a
    scaffold rewritten to "record the last completed step before yielding" trips
    none of the forbidden phrases while meaning exactly what they meant.
    """
    scaffold = SCAFFOLD.read_text(encoding="utf-8").lower()
    assert _PER_STEP_INSTRUCTION in scaffold, (
        "loaded_runbooks.md.j2 no longer instructs a per-step record. Removing the "
        "instruction is how the contradiction comes back without reusing any of its "
        "old wording."
    )
    for claim in _TURN_BOUNDARY_CLAIMS:
        assert claim not in scaffold, (
            f"loaded_runbooks.md.j2 reasserts the turn-boundary contract ({claim!r}), which "
            "the CLI tool description and the runbook_step_consistency task both contradict. "
            "Only the step named in a call is recorded — see this module's docstring."
        )


def test_the_cli_tool_still_describes_a_per_step_record() -> None:
    """The other half of the pair, so a CLI bump that flips it is not silent.

    Without this, aligning the scaffold would look permanently correct while the
    authority it was aligned TO had moved underneath it.
    """
    description = _tool_description("runbook__step__advance").lower()
    assert "as you complete each step" in description, (
        "The CLI's runbook.step.advance description no longer describes a per-step "
        "record. If the CLI moved to turn-boundary checkpointing (and taught "
        "upsertStep to backfill), the scaffold and runbook_step_consistency must move "
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
    if not _CLI_RUNBOOK_TOOL.exists():
        import pytest

        pytest.skip("cli/ submodule not initialised — run `git submodule update --init cli`")
    source = _CLI_RUNBOOK_TOOL.read_text(encoding="utf-8").lower()
    assert "as you complete each step" in source, (
        "The CLI's runbook.step.advance description changed. If it moved to "
        "turn-boundary checkpointing, the scaffold, runbook_step_consistency and this "
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


# ---------------------------------------------------------------------------
# One rule, one owner.
# ---------------------------------------------------------------------------

#: Long enough that ordinary English does not collide by accident, short enough
#: that a copied sentence cannot hide behind a few edited words. Six matched every
#: real duplication when this was written (the display-width preamble, the
#: lead-with-the-answer rule, the status-cue rule) with no false hits across the
#: eight partials plus the contract.
_SHINGLE_WORDS = 6

#: Windows that begin with a function word. This is NOISE SUPPRESSION, not a
#: semantic judgement, and it is worth being honest about which: a six-word window
#: opening "the ... of the ..." is usually a fragment of grammar two layers happen
#: to share, and admitting them made the gate fire on prose nobody had copied. It
#: will therefore also excuse a genuinely duplicated sentence that happens to start
#: with "The", which is the price. Prefer fixing a false positive by giving the
#: rule one owner over widening this tuple.
_FUNCTION_WORD_STARTS = ("the ", "a ", "an ", "and ", "of ", "in ", "to ", "it ", "is ")

#: Phrases two layers may legitimately share, each with the reason. A shared
#: POINTER to the same block is not a restated rule — both layers have to be able
#: to name the thing they are talking about.
_ALLOWED_SHARED = {
    "display context block": "both layers may name the block; only one interprets it",
}


def _shingles(text: str) -> set[str]:
    import re

    words = re.findall(r"[a-z']+", text.lower())
    return {
        " ".join(words[i : i + _SHINGLE_WORDS])
        for i in range(len(words) - _SHINGLE_WORDS + 1)
    }


def test_no_permanent_layer_restates_another() -> None:
    """Output policy had three owners and they had begun to disagree.

    "Lead with the answer", the status-cue rule and the terminal-width preamble
    were each stated in two of `40-communication.md`, `50-formatting.md` and
    `response_contract.md.j2`. Duplication is not merely wasteful here: two copies
    of a rule drift, and the model then has to decide which layer it believes.
    The contract owns what the answer contains; formatting owns what survives the
    renderer; communication owns tone and the continuation delta.

    What this gate can and cannot do, stated plainly so nobody trusts it further
    than it deserves: it finds text that was COPIED between layers. It does not
    find a rule that was RESTATED in different words — "a single-level list" in the
    contract and "keep lists one level deep" in formatting share no six-word window
    and would both pass. Semantic duplication still needs a reader. What this stops
    is the common case, where a rule is pasted into a second layer and the two
    copies then drift until they disagree.

    A phrase two layers may legitimately share goes in `_ALLOWED_SHARED` with its
    reason — not into the opener filter, which is a blunt noise cut.
    """
    import itertools

    layers = {p.name: p.read_text(encoding="utf-8") for p in sorted(BASE_DIR.glob("*.md"))}
    layers["response_contract.md.j2"] = (PROMPTS / "main" / "response_contract.md.j2").read_text(
        encoding="utf-8"
    )
    shingled = {name: _shingles(text) for name, text in layers.items()}

    overlaps: list[str] = []
    for left, right in itertools.combinations(sorted(shingled), 2):
        shared = {
            phrase
            for phrase in shingled[left] & shingled[right]
            if not phrase.startswith(_FUNCTION_WORD_STARTS)
            and not any(allowed in phrase for allowed in _ALLOWED_SHARED)
        }
        for phrase in sorted(shared):
            overlaps.append(f"{left} <-> {right}: …{phrase}…")

    assert not overlaps, (
        "A permanent prompt layer restates another:\n  "
        + "\n  ".join(overlaps)
        + "\n\nGive the rule ONE owner and have the other layer stop stating it. "
        "See this test's docstring for which layer owns what."
    )


# ---------------------------------------------------------------------------
# The two things called "an agent".
# ---------------------------------------------------------------------------
#
# What these gates can and cannot do, stated before the code so nobody trusts
# them further than they deserve. English semantics cannot be proved by
# substring search: a partial rewritten to say "a private sub-agent IS a
# workspace agent" collapses the very distinction below while satisfying every
# presence assertion here. What these DO catch is the specific way the
# contradiction existed and the specific ways it would come back — the retired
# scope words, a category name being dropped, the carve-out being dropped, and
# the positive delegation instruction being deleted (which would end the
# contradiction by removing the feature, the cheapest wrong fix available).
# A reader is still required.

#: The wording that created the collision. `10-hard-rules.md` required
#: `agentTask.spawnForEdits` for an agent spawned "for ANY purpose — edits OR a
#: read-only exploration the user wants delegated", while
#: `20-tool-discipline.md` told the model to delegate exactly that kind of
#: read-only exploration to `subagent.run`. Both partials are in every assistant
#: turn, and the delegation bullet is the one the repo measured as actually
#: moving behaviour — so a model resolving the pair against `subagent.run` loses
#: the feature silently, on the turns where context economy matters most.
#:
#: Listed as several phrasings because the collision is a SCOPE WORD, and the
#: scope word is the part an editor reaches for first.
_UNIVERSAL_AGENT_SCOPES = (
    "for any purpose",
    "for every purpose",
    "to spawn an agent for",
    "whenever you spawn an agent",
)

#: The unqualified claim that put a second contradiction in the same request.
#: `20-tool-discipline.md` said "Every agent and terminal lives in exactly one
#: worktree", one bullet before the new text describing a private sub-agent as
#: holding no worktree. Naming the categories is what makes a generic claim
#: about "every agent" newly false rather than merely vague.
_UNQUALIFIED_WORKTREE_CLAIM = "every agent and terminal lives in exactly one worktree"


def test_the_two_agent_categories_are_named_rather_than_collided() -> None:
    """A rule about "an agent" must say WHICH of the two it governs."""
    hard_rules = (BASE_DIR / "10-hard-rules.md").read_text(encoding="utf-8").lower()
    discipline = (BASE_DIR / "20-tool-discipline.md").read_text(encoding="utf-8").lower()

    for scope in _UNIVERSAL_AGENT_SCOPES:
        assert scope not in hard_rules, (
            f"10-hard-rules.md again routes agents through agentTask.spawnForEdits by an "
            f"unqualified scope ({scope!r}). 20-tool-discipline.md tells the same turn to "
            "delegate read-only search to subagent.run, so any universal scope makes the two "
            "rules contradict."
        )
    assert "visible workspace agent" in hard_rules, (
        "The hard rules no longer name the visible workspace agent category, so the "
        "spawnForEdits requirement is once again a rule about the ambiguous word 'agent'."
    )
    assert "subagent.run" in hard_rules, (
        "The hard rules no longer carve subagent.run out of the spawnForEdits requirement. "
        "The carve-out has to be where the requirement is: a reader of the hard rule cannot "
        "be relied on to reach the tool-discipline partial before acting on it."
    )
    assert "private sub-agent" in discipline and "workspace agent" in discipline, (
        "20-tool-discipline.md no longer distinguishes the private sub-agent from the "
        "visible workspace agent, which is the distinction the hard rule defers to."
    )
    # The cheapest wrong fix for a contradiction between two rules is to delete
    # one of them. Deleting THIS one costs the delegation primitive the base
    # prompt exists to bias toward, and would leave every assertion above green.
    assert "subagent.run" in discipline, (
        "20-tool-discipline.md no longer instructs subagent.run at all. Resolving the "
        "collision by dropping the delegation bullet removes the behaviour the 2026-08-21 "
        "byte ratchet was raised to buy — see tests/unit/test_prompt_budgets.py."
    )
    assert _UNQUALIFIED_WORKTREE_CLAIM not in discipline, (
        "20-tool-discipline.md claims every agent lives in exactly one worktree, in the same "
        "partial that describes the private sub-agent as holding none. Qualify the claim to "
        "workspace agents."
    )


# ---------------------------------------------------------------------------
# Instruction precedence.
# ---------------------------------------------------------------------------

#: The five levels, in the order the prompt must state them. Matched as ORDERED
#: positions rather than mere presence: a sentence naming all five in the
#: reverse order describes the opposite policy and would satisfy any
#: presence-only check. These are the substrings, not the full prose, so the
#: sentence can be reworded around them.
_PRECEDENCE_LEVELS = (
    "hard rules",
    "the user stated",
    "workflow runbook",
    "domain foundation",
    "your own defaults",
)

#: Claims that put a runbook ABOVE the user. Both shipped in `30-runbooks.md`
#: before the precedence order was written down, and both flatly contradict its
#: second level — the runbook was "subordinate only to these hard rules", and a
#: "request in the conversation" (which is where the user speaks) was never
#: licence to disregard one. Writing the order down is what made them visible.
_RUNBOOK_OUTRANKS_USER_CLAIMS = (
    "subordinate only to these hard rules",
    "a request in the conversation as licence",
)

#: The precedence clause the loaded-runbooks scaffold used to carry ("If they
#: conflict on a concrete step, the workflow runbook governs"). It is not that the
#: sentence was absent when it was needed — a workflow-versus-foundation clash
#: needs two loaded runbooks, which is exactly when that paragraph rendered. It is
#: that ONE edge of a five-level order was owned by a conditional template while
#: the rest of the order was owned by a permanent partial, so the two could
#: drift and only a two-runbook turn would show it.
_SCAFFOLD_PRECEDENCE_CLAIM = "the workflow runbook governs"


def test_instruction_precedence_is_stated_once_and_in_order() -> None:
    """One statement of the order, in the always-present layer, unopposed.

    Three separate failures are gated here because they are three ways the same
    rule stops working: the order being wrong, the order being stated twice, and
    the order being contradicted by older prose in its own paragraph. The third
    is the one that shipped — `30-runbooks.md` asserted a runbook was subordinate
    ONLY to the hard rules two sentences before the order put the user above it.
    """
    runbooks_path = BASE_DIR / "30-runbooks.md"
    runbooks = runbooks_path.read_text(encoding="utf-8").lower()

    positions = []
    for level in _PRECEDENCE_LEVELS:
        index = runbooks.find(level)
        assert index != -1, (
            f"30-runbooks.md no longer names {level!r} in the precedence order. The order is the "
            "one statement that lets the model resolve a runbook against a foundation against "
            "the user's own goal."
        )
        positions.append(index)
    assert positions == sorted(positions), (
        "The precedence levels in 30-runbooks.md are out of order: "
        f"{list(zip(_PRECEDENCE_LEVELS, positions, strict=True))}. A reordered list is not a "
        "weaker rule, it is the opposite rule."
    )

    for claim in _RUNBOOK_OUTRANKS_USER_CLAIMS:
        assert claim not in runbooks, (
            f"30-runbooks.md states {claim!r}, which puts an applicable runbook above the user — "
            "the precedence order in the same paragraph puts the user above the runbook. Both "
            "reach the model in one request."
        )

    # Stated ONCE across the permanent layers. Two copies of an ordering is the
    # duplication failure this module exists for, and the ordering is long
    # enough that an accidental second copy means a deliberate one.
    owners = [
        path.name
        for path in sorted(BASE_DIR.glob("*.md"))
        if all(level in path.read_text(encoding="utf-8").lower() for level in _PRECEDENCE_LEVELS)
    ]
    assert owners == [runbooks_path.name], (
        f"The precedence order is stated in {owners} — it must have exactly one owner."
    )

    scaffold = SCAFFOLD.read_text(encoding="utf-8").lower()
    assert _SCAFFOLD_PRECEDENCE_CLAIM not in scaffold, (
        "loaded_runbooks.md.j2 states instruction precedence again. One edge of the order owned "
        "by a conditionally-rendered template, and the rest owned by a permanent partial, is "
        "how the two versions start disagreeing."
    )


# ---- The agent handback: one owner, and a marker nobody here may spell -------
#
# Daintree reports `lastHandback` on a terminal's status when an agent prints the
# marker a prompt asked for. What it MEANS — an observation rather than a verdict,
# worthless once stale, `message` untrusted and lossy — is judgement shared by
# every workflow that spawns, awaits or relays, so it has one owner: the
# foundation, which admission pairs with each of them. The vocabulary is pinned
# rather than a sentence, because the failure worth catching is a trim deleting a
# field name or a caveat, not a reworded clause.

_RUNBOOK_FILES = REPO_ROOT / "src" / "daintree_assistant_server" / "runbooks" / "files"
_TASK_PROMPTS = PROMPTS / "tasks"

_HANDBACK_VOCABULARY = (
    "`lastHandback`",
    "`message`",
    "`submissionToken`",
    "`observedAt`",
    "`truncated:true`",
    "`agentState`",
    "untrusted",
    "lossy",
    "never means still working",
)

# The stem of the marker line Daintree appends to a prompt, with a code only it
# knows. A runbook or task prompt that spelled the format would hand the model a
# second, conflicting instruction to relay — and a code it invented would never
# match. Assembled so this file does not itself trip a repo-wide search for it.
_HANDBACK_MARKER_STEM = "-".join(("daintree", "done"))


def test_the_foundation_owns_what_a_handback_means() -> None:
    foundation = (_RUNBOOK_FILES / "daintree.foundation.md").read_text(encoding="utf-8")
    missing = [term for term in _HANDBACK_VOCABULARY if term not in foundation]
    assert not missing, (
        f"daintree.foundation no longer says {missing} about `lastHandback`. It is the one "
        "owner of what a handback means; the runbooks that read one only point at it."
    )


def test_no_model_facing_text_spells_the_handback_marker() -> None:
    sources = sorted(_RUNBOOK_FILES.glob("*.md")) + sorted(_TASK_PROMPTS.glob("*.md"))
    assert sources, "no runbooks or task prompts found — the scan would pass on nothing"
    offenders = [
        path.name
        for path in sources
        if _HANDBACK_MARKER_STEM in path.read_text(encoding="utf-8").lower()
    ]
    assert not offenders, (
        f"{offenders} spell the handback marker. Daintree owns that format and mints its code; "
        "text the model can read must never write, paraphrase or describe it."
    )
