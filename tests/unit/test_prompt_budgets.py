"""Budget and hygiene guards for prompts and skills.

Golden snapshots record *what* the prompt says; these guard *how big* it gets and
keep known anti-patterns (wide pipe-table examples that prime the model, missing
Report-back templates) from creeping back in. Ceilings are generous — they catch
regression toward bloat, not every incremental edit.
"""

from __future__ import annotations

from daintree_assistant_server.prompts.loader import PromptLoader
from daintree_assistant_server.skills.catalog import load_catalog

# Base prompt is ~32 KB; this ceiling catches regrowth without churning on small edits.
BASE_PROMPT_CEILING = 36 * 1024
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
    assert size <= BASE_PROMPT_CEILING, f"base prompt {size}B exceeds {BASE_PROMPT_CEILING}B ceiling"


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
