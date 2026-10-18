"""Benchmark case definitions and loading.

Cases live in ``cases/*.yaml``. Each file holds a ``cases:`` list; every case is a
frozen conversation plus the prior active-runbook set and the EXPECTED selection:

- ``required``: ids that must ALL be picked (recall). Empty means "must clear".
- ``optional``: ids that are allowed but not demanded (typically the domain
  foundation — both a lean selector and a pairing selector should pass).
- ``host_model`` (optional): ``control`` or ``inbox``, the host model whose catalog
  the case is scored against (``runbooks/catalog.py``, ``load_catalogs``). Omitted,
  the case holds for both and is scored against each. Every id it names must exist
  in each catalog it is scored against, so a case naming a runbook only one model
  has says which. Paired role cases and a reachable lane are the inbox model's.
- ``host_state`` (optional): the caller's reported host state, as Daintree sends it
  in its headers (``{hosts: none|configured|connected, lane: reachable}``). The case
  is scored with the host runbooks that state does not fit left out, as a search
  for that caller leaves them out. Omitted is an unreported caller, offered the
  whole catalog. A case may not expect, or hold active, a runbook its state leaves
  out: that would score as a selector miss what is really a broken case.

Anything picked outside ``required + optional`` is an ``extra`` (precision
failure). Every id is validated against the real catalog at load time so a typo
fails fast instead of silently scoring as a miss.

**The foundation convention, enforced at load.** This suite scores the RAW
classifier output, while the backend pairs a selected specific with its domain
foundation deterministically *downstream* (``resolve_active_runbooks``). So on a
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

**Paired role cases** (``scenario`` + ``role``). A hosts scenario is one piece of
work seen from both ends: the person's request on the hub, and the receiving lane's
search for the same work after reading the hub's brief. A case that names its
``scenario`` and ``role`` (``hub`` or ``lane``) is checked for the WRONG ROLE as
well as for recall: loading a procedure the role cannot execute (a lane given a
hub's ``host.*`` baseline, the hub given a lane's) fails it, because that
runbook would be delivered in full to a caller that must not follow it. The
forbidden set is derived from the catalog (``wrong_role_ids``) plus any explicit
``expect.forbid``, and ``pairs.py`` holds the structural checks and the report.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml

from daintree_assistant_server.contracts.openai import ChatMessage, FunctionCall, ToolCall
from daintree_assistant_server.runbooks.catalog import Catalog
from daintree_assistant_server.runbooks.host_state import HostModel, HostState, parse_host_state
from daintree_assistant_server.runbooks.models import Runbook

CASES_DIR = Path(__file__).resolve().parent / "cases"

#: Each host model's suite, scored against its own catalog.
HOST_MODELS: tuple[HostModel, ...] = ("control", "inbox")

_VALID_ROLES = {"user", "assistant", "tool"}

#: The two ends of a paired scenario: the Shell's hub, and a lane enrolled on a host.
CALLER_ROLES = ("hub", "lane")

#: In ``expect.forbid``: every hosts runbook with a baseline, at either end of the pair.
HOSTS_WILDCARD = "daintree.hosts.*"

_HUB_ONLY_TOOL_PREFIX = "host."
_HUB_ONLY_TOOLS = frozenset({"project.openOnHost"})


def baseline_role(runbook: Runbook) -> str | None:
    """Which end of the hub/lane pair a runbook's baseline path is written for.

    Only a ``daintree.hosts.*`` runbook with a baseline has one. A baseline that
    needs a ``host.*`` tool is the hub's, because a lane is never offered those; any
    other hosts baseline is a lane's. ``tests/unit/test_paired_roles.py`` checks
    this reading against the captured hub and lane surfaces, so it cannot drift
    from what each caller is actually offered.
    """
    if not runbook.id.startswith("daintree.hosts.") or not runbook.meta.required_tools:
        return None
    hub_only = any(
        tool.startswith(_HUB_ONLY_TOOL_PREFIX) or tool in _HUB_ONLY_TOOLS
        for tool in runbook.meta.required_tools
    )
    return "hub" if hub_only else "lane"


def wrong_role_ids(role: str, catalog: Catalog) -> list[str]:
    """The runbooks a caller in ``role`` must never be handed: the other end's."""
    other = "lane" if role == "hub" else "hub"
    return sorted(r.id for r in catalog.runbooks if baseline_role(r) == other)


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
    # Paired role cases: the scenario this case is one end of, and which end. Both
    # or neither (``_build_case`` refuses a half).
    scenario: str | None = None
    role: str | None = None
    # Runbooks that must NOT be loaded, whatever else is right: the role's wrong-end
    # runbooks (derived) plus ``expect.forbid``. Scored by ``bench local`` as a recall
    # failure; empty for every case without a role or an explicit ``forbid``.
    forbid: tuple[str, ...] = ()
    # The hub's emitted brief this lane case's query was drawn from (``pairs.py``
    # checks its shape). Lane cases only.
    brief: str | None = None
    #: The caller's reported host state; ``None`` is unreported.
    host_state: HostState | None = None
    #: What a search for this caller leaves out (``Catalog.excluded_for``), resolved
    #: at load so every replay passes the same set to ``select(exclude=...)``.
    exclude: frozenset[str] = frozenset()
    #: The one model the case is written for, or ``None`` for both.
    host_model: HostModel | None = None

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
    """Strict, unlike the transport: a case's state is authored, so a typo fails."""
    if raw is None:
        return None
    if not isinstance(raw, dict) or set(raw) - {"hosts", "lane"}:
        raise CaseError(f"{case_id}: host_state takes only 'hosts' and 'lane'")
    hosts, lane = raw.get("hosts"), raw.get("lane")
    state = (
        parse_host_state(hosts, lane)
        if isinstance(hosts, str) and (lane is None or isinstance(lane, str))
        else None
    )
    if state is None:
        raise CaseError(
            f"{case_id}: host_state.hosts must be none, configured or connected, "
            "and host_state.lane reachable or omitted"
        )
    return state


def _case_model(raw: dict[str, Any], case_id: str) -> HostModel | None:
    value = raw.get("host_model")
    if value is None:
        return None
    if value not in HOST_MODELS:
        raise CaseError(f"{case_id}: host_model must be one of {HOST_MODELS}, or omitted")
    model: HostModel = value
    return model


def _build_case(raw: dict[str, Any], source: str, catalog: Catalog) -> BenchCase:
    case_id = raw.get("id")
    if not case_id or not isinstance(case_id, str):
        raise CaseError(f"{source}: case missing string 'id'")
    host_model = _case_model(raw, case_id)
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
    scenario, role = raw.get("scenario"), raw.get("role")
    if (scenario is None) != (role is None):
        raise CaseError(f"{case_id}: scenario and role come together, or not at all")
    if role is not None and (role not in CALLER_ROLES or not isinstance(scenario, str)):
        raise CaseError(f"{case_id}: role must be one of {CALLER_ROLES} and scenario a string")
    if role is not None and host_model != "inbox":
        raise CaseError(f"{case_id}: hub and lane roles are the inbox model's (host_model: inbox)")
    brief = raw.get("brief")
    if brief is not None and (role != "lane" or not isinstance(brief, str)):
        raise CaseError(f"{case_id}: a brief is a string on a lane case")
    raw_forbid = list(expect.get("forbid") or [])
    if HOSTS_WILDCARD in raw_forbid:
        # A request that is the person's own, on one machine: no hosts procedure may load.
        raw_forbid = [i for i in raw_forbid if i != HOSTS_WILDCARD] + [
            r.id for r in catalog.runbooks if baseline_role(r) is not None
        ]
    forbid = _validate_ids(raw_forbid, catalog, case_id, "forbid")
    if role is not None:
        forbid = [*forbid, *wrong_role_ids(role, catalog)]
    forbid = sorted(set(forbid))
    if clash := sorted(set(forbid) & (set(required) | set(optional))):
        raise CaseError(
            f"{case_id}: {', '.join(clash)} is forbidden for this case (the wrong end of the "
            "hub/lane pair, or named in expect.forbid) and also expected"
        )
    host_state = _build_host_state(raw.get("host_state"), case_id)
    if host_state is not None and host_state.lane_reachable and host_model != "inbox":
        raise CaseError(f"{case_id}: a reachable lane is the inbox model's (host_model: inbox)")
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
        scenario=scenario,
        role=role,
        forbid=tuple(forbid),
        brief=brief,
        host_state=host_state,
        exclude=exclude,
        host_model=host_model,
    )


def load_cases(
    catalog: Catalog,
    cases_dir: Path = CASES_DIR,
    id_filter: str | None = None,
    *,
    model: HostModel = "control",
) -> list[BenchCase]:
    """Load, validate, and (optionally) filter ``model``'s case suite: every case
    written for it or for both, validated against ``catalog``, which must be that
    model's (``Catalogs.for_model``). Case ids are unique across both suites."""
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
                if _case_model(raw, case_id) not in (None, model):
                    continue
            case = _build_case(raw, path.name, catalog)
            cases.append(case)
    from .pairs import validate_pairs

    if problems := validate_pairs(cases):
        raise CaseError("paired role cases are malformed:\n  " + "\n  ".join(problems))
    if id_filter:
        needle = id_filter.lower()
        cases = [c for c in cases if needle in c.id.lower() or needle in c.category.lower()]
        if not cases:
            raise CaseError(f"filter {id_filter!r} matched no cases")
    return cases
