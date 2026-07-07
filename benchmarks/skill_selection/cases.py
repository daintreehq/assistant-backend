"""Benchmark case definitions and loading.

Cases live in ``cases/*.yaml``. Each file holds a ``cases:`` list; every case is a
frozen conversation plus the prior active-skill set and the EXPECTED selection:

- ``required``: ids that must ALL be picked (recall). Empty means "must clear".
- ``optional``: ids that are allowed but not demanded (typically the domain
  foundation — both a lean selector and a pairing selector should pass).

Anything picked outside ``required + optional`` is an ``extra`` (precision
failure). Every id is validated against the real catalog at load time so a typo
fails fast instead of silently scoring as a miss.
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


def _build_case(raw: dict[str, Any], source: str, catalog: Catalog) -> BenchCase:
    case_id = raw.get("id")
    if not case_id or not isinstance(case_id, str):
        raise CaseError(f"{source}: case missing string 'id'")
    messages_raw = raw.get("messages") or []
    if not messages_raw:
        raise CaseError(f"{case_id}: no messages")
    expect = raw.get("expect") or {}
    return BenchCase(
        id=case_id,
        category=str(raw.get("category") or "uncategorized"),
        source=source,
        messages=[_build_message(m, case_id, i) for i, m in enumerate(messages_raw)],
        active=_validate_ids(raw.get("active") or [], catalog, case_id, "active"),
        required=_validate_ids(expect.get("required") or [], catalog, case_id, "required"),
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
