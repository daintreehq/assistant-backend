"""Paired hub/lane scenarios: the structure around the routing cases, and what each end is handed.

``benchmarks/runbook_selection/cases/1142-hosts-paired-roles.yaml`` holds each scenario as
cases (a hub request and the receiving lane's search, with wrong-role traps) that
``bench local`` scores on the shipped selector. Retrieval quality is that bench's verdict; this
file checks everything about the pairs that needs no model:

- the case structure the report relies on (both ends, a trap, an emitted brief with its
  boundaries, queries as the app prescribes, no runbook id handed to the search);
- the wrong-role rule: a lane is never handed a hub baseline it cannot execute, read against the
  captured hub and lane surfaces;
- the caller contract of each scenario's two runbooks, and that no missing required tool has been
  parked in ``optionalTools`` to pass it;
- delivery over the MCP wire: the hub and the lane are separate sessions, each gets its
  prerequisites in full once, and a later reply in the same session only names them;
- the size budgets, per runbook and per bundle.

It proves selection structure and contract. It does not prove a notice is delivered or that an
agent follows a procedure; that takes a live hub and lane (``references/hub-lane-trials.md``).
"""

from __future__ import annotations

import re
from functools import cache
from typing import Any

import pytest
from daintree_mcp_surfaces import HUB_TOOLS, LANE_TOOLS, action_ids
from fastapi.testclient import TestClient
from test_runbook_budgets import RUNBOOK_BODY_RECOMMENDED, RUNBOOK_BUNDLE_CEILING

from benchmarks.runbook_selection.cases import (
    BenchCase,
    CaseError,
    baseline_role,
    load_cases,
    wrong_role_ids,
)
from benchmarks.runbook_selection.pairs import (
    BRIEF_LABELS,
    BRIEF_MAX_CHARS,
    WAKE_LINE_PREFIX,
    brief_problems,
    pair_cases,
    validate_pairs,
)
from benchmarks.runbook_selection.scoring import score_trial
from daintree_assistant_server.contracts.openai import ChatMessage
from daintree_assistant_server.runbook_mcp import SENT_EARLIER_NOTE, TOOL_NAME
from daintree_assistant_server.runbooks.catalog import Catalog, load_catalog

#: What the issue's scenario list names, each seen from both ends.
REQUIRED_SCENARIOS = {
    "implement",
    "review",
    "verify",
    "cohort",
    "steer",
    "integrate",
    "demo",
    "question",
    "designated-role",
}

#: Scenarios whose hub runbook does not itself name the lane runbook its brief should lead to,
#: each with the reason. A pinned gap, not an exemption: the test fails when the pointer lands,
#: so this list cannot go stale. Neither is fixed here (the hub runbooks sit at their bundle
#: ceiling; a pointer is the owning issue's edit).
HUB_NAMES_NO_RECEIVER = {
    # place-work briefs through delegate-to-host, whose brief names the single-unit receiver;
    # nothing on the hub sends a several-pieces message, so run-local-cohort is reached only by
    # the lane's own routing.
    "cohort": ("daintree.hosts.place-work", "daintree.hosts.run-local-cohort"),
    # ask-host-assistant sends a question; the lane reads it through the wake line, and nothing
    # on the hub names the envelope.
    "question": ("daintree.hosts.ask-host-assistant", "daintree.hosts.handle-hub-message"),
}

SESSION_HEADERS = {"Accept": "application/json, text/event-stream"}


@cache
def _catalog() -> Catalog:
    return load_catalog()


@cache
def _all_cases() -> tuple[BenchCase, ...]:
    return tuple(load_cases(_catalog()))


def _cases() -> list[BenchCase]:
    return pair_cases(list(_all_cases()))


def _scenarios() -> dict[str, list[BenchCase]]:
    grouped: dict[str, list[BenchCase]] = {}
    for case in _cases():
        grouped.setdefault(str(case.scenario), []).append(case)
    return grouped


def _success(members: list[BenchCase], role: str) -> BenchCase:
    """The scenario's selection case at one end: the first non-trap case that requires a runbook."""
    for case in members:
        if case.role == role and case.required and case.category == "single-runbook":
            return case
    raise AssertionError(f"no successful {role} case")


def _ends(name: str) -> tuple[str, str]:
    """The hub runbook and the lane runbook a scenario pairs, from its success cases."""
    members = _scenarios()[name]
    hub, lane = _success(members, "hub"), _success(members, "lane")
    return hub.required[0], lane.required[0]


# ------------------------------------------------------------------ structure


def test_the_shipped_scenarios_are_well_formed_and_cover_the_issues_list() -> None:
    assert validate_pairs(list(_all_cases())) == []
    assert set(_scenarios()) >= REQUIRED_SCENARIOS


def test_every_scenario_has_both_ends_a_trap_and_an_emitted_brief() -> None:
    for name, members in _scenarios().items():
        assert {c.role for c in members} == {"hub", "lane"}, name
        assert any(c.category == "routing-trap" for c in members if c.role == "lane"), name
        assert any(c.category in ("routing-trap", "no-task") for c in members if c.role == "hub")
        briefs = [c for c in members if c.brief]
        assert briefs and all(c.role == "lane" and c.required for c in briefs), name


def test_the_reverse_traps_the_issue_names_are_present() -> None:
    """Local requests at either end, the lane's bare wake line, a question rather than work, and
    a role the caller does not hold."""
    cases = _cases()
    local = [c for c in cases if c.id.endswith(("local-trap", "one-unit-trap"))]
    assert {c.role for c in local} == {"hub", "lane"}
    assert any(
        c.messages[0].content.startswith(WAKE_LINE_PREFIX) for c in cases if c.role == "lane"
    )
    assert any(c.scenario == "question" and c.category == "routing-trap" for c in cases)
    assert any(c.id == "pair-role-lane-asked-to-relay" for c in cases)
    assert any(c.id == "pair-role-hub-reads-host-message" for c in cases)


def test_a_lane_success_names_its_deliverable_by_what_it_is_not_by_a_runbook_id() -> None:
    """No pair case hands the search a runbook id or the word runbook, and no pair text carries
    an issue number of this repository's own program."""
    for case in _cases():
        text = "\n".join([*(m.content or "" for m in case.messages), case.brief or ""])
        assert "daintree." not in text.replace(WAKE_LINE_PREFIX, ""), case.id
        assert "runbook" not in text.lower(), case.id
        assert not re.search(r"#\d{2,3}\b", text), case.id


def _case(query: str, **kw: Any) -> BenchCase:
    base: dict[str, Any] = {
        "id": "x",
        "category": "single-runbook",
        "source": "t",
        "active": [],
        "optional": [],
        "turn_context": None,
        "available_tools": [],
        "note": "",
        "messages": [ChatMessage(role="user", content=query)],
        "required": ["daintree.hosts.do-delegated-work"],
        "scenario": "s",
        "role": "lane",
    }
    base.update(kw)
    return BenchCase(**base)


GOOD_BRIEF = (
    "Objective: Fix the flaky login test. Deliverable: one commit.\n"
    "Scope: This project only.\nReport: Branch and SHA.\nStop: Ask back on a mismatch."
)


def test_validation_names_each_structural_fault() -> None:
    hub = _case(
        "Have my other Mac fix the flaky login test and open a PR",
        role="hub",
        id="h",
        required=["daintree.hosts.delegate-to-host"],
    )
    lane = _case("Fix the flaky login test in a new worktree here", id="l", brief=GOOD_BRIEF)
    trap = _case(
        "Have Claude fix the login bug on this machine today please",
        id="t",
        category="routing-trap",
    )
    assert validate_pairs([hub, lane, trap]) == []
    # One end missing, no trap, no brief.
    assert any("no successful selection at the lane" in p for p in validate_pairs([hub, trap]))
    assert any("no wrong-role routing trap" in p for p in validate_pairs([hub, lane]))
    assert any("emitted brief" in p for p in validate_pairs([hub, _case("x" * 3, id="l2"), trap]))
    # A query that names an id, or is not one sentence of the app's length.
    named = _case(
        "Follow daintree.hosts.do-delegated-work for the login test fix", brief=GOOD_BRIEF
    )
    assert any("names a runbook id" in p for p in validate_pairs([hub, named, trap]))
    short = _case("Fix the login test", brief=GOOD_BRIEF)
    assert any("4 words" in p for p in validate_pairs([hub, short, trap]))
    # A query not drawn from its brief.
    drifted = _case("Bring the demo up on this host with sample data now", brief=GOOD_BRIEF)
    assert any("drawn from the brief" in p for p in validate_pairs([hub, drifted, trap]))
    # A wake line belongs to the lane.
    wake = _case(f"{WAKE_LINE_PREFIX} has a new message (exchange 1). Read it.", role="hub")
    assert any("not the hub's" in p for p in validate_pairs([wake, lane, trap]))


def test_a_brief_carries_its_boundaries_and_stays_a_reference() -> None:
    assert brief_problems(GOOD_BRIEF) == []
    for label in BRIEF_LABELS:
        thinner = re.sub(rf"^{label}:.*$", "", GOOD_BRIEF, flags=re.M)
        assert any(f"no '{label}:' line" in p for p in brief_problems(thinner)), label
    assert any("over" in p for p in brief_problems(GOOD_BRIEF + "x" * BRIEF_MAX_CHARS))
    assert any("runbook id" in p for p in brief_problems(GOOD_BRIEF + " daintree.hosts.foundation"))
    pasted = GOOD_BRIEF + "\nOn notice or resume: do the next step."
    assert any("procedure text" in p for p in brief_problems(pasted))


def test_the_shipped_briefs_are_small_and_each_carries_all_four_boundaries() -> None:
    briefs = [c.brief for c in _cases() if c.brief]
    assert len(briefs) >= len(REQUIRED_SCENARIOS)
    for brief in briefs:
        assert brief_problems(brief) == []
        assert len(brief) <= BRIEF_MAX_CHARS


# ------------------------------------------------------------------ the wrong-role rule


def test_the_role_reading_agrees_with_the_captured_hub_and_lane_surfaces() -> None:
    """`baseline_role` is read from tool names so the benchmark needs no capture. Here it is held
    to the captures: a hub baseline is one a lane is not offered, a lane baseline is one it is."""
    catalog = _catalog()
    hub_ids, lane_ids = [], []
    for runbook in catalog.runbooks:
        role = baseline_role(runbook)
        required = set(runbook.meta.required_tools)
        if role == "hub":
            hub_ids.append(runbook.id)
            assert required <= action_ids("hub-full"), runbook.id
            assert not required <= action_ids("lane-full"), runbook.id
        elif role == "lane":
            lane_ids.append(runbook.id)
            assert required <= action_ids("lane-full"), runbook.id
    assert {"daintree.hosts.delegate-to-host", "daintree.hosts.steer-swarm"} <= set(hub_ids)
    assert {"daintree.hosts.do-delegated-work", "daintree.hosts.handle-hub-message"} <= set(
        lane_ids
    )
    assert HUB_TOOLS and LANE_TOOLS  # the sets the policy pins, untouched here
    # Nothing outside the hosts namespace, and no foundation, has a role.
    assert baseline_role(catalog.get("daintree.hosts.foundation")) is None  # type: ignore[arg-type]
    assert baseline_role(catalog.get("daintree.edits.spawn-visible-agent")) is None  # type: ignore[arg-type]
    assert set(wrong_role_ids("lane", catalog)) == set(hub_ids)
    assert set(wrong_role_ids("hub", catalog)) == set(lane_ids)


def test_a_case_cannot_expect_what_it_forbids() -> None:
    catalog = _catalog()
    lane_only = next(r.id for r in catalog.runbooks if baseline_role(r) == "lane")
    hub_only = next(r.id for r in catalog.runbooks if baseline_role(r) == "hub")
    for role, expected in (("hub", lane_only), ("lane", hub_only)):
        assert expected in wrong_role_ids(role, catalog)
    for case in _cases():
        assert not set(case.forbid) & (set(case.required) | set(case.optional)), case.id
        derived = set(wrong_role_ids(str(case.role), catalog))
        assert derived <= set(case.forbid), case.id


def test_loading_refuses_a_half_declared_pair_and_a_self_contradicting_expectation(
    tmp_path: Any,
) -> None:
    catalog = _catalog()
    lane_runbook = _ends("implement")[1]
    hub_runbook = _ends("implement")[0]

    def load(body: str) -> None:
        (tmp_path / "c.yaml").write_text(body, encoding="utf-8")
        load_cases(catalog, tmp_path)

    head = "cases:\n  - id: c\n    category: single-runbook\n    messages:\n"
    head += "      - {role: user, content: x}\n"
    with pytest.raises(CaseError, match="come together"):
        load(head + "    scenario: s\n    expect: {required: [" + lane_runbook + "]}\n")
    with pytest.raises(CaseError, match="forbidden for this case"):
        load(
            head
            + "    scenario: s\n    role: lane\n    expect: {required: ["
            + hub_runbook
            + "]}\n"
        )
    with pytest.raises(CaseError, match="brief is a string on a lane case"):
        load(
            head
            + "    scenario: s\n    role: hub\n    brief: b\n    expect: {required: ["
            + hub_runbook
            + "]}\n"
        )


def test_loading_a_scenario_with_no_trap_is_refused(tmp_path: Any) -> None:
    catalog = _catalog()
    hub_runbook, lane_runbook = _ends("implement")
    body = (
        "cases:\n"
        f"  - {{id: h, scenario: s, role: hub, category: single-runbook, "
        f"messages: [{{role: user, content: 'Have my other Mac fix the flaky login test and open a PR'}}], "
        f"expect: {{required: [{hub_runbook}]}}}}\n"
        f"  - {{id: l, scenario: s, role: lane, category: single-runbook, "
        f"brief: 'Objective: Fix the flaky login test. Deliverable: one commit.\\nScope: here\\n"
        f"Report: SHA\\nStop: ask', "
        f"messages: [{{role: user, content: 'Fix the flaky login test in a new worktree here'}}], "
        f"expect: {{required: [{lane_runbook}]}}}}\n"
    )
    (tmp_path / "c.yaml").write_text(body, encoding="utf-8")
    with pytest.raises(CaseError, match="no wrong-role routing trap"):
        load_cases(catalog, tmp_path)


def test_a_forbidden_load_fails_recall_and_is_reported_per_case() -> None:
    """The point of `forbid`: the right runbook can be loaded and the case still fails, because
    the wrong end's runbook would be delivered in full beside it."""
    from pathlib import Path

    from benchmarks.runbook_selection.local_runner import build_local_doc

    catalog = _catalog()
    whole = list(_all_cases())
    by_id = {c.id: c for c in whole}
    hub_case = by_id["pair-implement-hub-request"]
    lane_case = by_id["pair-implement-lane-brief"]
    hub_runbook, lane_runbook = hub_case.required[0], lane_case.required[0]
    picked = [[hub_runbook, lane_runbook], [lane_runbook]]
    results = [
        score_trial(
            c,
            0,
            p,
            degraded=False,
            latency_s=0.0,
            confidence=None,
            task_type=None,
            reason=None,
            prompt_tokens=None,
            completion_tokens=None,
        )
        for c, p in zip([hub_case, lane_case], picked, strict=True)
    ]
    doc = build_local_doc(
        cases=[hub_case, lane_case],
        whole_corpus=whole,
        results=results,
        losses=[],
        spans={},
        picked=picked,
        reserved_cases=0,
        artifact=Path("."),
        catalog=catalog,
        candidates=32,
    )
    assert doc["recall_pass"] == [False, True]
    assert doc["forbidden"] == [[lane_runbook], []]
    assert doc["summary"]["forbidden_cases"] == 1
    assert doc["summary"]["recall_pass_cases"] == 1


# ------------------------------------------------------------------ the caller contract


@pytest.mark.parametrize("name", sorted(REQUIRED_SCENARIOS))
def test_each_scenarios_runbooks_run_on_their_own_caller_and_hide_nothing_in_optional(
    name: str,
) -> None:
    catalog = _catalog()
    hub_id, lane_id = _ends(name)
    hub, lane = catalog.get(hub_id), catalog.get(lane_id)
    assert hub is not None and lane is not None
    for runbook, surface in ((hub, "hub-full"), (lane, "lane-full")):
        bundle = [runbook, *catalog.supporting_for([runbook.id])]
        baseline = {t for member in bundle for t in member.meta.required_tools}
        assert baseline <= action_ids(surface), (runbook.id, sorted(baseline - action_ids(surface)))
        # `optionalTools` is for branches and caller variants the caller IS offered. A tool this
        # caller lacks parked there would let a baseline escape the contract unchecked.
        stray = set(runbook.meta.optional_tools) - action_ids(surface)
        assert not stray, (runbook.id, sorted(stray))
        assert not set(runbook.meta.optional_tools) & set(runbook.meta.required_tools)
    # The hub's baseline is something a lane cannot execute; that is the whole reason a lane
    # must not be handed it.
    assert not set(hub.meta.required_tools) <= action_ids("lane-full"), hub_id
    # The lane runbook's own procedure never calls the hub-only send.
    assert "host.sendMessage" not in lane.body, lane_id


@pytest.mark.parametrize("name", sorted(REQUIRED_SCENARIOS))
def test_each_hub_runbook_names_the_lane_runbook_its_brief_leads_to(name: str) -> None:
    catalog = _catalog()
    hub_id, lane_id = _ends(name)
    hub = catalog.get(hub_id)
    assert hub is not None
    if name in HUB_NAMES_NO_RECEIVER:
        assert (hub_id, lane_id) == HUB_NAMES_NO_RECEIVER[name]
        assert lane_id not in hub.body, (
            f"{hub_id} now names {lane_id}: drop {name!r} from HUB_NAMES_NO_RECEIVER"
        )
    else:
        assert lane_id in hub.body, f"{hub_id} does not name {lane_id} for the lane to find"


def test_every_pair_runbook_stays_inside_the_body_and_bundle_budgets() -> None:
    catalog = _catalog()
    for name in REQUIRED_SCENARIOS:
        for runbook_id in _ends(name):
            runbook = catalog.get(runbook_id)
            assert runbook is not None
            assert len(runbook.body.encode()) <= RUNBOOK_BODY_RECOMMENDED, runbook_id
            bundle = [runbook, *catalog.supporting_for([runbook_id])]
            size = sum(len(r.body.encode()) for r in bundle)
            assert size <= RUNBOOK_BUNDLE_CEILING, (runbook_id, size)


# ------------------------------------------------------------------ delivery on the wire


def _open_session(client: TestClient) -> dict[str, str]:
    init = client.post(
        "/v1/daintree/mcp",
        json={
            "jsonrpc": "2.0",
            "id": 1,
            "method": "initialize",
            "params": {"protocolVersion": "2025-06-18"},
        },
        headers=SESSION_HEADERS,
    )
    return {"Mcp-Session-Id": init.headers["mcp-session-id"]}


def _search(client: TestClient, headers: dict[str, str]) -> dict[str, Any]:
    response = client.post(
        "/v1/daintree/mcp",
        json={
            "jsonrpc": "2.0",
            "id": 2,
            "method": "tools/call",
            "params": {"name": TOOL_NAME, "arguments": {"query": "x", "max_results": 1}},
        },
        headers={**SESSION_HEADERS, **headers},
    )
    structured: dict[str, Any] = response.json()["result"]["structuredContent"]
    return structured


@pytest.mark.parametrize("name", sorted(REQUIRED_SCENARIOS))
def test_hub_and_lane_are_separate_sessions_each_sent_its_prerequisites_once(
    name: str, client: TestClient, app: Any
) -> None:
    """The hub and the lane are two clients, so two sessions: each is sent the general
    foundation and the hosts foundation in full on its first search, and a later search in the
    same session only names them. One session's record never spares the other's reading."""
    catalog: Catalog = app.state.container.catalog
    selector = app.state.container.selector
    hub_id, lane_id = _ends(name)
    expected = [r.id for r in catalog.supporting_for([hub_id])]
    assert expected == [r.id for r in catalog.supporting_for([lane_id])]
    assert expected[0] == "daintree.foundation" and "daintree.hosts.foundation" in expected

    hub_session, lane_session = _open_session(client), _open_session(client)
    assert hub_session != lane_session
    for session, runbook_id in ((hub_session, hub_id), (lane_session, lane_id)):
        selector.search_hits = [(runbook_id, 0.99, True)]
        first = _search(client, session)
        assert [r["id"] for r in first["runbooks"]] == [runbook_id]
        assert first["runbooks"][0]["instructions"] == catalog.get(runbook_id).body  # type: ignore[union-attr]
        assert [r["id"] for r in first["supporting_runbooks"]] == expected
        assert all("instructions" in r for r in first["supporting_runbooks"])

        again = _search(client, session)
        assert again["supporting_runbooks"] == [
            {"id": sid, "note": SENT_EARLIER_NOTE} for sid in expected
        ]
        # The procedure itself is never withheld.
        assert again["runbooks"][0]["instructions"] == first["runbooks"][0]["instructions"]
