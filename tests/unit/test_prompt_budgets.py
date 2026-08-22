"""Budget and hygiene guards for prompts and skills.

Golden snapshots record *what* the prompt says; these guard *how big* it gets and
keep known anti-patterns (wide pipe-table examples that prime the model, missing
Report-back templates) from creeping back in. Ceilings are generous — they catch
regression toward bloat, not every incremental edit.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from daintree_assistant_server.prompts.loader import TEMPLATE_SELECTOR_SYSTEM, PromptLoader
from daintree_assistant_server.skills.catalog import load_catalog

# The permanent base prompt is sent on EVERY request and is the second-largest
# region of it, after the client's tool schemas. A ceiling with 8 KB of slack let
# it drift upward unnoticed for a long time, so this is now a RATCHET: set just
# above the current size, and lowered whenever real compression lands. Raising it
# should require the same argument as adding a rule — what behaviour needs this,
# and why does it have to hold on every turn rather than in a skill?
#
# Current: ~23 KB across eight partials, down from 31.7 KB.
#
# THE MOVE THIS ANTICIPATED HAS NOW LANDED. The reduction from ~27.5 KB came from
# rules MOVED into `daintree.foundation`, not tighter wording. The earlier note
# here doubted that destination — "the foundation is only paired in when the
# selector picks a `daintree.*` skill, so it is absent on a greeting or a plain
# question" — which is true but was never measured. Running the shipped classifier
# and `resolve_active_skills` over all 166 routing-benchmark cases gives the rate:
# the foundation is active on 153 of the 153 cases that load ANY skill (100%), and
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
#   * A rule that can fire on a turn loading NO skill stays here. That is every
#     safety and restraint rule (secrets, untrusted data, terminal.close, the
#     irreversible class, confirmation), the turn mechanics, the formatting
#     contract, and `40-communication.md`'s no-task rule — which governs precisely
#     the 7 zero-skill turns and could not live anywhere else.
#   * A rule that only means anything once the turn IS Daintree work moved to the
#     foundation: supervision modes, finish detection, agent-state vocabulary,
#     batching, spawn hygiene, cheap-vs-capable delegation.
#   * A rule whose trigger is narrower than "Daintree work" moved to the runbook
#     that owns it (compaction-artifact recovery -> status-and-resume; docs
#     citation detail -> the pinned docs runbook).
#
# Several rules were COMPRESSED here rather than moved, deliberately: where the
# foundation elaborates but a zero-skill turn still needs the invariant, the base
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
# skill loaded, only the foundation, and a terminal is reachable on any turn — so
# a skill, which applies only when selected, cannot carry it.
#
# Raised again from 29 KiB for the no-task-turn rule in 40-communication.md: a
# greeting or an off-domain question ("how are you", "capital of France") got a
# bare answer and nothing else, spending a turn the user waited on without
# telling them anything about their own workspace.
#
# Same question, and this one answers it more strongly than the terminal case
# did. Those turns load NO skill AT ALL — `can_skip_selection` takes the no-task
# fast path so the selector never runs, and `daintree.foundation` is paired in
# only when the selector picks a `daintree.*` skill. There is no skill that could
# hold this rule, because on precisely the turns it governs, no skill exists. The
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
#   Why every turn rather than a skill?  Because a research request is exactly the
#   kind of turn that can load NO skill — a cold degraded selector falls back to an
#   empty active set — and that turn is the one where searching in the main thread
#   does the most damage. By this file's own split rule ("a rule that can fire on a
#   turn loading NO skill stays here"), the core bias belongs in base.
#
#   What stayed OUT?  The elaboration — fan-out batching, brief-writing, how to
#   relay a finding — lives in `daintree.foundation`, where the cheap-vs-capable
#   delegation guidance already sits. Base carries one bullet: when to delegate,
#   what never to delegate, and to relay a partial as partial.
BASE_PROMPT_CEILING = 25 * 1024

# Per-partial ceilings. The aggregate alone hides where growth actually happens —
# tool-discipline is nearly half the prompt on its own, and a rule added there is
# far more likely to be skill-local than one added to the hard rules.
BASE_PARTIAL_CEILINGS = {
    "00-persona.md": 1024,
    "10-hard-rules.md": 4 * 1024,
    # The turn-finality control line (issue #61). CONDITIONAL now: the container
    # ships this partial only when a compaction producer exists, which today is
    # never, so it costs nothing on a real request — but it is still base by the
    # split rule (it would fire on every reply, a zero-skill turn included) and
    # cannot live anywhere else. Sized to the measured 645B; the AGGREGATE ceiling
    # binds well before this one does, which is the correct precedence.
    "15-response-declaration.md": 1024,
    # Raised with BASE_PROMPT_CEILING above (see the argument there) — the
    # delegation bullet lands in this partial.
    "20-tool-discipline.md": 10 * 1024,
    "30-skills.md": 3 * 1024,
    "35-daintree-docs.md": 2304,
    # Raised from 3 KiB with the base ceiling above, and for the same rule: the
    # no-task turn is a COMMUNICATION concern (what a reply must contain when the
    # user hands you nothing to do), so it belongs here rather than in the hard
    # rules or tool discipline.
    "40-communication.md": 4 * 1024,
    "50-formatting.md": 3 * 1024,
}
# The selector system prompt is the catalog rendered as a classification menu, so it
# grows monotonically with every skill added — and it sits on the turn's FIRST-BYTE
# path (the selector is hardcoded non-thinking precisely to keep that path fast). Left
# unguarded, catalog growth silently taxes every turn's latency. This is a runaway
# guard, not a style target: measured 28,110B over 23 skills (~1.2 KB/skill), so this
# ceiling leaves room for roughly 30 more skills at today's density and trips only on a
# rough doubling. Raise it deliberately (and re-measure per-skill cost) if the catalog
# genuinely grows into it; do not raise it to paper over one bloated frontmatter.
SELECTOR_PROMPT_CEILING = 64 * 1024
# Per-skill selector-metadata ceilings. whenToUse is the PRIMARY routing signal and
# summary is the secondary one; routing quality lives in them, so these are set
# generously above the current maxima on purpose. A tight ceiling here would do real
# harm (it would push authors to strip the discriminating detail the selector needs) —
# these exist only to catch a whole runbook being pasted into the frontmatter, which
# would land in every selector call forever. Deliberately tighter than the SkillMeta
# field caps (4096 / 2048), which bound validity, not prompt budget. Current maxima:
# whenToUse 1000B, summary 207B — roughly 2x headroom each.
SKILL_WHEN_TO_USE_CEILING = 2048
SKILL_SUMMARY_CEILING = 512
# Hard ceiling — a runaway guard only (the old multi-agent runbook was 28 KB). A
# genuinely complex skill is allowed to be large; this just catches unbounded bloat.
SKILL_BODY_HARD_CEILING = 24 * 1024
# Soft budget — a nudge to keep NEW skills reasonable, not a hard rule; complex skills
# may legitimately exceed it (allowlisted). Loading MULTIPLE skills, not shrinking each,
# is the priority, so this is deliberately generous.
SKILL_BODY_RECOMMENDED = 16 * 1024
# Empty for now: multi-agent was trimmed back under SKILL_BODY_RECOMMENDED, so nothing
# currently needs an exemption. Add a skill id here (with justification) if one legitimately
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
        "agents, or answering a docs question, it belongs in that skill instead."
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
# with no skill loaded"; here that is every turn — a sub-agent's menu is 2 skills
# wide today and most delegated turns load none — so the question is instead
# whether the rule is about being a BOUNDED READ-ONLY WORKER. If it is about how to
# do one particular kind of research, it belongs in a skill.
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
        "read-only worker at all, or whether it belongs in a skill."
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


def test_selector_prompt_under_ceiling() -> None:
    # Rendered against the REAL catalog, the same way SkillSelector.select renders it,
    # so the guard tracks what production actually sends.
    catalog = load_catalog()
    rendered = PromptLoader().render(
        TEMPLATE_SELECTOR_SYSTEM,
        catalog=catalog.selector_metadata(),
        active_ids=[],
        max_active=3,
    )
    size = len(rendered.encode("utf-8"))
    assert size <= SELECTOR_PROMPT_CEILING, (
        f"selector prompt {size}B over {len(catalog.skills)} skills exceeds the "
        f"{SELECTOR_PROMPT_CEILING}B ceiling — this prompt gates the turn's first byte; "
        "trim per-skill summary/whenToUse or drop a field from selector_view()"
    )


def test_skill_when_to_use_under_ceiling() -> None:
    for skill in load_catalog().skills:
        size = len(skill.when_to_use.encode("utf-8"))
        assert size <= SKILL_WHEN_TO_USE_CEILING, (
            f"{skill.id} whenToUse is {size}B, over the {SKILL_WHEN_TO_USE_CEILING}B "
            "ceiling — it ships in every selector call; put procedure in the body"
        )


def test_skill_summary_under_ceiling() -> None:
    for skill in load_catalog().skills:
        size = len(skill.summary.encode("utf-8"))
        assert size <= SKILL_SUMMARY_CEILING, (
            f"{skill.id} summary is {size}B, over the {SKILL_SUMMARY_CEILING}B ceiling — "
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


def test_skill_bodies_under_hard_ceiling() -> None:
    for skill in load_catalog().skills:
        size = len(skill.body.encode("utf-8"))
        assert size <= SKILL_BODY_HARD_CEILING, f"{skill.id} body {size}B exceeds hard ceiling"


def test_new_skill_bodies_under_recommended_budget() -> None:
    for skill in load_catalog().skills:
        if skill.id in OVERSIZE_ALLOWLIST:
            continue
        size = len(skill.body.encode("utf-8"))
        assert size <= SKILL_BODY_RECOMMENDED, (
            f"{skill.id} body {size}B exceeds the {SKILL_BODY_RECOMMENDED}B recommended budget; "
            "split or trim it, or add it to OVERSIZE_ALLOWLIST with justification"
        )


def test_oversize_allowlist_is_not_stale() -> None:
    # If an allowlisted skill drops under budget, remove it from the allowlist.
    catalog = load_catalog()
    for skill_id in OVERSIZE_ALLOWLIST:
        skill = catalog.get(skill_id)
        assert skill is not None, f"allowlisted skill {skill_id} no longer exists"
        size = len(skill.body.encode("utf-8"))
        assert size > SKILL_BODY_RECOMMENDED, (
            f"{skill_id} is now {size}B (under budget) — remove it from OVERSIZE_ALLOWLIST"
        )


def test_skills_have_no_pipe_tables() -> None:
    for skill in load_catalog().skills:
        assert "|---" not in skill.body, f"{skill.id} contains a pipe table"


def test_skills_end_with_report_back() -> None:
    for skill in load_catalog().skills:
        assert "report back" in skill.body.lower(), f"{skill.id} has no Report-back template"
