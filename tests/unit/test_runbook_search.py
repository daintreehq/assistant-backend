"""``LocalSelector.search`` — the ranking behind the runbook-search MCP tool.

The read-off-the-ballot rules run against a stub classifier in the ordinary
sweep, and the claim that the search actually FINDS the right runbook runs
against the shipped artifact and skips without it.
"""

from __future__ import annotations

import asyncio
import inspect
from pathlib import Path
from typing import Any

import pytest

from daintree_assistant_server.runbooks.local.runtime import InferencePool
from daintree_assistant_server.runbooks.local.selector import LocalSelector, RunbookSelector
from daintree_assistant_server.runbooks.local.setwise import Selection

_ARTIFACT = (
    Path(__file__).resolve().parents[2]
    / "src/daintree_assistant_server/runbooks/local/artifacts/selector"
)


def _artifact_present() -> bool:
    head = _ARTIFACT / "head.onnx"
    # An un-pulled LFS file is a ~130-byte pointer, not a graph.
    return head.exists() and head.stat().st_size > 1024


class _Classifier:
    def __init__(self, ranked: list[tuple[str, float]], admitted: list[str]) -> None:
        self.ranked = ranked
        self.admitted = admitted
        self.calls: list[dict[str, Any]] = []
        self.fail = False

    def query_fits(self, text: str) -> bool:  # noqa: ARG002
        return True

    def select(self, query: str, active_ids: Any = (), **kwargs: Any) -> Selection:
        if self.fail:
            raise RuntimeError("bad graph")
        self.calls.append({"query": query, "active": list(active_ids), **kwargs})
        return Selection(
            runbook_ids=list(self.admitted),
            docs_wanted=False,
            cardinality=len(self.admitted),
            scores={},
            ranked=tuple(self.ranked),
        )


class _Catalog:
    def __init__(self, selectable: set[str]) -> None:
        self.selectable_ids = frozenset(selectable)


def _search(
    clf: _Classifier,
    selectable: set[str],
    query: str,
    limit: int,
    exclude: frozenset[str] = frozenset(),
) -> Any:
    async def go() -> Any:
        pool = InferencePool(workers=1, queue_depth=2, timeout_seconds=5.0)
        try:
            selector = LocalSelector(
                clf,  # type: ignore[arg-type]
                pool,
                _Catalog(selectable),  # type: ignore[arg-type]
            )
            return await selector.search(query, limit=limit, exclude=exclude)
        finally:
            pool.close()

    return asyncio.run(go())


def test_search_keeps_the_ranking_not_just_the_admitted_set() -> None:
    """The classifier admitted one runbook here, and the caller still gets the
    three it asked for, with the admission verdict as ``selected``."""
    clf = _Classifier([("a", 0.9), ("b", 0.6), ("c", 0.2), ("d", 0.1)], admitted=["a"])
    hits = _search(clf, {"a", "b", "c", "d"}, "rename terminals", 3)
    assert [(h.runbook_id, h.score, h.selected) for h in hits] == [
        ("a", 0.9, True),
        ("b", 0.6, False),
        ("c", 0.2, False),
    ]


def test_search_is_a_cold_query_with_no_docs_pass() -> None:
    clf = _Classifier([("a", 0.9)], admitted=[])
    _search(clf, {"a"}, "rename terminals", 5)
    (call,) = clf.calls
    assert call["active"] == []
    assert call.get("docs_query") is None
    assert "rename terminals" in call["query"]


def test_search_drops_runbooks_that_are_not_selectable() -> None:
    clf = _Classifier([("hidden", 0.99), ("a", 0.5)], admitted=[])
    hits = _search(clf, {"a"}, "q", 5)
    assert [h.runbook_id for h in hits] == ["a"]


def test_search_hands_the_exclusion_to_the_classifier_and_never_returns_it() -> None:
    """The classifier leaves excluded runbooks off the ballot (before the shortlist);
    the read-off drops them as well, so no seam can offer one."""
    clf = _Classifier([("host", 0.9), ("a", 0.5), ("b", 0.4)], admitted=["host", "a"])
    hits = _search(clf, {"host", "a", "b"}, "q", 5, exclude=frozenset({"host"}))
    assert [(h.runbook_id, h.selected) for h in hits] == [("a", True), ("b", False)]
    (call,) = clf.calls
    assert call["exclude"] == frozenset({"host"})


def test_an_unfiltered_search_excludes_nothing() -> None:
    clf = _Classifier([("a", 0.9)], admitted=[])
    _search(clf, {"a"}, "q", 5)
    (call,) = clf.calls
    assert call["exclude"] == frozenset()


def test_a_failed_search_is_none_not_empty() -> None:
    clf = _Classifier([], admitted=[])
    clf.fail = True
    assert _search(clf, {"a"}, "q", 5) is None


def test_the_scripted_seam_matches_the_search_signature() -> None:
    from fakes import ScriptedSelector

    protocol = inspect.signature(RunbookSelector.search)
    assert inspect.signature(LocalSelector.search) == protocol
    assert inspect.signature(ScriptedSelector.search) == protocol


@pytest.mark.skipif(not _artifact_present(), reason="selector artifact not present (LFS)")
def test_shipped_search_finds_the_obvious_runbook() -> None:
    from daintree_assistant_server.runbooks.catalog import default_runbooks_dir, load_catalog
    from daintree_assistant_server.runbooks.local.setwise import SetwiseClassifier

    catalog = load_catalog(default_runbooks_dir())
    clf = SetwiseClassifier(_ARTIFACT)
    # Embedding runbooks the artifact has not seen is a one-time startup cost (the app's
    # lifespan syncs before serving), not part of a search: keep it outside the timed call.
    clf.sync(catalog)

    async def go() -> Any:
        pool = InferencePool(workers=1, queue_depth=2, timeout_seconds=30.0)
        try:
            selector = LocalSelector(clf, pool, catalog)
            return await selector.search(
                "rename my terminals so i can tell the agents apart", limit=4
            )
        finally:
            pool.close()

    hits = asyncio.run(go())
    assert hits is not None and len(hits) == 4
    assert hits[0].runbook_id == "daintree.terminals.rename"
    assert hits[0].selected
    scores = [h.score for h in hits]
    assert scores == sorted(scores, reverse=True)


@pytest.mark.skipif(not _artifact_present(), reason="selector artifact not present (LFS)")
def test_shipped_search_offers_host_runbooks_only_where_the_state_fits() -> None:
    """Issue #153's pairs: the same fan-out request with and without hosts, and a
    lane with no hosts of its own still answered from its inbox wake line."""
    from daintree_assistant_server.runbooks.catalog import default_runbooks_dir, load_catalog
    from daintree_assistant_server.runbooks.host_state import HostState
    from daintree_assistant_server.runbooks.local.setwise import SetwiseClassifier

    catalog = load_catalog(default_runbooks_dir())
    clf = SetwiseClassifier(_ARTIFACT)
    clf.sync(catalog)
    fan_out = "Work these six issues in parallel, an agent on each"
    wake = (
        "Daintree: assistant inbox has a new message (exchange "
        "6a2d9f14-0b7e-4c53-8e21-c4f07b9a3d58). Read it with assistant.getInbox."
    )

    async def go(query: str, state: HostState | None) -> Any:
        pool = InferencePool(workers=1, queue_depth=2, timeout_seconds=30.0)
        try:
            selector = LocalSelector(clf, pool, catalog)
            return await selector.search(
                query, limit=10, exclude=catalog.excluded_for(state)
            )
        finally:
            pool.close()

    no_hosts = asyncio.run(go(fan_out, HostState("none")))
    assert len(no_hosts) == 10
    assert no_hosts[0].runbook_id == "daintree.orchestration.fan-out-work"
    assert no_hosts[0].selected
    assert not {h.runbook_id for h in no_hosts} & catalog.excluded_for(HostState("none"))

    lane = asyncio.run(go(wake, HostState("none", lane_reachable=True)))
    assert lane[0].runbook_id == "daintree.hosts.handle-hub-message"
    assert lane[0].selected


def test_a_query_the_tokenizer_rejects_is_a_failed_search_not_a_crash() -> None:
    """Fitting runs on the worker, inside the pool's failure boundary."""
    clf = _Classifier([("a", 0.9)], admitted=["a"])

    def boom(text: str) -> bool:
        raise ValueError("unencodable")

    clf.query_fits = boom  # type: ignore[method-assign]
    assert _search(clf, {"a"}, "q", 5) is None
