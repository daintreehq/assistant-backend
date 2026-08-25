"""Budget and hygiene guards for prompts and runbooks.

Golden snapshots record *what* the prompt says; these guard *how big* it gets and
keep known anti-patterns (wide pipe-table examples that prime the model, missing
Report-back templates) from creeping back in. Ceilings are generous — they catch
regression toward bloat, not every incremental edit.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from daintree_assistant_server.prompts.loader import PromptLoader
from daintree_assistant_server.runbooks.catalog import load_catalog

# The permanent base prompt is sent on EVERY request and is the second-largest
# region of it, after the client's tool schemas. A ceiling with 8 KB of slack let
# it drift upward unnoticed for a long time, so this is now a RATCHET: set just
# above the current size, and lowered whenever real compression lands. Raising it
# should require the same argument as adding a rule — what behaviour needs this,
# and why does it have to hold on every turn rather than in a runbook?
#
# Current: ~23 KB across eight partials, down from 31.7 KB.
#
# THE MOVE THIS ANTICIPATED HAS NOW LANDED. The reduction from ~27.5 KB came from
# rules MOVED into `daintree.foundation`, not tighter wording. The earlier note
# here doubted that destination — "the foundation is only paired in when the
# selector picks a `daintree.*` runbook, so it is absent on a greeting or a plain
# question" — which is true but was never measured. Running the shipped classifier
# and `resolve_active_runbooks` over all 166 routing-benchmark cases gives the rate:
# the foundation is active on 153 of the 153 cases that load ANY runbook (100%), and
# the 13 that load none are 6 pure-docs questions (which take the pinned docs
# runbook instead) and 7 genuine greeting/off-domain turns.
#
# That rate is a property of the CLASSIFIER, not a runtime guarantee, and the gap
# matters: `LocalSelector` degrades OPEN on a stale provenance stamp or a runtime
# failure, falling back to the prior active set — which on a COLD degraded turn is
# empty. A real operational request served that way gets no foundation and therefore
# none of the mechanics below. Core irreversible-action safeguards stay in base for
# exactly this reason; what a degraded cold turn loses is spawn hygiene, supervision
# and batching detail, which costs a clumsy turn rather than an unsafe one.
#
# So the split is by TRIGGER, and it is what the current partitioning encodes:
#
#   * A rule that can fire on a turn loading NO runbook stays here. That is every
#     safety and restraint rule (secrets, untrusted data, terminal.close, the
#     irreversible class, confirmation), the turn mechanics, the RENDERER rules in
#     `50-formatting.md`, and `40-communication.md`'s no-task rule — which governs precisely
#     the 7 zero-runbook turns and could not live anywhere else.
#   * A rule that only means anything once the turn IS Daintree work moved to the
#     foundation: supervision modes, finish detection, agent-state vocabulary,
#     batching, spawn hygiene, cheap-vs-capable delegation.
#   * A rule whose trigger is narrower than "Daintree work" moved to the runbook
#     that owns it (compaction-artifact recovery -> status-and-resume; docs
#     citation detail -> the pinned docs runbook).
#
# Several rules were COMPRESSED here rather than moved, deliberately: where the
# foundation elaborates but a zero-runbook turn still needs the invariant, the base
# keeps one tight sentence and the runbook carries the mechanics.
#
# What still gates a further cut is unchanged: a LIVE tool-use benchmark on a paid
# key. This pass only moved rules to destinations proven to co-load, and left the
# safety surface where it was.
# Raised from 28 KiB (28,672 B) once, deliberately, for the two terminal-safety
# rules added after a session where the assistant typed raw git into the user's
# own shell — moving their checkout onto a new branch and breaking the operation
# it was attempting. The test above asks the right question before a raise: does
# the rule have to hold on EVERY turn? It does. That session had no orchestration
# runbook loaded, only the foundation, and a terminal is reachable on any turn — so
# a runbook, which applies only when selected, cannot carry it.
#
# Raised again from 29 KiB for the no-task-turn rule in 40-communication.md: a
# greeting or an off-domain question ("how are you", "capital of France") got a
# bare answer and nothing else, spending a turn the user waited on without
# telling them anything about their own workspace.
#
# Same question, and this one answers it more strongly than the terminal case
# did. Those turns load NO runbook AT ALL — `can_skip_selection` takes the no-task
# fast path so the selector never runs, and `daintree.foundation` is paired in
# only when the selector picks a `daintree.*` runbook. There is no runbook that could
# hold this rule, because on precisely the turns it governs, no runbook exists. The
# base prompt is the only text on the wire.
# RAISED 2026-08-21, 24 KB -> 25 KB, for the delegation rule in
# `20-tool-discipline.md`. The argument this ratchet asks for:
#
#   What behaviour needs this?  `subagent.run` — delegating an expensive SEARCH to
#   a read-only worker in its own conversation — is a new first-class primitive,
#   and its value is entirely in being reached for BEFORE the expensive search
#   happens. A tool the model discovers only after three fruitless rounds has
#   already cost what it exists to save, so the bias has to be prompt-level, not
#   schema-level.
#
#   Why every turn rather than a runbook?  Because a research request is exactly the
#   kind of turn that can load NO runbook — a cold degraded selector falls back to an
#   empty active set — and that turn is the one where searching in the main thread
#   does the most damage. By this file's own split rule ("a rule that can fire on a
#   turn loading NO runbook stays here"), the core bias belongs in base.
#
#   What stayed OUT?  The elaboration — fan-out batching, brief-writing, how to
#   relay a finding — lives in `daintree.foundation`, where the cheap-vs-capable
#   delegation guidance already sits. Base carries one bullet: when to delegate,
#   what never to delegate, and to relay a partial as partial.
# NOTE the scope: this covers `main/base/*.md` only. Output SHAPE — length, lists
# vs tables, status cues, the blocked format — is owned by `response_contract.md.j2`,
# which is the SECOND static system message and is not measured here. A cleanup that
# moves shape rules out of base lowers this number without lowering what the model
# receives, so read a drop against the contract's size too.
#
# RAISED 2026-08-25, 25600 -> 26112 B, for the same change as the
# `40-communication.md` ceiling below — withdrawing the opening-clause permission
# now that the fast-response stage runs on every eligible turn. The whole increase
# is that one rule; nothing else grew. See the per-partial note for the argument.
BASE_PROMPT_CEILING = 26112

# Per-partial ceilings. The aggregate alone hides where growth actually happens —
# tool-discipline is nearly half the prompt on its own, and a rule added there is
# far more likely to be runbook-local than one added to the hard rules.
BASE_PARTIAL_CEILINGS = {
    "00-persona.md": 1024,
    # RAISED 2026-08-22, 4096 -> 4224 B, to end the "an agent" collision.
    #
    #   What behaviour needs this?  This partial required `agentTask.spawnForEdits`
    #   for an agent spawned "for ANY purpose — edits OR a read-only exploration the
    #   user wants delegated", while `20-tool-discipline.md` instructs exactly that
    #   read-only exploration to go to `subagent.run`. Both ship in every assistant
    #   turn. A model resolving the pair toward the HARD rule (the stronger layer, and
    #   the one whose scope word was "ANY") stops delegating searches — silently
    #   losing the primitive the 2026-08-21 ratchet above was raised to buy, on the
    #   turns where main-thread context is most expensive.
    #
    #   Why here rather than only in tool-discipline?  Naming the two categories in
    #   the partial that DEFINES `subagent.run` is necessary but not sufficient: a
    #   reader who acts on the hard rule has, by construction, not yet been persuaded
    #   to read further. Rewriting every generic-agent sentence in this file to name a
    #   category would also work and would cost fewer bytes; it was rejected because
    #   the carve-out then exists only by implication, and an implication is what the
    #   old wording already relied on.
    #
    #   What it cost: 113 B for the rewritten spawn rule, plus 11 B qualifying the
    #   "spawning an AGENT is the only thing that brings a fresh terminal" bullet,
    #   which the naming made newly ambiguous. 4184 B measured against a 4224 B
    #   ceiling — the same ~40 B of slack the 4096 B ceiling carried, deliberately,
    #   so this stays a ratchet rather than a raised roof.
    "10-hard-rules.md": 4224,
    # The turn-finality control line (issue #61). CONDITIONAL now: the container
    # ships this partial only when a compaction producer exists, which today is
    # never, so it costs nothing on a real request — but it is still base by the
    # split rule (it would fire on every reply, a zero-runbook turn included) and
    # cannot live anywhere else. Sized to the measured 645B; the AGGREGATE ceiling
    # binds well before this one does, which is the correct precedence.
    "15-response-declaration.md": 1024,
    # Raised with BASE_PROMPT_CEILING above (see the argument there) — the
    # delegation bullet lands in this partial.
    "20-tool-discipline.md": 10 * 1024,
    "30-runbooks.md": 3 * 1024,
    "35-daintree-docs.md": 2304,
    # Raised from 3 KiB with the base ceiling above, and for the same rule: the
    # no-task turn is a COMMUNICATION concern (what a reply must contain when the
    # user hands you nothing to do), so it belongs here rather than in the hard
    # rules or tool discipline.
    # RAISED 2026-08-25, 4096 -> 4608 B, to withdraw the opening-clause permission.
    #
    #   What behaviour needs this?  The fast-response stage now runs on EVERY
    #   eligible turn, so two or three sentences describing the work are already on
    #   the user's screen before the executor writes anything. The rule this file
    #   used to carry — "only a turn's OPENING round may spend one short clause
    #   orienting the user" — became a licence to say the same thing twice in two
    #   seconds, which is exactly what makes a fast reply read as padded.
    #
    #   Why here rather than a separate partial?  A conditional partial contradicting
    #   this permission was tried and reverted (`TODO.md`): it did not change the
    #   behaviour, and it coincided with tool-call syntax leaking into visible
    #   content. The permission lives in this file, so withdrawing it belongs in this
    #   file — one rule, one owner. There is no longer a deployment without the
    #   preamble, so the rule needs no condition.
    "40-communication.md": 4608,
    "50-formatting.md": 3 * 1024,
}
# The SECOND static system message, and the other half of the permanent prompt.
# It had no ratchet until 2026-08-22, which is how it grew 552 B in one change
# while every base partial was individually guarded: the aggregate ceiling above
# measures `main/base/*.md` ONLY, and its own docstring says so. Two permanent
# regions and one ratchet is not a ratchet — a rule moved out of base and into
# the contract lowers the guarded number without lowering a single byte the model
# receives.
#
# Measured RENDERED (3251 B at introduction), not as the template file: the Jinja
# tags never reach the model, and the file is ~130 B larger than the message. That
# is also how the base prompt and the selector prompt are measured here.
RESPONSE_CONTRACT_CEILING = 3456

# Per-runbook selector-metadata ceilings. whenToUse is the PRIMARY routing signal and
# summary is the secondary one; routing quality lives in them, so these are set
# generously above the current maxima on purpose. A tight ceiling here would do real
# harm (it would push authors to strip the discriminating detail the selector needs) —
# these exist only to catch a whole runbook being pasted into the frontmatter, which
# would land in every selector call forever. Deliberately tighter than the RunbookMeta
# field caps (4096 / 2048), which bound validity, not prompt budget. Current maxima:
# whenToUse 1000B, summary 207B — roughly 2x headroom each.
RUNBOOK_WHEN_TO_USE_CEILING = 2048
RUNBOOK_SUMMARY_CEILING = 512
# Hard ceiling — a runaway guard only (the old multi-agent runbook was 28 KB). A
# genuinely complex runbook is allowed to be large; this just catches unbounded bloat.
RUNBOOK_BODY_HARD_CEILING = 24 * 1024
# Soft budget — a nudge to keep NEW runbooks reasonable, not a hard rule; complex runbooks
# may legitimately exceed it (allowlisted). Loading MULTIPLE runbooks, not shrinking each,
# is the priority, so this is deliberately generous.
RUNBOOK_BODY_RECOMMENDED = 16 * 1024
# Empty for now: multi-agent was trimmed back under RUNBOOK_BODY_RECOMMENDED, so nothing
# currently needs an exemption. Add a runbook id here (with justification) if one legitimately
# grows past the soft budget.
OVERSIZE_ALLOWLIST: set[str] = set()


def _base() -> str:
    return PromptLoader().base_prompt()


def test_base_prompt_under_ceiling() -> None:
    size = len(_base().encode("utf-8"))
    assert size <= BASE_PROMPT_CEILING, (
        f"base prompt {size}B exceeds the {BASE_PROMPT_CEILING}B ratchet. This text "
        "is sent on every request. Before raising the ceiling, ask whether the new "
        "rule has to hold on EVERY turn — if it only matters while orchestrating "
        "agents, or answering a docs question, it belongs in that runbook instead."
    )


def _base_dir() -> Path:
    return PromptLoader().dir / "main" / "base"


@pytest.mark.parametrize("name", sorted(BASE_PARTIAL_CEILINGS))
def test_each_base_partial_under_its_ceiling(name: str) -> None:
    """Per-partial, because the aggregate hides where growth happens."""
    path = _base_dir() / name
    assert path.is_file(), f"{name} is missing; update BASE_PARTIAL_CEILINGS"
    size = len(path.read_bytes())
    ceiling = BASE_PARTIAL_CEILINGS[name]
    assert size <= ceiling, f"{name} is {size}B, over its {ceiling}B ceiling"


def test_every_base_partial_has_a_ceiling() -> None:
    """A new partial must be budgeted deliberately, not slip in unbounded."""
    on_disk = {p.name for p in _base_dir().glob("*.md")}
    assert on_disk == set(BASE_PARTIAL_CEILINGS), (
        "base partials and BASE_PARTIAL_CEILINGS disagree: "
        f"{sorted(on_disk ^ set(BASE_PARTIAL_CEILINGS))}"
    )


# The sub-agent persona, budgeted separately from the base prompt because it is a
# separate prompt on a separate path: it ships on every DELEGATED turn, and the
# sub-agent is the profile least able to absorb prompt bytes — it runs to a hard
# round budget in a context it will spend on tool results. Sized to the measured
# 5,373B at introduction, rounded up per partial, so this can only ratchet.
#
# Note the split rule differs from base's. Base asks "does this rule fire on a turn
# with no runbook loaded"; here that is every turn — a sub-agent's menu is 2 runbooks
# wide today and most delegated turns load none — so the question is instead
# whether the rule is about being a BOUNDED READ-ONLY WORKER. If it is about how to
# do one particular kind of research, it belongs in a runbook.
SUBAGENT_PROMPT_CEILING = 6 * 1024

SUBAGENT_PARTIAL_CEILINGS = {
    "00-persona.md": 1024,
    "10-hard-rules.md": 2 * 1024,
    "20-method.md": 2 * 1024,
    "30-report.md": 2 * 1024,
}


def _subagent_dir() -> Path:
    return PromptLoader().dir / "main" / "subagent"


def test_subagent_prompt_under_ceiling() -> None:
    size = len(PromptLoader().subagent_prompt().encode("utf-8"))
    assert size <= SUBAGENT_PROMPT_CEILING, (
        f"sub-agent prompt {size}B exceeds the {SUBAGENT_PROMPT_CEILING}B ratchet. "
        "A sub-agent spends its context on tool results, not on instructions — "
        "before raising this, ask whether the rule is about being a bounded "
        "read-only worker at all, or whether it belongs in a runbook."
    )


@pytest.mark.parametrize("name", sorted(SUBAGENT_PARTIAL_CEILINGS))
def test_each_subagent_partial_under_its_ceiling(name: str) -> None:
    path = _subagent_dir() / name
    assert path.is_file(), f"{name} is missing; update SUBAGENT_PARTIAL_CEILINGS"
    size = len(path.read_bytes())
    ceiling = SUBAGENT_PARTIAL_CEILINGS[name]
    assert size <= ceiling, f"{name} is {size}B, over its {ceiling}B ceiling"


def test_every_subagent_partial_has_a_ceiling() -> None:
    """Same rule as the base prompt: a new partial is budgeted, never unbounded."""
    on_disk = {p.name for p in _subagent_dir().glob("*.md")}
    assert on_disk == set(SUBAGENT_PARTIAL_CEILINGS), (
        "sub-agent partials and SUBAGENT_PARTIAL_CEILINGS disagree: "
        f"{sorted(on_disk ^ set(SUBAGENT_PARTIAL_CEILINGS))}"
    )


def test_runbook_when_to_use_under_ceiling() -> None:
    for runbook in load_catalog().runbooks:
        size = len(runbook.when_to_use.encode("utf-8"))
        assert size <= RUNBOOK_WHEN_TO_USE_CEILING, (
            f"{runbook.id} whenToUse is {size}B, over the {RUNBOOK_WHEN_TO_USE_CEILING}B "
            "ceiling — it ships in every selector call; put procedure in the body"
        )


def test_runbook_summary_under_ceiling() -> None:
    for runbook in load_catalog().runbooks:
        size = len(runbook.summary.encode("utf-8"))
        assert size <= RUNBOOK_SUMMARY_CEILING, (
            f"{runbook.id} summary is {size}B, over the {RUNBOOK_SUMMARY_CEILING}B ceiling — "
            "summary is a one-line signal; routing detail belongs in whenToUse"
        )


def test_formatting_prompt_has_no_pipe_table_example() -> None:
    # A rendered ``|---|`` example primes the very layout the contract discourages.
    formatting = (PromptLoader().dir / "main" / "base" / "50-formatting.md").read_text(
        encoding="utf-8"
    )
    assert "|---" not in formatting
    assert "|--" not in formatting


def test_base_prompt_has_no_pipe_table_example() -> None:
    assert "|---" not in _base()


def test_runbook_bodies_under_hard_ceiling() -> None:
    for runbook in load_catalog().runbooks:
        size = len(runbook.body.encode("utf-8"))
        assert size <= RUNBOOK_BODY_HARD_CEILING, f"{runbook.id} body {size}B exceeds hard ceiling"


def test_new_runbook_bodies_under_recommended_budget() -> None:
    for runbook in load_catalog().runbooks:
        if runbook.id in OVERSIZE_ALLOWLIST:
            continue
        size = len(runbook.body.encode("utf-8"))
        assert size <= RUNBOOK_BODY_RECOMMENDED, (
            f"{runbook.id} body {size}B exceeds the {RUNBOOK_BODY_RECOMMENDED}B recommended budget; "
            "split or trim it, or add it to OVERSIZE_ALLOWLIST with justification"
        )


def test_oversize_allowlist_is_not_stale() -> None:
    # If an allowlisted runbook drops under budget, remove it from the allowlist.
    catalog = load_catalog()
    for runbook_id in OVERSIZE_ALLOWLIST:
        runbook = catalog.get(runbook_id)
        assert runbook is not None, f"allowlisted runbook {runbook_id} no longer exists"
        size = len(runbook.body.encode("utf-8"))
        assert size > RUNBOOK_BODY_RECOMMENDED, (
            f"{runbook_id} is now {size}B (under budget) — remove it from OVERSIZE_ALLOWLIST"
        )


def test_runbooks_have_no_pipe_tables() -> None:
    for runbook in load_catalog().runbooks:
        assert "|---" not in runbook.body, f"{runbook.id} contains a pipe table"


def test_runbooks_end_with_report_back() -> None:
    for runbook in load_catalog().runbooks:
        assert "report back" in runbook.body.lower(), f"{runbook.id} has no Report-back template"


def test_response_contract_under_ceiling() -> None:
    """The other permanent system message, ratcheted like the first.

    Rendered through the assembler rather than read off disk, so this measures
    what is actually sent. Before raising it, ask the question the base ceiling
    asks in reverse: output SHAPE is this file's job, so a rule arriving here
    from `main/base/*.md` is a correct move and should show up as a matching
    DROP in the base numbers above. Growth on both sides at once is new text,
    not a relocation, and needs the same written argument a base raise does.
    """
    from daintree_assistant_server.config import Settings
    from daintree_assistant_server.services.prompt_assembler import PromptAssembler

    settings = Settings(STATE_SIGNING_KEY_CURRENT="x" * 32)
    rendered = PromptAssembler(PromptLoader(), settings).render_response_contract()
    size = len(rendered.encode("utf-8"))
    assert size <= RESPONSE_CONTRACT_CEILING, (
        f"the response contract renders to {size}B, over its {RESPONSE_CONTRACT_CEILING}B "
        "ratchet. It is a system message on every request, ahead of ~21.5k tokens of tool "
        "schemas in the serving model's serialization — these bytes are paid by every caller "
        "on every turn."
    )
