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
#
# RAISED 26112 -> 26368 B, for ONE new rule in `20-tool-discipline.md`: a delay the
# user asked for is part of the goal.
#
#   What behaviour needs this?  A request to act after a stated wait was answered by
#   acting immediately, and then by reporting the wait as having happened. The
#   correction has to fire on a ZERO-RUNBOOK turn — the incident had no runbook
#   loaded — so a runbook cannot own it and `daintree.orchestration.watch-outcome`
#   (which does own the mechanism) is not reached in time to help.
#
#   Why here rather than the hard rules?  It is turn mechanics: it says what to do
#   with a delay, not what is permitted. It sits directly under the "no reliable
#   clock" bullet, which is the other half of the same subject and its only
#   plausible alternative owner.
#
#   What it cost: 176 B for the new bullet, less 33 B recovered by tightening the
#   Supervision bullet in the same file (a refused unattended call is refused ON THE
#   SPOT; the pending-approval item authorizes future fires and replays nothing).
#   26183 B measured against a 26368 B ceiling — the same ~185 B of slack the 26112 B
#   ceiling carried, so this stays a ratchet rather than a raised roof.
BASE_PROMPT_CEILING = 26368

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


# ---- runbook confirmations ------------------------------------------------------
#
# The confirmation is the only runbook text that reaches a user's screen VERBATIM,
# chosen by a classifier score before the model has read anything
# (`runbooks/confirmations.py`, and `docs/RUNBOOK_CONFIRMATIONS.md` for the authoring rules).
# Everything else in a runbook is advice a model mediates; this is published. So
# its house style is a test rather than a convention — the failure mode is a
# wrong sentence in front of a real person, and there is no model in the loop to
# soften it.
#
# THE REGISTER IS COMPREHENSION, NOT ANNOUNCEMENT, and the reason is structural:
# the executor opens its own reply with "I'll start by reading the current
# state…", so a confirmation that also announces is a visible duplicate of the line
# rendering directly beneath it.

#: Words that describe the machinery rather than the work. The product's own
#: user-facing nouns — agents, terminals, worktrees, watchers — are deliberately
#: NOT here: they are what the user said, and a comprehension line about
#: supervising agents cannot be written without them.
_OPENER_BANNED_WORDS = (
    "skill",
    "classifier",
    "selector",
    "catalog",
    "procedure",
    "prompt",
)

#: Characters that either do not survive the terminal or set the wrong register.
_OPENER_BANNED_CHARS = ("`", "*", "#", "|", "!", "?", "\n", "\r")


def _confirmations() -> list[tuple[str, str]]:
    return [(r.id, r.confirmation) for r in load_catalog().runbooks if r.confirmation]


def test_every_user_facing_runbook_has_a_confirmation() -> None:
    """A new runbook lands with its opening paragraph, or it does not land.

    This is the gate that keeps the feature whole. A runbook with no confirmation is
    silently exempt from the whole mechanism — it loads, the turn works, and the
    only symptom is that the product goes quiet for that one kind of request.
    Nothing else would ever notice.

    Two exemptions, both structural rather than editorial: a FOUNDATION is paired
    in by admission without a score, and the SERVER-PINNED documentation runbook
    is hidden from the selector, so neither can be scored and neither can ever
    speak. `test_runbook_confirmations.py` asserts the converse — that neither carries
    a confirmation it could not use.
    """
    from daintree_assistant_server.config import DOCS_RUNBOOK_ID

    missing = [
        r.id
        for r in load_catalog().runbooks
        if r.confirmation is None and not r.meta.foundation and r.id != DOCS_RUNBOOK_ID
    ]
    assert not missing, (
        "these runbooks have no `confirmation` and will load silently: "
        f"{missing} — see docs/RUNBOOK_CONFIRMATIONS.md"
    )


def test_every_confirmation_passes_the_preamble_validator() -> None:
    """The authored text must clear the bar the LIVE model's text had to clear.

    `validate_preamble` is the retired fast-response stage's own validator, and
    reusing it is the whole point rather than a convenience: these confirmations occupy
    exactly that slot, under exactly that display contract, and were written to
    the register that prompt spent most of its length establishing. A separate
    reimplementation here could drift from it, and the drift would show up as a
    duplicate announcement on a user's screen rather than as a failing test.

    It covers the announcement opening, overclaimed progress, internal
    disclosure, markup, structure, control markers and both length bounds.
    """
    from daintree_assistant_server.services.fast_response import validate_preamble

    for rid, confirmation in _confirmations():
        verdict = validate_preamble(confirmation, finish_reason="stop", had_tool_calls=False)
        assert verdict.valid, (
            f"{rid} confirmation is refused by the preamble validator "
            f"({verdict.reject_reason}): {confirmation!r} — see docs/RUNBOOK_CONFIRMATIONS.md"
        )
        # `valid` alone is not enough. The validator NORMALIZES before it judges —
        # it folds exotic spaces (U+202F and friends) to plain ones — so a caption
        # containing one passes here while the server emits the UNNORMALIZED
        # original, which is the string that reaches a terminal. Requiring the
        # verdict's content to be the authored text is what closes the gap between
        # "the validator would accept this" and "this is what we send".
        assert verdict.content == confirmation, (
            f"{rid} confirmation only passes after normalization; the server emits the "
            f"original: {confirmation!r} -> {verdict.content!r}"
        )


def test_confirmations_are_two_sentences() -> None:
    """The shape, and the bound that carries the whole design.

    Three revisions bracketed this. A 140-char ANNOUNCEMENT said nothing and
    duplicated the executor's opening. A 400-char PARAGRAPH measured 62.6 words
    on average — about fifteen seconds of reading in a window that is often one
    second, so the answer arrived mid-paragraph. An 80-char CAPTION was scannable
    but too compressed to state a goal.

    Two sentences is the settled form: a lead that lands in a glance, then one
    sentence of scope. The WORD ceiling is what bites; the character ceiling
    stops one very long sentence sneaking past it.
    """
    import re

    from daintree_assistant_server.runbooks.models import CONFIRMATION_MAX_CHARS

    for rid, confirmation in _confirmations():
        words = len(confirmation.split())
        assert len(confirmation) <= CONFIRMATION_MAX_CHARS, (
            f"{rid} confirmation is {len(confirmation)} chars, over "
            f"{CONFIRMATION_MAX_CHARS}: {confirmation!r}"
        )
        assert words <= 38, (
            f"{rid} confirmation is {words} words, over the 38-word ceiling — it "
            f"is two sentences, not a paragraph: {confirmation!r}"
        )
        assert words >= 15, (
            f"{rid} confirmation is only {words} words — too compressed to state "
            f"a goal, which is the failure the caption revision had: {confirmation!r}"
        )
        assert confirmation == confirmation.strip()
        sentences = re.findall(r"[.!?](?:\s|$)", confirmation)
        assert len(sentences) == 2, (
            f"{rid} confirmation reads as {len(sentences)} sentences; the shape is "
            f"a lead then a scope: {confirmation!r}"
        )


def test_confirmation_leads_are_a_short_gerund_sentence() -> None:
    """The lead is the primary scan target and must survive a 500ms glance.

    Three to eight words, and no first-person subject — the gerund is what lets
    it say WHAT IS HAPPENING without competing with the executor's own "I'll
    start by…" opening. `test_confirmations_never_announce` enforces the person
    rule over the whole string; this one holds the lead's shape and length.
    """
    for rid, confirmation in _confirmations():
        lead = confirmation.split(".", 1)[0]
        words = len(lead.split())
        assert 3 <= words <= 8, (
            f"{rid} confirmation's lead sentence is {words} words; it must be "
            f"three to eight so a glance lands on it: {lead!r}"
        )
        assert lead[0].isupper(), (
            f"{rid} confirmation does not open in sentence case: {confirmation!r}"
        )


def test_confirmation_leads_are_distinct() -> None:
    """Two runbooks opening with the same sentence defeat the purpose.

    The distinctness of the whole string is checked below; this checks the part a
    reader actually uses. A lead that matches another's answers "which kind of
    work did I get?" with the wrong answer.
    """
    seen: dict[str, str] = {}
    for rid, confirmation in _confirmations():
        lead = confirmation.split(".", 1)[0].casefold()
        assert lead not in seen, (
            f"{rid} and {seen[lead]} share the lead sentence {lead!r}, so neither "
            "tells the user which kind of work they got"
        )
        seen[lead] = rid


def test_confirmations_state_a_goal_not_an_outcome() -> None:
    """The second sentence carries the goal, and must not assert a result.

    Under expectation-disconfirmation theory the penalty for a stated goal the
    result then misses is WORSE than having stated no goal — so the house
    phrasing is "the aim is to" / "the goal is", and a completed-work verb in the
    future or perfect is refused. `validate_preamble`'s own overclaim patterns
    cover the past tense; this covers the FORWARD promise it was never written
    for.
    """
    import re

    promises = re.compile(
        r"\b(?:will|shall)\s+(?:then\s+)?(?:be\s+)?"
        r"(?:fix|fixes|fixed|resolve|resolved|repair|repaired|correct|corrected|"
        r"complete|completed|guarantee|guaranteed|ensure|ensured)\b",
        re.I,
    )
    for rid, confirmation in _confirmations():
        found = promises.search(confirmation)
        assert found is None, (
            f"{rid} confirmation promises an outcome ({found.group(0)!r}), which "
            f"the answer then has to live up to: {confirmation!r}"
        )


def test_confirmations_never_announce() -> None:
    """No first person, anywhere — not just at the opening.

    `validate_preamble` rejects an announcing OPENING. This goes further, because
    an authored line has no excuse for a buried one: "the branch gets checked
    first, then I'll summarise" passes an opening test and is still the duplicate
    register. Banning the first person outright is the cheap, total version of
    the rule, and every worked example in docs/RUNBOOK_CONFIRMATIONS.md already obeys
    it.
    """
    import re

    # The apostrophes are REQUIRED on the contractions. Without them "lets" and
    # "were" are ordinary English words — "a record that lets the thread survive"
    # is exactly the prose this register wants, and rejecting it would push
    # authors into worse sentences to satisfy a check not aimed at them.
    first_person = re.compile(r"\b(?:I|I'll|I'm|I've|we'll|we're|let's)\b")
    for rid, confirmation in _confirmations():
        found = first_person.search(confirmation)
        assert found is None, (
            f"{rid} confirmation uses the first person ({found.group(0)!r}), which is the "
            f"executor's register and duplicates its opening line: {confirmation!r}"
        )


def test_confirmations_name_no_internal_machinery() -> None:
    """Beyond what `_DISCLOSURE` in the validator already refuses.

    Matched on WORD BOUNDARIES, not as substrings. A plain `in` test rejected
    "unprompted" for containing "prompt" — a false positive that pushes an author
    toward worse prose to satisfy a check that was not aimed at them.
    """
    import re

    for rid, confirmation in _confirmations():
        for word in _OPENER_BANNED_WORDS:
            assert not re.search(rf"\b{word}\b", confirmation, re.I), (
                f"{rid} confirmation names internal machinery ({word!r}): {confirmation!r} — "
                "say what the user would say"
            )


def test_confirmations_carry_no_markup_or_shouting() -> None:
    for rid, confirmation in _confirmations():
        for char in _OPENER_BANNED_CHARS:
            assert char not in confirmation, (
                f"{rid} confirmation contains {char!r}, which the terminal renders raw "
                f"or reads as the wrong register: {confirmation!r}"
            )
        assert not any(ord(c) > 0x2100 for c in confirmation), (
            f"{rid} confirmation contains an emoji or symbol: {confirmation!r}"
        )


def test_confirmations_invent_nothing() -> None:
    """Chosen before anything has been read, so it cannot know a number.

    Digits are the cheapest reliable tell that a confirmation has drifted from "what
    this kind of request turns on" into "what the answer is".
    """
    for rid, confirmation in _confirmations():
        assert not any(c.isdigit() for c in confirmation), (
            f"{rid} confirmation states a number before anything has been read: {confirmation!r}"
        )


def test_confirmations_do_not_restate_the_question() -> None:
    """The one register failure the shared validator does not catch.

    Repeating the request back proves nothing about whether it was understood and
    burns the second or two the confirmation exists to fill.
    """
    openings = (
        "the question asks",
        "the request asks",
        "this request is about",
        "the request hinges",
        "the request targets",
        "you are asking",
        "you want to",
    )
    for rid, confirmation in _confirmations():
        lowered = confirmation.lower()
        for phrase in openings:
            assert not lowered.startswith(phrase), (
                f"{rid} confirmation restates the question ({phrase!r}): {confirmation!r}"
            )


def test_confirmations_are_distinct() -> None:
    """Two runbooks sharing text make the announcement uninformative.

    Neighbours are deliberately written to be interchangeable-ish, so near
    misses are expected and fine; an EXACT duplicate is a copy-paste, and reads
    as a canned response — the opposite of showing this ask was understood.
    """
    seen: dict[str, str] = {}
    for rid, confirmation in _confirmations():
        assert confirmation not in seen, (
            f"{rid} and {seen[confirmation]} share a confirmation verbatim: {confirmation!r}"
        )
        seen[confirmation] = rid


def test_the_confirmation_threshold_is_not_duplicated_in_prose() -> None:
    """`docs/RUNBOOK_CONFIRMATIONS.md` quotes the threshold; `config.py` owns it.

    A number written in two places drifts, and this one drifts SILENTLY: the doc
    would keep telling authors to write for a confidence the server no longer
    requires, and nothing else in the repo compares them.
    """
    from pathlib import Path

    from daintree_assistant_server.config import RUNBOOK_CONFIRMATION_THRESHOLD

    doc = Path("docs/RUNBOOK_CONFIRMATIONS.md").read_text(encoding="utf-8")
    assert f"{RUNBOOK_CONFIRMATION_THRESHOLD:.2f}" in doc, (
        f"docs/RUNBOOK_CONFIRMATIONS.md does not mention the shipped threshold "
        f"{RUNBOOK_CONFIRMATION_THRESHOLD:.2f} — update the doc with the constant"
    )


def test_editing_a_confirmation_busts_the_catalog_revision() -> None:
    """The claim `runbooks/models.py` makes about frontmatter, actually tested.

    A confirmation rides `content_hash_of`, so editing one must change
    `catalog_revision` and force a client holding older state to re-select. If it
    did not, a caption could change under a live conversation while every client
    kept replaying a token minted against the previous text.
    """
    from dataclasses import replace

    from daintree_assistant_server.runbooks.catalog import Catalog, load_catalog

    original = load_catalog()
    target = next(r for r in original.runbooks if r.confirmation)
    edited = Catalog(
        tuple(
            replace(r, meta=r.meta.model_copy(update={"confirmation": "Changed mode — a different boundary entirely"}))
            if r.id == target.id
            else r
            for r in original.runbooks
        )
    )
    assert edited.revision != original.revision
