"""Benchmark case definitions and loading.

Cases live in ``cases/*.yaml``. Each file holds a ``cases:`` list; every case is a
frozen conversation plus the prior active-skill set and the EXPECTED selection:

- ``required``: ids that must ALL be picked (recall). Empty means "must clear".
- ``optional``: ids that are allowed but not demanded (typically the domain
  foundation — both a lean selector and a pairing selector should pass).

Anything picked outside ``required + optional`` is an ``extra`` (precision
failure). Every id is validated against the real catalog at load time so a typo
fails fast instead of silently scoring as a miss.

**The foundation convention, enforced at load.** This suite scores the RAW
classifier output, while the backend pairs a selected specific with its domain
foundation deterministically *downstream* (``resolve_active_skills``). So on a
case whose answer is a SPECIFIC, demanding the foundation would fail a correct
lean pick for something the classifier never has to say: the foundation belongs
in ``optional`` there, never in ``required``. A case whose answer IS the
foundation — no runbook squarely matches, load it ALONE — is the one shape that
may require it, and it must then be the ONLY required id. ``_check_foundation_
convention`` rejects the mixed shape rather than leaving it to review.

The rule is deliberately about the SHAPE of ``required``, not about the
``category`` label: "load the foundation alone" is the right answer in several
categories (a routing trap that must not over-pick, adversarial keyword bait, a
background wake), and forcing those into the ``foundation-only`` category to
satisfy a load check would gut the category signal the report groups on.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml

from daintree_assistant_server.contracts.openai import ChatMessage, FunctionCall, ToolCall
from daintree_assistant_server.skills.catalog import Catalog

CASES_DIR = Path(__file__).resolve().parent / "cases"

_VALID_ROLES = {"user", "assistant", "tool"}


class CaseError(Exception):
    """A malformed benchmark case file."""


@dataclass(frozen=True)
class BenchCase:
    id: str
    category: str
    source: str
    messages: list[ChatMessage]
    active: list[str]
    required: list[str]
    optional: list[str]
    turn_context: str | None
    available_tools: list[str]
    note: str

    @property
    def allowed(self) -> set[str]:
        return set(self.required) | set(self.optional)


def _build_message(raw: dict[str, Any], case_id: str, index: int) -> ChatMessage:
    role = raw.get("role")
    if role not in _VALID_ROLES:
        raise CaseError(f"{case_id}: message {index} has invalid role {role!r}")
    tool_calls = None
    names = raw.get("tool_calls") or []
    if names:
        tool_calls = [
            ToolCall(id=f"call_{case_id}_{index}_{i}", function=FunctionCall(name=n))
            for i, n in enumerate(names)
        ]
    return ChatMessage(
        role=role,
        content=raw.get("content"),
        name=raw.get("name"),
        tool_calls=tool_calls,
        tool_call_id=f"call_{case_id}_{index}" if role == "tool" else None,
    )


def _validate_ids(ids: list[str], catalog: Catalog, case_id: str, kind: str) -> list[str]:
    unknown = [sid for sid in ids if sid not in catalog]
    if unknown:
        raise CaseError(f"{case_id}: unknown {kind} skill id(s): {', '.join(unknown)}")
    return list(ids)


def _check_foundation_convention(required: list[str], catalog: Catalog, case_id: str) -> None:
    """Reject a foundation id demanded ALONGSIDE a specific (see module docstring)."""
    foundations = [sid for sid in required if (s := catalog.get(sid)) and s.meta.foundation]
    if foundations and len(required) > 1:
        others = [sid for sid in required if sid not in foundations]
        raise CaseError(
            f"{case_id}: foundation skill(s) {', '.join(foundations)} in expect.required "
            f"alongside specific(s) {', '.join(others)}. The backend pairs the foundation "
            "downstream, so a case whose answer is a specific must list the foundation in "
            "expect.optional. Only a foundation-ALONE case may require it."
        )


def _build_case(raw: dict[str, Any], source: str, catalog: Catalog) -> BenchCase:
    case_id = raw.get("id")
    if not case_id or not isinstance(case_id, str):
        raise CaseError(f"{source}: case missing string 'id'")
    messages_raw = raw.get("messages") or []
    if not messages_raw:
        raise CaseError(f"{case_id}: no messages")
    expect = raw.get("expect") or {}
    required = _validate_ids(expect.get("required") or [], catalog, case_id, "required")
    _check_foundation_convention(required, catalog, case_id)
    return BenchCase(
        id=case_id,
        category=str(raw.get("category") or "uncategorized"),
        source=source,
        messages=[_build_message(m, case_id, i) for i, m in enumerate(messages_raw)],
        active=_validate_ids(raw.get("active") or [], catalog, case_id, "active"),
        required=required,
        optional=_validate_ids(expect.get("optional") or [], catalog, case_id, "optional"),
        turn_context=raw.get("turn_context"),
        available_tools=list(raw.get("available_tools") or []),
        note=str(raw.get("note") or ""),
    )


def load_cases(
    catalog: Catalog,
    cases_dir: Path = CASES_DIR,
    id_filter: str | None = None,
) -> list[BenchCase]:
    """Load, validate, and (optionally) filter the case suite."""
    files = sorted(cases_dir.glob("*.yaml"))
    if not files:
        raise CaseError(f"no case files under {cases_dir}")
    cases: list[BenchCase] = []
    seen: set[str] = set()
    for path in files:
        data = yaml.safe_load(path.read_text(encoding="utf-8"))
        if not isinstance(data, dict) or not isinstance(data.get("cases"), list):
            raise CaseError(f"{path.name}: expected a top-level 'cases' list")
        for raw in data["cases"]:
            case = _build_case(raw, path.name, catalog)
            if case.id in seen:
                raise CaseError(f"duplicate case id {case.id!r} in {path.name}")
            seen.add(case.id)
            cases.append(case)
    if id_filter:
        needle = id_filter.lower()
        cases = [c for c in cases if needle in c.id.lower() or needle in c.category.lower()]
        if not cases:
            raise CaseError(f"filter {id_filter!r} matched no cases")
    return cases
