"""Frozen routing cases for the current catalog.

Every expected id and host-state exclusion is validated against the live catalog.
The inbox generation's cases are archived outside this active suite; their text
remains protected from training by ``training.compile_corpus``.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml

from daintree_assistant_server.contracts.openai import ChatMessage, FunctionCall, ToolCall
from daintree_assistant_server.runbooks.catalog import Catalog
from daintree_assistant_server.runbooks.host_state import HostState, parse_host_state

CASES_DIR = Path(__file__).resolve().parent / "cases"

_VALID_ROLES = {"user", "assistant", "tool"}
HOSTS_WILDCARD = "daintree.hosts.*"


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
    # The selector's SECOND answer: does this turn warrant a documentation
    # lookup? `True` demands a query, `False` demands `null`, and `None` — the
    # default for the many cases written before the classifier existed — leaves
    # it unscored rather than silently asserting one answer over the whole
    # corpus. It is scored SEPARATELY from runbook ids: a turn can route correctly
    # and classify the docs question wrongly, and a single pass/fail would hide
    # which half moved.
    expect_docs: bool | None = None
    # Explicitly forbidden procedures are failures even when recall succeeds.
    forbid: tuple[str, ...] = ()
    #: The caller's reported host state; ``None`` is unreported.
    host_state: HostState | None = None
    #: What a search for this caller leaves out (``Catalog.excluded_for``), resolved
    #: at load so every replay passes the same set to ``select(exclude=...)``.
    exclude: frozenset[str] = frozenset()

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
        raise CaseError(f"{case_id}: unknown {kind} runbook id(s): {', '.join(unknown)}")
    return list(ids)


def _check_foundation_convention(required: list[str], catalog: Catalog, case_id: str) -> None:
    """Reject a foundation id demanded ALONGSIDE a specific (see module docstring)."""
    foundations = [sid for sid in required if (s := catalog.get(sid)) and s.meta.foundation]
    if foundations and len(required) > 1:
        others = [sid for sid in required if sid not in foundations]
        raise CaseError(
            f"{case_id}: foundation runbook(s) {', '.join(foundations)} in expect.required "
            f"alongside specific(s) {', '.join(others)}. The backend pairs the foundation "
            "downstream, so a case whose answer is a specific must list the foundation in "
            "expect.optional. Only a foundation-ALONE case may require it."
        )


def _build_host_state(raw: Any, case_id: str) -> HostState | None:
    if raw is None:
        return None
    if not isinstance(raw, dict) or set(raw) != {"hosts"}:
        raise CaseError(f"{case_id}: host_state takes only 'hosts'")
    hosts = raw["hosts"]
    state = parse_host_state(hosts) if isinstance(hosts, str) else None
    if state is None:
        raise CaseError(f"{case_id}: host_state.hosts must be none, configured or connected")
    return state


def _build_case(raw: dict[str, Any], source: str, catalog: Catalog) -> BenchCase:
    case_id = raw.get("id")
    if not case_id or not isinstance(case_id, str):
        raise CaseError(f"{source}: case missing string 'id'")
    if raw.get("host_model") not in (None, "control"):
        raise CaseError(f"{case_id}: retired host_model belongs in archive/inbox-v0.44/cases")
    if any(key in raw for key in ("scenario", "role", "brief")):
        raise CaseError(f"{case_id}: retired hub/lane case belongs in archive/inbox-v0.44/cases")
    messages_raw = raw.get("messages") or []
    if not messages_raw:
        raise CaseError(f"{case_id}: no messages")
    expect = raw.get("expect") or {}
    required = _validate_ids(expect.get("required") or [], catalog, case_id, "required")
    _check_foundation_convention(required, catalog, case_id)
    expect_docs = expect.get("docs")
    if expect_docs is not None and not isinstance(expect_docs, bool):
        raise CaseError(f"{case_id}: expect.docs must be true, false, or omitted")
    optional = _validate_ids(expect.get("optional") or [], catalog, case_id, "optional")
    raw_forbid = list(expect.get("forbid") or [])
    if HOSTS_WILDCARD in raw_forbid:
        # A request that is the person's own, on one machine: no hosts procedure may load.
        raw_forbid = [i for i in raw_forbid if i != HOSTS_WILDCARD] + [
            r.id
            for r in catalog.runbooks
            if r.id.startswith("daintree.hosts.") and r.meta.required_tools
        ]
    forbid = _validate_ids(raw_forbid, catalog, case_id, "forbid")
    forbid = sorted(set(forbid))
    if clash := sorted(set(forbid) & (set(required) | set(optional))):
        raise CaseError(
            f"{case_id}: {', '.join(clash)} is forbidden in expect.forbid and also expected"
        )
    host_state = _build_host_state(raw.get("host_state"), case_id)
    exclude = catalog.excluded_for(host_state)
    named = set(required) | set(optional) | set(raw.get("active") or [])
    if named & exclude:
        raise CaseError(
            f"{case_id}: its host_state leaves out {', '.join(sorted(named & exclude))}, "
            "which it expects or holds active"
        )
    return BenchCase(
        id=case_id,
        category=str(raw.get("category") or "uncategorized"),
        source=source,
        messages=[_build_message(m, case_id, i) for i, m in enumerate(messages_raw)],
        active=_validate_ids(raw.get("active") or [], catalog, case_id, "active"),
        required=required,
        optional=optional,
        turn_context=raw.get("turn_context"),
        available_tools=list(raw.get("available_tools") or []),
        note=str(raw.get("note") or ""),
        expect_docs=expect_docs,
        forbid=tuple(forbid),
        host_state=host_state,
        exclude=exclude,
    )


def load_cases(
    catalog: Catalog,
    cases_dir: Path = CASES_DIR,
    id_filter: str | None = None,
) -> list[BenchCase]:
    """Load the current suite; duplicate ids and retired cases fail loudly."""
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
            case_id = raw.get("id") if isinstance(raw, dict) else None
            if isinstance(case_id, str):
                if case_id in seen:
                    raise CaseError(f"duplicate case id {case_id!r} in {path.name}")
                seen.add(case_id)
            case = _build_case(raw, path.name, catalog)
            cases.append(case)
    if id_filter:
        needle = id_filter.lower()
        cases = [c for c in cases if needle in c.id.lower() or needle in c.category.lower()]
        if not cases:
            raise CaseError(f"filter {id_filter!r} matched no cases")
    return cases
