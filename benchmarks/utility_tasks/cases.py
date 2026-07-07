"""Benchmark case definitions and loading for the utility-task accuracy suite.

Cases live in ``cases/*.yaml``. Each file holds a ``cases:`` list; every case
freezes one utility-task invocation — the ``task`` id, the ``input`` payload
(exactly what a client sends), and the EXPECTED output fields:

- ``expect`` is a map of output-field -> acceptable value. A scalar means the
  field must equal it; a **list** means the field must be one of the members
  (an "acceptable set", used mainly for the watcher's enum). A trial passes
  only when EVERY ``expect`` field matches and the task did not error.

Every case is validated at load time against the real production profile: the
``task`` must be a supported classifier, the ``input`` must validate against the
task's real Pydantic input model, and each ``expect`` key must be a real output
field of that task. A typo fails fast instead of silently scoring as a miss —
the same posture as the skill-selection suite validating skill ids.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml
from pydantic import ValidationError

from daintree_assistant_server.services.task_runner import build_profiles

CASES_DIR = Path(__file__).resolve().parent / "cases"

# The classifier/judge tasks this suite scores, mapped to their output fields.
# Only these tasks have a single, unambiguous ground-truth field (or enum), so
# only these are benchmarkable as strict accuracy. (Text tasks — summarize,
# extract_text — and the compaction tasks need a judge/reference scorer and are
# out of scope here.) An ``expect`` key outside a task's field set fails at load.
SUPPORTED_TASKS: dict[str, frozenset[str]] = {
    "terminal_judge": frozenset({"matched", "confidence", "reason"}),
    "watcher_classify": frozenset(
        {"classification", "confidence", "summary", "evidence", "recommendedAction"}
    ),
    "extraction_verdict": frozenset({"pass", "reason"}),
    "skill_step_consistency": frozenset({"matched", "confidence", "reason"}),
}

_PROFILES = build_profiles()


class CaseError(Exception):
    """A malformed benchmark case file."""


@dataclass(frozen=True)
class TaskCase:
    id: str
    task: str
    category: str
    source: str
    input: dict[str, Any]
    expect: dict[str, Any]
    note: str


def _matches(got: Any, want: Any) -> bool:
    """A single ``expect`` assertion: membership when ``want`` is a list of
    acceptable values, exact equality otherwise."""
    if isinstance(want, list):
        return got in want
    return got == want


def mismatches(output: dict[str, Any], expect: dict[str, Any]) -> list[str]:
    """Return a human-readable note for each failed ``expect`` field ([] = pass)."""
    out: list[str] = []
    for field, want in expect.items():
        got = output.get(field)
        if not _matches(got, want):
            out.append(f"{field}={got!r}!={want!r}")
    return out


def _build_case(raw: dict[str, Any], source: str) -> TaskCase:
    case_id = raw.get("id")
    if not case_id or not isinstance(case_id, str):
        raise CaseError(f"{source}: case missing string 'id'")
    task = raw.get("task")
    if task not in SUPPORTED_TASKS:
        raise CaseError(
            f"{case_id}: unsupported task {task!r} "
            f"(supported: {', '.join(sorted(SUPPORTED_TASKS))})"
        )
    payload = raw.get("input")
    if not isinstance(payload, dict):
        raise CaseError(f"{case_id}: 'input' must be a mapping")
    try:
        _PROFILES[task].input_model.model_validate(payload)
    except ValidationError as exc:
        raise CaseError(
            f"{case_id}: invalid input for task {task!r}: {exc.errors()[0].get('msg', '')}"
        ) from exc
    expect = raw.get("expect")
    if not isinstance(expect, dict) or not expect:
        raise CaseError(f"{case_id}: 'expect' must be a non-empty mapping")
    unknown = set(expect) - SUPPORTED_TASKS[task]
    if unknown:
        raise CaseError(
            f"{case_id}: expect field(s) {sorted(unknown)} are not outputs of {task!r} "
            f"(valid: {', '.join(sorted(SUPPORTED_TASKS[task]))})"
        )
    return TaskCase(
        id=case_id,
        task=task,
        category=str(raw.get("category") or "uncategorized"),
        source=source,
        input=payload,
        expect=expect,
        note=str(raw.get("note") or ""),
    )


def load_cases(cases_dir: Path = CASES_DIR, id_filter: str | None = None) -> list[TaskCase]:
    """Load, validate, and (optionally) filter the case suite."""
    files = sorted(cases_dir.glob("*.yaml"))
    if not files:
        raise CaseError(f"no case files under {cases_dir}")
    cases: list[TaskCase] = []
    seen: set[str] = set()
    for path in files:
        data = yaml.safe_load(path.read_text(encoding="utf-8"))
        if not isinstance(data, dict) or not isinstance(data.get("cases"), list):
            raise CaseError(f"{path.name}: expected a top-level 'cases' list")
        for raw in data["cases"]:
            case = _build_case(raw, path.name)
            if case.id in seen:
                raise CaseError(f"duplicate case id {case.id!r} in {path.name}")
            seen.add(case.id)
            cases.append(case)
    if id_filter:
        needle = id_filter.lower()
        cases = [
            c
            for c in cases
            if needle in c.id.lower() or needle in c.category.lower() or needle in c.task.lower()
        ]
        if not cases:
            raise CaseError(f"filter {id_filter!r} matched no cases")
    return cases
