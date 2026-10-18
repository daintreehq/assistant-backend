"""The caller's host state: the header contract, each runbook's eligibility, and the
exclusion a search applies before the shortlist (issue #153).

The policy runs against the packaged catalog in the ordinary sweep. The claim that
excluded runbooks free their shortlist slots for eligible ones runs against the
shipped artifact and skips without it.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from pydantic import ValidationError

from daintree_assistant_server.runbooks.catalog import Catalog, load_catalogs
from daintree_assistant_server.runbooks.host_state import (
    HOST_ELIGIBILITY,
    HostEligibility,
    HostState,
    fits,
    parse_host_state,
)
from daintree_assistant_server.runbooks.models import RunbookMeta

_ARTIFACT = (
    Path(__file__).resolve().parents[2]
    / "src/daintree_assistant_server/runbooks/local/artifacts/selector"
)

_ALL_STATES = [
    HostState(hosts=hosts, lane_reachable=lane)  # type: ignore[arg-type]
    for hosts in ("none", "configured", "connected")
    for lane in (False, True)
]


def _artifact_present() -> bool:
    head = _ARTIFACT / "head.onnx"
    # Absent until `training/fetch_selector.sh` has run; a stub is not a graph.
    return head.exists() and head.stat().st_size > 1024


@pytest.fixture(scope="module")
def catalog() -> Catalog:
    """The inbox catalog: the one v0.44 clients, the only callers with lanes, are
    served. The control catalog's hosts runbooks are checked in ``test_catalogs.py``."""
    return load_catalogs().inbox


# ---- the header contract ------------------------------------------------


@pytest.mark.parametrize(
    ("hosts", "lane", "expected"),
    [
        ("none", None, HostState("none")),
        ("configured", None, HostState("configured")),
        ("connected", None, HostState("connected")),
        ("connected", "reachable", HostState("connected", lane_reachable=True)),
        (" Connected ", " REACHABLE ", HostState("connected", lane_reachable=True)),
        ("none", "reachable", HostState("none", lane_reachable=True)),
    ],
)
def test_a_recognised_snapshot_is_reported(
    hosts: str, lane: str | None, expected: HostState
) -> None:
    assert parse_host_state(hosts, lane) == expected


@pytest.mark.parametrize(
    ("hosts", "lane"),
    [
        (None, None),
        (None, "reachable"),  # lane alone is no snapshot: Daintree always sends hosts
        ("", None),
        ("some", None),
        ("connected, none", None),
        ("connected", ""),
        ("connected", "yes"),
    ],
)
def test_a_missing_or_malformed_snapshot_is_unreported_not_an_error(
    hosts: str | None, lane: str | None
) -> None:
    """Unreported filters nothing, so an unknown value never reads as a definite no."""
    assert parse_host_state(hosts, lane) is None


def test_eligibility_against_every_reported_state() -> None:
    expected: dict[HostEligibility, set[tuple[str, bool]]] = {
        "any": {(s.hosts, s.lane_reachable) for s in _ALL_STATES},
        "configured": {(h, lane) for h in ("configured", "connected") for lane in (False, True)},
        "connected": {("connected", False), ("connected", True)},
        "lane": {(h, True) for h in ("none", "configured", "connected")},
    }
    assert set(expected) == HOST_ELIGIBILITY
    for eligibility, fitting in expected.items():
        got = {(s.hosts, s.lane_reachable) for s in _ALL_STATES if fits(eligibility, s)}
        assert got == fitting, eligibility


# ---- the frontmatter rule -------------------------------------------------


def _meta(**fields: object) -> RunbookMeta:
    return RunbookMeta.model_validate(
        {"title": "T", "summary": "S", "whenToUse": "Use when.", **fields}
    )


def test_a_hosts_runbook_must_declare_its_eligibility() -> None:
    with pytest.raises(ValidationError, match="must declare hostEligibility"):
        _meta(id="daintree.hosts.new-thing")
    assert _meta(id="daintree.hosts.new-thing", hostEligibility="lane").host_eligibility == "lane"


def test_no_runbook_outside_the_hosts_namespace_may_declare_one() -> None:
    for runbook_id in ("daintree.orchestration.fan-out-work", "daintree.hostsx.ask"):
        with pytest.raises(ValidationError, match="outside daintree.hosts"):
            _meta(id=runbook_id, hostEligibility="any")


def test_an_unknown_eligibility_is_refused() -> None:
    with pytest.raises(ValidationError):
        _meta(id="daintree.hosts.new-thing", hostEligibility="sometimes")


def test_every_packaged_hosts_runbook_is_classified(catalog: Catalog) -> None:
    hosts = {
        r.id: r.meta.host_eligibility
        for r in catalog.runbooks
        if r.id.startswith("daintree.hosts.")
    }
    assert hosts and all(v is not None for v in hosts.values())
    # The issue's lane group, exactly: the runbooks a Remote Host's own assistant runs.
    assert {rid for rid, v in hosts.items() if v == "lane"} == {
        f"daintree.hosts.{leaf}"
        for leaf in (
            "handle-hub-message",
            "do-delegated-work",
            "run-local-cohort",
            "review-for-hub",
            "demo-for-hub",
            "verify-for-hub",
            "steer-for-hub",
            "integrate-for-hub",
            "clean-up-for-hub",
        )
    }
    # Adding the first host, and the domain's base judgment, fit a caller with none.
    assert {rid for rid, v in hosts.items() if v == "any"} == {
        "daintree.hosts.connect-host",
        "daintree.hosts.foundation",
    }
    # What is wanted right after hosts drop stays offered while they are down, and the
    # UI-only procedures need a host, not its messaging (messaging off is `configured`).
    for leaf in (
        "recover-and-clean-up",
        "park-swarm",
        "undo-swarm-changes",
        "diagnose-messaging",
        "copy-portable-settings",
        "open-project-on-host",
    ):
        assert hosts[f"daintree.hosts.{leaf}"] == "configured", leaf


# ---- the catalog's exclusion ------------------------------------------------


def test_an_unreported_caller_is_offered_the_whole_catalog(catalog: Catalog) -> None:
    assert catalog.excluded_for(None) == frozenset()


@pytest.mark.parametrize("state", _ALL_STATES)
def test_only_hosts_runbooks_that_do_not_fit_are_excluded(
    catalog: Catalog, state: HostState
) -> None:
    excluded = catalog.excluded_for(state)
    for runbook in catalog.runbooks:
        eligibility = runbook.meta.host_eligibility
        should = eligibility is not None and not fits(eligibility, state)
        assert (runbook.id in excluded) == should, runbook.id
    # A view per request: the catalog's own selectable set never narrows.
    assert excluded <= catalog.selectable_ids
    assert len(catalog.selectable_ids) == len(catalog) - 1


def test_a_caller_with_no_hosts_and_no_lane_keeps_only_setup(catalog: Catalog) -> None:
    offered = {
        rid
        for rid in catalog.selectable_ids - catalog.excluded_for(HostState("none"))
        if rid.startswith("daintree.hosts.")
    }
    assert offered == {"daintree.hosts.connect-host", "daintree.hosts.foundation"}


# ---- benchmark cases --------------------------------------------------------


def _case(**fields: object) -> dict[str, object]:
    return {
        "id": "c",
        "messages": [{"role": "user", "content": "add a host"}],
        "expect": {"required": ["daintree.hosts.connect-host"]},
        **fields,
    }


def test_a_case_carries_its_state_and_the_exclusion_it_implies(catalog: Catalog) -> None:
    from benchmarks.runbook_selection.cases import _build_case

    case = _build_case(
        _case(host_model="inbox", host_state={"hosts": "none", "lane": "reachable"}), "f", catalog
    )
    assert case.host_state == HostState("none", lane_reachable=True)
    assert case.exclude == catalog.excluded_for(case.host_state)
    unreported = _build_case(_case(), "f", catalog)
    assert unreported.host_state is None and unreported.exclude == frozenset()


@pytest.mark.parametrize(
    "host_state",
    [{"hosts": "nope"}, {"hosts": "none", "lane": "yes"}, {"lane": "reachable"}, "none",
     {"hosts": "none", "extra": 1}],
)
def test_a_malformed_case_state_fails_loudly(catalog: Catalog, host_state: object) -> None:
    """Strict, unlike the transport: a case's state is authored, and a typo that read
    as unreported would score the case against the whole catalog unnoticed."""
    from benchmarks.runbook_selection.cases import CaseError, _build_case

    with pytest.raises(CaseError, match="host_state"):
        _build_case(_case(host_state=host_state), "f", catalog)


def test_a_reachable_lane_is_the_inbox_models(catalog: Catalog) -> None:
    from benchmarks.runbook_selection.cases import CaseError, _build_case

    with pytest.raises(CaseError, match="inbox model's"):
        _build_case(_case(host_state={"hosts": "none", "lane": "reachable"}), "f", catalog)


def test_a_case_may_not_expect_what_its_state_leaves_out(catalog: Catalog) -> None:
    """Scored, it would be a candidate miss that is really a broken case."""
    from benchmarks.runbook_selection.cases import CaseError, _build_case

    raw = _case(
        host_state={"hosts": "none"},
        expect={
            "required": ["daintree.orchestration.fan-out-work"],
            "optional": ["daintree.hosts.swarm-fan-out"],
        },
    )
    with pytest.raises(CaseError, match="daintree.hosts.swarm-fan-out"):
        _build_case(raw, "f", catalog)


# ---- the selector, against the shipped artifact ------------------------------


@pytest.mark.skipif(not _artifact_present(), reason="selector model not fetched (training/fetch_selector.sh)")
def test_excluded_runbooks_free_their_shortlist_slots_for_eligible_ones(
    catalog: Catalog,
) -> None:
    """Filtering after the head would leave the ballot short by every excluded
    runbook retrieval ranked; excluding before it fills all ``k`` slots."""
    from daintree_assistant_server.runbooks.local.setwise import SetwiseClassifier

    clf = SetwiseClassifier(_ARTIFACT)
    clf.sync(catalog)
    query = "Work these six issues in parallel, an agent on each"
    exclude = catalog.excluded_for(HostState("none"))

    whole = clf.select(query, [], catalog=catalog)
    hosted = [sid for sid, _ in whole.ranked if sid in exclude]
    assert hosted, "the query no longer shortlists a host runbook: pick one that does"

    narrowed = clf.select(query, [], catalog=catalog, exclude=exclude)
    assert len(narrowed.ranked) == len(whole.ranked) == clf.candidates
    assert not {sid for sid, _ in narrowed.ranked} & exclude
    assert narrowed.runbook_ids[0] == "daintree.orchestration.fan-out-work"

    # An empty exclusion is the unfiltered search, exactly.
    assert clf.select(query, [], catalog=catalog, exclude=frozenset()).ranked == whole.ranked


@pytest.mark.skipif(not _artifact_present(), reason="selector model not fetched (training/fetch_selector.sh)")
def test_an_excluded_active_runbook_is_neither_reserved_nor_kept(catalog: Catalog) -> None:
    from daintree_assistant_server.runbooks.local.setwise import SetwiseClassifier

    clf = SetwiseClassifier(_ARTIFACT)
    clf.sync(catalog)
    active = "daintree.hosts.swarm-fan-out"
    exclude = catalog.excluded_for(HostState("none"))
    assert active in exclude
    sel = clf.select("how is it going?", [active], catalog=catalog, exclude=exclude)
    assert active not in {sid for sid, _ in sel.ranked}
    assert active not in sel.runbook_ids and active not in sel.reserved_ids


@pytest.mark.skipif(not _artifact_present(), reason="selector model not fetched (training/fetch_selector.sh)")
def test_excluding_the_whole_table_is_an_empty_selection(catalog: Catalog) -> None:
    from daintree_assistant_server.runbooks.local.setwise import SetwiseClassifier

    clf = SetwiseClassifier(_ARTIFACT)
    clf.sync(catalog)
    sel = clf.select("rename my terminals", [], catalog=catalog, exclude=catalog.selectable_ids)
    assert sel.runbook_ids == [] and sel.ranked == ()


@pytest.mark.skipif(not _artifact_present(), reason="selector model not fetched (training/fetch_selector.sh)")
def test_the_docs_pass_builds_its_own_ballot_under_the_same_exclusion(
    catalog: Catalog,
) -> None:
    from daintree_assistant_server.runbooks.local.setwise import SetwiseClassifier

    clf = SetwiseClassifier(_ARTIFACT)
    clf.sync(catalog)
    exclude = catalog.excluded_for(HostState("none"))
    seen: list[frozenset[str]] = []
    real = clf._encode_pass

    def spy(table, text, active, spans=None, exclude=frozenset()):  # type: ignore[no-untyped-def]
        seen.append(exclude)
        return real(table, text, active, spans, exclude)

    clf._encode_pass = spy  # type: ignore[method-assign]
    clf.select(
        "[recent: the last turn]\nwhat is a remote host?",
        [],
        catalog=catalog,
        docs_query="what is a remote host?",
        exclude=exclude,
    )
    assert seen == [exclude, exclude]
