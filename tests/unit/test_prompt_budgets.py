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
# Current: ~27.5 KB across seven partials, down from 31.7 KB.
#
# WHY IT STOPPED THERE. The remaining reduction to the 15-20 KB target needs
# rules MOVED out of the permanent block, not tighter wording — and the two
# destinations both have a catch:
#
#   * A skill. `daintree.foundation` is only paired in when the selector picks a
#     `daintree.*` skill, so it is absent on a greeting or a plain question. An
#     audit of the six bullets trimmed here found the foundation does NOT in fact
#     restate several of them (per-task supervision scope, the one-agent extract
#     wait, partial results on an interrupted await, PR-watcher auto-stop), so
#     those were restored rather than left to a skill that may not load.
#   * A tool description. Those live in the CLI repository, not here.
#
# Both routes change behaviour in ways only a LIVE tool-use benchmark can
# falsify, and that benchmark needs a paid key. Compressing a safety-critical
# prompt on faith is exactly the change that looks fine offline and degrades in
# production, so the byte target waits on the measurement rather than the other
# way round.
BASE_PROMPT_CEILING = 28 * 1024

# Per-partial ceilings. The aggregate alone hides where growth actually happens —
# tool-discipline is nearly half the prompt on its own, and a rule added there is
# far more likely to be skill-local than one added to the hard rules.
BASE_PARTIAL_CEILINGS = {
    "00-persona.md": 1024,
    "10-hard-rules.md": 5 * 1024,
    "20-tool-discipline.md": 14 * 1024,
    "30-skills.md": 3 * 1024,
    "35-daintree-docs.md": 3 * 1024,
    "40-communication.md": 3 * 1024,
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
