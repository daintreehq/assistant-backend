"""The rename's own gate: "skill" must not come back as a Daintree runtime term.

The 5,196-occurrence sweep that renamed the procedural catalog is the kind of change
that reverses one file at a time — a merge that resurrects an old branch's wording, a
comment written from memory, a fixture regenerated from a stale capture. Nothing else
in the suite would notice: every test was renamed alongside the code, so the tests and
the code agree with each other whichever word they are both using.

This is deliberately a VOCABULARY check and not a spelling preference. The allowlist
below is the whole design: each entry is a place where "skill" names something this
repository does not own, and adding to it should feel like a decision.
"""

from __future__ import annotations

import re
import subprocess
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]

#: Paths where "skill" is correct, with the reason it is correct.
_ALLOWED_PATHS: dict[str, str] = {
    # Claude Code's own skill directory. Anthropic's product term, not ours.
    ".claude/": "Claude Code skills — an external product's term",
    # The Go CLI, a separate repository on its own rename schedule.
    "cli/": "the vendored CLI submodule, renamed on its own schedule",
    # This file names the forbidden word in order to forbid it.
    "tests/unit/test_legacy_vocabulary.py": "this gate",
}

#: Individual lines that legitimately say "skill", keyed by path. Each value is a
#: substring that must appear in the offending line for it to be forgiven, so a
#: blanket exemption cannot hide behind a file-level entry.
_ALLOWED_LINES: dict[str, tuple[str, ...]] = {
    # Rendered copies of the external agent capability guidance below.
    "tests/golden/snapshots/base_prompt.md": ("Agent commands and skills",),
    "tests/golden/snapshots/task_workflow_plan.md": ("For ordered agent commands or skills",),
    "tests/golden/snapshots/task_workflow_reconcile.md": ("For command/skill sequences",),
    "tests/golden/snapshots/upstream_messages.json": ("Agent commands and skills",),
    "tests/golden/snapshots/upstream_messages_with_terminals.json": ("Agent commands and skills",),
    # External agents' command/skill catalogs; these are not Daintree runbooks.
    "benchmarks/runbook_selection/cases/17-fan-out-and-status.yaml": (
        "fanout-codex-skill-command-chains",
        "ordered skills inside each terminal",
        "work-issue and review skills",
        "single-issue-custom-skill-not-fanout",
        "single issue with a custom skill",
        "my Codex $work-issue skill",
    ),
    "src/daintree_assistant_server/prompts/main/base/20-tool-discipline.md": (
        "Agent commands and skills",
    ),
    "src/daintree_assistant_server/prompts/tasks/workflow_plan.md": (
        "For ordered agent commands or skills",
    ),
    "src/daintree_assistant_server/prompts/tasks/workflow_reconcile.md": (
        "For command/skill sequences",
    ),
    "src/daintree_assistant_server/runbooks/files/daintree.foundation.md": (
        "Agent capability lookup:",
    ),
    "src/daintree_assistant_server/runbooks/files/daintree.edits.spawn-visible-agent.md": (
        "3b. A named command or skill",
    ),
    "src/daintree_assistant_server/runbooks/files/daintree.orchestration.fan-out-work.md": (
        "summary: Launch one agent terminal",
        "whenToUse:",
        "4. Launch ENTRY A",
        "NAMED CAPABILITY GATE",
    ),
    "src/daintree_assistant_server/runbooks/files/daintree.workflow.start-work-on-issue.md": (
        "For a user-supplied command or skill",
    ),
    "CLAUDE.md": (".claude/skills",),
    "README.md": (".claude/skills",),
    "cloudbuild.yaml": (".claude/skills",),
    # Same reason as the three above: the smoke script's comments cite the release
    # automation by path when explaining why a flag spelling has to keep working
    # for the callers that already pass it. The path is Claude Code's, not ours.
    "infra/gcp/smoke.sh": (".claude/skills",),
    "pyproject.toml": (".claude skill",),
    # The two CLI tool names that run AHEAD of the pinned submodule, plus the
    # comment block explaining why. See the fixture header.
    "tests/fixtures/cli_tool_names.txt": ("skill__step__advance", "skill__run__get", "skill"),
    # Gates and sentinels that must keep quoting the retired vocabulary in order
    # to detect its return. Renaming these is what makes them stop working.
    "tests/unit/test_docs_drift.py": ("skill", "SKILL_LEARNING"),
    "tests/unit/test_policy_contradictions.py": ("skill",),
    # The confirmation house-style gate BANS the retired word from user-facing text, so
    # it has to name it. Same shape as the two gates above: quoting the vocabulary
    # is how the check detects it, and renaming the literal is what would silently
    # switch the check off.
    "tests/unit/test_prompt_budgets.py": ("skill",),
    # The confirmation authoring reference BANS the retired word from user-facing
    # text, so its rule has to name it. Same shape as the gates above: quoting the
    # vocabulary is how the rule is stated, and renaming the literal would quietly
    # permit the word it exists to forbid.
    "docs/RUNBOOK_CONFIRMATIONS.md": ("*skill*",),
    # Deferred-work entries that quote the retired vocabulary to describe the
    # rename itself, plus the Go path the CLI still uses.
    "TODO.md": (
        "skill.go",
        "skill/skill.go",
        "skill__",
        "skill.",
        'skill" ->',
        "skill ->",
        ".claude/skills",
        "not `skills`",
    ),
    "benchmarks/_harness/fixtures/README.md": ("skill",),
    # The self-retiring CLI-divergence gate must NAME the pre-rename tool ids;
    # that is how it detects the Go side catching up.
    "tests/unit/test_runbook_catalog_hygiene.py": ("skill.", "skill/skill.go", "`skill.*`"),
    # Prose and comments that name the retired word IN ORDER to explain the break.
    # Each is pinned to the phrase that does the explaining, so the exemption cannot
    # drift into an ordinary present-tense use of the old term.
    "docs/DAINTREE_API.md": ("rather than `skills`",),
    "src/daintree_assistant_server/__init__.py": ('said "skill"',),
    "src/daintree_assistant_server/state/codec.py": ("`active_skills`",),
    # The v1 state-token test must build a payload carrying the RETIRED field name;
    # that literal is the whole point of the fixture.
    "tests/unit/test_state_codec.py": ("active_skills",),
    # Claude Code's own skill directory, referenced by path from the training docs
    # and, in the ingest module, from the docstring that names where the generating
    # agents' output schemas are defined. Anthropic's product term, not ours.
    "training/EXPERIMENTS.md": (".claude/skills",),
    "training/README.md": (".claude/skills",),
    "training/ingest_journal.py": (".claude/skills",),
    # Ignores the pre-rename PACKAGE PATH, which is a directory name and not a
    # present-tense claim about what the code is called — `git mv` cannot rename
    # what was never tracked, so untracked fp32 export leftovers kept surfacing
    # there after the rename moved everything real out.
    ".gitignore": ("skills/",),
}

_SKILL = re.compile(r"\bskills?\b|skill_|_skill|Skill", re.IGNORECASE)

_BINARY_SUFFIXES = {".onnx", ".npz", ".bin", ".safetensors", ".png", ".jpg", ".gz", ".pyc"}


def _tracked_files() -> list[Path]:
    out = subprocess.run(
        ["git", "ls-files"], cwd=REPO_ROOT, capture_output=True, text=True, check=True
    ).stdout.splitlines()
    return [Path(p) for p in out]


def _is_allowed_path(rel: str) -> bool:
    return any(rel == key or rel.startswith(key) for key in _ALLOWED_PATHS)


def test_no_daintree_runtime_surface_still_says_skill() -> None:
    """One assertion over every tracked file, with a reasoned allowlist."""
    offenders: list[str] = []
    for rel_path in _tracked_files():
        rel = rel_path.as_posix()
        if _is_allowed_path(rel) or rel_path.suffix in _BINARY_SUFFIXES:
            continue
        # The shipped selector artifacts are model weights and vocabularies; the
        # tokenizer vocab genuinely contains the English word.
        if "artifacts/" in rel:
            continue
        full = REPO_ROOT / rel_path
        if not full.is_file():
            continue
        try:
            text = full.read_text(encoding="utf-8")
        except (UnicodeDecodeError, OSError):
            continue
        permitted = _ALLOWED_LINES.get(rel, ())
        for number, line in enumerate(text.splitlines(), start=1):
            if not _SKILL.search(line):
                continue
            if any(token in line for token in permitted):
                continue
            offenders.append(f"{rel}:{number}: {line.strip()[:120]}")

    assert not offenders, (
        "these lines still use the retired Daintree runtime vocabulary:\n  "
        + "\n  ".join(offenders)
        + "\n\nDaintree's procedural catalog is called RUNBOOKS. If one of these names "
        "something external — a Claude Code skill, the Go CLI, a provider feature — add "
        "it to _ALLOWED_PATHS/_ALLOWED_LINES with the reason."
    )


def test_the_allowlist_does_not_rot() -> None:
    """An allowlist entry for a file that no longer exists is a silent widening.

    It is also how this gate would decay into a no-op: entries accumulate, nobody
    removes them, and eventually the exemptions cover the surface being guarded.
    """
    missing = [
        rel for rel in list(_ALLOWED_PATHS) + list(_ALLOWED_LINES) if not (REPO_ROOT / rel).exists()
    ]
    assert not missing, (
        f"allowlist names paths that no longer exist: {missing}. Remove them — an "
        "exemption for a deleted file only ever widens what the next one covers."
    )
