"""Unit tests for local (in-process) runbook selection.

Deliberately split by what needs a model and what does not. The document
rendering, the selector's decision rules and the worker pool are pure logic and
are tested with fakes, so they run in the normal ~5s unit sweep. The tests that need the real
ONNX graphs are marked and skip when the artifacts are absent, because those are
Git-LFS files a fresh clone may not have pulled.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import numpy as np
import pytest
from structlog.testing import capture_logs

from daintree_assistant_server.runbooks.local.documents import (
    query_document,
    runbook_document,
    split_when_to_use,
)
from daintree_assistant_server.runbooks.local.runtime import InferencePool
from daintree_assistant_server.runbooks.local.setwise import SetwiseClassifier, admit


@dataclass
class _Meta:
    id: str
    title: str
    summary: str
    when_to_use: str
    foundation: bool = False


@dataclass
class _Msg:
    role: str
    content: str


# --------------------------------------------------------------------------
# documents
# --------------------------------------------------------------------------


def test_split_when_to_use_separates_routing_exclusions() -> None:
    head, tail = split_when_to_use(
        "Use when the user wants to rename terminals. Do not use when they want a report."
    )
    assert head == "Use when the user wants to rename terminals."
    assert tail.startswith("Do not use")


def test_split_when_to_use_without_exclusions_returns_empty_tail() -> None:
    head, tail = split_when_to_use("Use when the user wants X.")
    assert head == "Use when the user wants X."
    assert tail == ""


def test_short_variant_excludes_the_exclusion_clause() -> None:
    """The compact renderings drop the routing clauses; `full` keeps them."""
    meta = _Meta("x", "Title", "Summary.", "Use for A. Do not use for B — use the B runbook.")
    assert "Do not use" not in runbook_document(meta, variant="short")
    assert "Do not use" not in runbook_document(meta, variant="trigger")
    assert "Do not use" in runbook_document(meta, variant="full")


def test_unknown_document_variant_raises() -> None:
    with pytest.raises(ValueError, match="unknown runbook document variant"):
        runbook_document(_Meta("x", "T", "S", "W"), variant="nope")


def test_query_document_always_admits_the_newest_message() -> None:
    """Even when history alone would exhaust the budget.

    A query built from history only describes a task the ranker was never shown,
    which is strictly worse than a truncated one.
    """
    messages = [_Msg("user", "x" * 5000), _Msg("user", "rename my terminals")]
    assert query_document(messages).endswith("rename my terminals")


def test_query_document_includes_bounded_history() -> None:
    messages = [_Msg("user", "spin up three agents"), _Msg("user", "how is it going")]
    got = query_document(messages)
    assert "spin up three agents" in got
    assert got.endswith("how is it going")


def test_query_document_history_is_bounded() -> None:
    messages = [_Msg("user", "a" * 10_000), _Msg("user", "now what")]
    assert len(query_document(messages)) < 1000


def test_query_document_ignores_system_messages() -> None:
    messages = [_Msg("system", "SECRET"), _Msg("user", "rename terminals")]
    assert "SECRET" not in query_document(messages)


def test_query_document_with_no_eligible_messages_falls_back_to_turn_context() -> None:
    assert query_document([], turn_context="a watcher fired") == "a watcher fired"


# --------------------------------------------------------------------------
# admission policy — pure, no model needed
# --------------------------------------------------------------------------


def test_admit_takes_the_cardinality_count_in_rank_order() -> None:
    ranked = [("a", 0.9), ("b", 0.8), ("c", 0.2), ("d", 0.1)]
    assert admit(ranked, set(), 2, 3) == ["a", "b"]


def test_admit_zero_count_returns_empty_and_no_bonus_fires() -> None:
    """The bonus never turns an empty answer into a loaded runbook —
    polluting a no-task turn is how loading noise becomes the norm."""
    ranked = [("a", 0.9), ("b", 0.6)]
    assert admit(ranked, set(), 0, 3) == []


def test_admit_rejected_newcomer_does_not_consume_the_slot() -> None:
    """Cardinality says one; rank 1 is a filtered newcomer; rank 2 is the
    active runbook. The scan must continue to rank 2 instead of returning an
    authoritative empty set that evicts a live workflow."""
    ranked = [("newcomer", 0.60), ("running", 0.55)]
    assert admit(ranked, {"running"}, 1, 3) == ["running"]


def test_admit_bonus_rides_above_the_floor_up_to_the_cap() -> None:
    """Every candidate here clears the bonus floor, so the CAP is what stops the
    scan — not the floor. The earlier fixture used 0.51 for the fourth entry,
    which fell below the floor once it moved to 0.55: the assertion still passed
    but for the wrong reason, and a removed cap check would not have failed it.
    """
    ranked = [("a", 0.90), ("b", 0.80), ("c", 0.70), ("d", 0.69)]
    assert all(p > SetwiseClassifier.BONUS_FLOOR for _, p in ranked)
    # cardinality said one; all four clear the floor, so only the cap of 3 binds
    assert admit(ranked, set(), 1, 3) == ["a", "b", "c"]


def test_admit_bonus_floor_excludes_the_band_below_it() -> None:
    """The band immediately BELOW the floor is excluded, immediately above rides.

    Written as an epsilon either side of the constant rather than as two
    literals. The floor is a calibrated value — it is re-derived against each
    exported artifact's sigmoid by ``training/calibrate_floors.py`` and has
    already moved once (0.55 under the fixed-head classifier, 0.45 under the
    set-wise head) — so hardcoded scores stop testing the boundary the moment it
    moves and quietly become mid-band cases that pass for the wrong reason.
    """
    floor = SetwiseClassifier.BONUS_FLOOR
    assert admit([("correct", 0.91), ("marginal", floor - 0.01)], set(), 1, 3) == [
        "correct"
    ]
    # ...and the floor still admits what it is meant to admit
    assert admit([("correct", 0.91), ("worth_it", floor + 0.01)], set(), 1, 3) == [
        "correct",
        "worth_it",
    ]


def test_admit_bonus_stops_at_the_first_below_floor_score() -> None:
    floor = SetwiseClassifier.BONUS_FLOOR
    ranked = [("a", 0.9), ("b", floor - 0.01), ("c", floor - 0.02)]
    assert admit(ranked, set(), 1, 3) == ["a"]


def test_admit_bonus_does_not_readmit_an_active_runbook_below_the_floor() -> None:
    """The bonus floor applies to EVERY candidate, active ones included.

    The rule the code comment calls load-bearing: the bonus exists to rescue a
    near-miss RANKING, not to re-admit a runbook the model scored below a coin flip
    just because it used to be loaded. The sibling below-floor test drives this
    with nothing active, so it would still pass a refactor that exempted active
    runbooks from the floor — which is exactly the drift this pins.
    """
    ranked = [("newcomer", 0.9), ("stale", SetwiseClassifier.BONUS_FLOOR - 0.01)]
    assert admit(ranked, {"stale"}, 1, 3) == ["newcomer"]


def test_admit_floors_are_inclusive_at_their_exact_boundary() -> None:
    """Both floors are `>=` comparisons, so a score landing exactly on one is
    admitted.

    Driven off the constants rather than literals: both floors are tuned values
    (BONUS_FLOOR moved 0.50 -> 0.55 to stop sub-0.55 noise riding along), and a
    boundary test written with a hardcoded number stops testing the boundary the
    moment one moves — it just becomes another mid-band case.
    """
    newcomer_floor = SetwiseClassifier.NEWCOMER_FLOOR_WHEN_ACTIVE
    bonus_floor = SetwiseClassifier.BONUS_FLOOR
    # a newcomer exactly ON the newcomer floor, with a workflow active
    assert admit([("running", 0.9), ("newcomer", newcomer_floor)], {"running"}, 2, 3) == [
        "running",
        "newcomer",
    ]
    # a bonus candidate exactly ON the bonus floor
    assert admit([("a", 0.9), ("b", bonus_floor)], set(), 1, 3) == ["a", "b"]


def test_admit_bonus_newcomer_still_owes_the_higher_floor() -> None:
    """A score BETWEEN the two floors clears the bonus floor but not the
    newcomer floor: a newcomer at it stays out, an ACTIVE runbook at it rides."""
    between = (
        SetwiseClassifier.BONUS_FLOOR + SetwiseClassifier.NEWCOMER_FLOOR_WHEN_ACTIVE
    ) / 2
    assert between > SetwiseClassifier.BONUS_FLOOR
    assert between < SetwiseClassifier.NEWCOMER_FLOOR_WHEN_ACTIVE
    ranked = [("running", 0.9), ("newcomer", between), ("also_running", between)]
    got = admit(ranked, {"running", "also_running"}, 1, 3)
    assert got == ["running", "also_running"]


def test_admit_respects_max_active_over_count() -> None:
    ranked = [("a", 0.9), ("b", 0.8), ("c", 0.7), ("d", 0.6)]
    assert admit(ranked, set(), 3, 2) == ["a", "b"]


# --------------------------------------------------------------------------
# LocalSelector — the serving contract
# --------------------------------------------------------------------------


class _StubClassifier:
    """Duck-typed SetwiseClassifier: fixed answer, recorded inputs."""

    def __init__(self, runbook_ids: list[str] | None = None,
                 docs: bool = False, fail: bool = False) -> None:
        self._ids = runbook_ids if runbook_ids is not None else ["daintree.terminals.rename"]
        self._docs = docs
        self._fail = fail
        self.calls: list[tuple[str, list[str]]] = []
        #: Every catalog handed to select(), so a test can assert the selector
        #: passes the SNAPSHOT it validated against and not a re-read of live state.
        self.catalogs: list[object] = []
        #: Every docs_query handed to select(). The docs decision is taken on a
        #: DIFFERENT string from the runbook decision, and a test that cannot see
        #: which string arrived cannot tell the two apart.
        self.docs_queries: list[str | None] = []
        #: What the stub should report as force-included, for the telemetry test.
        self.reserved: tuple[str, ...] = ()

    def query_fits(self, text: str) -> bool:  # noqa: ARG002 - the stub never shrinks
        """The real classifier measures against its tokenizer; the stub has none.

        Always True, so ``fit_query`` takes its first schedule entry and these
        tests keep asserting the exact string the renderer produces by default.
        """
        return True

    def select(self, query: str, active_ids=(), *, catalog=None, max_active: int = 3,
               docs_query: str | None = None, docs_threshold: float = 0.4):
        if self._fail:
            raise RuntimeError("bad graph")
        self.calls.append((query, list(active_ids)))
        self.catalogs.append(catalog)
        self.docs_queries.append(docs_query)
        from daintree_assistant_server.runbooks.local.setwise import Selection

        return Selection(runbook_ids=list(self._ids), docs_wanted=self._docs,
                         cardinality=len(self._ids),
                         scores=dict.fromkeys(self._ids, 0.9),
                         reserved_ids=self.reserved)


class _StubRunbook:
    def __init__(self, sid: str) -> None:
        self.meta = _Meta(sid, f"Title {sid}", "Summary.", "Use when X.")


class _StubCatalog:
    def __init__(self, selectable: set[str]) -> None:
        self.selectable_ids = selectable
        self.runbooks = [_StubRunbook(sid) for sid in sorted(selectable)]


class _StubManager:
    def __init__(self, catalog: _StubCatalog) -> None:
        self.catalog = catalog


class _StubSettings:
    max_active_runbooks = 3


def _local_selector(classifier: _StubClassifier):
    from daintree_assistant_server.runbooks.local.selector import LocalSelector

    catalog = _StubCatalog({"daintree.terminals.rename", "daintree.foundation"})
    pool = InferencePool(workers=1, queue_depth=2, timeout_seconds=5.0)
    return LocalSelector(classifier, pool, _StubManager(catalog), _StubSettings()), pool  # type: ignore[arg-type]


def test_local_selector_returns_the_classifier_answer() -> None:
    async def go() -> None:
        clf = _StubClassifier()
        sel, pool = _local_selector(clf)
        decision = await sel.select([_Msg("user", "rename my terminals")], [])
        assert decision.runbook_ids == ["daintree.terminals.rename"]
        assert decision.ran and decision.authoritative and not decision.degraded
        assert decision.endpoint == "local"
        pool.close()

    asyncio.run(go())


def test_local_selector_serves_a_catalog_the_artifact_never_saw() -> None:
    """A changed catalog is answered, not degraded — the architecture's point.

    The previous selector owned one weight row per runbook, so it had to compare a
    stamped routing revision and degrade every turn open until a human
    retrained. This one reads runbook EMBEDDINGS, so an unfamiliar catalog is just
    unfamiliar vectors: it is passed through to the model and answered.
    """

    async def go() -> None:
        clf = _StubClassifier()
        sel, pool = _local_selector(clf)
        decision = await sel.select([_Msg("user", "rename my terminals")], ["prior.runbook"])
        assert not decision.degraded
        assert decision.ran and decision.authoritative
        assert clf.calls, "the model must be consulted for an unfamiliar catalog"
        pool.close()

    asyncio.run(go())


def test_local_selector_passes_the_catalog_snapshot_to_the_model() -> None:
    """The embedding table is reconciled against the SAME snapshot the decision
    used — a re-read could reconcile against a catalog the guards never saw."""

    async def go() -> None:
        clf = _StubClassifier()
        sel, pool = _local_selector(clf)
        await sel.select([_Msg("user", "rename my terminals")], [])
        assert clf.catalogs[0] is sel.catalog
        pool.close()

    asyncio.run(go())


def test_local_selector_fails_open_when_the_model_raises() -> None:
    async def go() -> None:
        clf = _StubClassifier(fail=True)
        sel, pool = _local_selector(clf)
        decision = await sel.select([_Msg("user", "rename my terminals")], [])
        assert decision.degraded
        assert decision.warnings == ["local_runbook_selector_failed_open"]
        pool.close()

    asyncio.run(go())


def test_local_selector_background_wake_keeps_the_active_set() -> None:
    async def go() -> None:
        clf = _StubClassifier()
        sel, pool = _local_selector(clf)
        convo = [_Msg("user", "watch it"), _Msg("assistant", "watcher armed, ending turn")]
        decision = await sel.select(convo, ["daintree.foundation"])
        assert decision.runbook_ids == ["daintree.foundation"]
        assert decision.task_type == "background_wake"
        assert clf.calls == [], "a wake has no new instruction to classify"
        pool.close()

    asyncio.run(go())


def test_local_selector_filters_ids_the_live_catalog_does_not_have() -> None:
    async def go() -> None:
        clf = _StubClassifier(runbook_ids=["daintree.terminals.rename", "runbook.ghost"])
        sel, pool = _local_selector(clf)
        decision = await sel.select([_Msg("user", "rename")], [])
        assert decision.runbook_ids == ["daintree.terminals.rename"]
        pool.close()

    asyncio.run(go())


def test_local_selector_degrades_when_no_named_id_resolves() -> None:
    """Named-but-unresolvable is a mismatch, not an authoritative clear."""

    async def go() -> None:
        clf = _StubClassifier(runbook_ids=["runbook.ghost", "runbook.phantom"])
        sel, pool = _local_selector(clf)
        decision = await sel.select([_Msg("user", "rename")], ["daintree.foundation"])
        assert decision.degraded
        assert decision.warnings == ["local_runbook_selector_unknown_ids"]
        pool.close()

    asyncio.run(go())


def test_local_selector_hides_pinned_actives_from_the_classifier() -> None:
    """A server-pinned (non-selectable) active id never reaches the model.

    It was absent from every training example's [loaded: …] prefix, and feeding
    it in was measured to change 49 of 124 benchmark answers.
    """

    async def go() -> None:
        clf = _StubClassifier()
        sel, pool = _local_selector(clf)
        await sel.select(
            [_Msg("user", "rename my terminals")],
            ["daintree.foundation", "daintree.docs.pinned-not-selectable"],
        )
        assert clf.calls[0][1] == ["daintree.foundation"]
        pool.close()

    asyncio.run(go())


def test_local_selector_logs_a_reservation_without_conversation_text() -> None:
    """The one number worth watching as the catalog grows, and nothing else.

    Reservation firing is how often the pre-reservation ballot would have been
    evicting a live workflow, so it is logged — but runbook ids only. This
    codebase never logs user text, and a selector log line is one of the easier
    places to leak it by accident.

    Captured through `structlog.testing` rather than `caplog`, for the reason
    `test_provider_failover.py` gives: once `configure_logging` has run — which it
    has, in any session that touched the app — the logger factory prints to stdout
    and never reaches stdlib logging, so a `caplog` assertion would pass or fail on
    TEST ORDER rather than on behaviour.
    """
    captured: list[dict[str, object]] = []

    async def go() -> None:
        clf = _StubClassifier(["daintree.terminals.rename"])
        clf.reserved = ("daintree.terminals.rename",)
        sel, pool = _local_selector(clf)
        with capture_logs() as logs:
            await sel.select([_Msg("user", "PRIVATE-USER-TEXT")], ["daintree.terminals.rename"])
        captured.extend(logs)
        pool.close()

    asyncio.run(go())
    records = [e for e in captured if e["event"] == "local_selector_reserved_active"]
    assert len(records) == 1
    assert records[0]["reserved"] == ["daintree.terminals.rename"]
    assert records[0]["retained"] == ["daintree.terminals.rename"]
    assert "PRIVATE-USER-TEXT" not in repr(captured)


def test_local_selector_does_not_log_when_nothing_was_reserved() -> None:
    """The common path stays silent — this is a rate to watch, not a heartbeat."""

    async def go() -> tuple[object, ...]:
        clf = _StubClassifier()
        sel, pool = _local_selector(clf)
        try:
            with capture_logs() as logs:
                await sel.select([_Msg("user", "rename my terminals")], [])
        finally:
            pool.close()
        return tuple(entry["event"] for entry in logs)

    assert "local_selector_reserved_active" not in asyncio.run(go())


def test_local_selector_docs_query_is_the_cleaned_last_user_message() -> None:
    async def go() -> None:
        clf = _StubClassifier(docs=True)
        sel, pool = _local_selector(clf)
        decision = await sel.select(
            [_Msg("user", "  what   is\na watcher?  ")], []
        )
        assert decision.docs_query == "what is a watcher?"
        pool.close()

    asyncio.run(go())


# --------------------------------------------------------------------------
# SetwiseClassifier — against the real shipped artifact (skips without LFS)
# --------------------------------------------------------------------------

_ARTIFACT = (
    Path(__file__).resolve().parents[2]
    / "src/daintree_assistant_server/runbooks/local/artifacts/selector"
)


def _artifact_present() -> bool:
    """True when both int8 graphs are real bytes, not unfetched LFS pointers."""
    graphs = [
        _ARTIFACT / "retriever" / "model_int8.onnx",
        _ARTIFACT / "ranking" / "model_int8.onnx",
        _ARTIFACT / "head.onnx",
    ]
    return all(g.is_file() and g.stat().st_size > 1_000_000 for g in graphs[:2]) and (
        graphs[2].is_file() and graphs[2].stat().st_size > 1_000
    )


def _live_selector():  # type: ignore[no-untyped-def]
    from daintree_assistant_server.runbooks.catalog import default_runbooks_dir, load_catalog
    from daintree_assistant_server.runbooks.local.setwise import SetwiseClassifier

    catalog = load_catalog(default_runbooks_dir())
    clf = SetwiseClassifier(_ARTIFACT)
    clf.sync(catalog)
    return clf, catalog


@pytest.mark.skipif(not _artifact_present(), reason="selector artifact not present (LFS)")
def test_shipped_classifier_selects_a_plausible_runbook() -> None:
    clf, _ = _live_selector()
    sel = clf.select("rename my terminals so i can tell the agents apart", [])
    assert "daintree.terminals.rename" in sel.runbook_ids
    assert 0 < sel.cardinality <= 3


@pytest.mark.skipif(not _artifact_present(), reason="selector artifact not present (LFS)")
def test_shipped_classifier_returns_empty_on_chatter() -> None:
    clf, _ = _live_selector()
    assert clf.select("thanks, that's perfect", []).runbook_ids == []


@pytest.mark.skipif(not _artifact_present(), reason="selector artifact not present (LFS)")
def test_shipped_table_covers_exactly_the_selectable_catalog() -> None:
    """The table is built FROM the live catalog, so it can only ever match it.

    Asserted anyway because the ids in it are what the selector may return, and
    a table built from a stale ``vectors.npz`` that ``sync`` failed to reconcile
    would offer runbooks the catalog cannot load.
    """
    clf, catalog = _live_selector()
    table = clf.table
    assert table is not None
    assert set(table.ids) == set(catalog.selectable_ids)


@pytest.mark.skipif(not _artifact_present(), reason="selector artifact not present (LFS)")
def test_a_runbook_added_after_training_is_routable_without_retraining() -> None:
    """The property the whole two-stage design exists for.

    A runbook the artifact has never seen — no weight row, no training row, not in
    ``vectors.npz`` — is embedded on ``sync`` and can win a turn. Under the
    previous fixed-head selector this was structurally impossible: there was no
    output for it, and the routing-revision gate degraded every turn open until
    someone retrained.

    Note what this does and does not pin. The invented runbook is deliberately
    UNCONTESTED — nothing else in the catalog claims credential rotation — which
    is the case zero-shot routing actually handles. Measured against runbooks that
    share vocabulary with a trained neighbour it is much weaker (4/14 vs 13/14;
    training/EXPERIMENTS.md), so this asserts "reachable", never "as good as
    trained".
    """
    from daintree_assistant_server.runbooks.catalog import default_runbooks_dir, load_catalog
    from daintree_assistant_server.runbooks.local.setwise import SetwiseClassifier

    catalog = load_catalog(default_runbooks_dir())
    clf = SetwiseClassifier(_ARTIFACT)
    before = clf.sync(catalog)
    invented = "daintree.secrets.rotate-credentials"
    assert invented not in before.ids

    # Deliberately IN-DOMAIN and unclaimed. An off-domain invention (the first
    # draft of this test used piano tuning) tests the wrong thing: the
    # cardinality head would rightly answer "this turn needs no runbook", and the
    # test would fail for a correct reason.
    grown = _CatalogPlusOne(
        catalog,
        _StubRunbook2(
            invented,
            "Rotate service credentials",
            "Rotate, revoke and reissue API keys and tokens for a service, then "
            "update everything that consumes them.",
            "Use when the user wants credentials rotated, an API key reissued, or "
            "a leaked token revoked and replaced.",
        ),
    )
    after = clf.sync(grown)
    assert invented in after.ids
    # Not merely present — reachable. It must win its own obvious request.
    sel = clf.select(
        "rotate the api keys for the staging service and revoke the old ones",
        [],
        catalog=grown,
    )
    assert invented in sel.runbook_ids
    # And every other vector was REUSED rather than recomputed: the digest keys
    # the row to the document it was built from, so a one-runbook change costs one
    # embedding, not a full rebuild.
    kept = [sid for sid in before.ids if sid in after.ids]
    for sid in kept:
        assert before.digests[sid] == after.digests[sid]


class _StubRunbook2:
    def __init__(self, sid: str, title: str, summary: str, when: str) -> None:
        self.id = sid
        self.meta = _Meta(sid, title, summary, when)


class _CatalogPlusOne:
    """The live catalog with one extra runbook, as a catalog-shaped duck type."""

    def __init__(self, base, extra) -> None:  # type: ignore[no-untyped-def]
        self._base = base
        self._extra = extra
        self.selectable_ids = set(base.selectable_ids) | {extra.id}

    def get(self, sid: str):  # type: ignore[no-untyped-def]
        return self._extra if sid == self._extra.id else self._base.get(sid)


@pytest.mark.skipif(not _artifact_present(), reason="selector artifact not present (LFS)")
@pytest.mark.parametrize(
    ("query", "expected"),
    [
        (
            "File a new issue called Login crash and label it bug.",
            "daintree.forge.manage-issue",
        ),
        (
            "Squash and merge pull request 42 after checking every gate.",
            "daintree.forge.land-pull-request",
        ),
        (
            "Plan this multi-stage migration with dependencies and track it across restarts.",
            "daintree.orchestration.execute-durable-workflow",
        ),
        (
            "Pause the remote devbox for this worktree to save cost.",
            "daintree.worktree.manage-resource",
        ),
        (
            "The existing Daintree Dev Preview is blank; inspect its console error.",
            "daintree.runtime.debug-web-app",
        ),
    ],
)
def test_shipped_classifier_routes_each_new_mcp_runbook(query: str, expected: str) -> None:
    """Every shipped runbook must be reachable from a representative request."""
    clf, _ = _live_selector()
    assert expected in clf.select(query, []).runbook_ids


@pytest.mark.skipif(not _artifact_present(), reason="selector artifact not present (LFS)")
@pytest.mark.parametrize(
    ("query", "excluded"),
    [
        (
            "Bundle the relevant parser files into one CopyTree reference file.",
            "daintree.orchestration.execute-durable-workflow",
        ),
        (
            "Explore the React source and explain how routing works; do not change files.",
            "daintree.runtime.debug-web-app",
        ),
        (
            "Create a plain git worktree for a new feature branch.",
            "daintree.worktree.manage-resource",
        ),
    ],
)
def test_shipped_classifier_respects_new_runbook_boundaries(query: str, excluded: str) -> None:
    """Broad MCP runbooks must not displace their common read-only/local neighbours."""
    clf, _ = _live_selector()
    assert excluded not in clf.select(query, []).runbook_ids


# --------------------------------------------------------------------------
# inference pool
# --------------------------------------------------------------------------


def test_pool_runs_work_and_counts_it() -> None:
    async def go() -> None:
        pool = InferencePool(workers=2, queue_depth=4, timeout_seconds=5.0)
        assert await pool.run(lambda: 41 + 1) == 42
        assert pool.stats().completed == 1
        assert pool.stats().degraded == 0
        pool.close()

    asyncio.run(go())


def test_pool_fails_open_when_the_model_raises() -> None:
    """A model error must degrade the turn, never propagate into the request."""

    async def go() -> None:
        pool = InferencePool(workers=1, queue_depth=2, timeout_seconds=5.0)

        def boom() -> int:
            raise RuntimeError("bad graph")

        assert await pool.run(boom) is None
        assert pool.stats().failed == 1
        pool.close()

    asyncio.run(go())


def test_pool_times_out_rather_than_hanging_a_turn() -> None:
    async def go() -> None:
        import time

        pool = InferencePool(workers=1, queue_depth=2, timeout_seconds=0.05)
        assert await pool.run(lambda: time.sleep(1.0)) is None
        assert pool.stats().timed_out == 1
        pool.close()

    asyncio.run(go())


def test_pool_refuses_work_after_close() -> None:
    async def go() -> None:
        pool = InferencePool(workers=1, queue_depth=2, timeout_seconds=1.0)
        pool.close()
        assert await pool.run(lambda: 1) is None

    asyncio.run(go())


def test_pool_rejects_when_every_admission_slot_is_taken() -> None:
    """Depth is bounded, and overflow degrades rather than growing a backlog."""

    async def go() -> None:
        import time

        pool = InferencePool(workers=1, queue_depth=1, timeout_seconds=5.0)
        slow = asyncio.create_task(pool.run(lambda: time.sleep(0.3)))
        await asyncio.sleep(0.05)
        assert await pool.run(lambda: 1) is None
        assert pool.stats().rejected_full == 1
        await slow
        pool.close()

    asyncio.run(go())


# --------------------------------------------------------------------------
# The docs decision reads a different string from the runbook decision
# --------------------------------------------------------------------------
#
# Regression cover for a silent, near-total outage of documentation lookup. The
# runbook query carries an ACTIVE_PREFIX and a tail of prior turns; the docs head
# was trained only on bare standalone questions, so either one drove its
# probability to ~0 and a mid-conversation usage question never retrieved. The
# fix classifies docs on the bare newest user message. See the note at the docs
# decision in ``setwise.py`` for the corpus measurements behind it.


def test_the_docs_decision_gets_the_bare_question_not_the_ranking_query() -> None:
    """The ranking query is context-laden by design; the docs one must not be."""

    async def go() -> None:
        clf = _StubClassifier(docs=True)
        sel, pool = _local_selector(clf)
        await sel.select(
            [
                _Msg("user", "rename my terminals"),
                _Msg("assistant", "Renamed all four."),
                _Msg("user", "how do I create a worktree"),
            ],
            ["daintree.foundation"],
        )
        ranking_query, _ = clf.calls[0]
        docs_query = clf.docs_queries[0]
        assert docs_query == "how do I create a worktree"
        # The ranking query carries the history the docs query must not.
        assert "rename my terminals" in ranking_query
        assert "rename my terminals" not in (docs_query or "")
        pool.close()

    asyncio.run(go())


def test_the_string_classified_is_the_string_searched_with() -> None:
    """One string, so the head's decision and the MCP search cannot drift apart."""

    async def go() -> None:
        clf = _StubClassifier(docs=True)
        sel, pool = _local_selector(clf)
        decision = await sel.select([_Msg("user", "what is a watcher?")], [])
        assert decision.docs_query == "what is a watcher?"
        assert decision.docs_query == clf.docs_queries[0]
        pool.close()

    asyncio.run(go())


def test_no_docs_query_is_returned_when_the_head_did_not_ask() -> None:
    """The string is always computed; it is only SURFACED when docs are wanted."""

    async def go() -> None:
        clf = _StubClassifier(docs=False)
        sel, pool = _local_selector(clf)
        decision = await sel.select([_Msg("user", "what is a watcher?")], [])
        assert decision.docs_query is None
        # ...but the classifier still received it, or it could not have decided.
        assert clf.docs_queries[0] == "what is a watcher?"
        pool.close()

    asyncio.run(go())


@pytest.mark.skipif(not _artifact_present(), reason="selector artifact not present (LFS)")
def test_a_usage_question_retrieves_docs_even_mid_conversation() -> None:
    """THE regression. The shape of session ses_aa9b63b4 turn 2.

    A usage question asked after a loaded runbook and a prior exchange. Before the
    fix this returned docs_wanted=False and the turn answered a product question
    with no product documentation.

    The question is FIRST person on purpose, and this is load-bearing. The live
    session used the second person, but that phrasing sits so near the decision
    boundary that it flips on quantisation alone — fp32 and int8 disagree about
    it on one machine (see DOCS_THRESHOLD in setwise.py). Asserting it here once
    reddened CI on x86 while passing on arm64. What this test is FOR is the
    context suppression, so it varies context and holds phrasing far from any
    boundary; the pronoun gap is a corpus problem, tracked in EXPERIMENTS.md and
    scored — not asserted — by the benchmark.
    """
    from daintree_assistant_server.runbooks.local.documents import query_document

    clf, catalog = _live_selector()
    convo = [
        _Msg("user", "Which MCP endpoint are you connected to?"),
        _Msg("assistant", "The Daintree MCP endpoint: http://127.0.0.1:45454/mcp."),
        _Msg("user", "How do I create a worktree in Daintree?"),
    ]
    bare = "How do I create a worktree in Daintree?"
    ranking_query = query_document(convo)

    assert clf.select(
        ranking_query, ["daintree.foundation"], catalog=catalog, docs_query=bare
    ).docs_wanted, "a usage question must retrieve docs with a runbook already loaded"

    # Deliberately NOT also asserting that docs_query=None still answers False.
    # That is characterisation of THIS artifact's defect, not a contract: a
    # retrain with contextual docs positives should make the context-laden path
    # answer correctly too, and an assertion that fails on that improvement is a
    # trap for whoever runs it. The contract is the line above — the question
    # gets its documentation — and it holds under both artifacts.


@pytest.mark.skipif(not _artifact_present(), reason="selector artifact not present (LFS)")
def test_conversation_context_no_longer_suppresses_documentation() -> None:
    """Recall must not depend on how far into a conversation the question lands."""
    clf, catalog = _live_selector()
    questions = [
        "what is a watcher",
        "how do I create a worktree",
        "how do recipes work in Daintree",
        "where do I change the keybinding for the terminal",
    ]
    from daintree_assistant_server.runbooks.local.documents import query_document

    for q in questions:
        cold = clf.select(q, [], catalog=catalog, docs_query=q)
        # A real conversation, rendered by the real renderer: the ACTIVE_PREFIX
        # is the classifier's job and must not be hand-written here, or the
        # model is fed two of them and the test measures a shape serving never
        # produces.
        convo = [
            _Msg("user", "rename my terminals so I can tell the agents apart"),
            _Msg("assistant", "Renamed all four."),
            _Msg("user", q),
        ]
        loaded = clf.select(
            query_document(convo, turn_context=f"current goal: {q}"),
            ["daintree.worktree.create-plain-worktree"],
            catalog=catalog,
            docs_query=q,
        )
        assert cold.docs_wanted, q
        assert loaded.docs_wanted, f"{q!r} lost its docs lookup mid-conversation"


@pytest.mark.skipif(not _artifact_present(), reason="selector artifact not present (LFS)")
def test_chatter_still_does_not_retrieve_documentation() -> None:
    """The threshold sits below 0.5; the no-task side must not have moved.

    Measured at 0 false positives on 169 chatter/no-task corpus queries for every
    threshold down to 0.15, so this is the cheap standing guard on that.
    """
    clf, catalog = _live_selector()
    for q in ("thanks, that's perfect", "ok cool", "close all the terminals"):
        assert not clf.select(q, [], catalog=catalog, docs_query=q).docs_wanted, q


@pytest.mark.skipif(not _artifact_present(), reason="selector artifact not present (LFS)")
def test_the_docs_pass_does_not_disturb_runbook_selection() -> None:
    """Only the docs logit is re-read; the ranking answer must be untouched."""
    clf, catalog = _live_selector()
    q = "rename my terminals so i can tell the agents apart"
    without = clf.select(q, [], catalog=catalog, docs_query=None)
    with_docs = clf.select(q, [], catalog=catalog, docs_query="what is a watcher")
    assert without.runbook_ids == with_docs.runbook_ids
    assert without.cardinality == with_docs.cardinality


@pytest.mark.skipif(not _artifact_present(), reason="selector artifact not present (LFS)")
def test_the_second_pass_is_skipped_when_the_strings_already_agree() -> None:
    """A cold opening turn pays nothing — asserted by COUNTING the work.

    Comparing outputs would not do it: an implementation that always ran the
    second pass would return the same answer and pass. So count head runs, which
    is the cost the skip exists to avoid.
    """
    clf, catalog = _live_selector()
    q = "what is a watcher"
    runs: list[int] = []
    real_run = clf._head.run

    def counting_run(*args, **kwargs):  # type: ignore[no-untyped-def]
        runs.append(1)
        return real_run(*args, **kwargs)

    clf._head = _Counting(clf._head, counting_run)  # type: ignore[assignment]
    try:
        clf.select(q, [], catalog=catalog, docs_query=q)
        assert sum(runs) == 1, "identical strings must not buy a second head run"
        runs.clear()
        clf.select(q, ["daintree.foundation"], catalog=catalog, docs_query=q)
        assert sum(runs) == 2, "a context-laden query must take the docs pass"
    finally:
        clf._head = clf._head.inner  # type: ignore[attr-defined]


class _Counting:
    """Wraps an ONNX session so ``run`` can be counted without touching select()."""

    def __init__(self, inner: Any, run: Any) -> None:
        self.inner = inner
        self.run = run

    def __getattr__(self, name: str) -> Any:
        return getattr(self.inner, name)


@pytest.mark.skipif(not _artifact_present(), reason="selector artifact not present (LFS)")
def test_an_empty_docs_query_behaves_like_no_docs_query() -> None:
    """"" is not a question. It must not be embedded and rated as one."""
    clf, catalog = _live_selector()
    q = "rename my terminals"
    assert (
        clf.select(q, [], catalog=catalog, docs_query="").docs_wanted
        == clf.select(q, [], catalog=catalog, docs_query=None).docs_wanted
    )


@pytest.mark.skipif(not _artifact_present(), reason="selector artifact not present (LFS)")
def test_a_narrowed_catalog_does_not_evict_the_full_catalog_s_vectors() -> None:
    """A sub-agent turn must not make the NEXT orchestrator turn re-embed.

    `_cached` is one slot. The sub-agent profile syncs a catalog narrowed by
    `Catalog.executable_with` (2 of 39 runbooks today), which replaces that slot —
    and before the digest memo existed, the next full-catalog turn re-encoded
    every evicted runbook on the first-byte path. Measured at 5.3s for 37 runbooks,
    once per alternation, against a ~10ms selection budget. Fan-out is the
    designed usage, so this fired on every delegation.

    Asserted as encoder CALLS rather than wall-clock: the cost is the ONNX runs,
    and a timing assertion on a shared CI box is a flake waiting to happen.
    """
    from daintree_assistant_server.runbooks.local import setwise as setwise_mod

    clf, catalog = _live_selector()
    narrow = catalog.executable_with(["fs.read", "fs.list"])
    assert 0 < len(narrow) < len(catalog), "fixture must actually narrow the catalog"

    calls = 0
    real = setwise_mod._run_encoder

    def counting(*args: Any, **kwargs: Any) -> Any:
        nonlocal calls
        calls += 1
        return real(*args, **kwargs)

    setwise_mod._run_encoder = counting  # type: ignore[assignment]
    try:
        clf.sync(narrow)
        clf.sync(catalog)
        clf.sync(narrow)
        clf.sync(catalog)
    finally:
        setwise_mod._run_encoder = real  # type: ignore[assignment]

    assert calls == 0, (
        f"alternating profiles re-embedded ({calls} encoder runs); every vector "
        "was already computed by the initial full sync"
    )


# --------------------------------------------------------------------------
# candidate reservation, end to end through the real graphs
# --------------------------------------------------------------------------


@pytest.mark.skipif(not _artifact_present(), reason="selector artifact not present (LFS)")
def test_an_active_runbook_reaches_the_head_even_when_retrieval_ranks_it_last() -> None:
    """The reservation guarantee, measured against the real retriever.

    The evidence used here is a genuine turn, not a synthetic similarity row:
    three loaded runbooks and a long message about something else, which is the
    worst case the ``ACTIVE_PREFIX`` mitigation measures at (ranks 8/10/11 of 38;
    see ``candidates.py``). The shortlist is then narrowed to 4 so that a rank the
    live catalog is too small to punish becomes the miss it will be at 200 runbooks.

    At the shipped ``k=32`` against 38 selectable runbooks this is unobservable —
    84% of the catalog is on the ballot regardless — which is exactly why it
    needs pinning before the catalog grows into the regime where it bites.
    """
    clf, _ = _live_selector()
    active = {
        "daintree.terminals.rename",
        "daintree.foundation",
        "daintree.worktree.create-plain-worktree",
    }
    query = (
        "The test run came back with three failures in the integration suite and I "
        "want to understand whether they are flaky or real, so please dig through "
        "the recent commit history, look at what changed in the database layer, and "
        "give me a written summary of the likely root cause."
    )

    table = clf.table
    assert table is not None
    from daintree_assistant_server.runbooks.local.candidates import assemble_candidates
    from daintree_assistant_server.runbooks.local.documents import ACTIVE_PREFIX
    from daintree_assistant_server.runbooks.local.setwise import _run_encoder

    text = ACTIVE_PREFIX.format(", ".join(sorted(active))) + query
    q = _run_encoder(clf._retriever, clf._retriever_tok, [text], normalize=True)[0]
    sims = table.retrieval @ q

    plain = {table.ids[int(i)] for i in assemble_candidates(sims, table.ids, set(), 4).indices}
    assert not (active & plain), (
        f"fixture is not exercising reservation: retrieval already returns "
        f"{active & plain} inside a four-slot shortlist"
    )

    chosen = assemble_candidates(sims, table.ids, active, 4)
    assert active <= {table.ids[int(i)] for i in chosen.indices}
    assert int(chosen.reserved.sum()) == 3
    # Three seats taken by reservation, one still earned by retrieval: the
    # ballot is not handed over to the active set, only guaranteed to include it.
    assert len(chosen) == 4


def _table_hiding(clf: Any, runbook_id: str, text: str):  # type: ignore[no-untyped-def]
    """The live table with ``runbook_id``'s RETRIEVAL vector pointed away from ``text``.

    The only way to observe reservation end to end on a 38-runbook catalog: at
    ``k=32`` an active runbook has to be beaten by 32 others to fall off the
    ballot, and the ``ACTIVE_PREFIX`` naming its own id makes that essentially
    impossible for a real query. So retrieval is made to miss it by construction
    — cosine driven to the floor — while its RANKING vector, the one the head
    actually reads, stays exactly as the catalog built it.

    That is the combination worth pinning: a real runbook embedding arriving with
    ``active=1``, ``rank_norm=1.0`` and a true cosine at the bottom, which is
    precisely what a 500-runbook catalog will hand the head on an ordinary vague
    follow-up.
    """
    from daintree_assistant_server.runbooks.local.setwise import RunbookTable, _run_encoder

    table = clf.table
    assert table is not None
    row = table.ids.index(runbook_id)
    q = _run_encoder(clf._retriever, clf._retriever_tok, [text], normalize=True)[0]
    retrieval = table.retrieval.copy()
    retrieval[row] = -q
    return RunbookTable(
        ids=table.ids,
        retrieval=retrieval,
        ranking=table.ranking,
        digests=table.digests,
        precision=table.precision,
    )


@pytest.mark.skipif(not _artifact_present(), reason="selector artifact not present (LFS)")
def test_a_reserved_candidate_reaches_the_head_with_the_right_features() -> None:
    """The tensor the head is actually fed, on a turn that really did reserve.

    Everything else here checks the assembler in isolation or the classifier on
    a turn where retrieval happened to succeed anyway. This is the one that would
    fail if ``_encode_pass`` were reverted to a plain top-k: the active runbook is
    unreachable by cosine, so its presence in the feed can only come from
    reservation, and its feature row is asserted rather than assumed.
    """
    from daintree_assistant_server.runbooks.local.documents import ACTIVE_PREFIX
    from daintree_assistant_server.runbooks.local.setwise import FEATURE_NAMES

    clf, _ = _live_selector()
    active = "daintree.terminals.rename"
    text = ACTIVE_PREFIX.format(active) + "how is that going?"
    table = _table_hiding(clf, active, text)

    chosen, feed = clf._encode_pass(table, text, {active})

    ids = [table.ids[int(i)] for i in chosen.indices]
    assert active in ids, "reservation did not seat an unreachable active runbook"
    slot = ids.index(active)
    assert bool(chosen.reserved[slot])

    feats = feed["features"][0]
    cosine, is_active, rank_norm = (FEATURE_NAMES.index(f) for f in FEATURE_NAMES)
    # Its real cosine — driven to the floor by the fixture, and reported as such
    # rather than dressed up.
    assert feats[slot][cosine] == pytest.approx(-1.0, abs=1e-3)
    assert feats[slot][is_active] == 1.0
    assert feats[slot][rank_norm] == 1.0
    assert feed["mask"][0][slot] == 1.0
    # And the embedding the head reads is the runbook's own, not a zero pad.
    row = table.ids.index(active)
    assert np.allclose(feed["candidates"][0][slot], table.ranking[row])


@pytest.mark.skipif(not _artifact_present(), reason="selector artifact not present (LFS)")
def test_a_reserved_candidate_is_scored_rather_than_absent() -> None:
    """The promise reservation makes, end to end: a hearing.

    The head returns a probability for the unreachable active runbook instead of
    never seeing it. What the head then DOES with that probability is a property
    of the weights, not of this policy — and on this artifact, which was trained
    before reservation existed, it scores it down (see the sweep recorded in
    ``candidates.py``). Asserting retention here would be asserting something
    the shipped weights do not yet do.
    """
    from daintree_assistant_server.runbooks.local.documents import ACTIVE_PREFIX

    clf, _ = _live_selector()
    active = "daintree.terminals.rename"
    query = "how is that going?"
    table = _table_hiding(clf, active, ACTIVE_PREFIX.format(active) + query)
    clf._cached = (None, table)

    sel = clf.select(query, [active])
    assert active in sel.reserved_ids, "fixture did not actually reserve"

    # Scored: the id reaches `admit` as a ranked candidate. Read off the head
    # directly rather than off `scores`, which reports only the top five.
    chosen, feed = clf._encode_pass(table, ACTIVE_PREFIX.format(active) + query, {active})
    logits, _docs, _card = clf._head.run(None, feed)
    slot = [table.ids[int(i)] for i in chosen.indices].index(active)
    assert np.isfinite(logits[0][slot])
    assert feed["mask"][0][slot] == 1.0


@pytest.mark.skipif(not _artifact_present(), reason="selector artifact not present (LFS)")
def test_a_topic_shift_evicts_the_prior_runbook() -> None:
    """The same rule on the UNMODIFIED catalog, where retrieval works normally.

    Not a reservation test — here the rename runbook is retrieved on its own merits
    — but the eviction it checks is the property reservation must not break.
    """
    clf, _ = _live_selector()
    sel = clf.select(
        "forget that — I need to create a git worktree for the new feature branch",
        ["daintree.terminals.rename"],
    )
    assert "daintree.terminals.rename" not in sel.runbook_ids


@pytest.mark.skipif(not _artifact_present(), reason="selector artifact not present (LFS)")
def test_reservation_stays_dormant_on_ordinary_follow_ups_today() -> None:
    """At 38 runbooks the change is inert in practice, and that is worth pinning.

    Not because 38 < 32 — it is not, and six runbooks genuinely do sit off every
    dense ballot — but because the ``ACTIVE_PREFIX`` names the active id in the
    query, which keeps a single loaded runbook at or near rank 0 (measured in
    ``candidates.py``). So reservation never fires on these turns, and the
    canonical evaluation is unchanged at 164/177.

    Pinned so that a future ``k``, catalog or evidence change that starts
    reserving on everyday turns surfaces here, where it is attributable, rather
    than as unexplained benchmark drift.
    """
    clf, _ = _live_selector()
    active = ["daintree.terminals.rename"]
    for query in (
        "how is that going?",
        "try again with the other approach",
        "now rename the second one too",
        "that failed, fix the error",
    ):
        sel = clf.select(query, active)
        assert sel.reserved_ids == (), (
            f"{query!r} reserved {sel.reserved_ids}; the loaded-runbook prefix "
            "should keep a single active runbook inside the shortlist at this "
            "catalog size"
        )


@pytest.mark.skipif(not _artifact_present(), reason="selector artifact not present (LFS)")
def test_a_pinned_nonselectable_runbook_is_never_seated_as_a_candidate() -> None:
    """The docs runbook is server-pinned and hidden from selection.

    It legitimately appears in a prior active set, so the reservation path has
    to treat it as an id with no table row — nothing to seat — rather than
    forcing a runbook the classifier was never trained to see. Injecting it as an
    active id was previously measured to change 49 of 124 benchmark answers.
    """
    from daintree_assistant_server.config import DOCS_RUNBOOK_ID

    clf, catalog = _live_selector()
    assert DOCS_RUNBOOK_ID not in catalog.selectable_ids

    table = clf.table
    assert table is not None
    from daintree_assistant_server.runbooks.local.candidates import assemble_candidates

    sims = np.zeros(len(table.ids), dtype=np.float32)
    chosen = assemble_candidates(sims, table.ids, {DOCS_RUNBOOK_ID}, 32)
    assert not chosen.reserved.any()
    assert DOCS_RUNBOOK_ID not in [table.ids[int(i)] for i in chosen.indices]


@pytest.mark.skipif(not _artifact_present(), reason="selector artifact not present (LFS)")
def test_a_narrowed_profile_cannot_seat_a_runbook_outside_its_menu() -> None:
    """Reservation reads the table, and the table is the profile's own catalog.

    A sub-agent carrying an orchestration runbook in its prior active set must
    not have that runbook forced onto a ballot drawn from its narrowed menu —
    the whole point of the narrowed menu is that it cannot execute it.
    """
    from daintree_assistant_server.runbooks.catalog import default_runbooks_dir, load_catalog
    from daintree_assistant_server.runbooks.local.candidates import assemble_candidates
    from daintree_assistant_server.runbooks.local.setwise import SetwiseClassifier

    catalog = load_catalog(default_runbooks_dir())
    clf = SetwiseClassifier(_ARTIFACT)
    narrow = catalog.executable_with(["fs.read", "fs.list"])
    table = clf.sync(narrow)

    outside = sorted(set(catalog.selectable_ids) - set(narrow.selectable_ids))
    assert outside, "fixture must actually narrow the catalog"

    sims = np.zeros(len(table.ids), dtype=np.float32)
    chosen = assemble_candidates(sims, table.ids, set(outside), 32)
    assert not chosen.reserved.any()
    assert {table.ids[int(i)] for i in chosen.indices} <= set(narrow.selectable_ids)


# --------------------------------------------------------------------------
# pool queue-wait instrumentation
# --------------------------------------------------------------------------


def test_queue_wait_measures_time_spent_behind_a_busy_worker() -> None:
    """The metric has to see executor contention, which is the only wait there is.

    Written against the shape that broke the first version: the semaphore holds
    `queue_depth` permits and the pool REFUSES rather than queues once they are
    gone, so `acquire()` never blocks and a timer around it reads zero under
    exactly the load it claims to measure. One worker, two jobs, the first
    holding the thread — the second's wait is real and must be recorded.
    """
    import threading

    async def go() -> None:
        pool = InferencePool(workers=1, queue_depth=4, timeout_seconds=5.0)
        release = threading.Event()

        def blocker() -> str:
            release.wait(timeout=2.0)
            return "first"

        first = asyncio.create_task(pool.run(blocker))
        await asyncio.sleep(0.05)  # let the worker pick the blocker up
        second = asyncio.create_task(pool.run(lambda: "second"))
        await asyncio.sleep(0.05)
        release.set()
        assert await first == "first"
        assert await second == "second"

        stats = pool.stats()
        assert stats.queue_waits == 2
        # The second job waited behind the first; a timer on the semaphore would
        # have reported ~0 here.
        assert stats.queue_wait_ms_max >= 40.0
        assert stats.queue_wait_ms_mean > 0.0
        pool.close()

    asyncio.run(go())


def test_queue_wait_is_zero_when_nothing_is_contending() -> None:
    """The control: without it, a metric that always reported a large number
    would pass the test above."""

    async def go() -> None:
        pool = InferencePool(workers=1, queue_depth=4, timeout_seconds=5.0)
        assert await pool.run(lambda: "x") == "x"
        stats = pool.stats()
        assert stats.queue_waits == 1
        assert stats.queue_wait_ms_max < 40.0
        pool.close()

    asyncio.run(go())


def test_a_rejected_job_never_counts_as_a_queue_wait() -> None:
    """A pool-full rejection never reaches a worker, so it has no wait to record
    — and counting it would dilute the mean with the jobs that waited least."""

    async def go() -> None:
        import threading

        pool = InferencePool(workers=1, queue_depth=1, timeout_seconds=5.0)
        release = threading.Event()
        held = asyncio.create_task(pool.run(lambda: release.wait(timeout=2.0)))
        await asyncio.sleep(0.05)
        assert await pool.run(lambda: "refused") is None
        release.set()
        await held
        assert pool.stats().rejected_full == 1
        assert pool.stats().queue_waits == 1
        pool.close()

    asyncio.run(go())


# ---------------------------------------------------------------------------
# The seam and the production selector must stay the same shape.
# ---------------------------------------------------------------------------


def test_the_scripted_seam_matches_the_production_selector_signature() -> None:
    """`ScriptedSelector` lives in test code; nothing else stops it drifting.

    Orchestration tests drive selection through the scripted seam, so every
    assertion about cadence, active-set resolution, docs pinning and fail-open
    behaviour is written against ITS signature rather than `LocalSelector`'s.
    That was safe while the seam was a second SHIPPED implementation held to the
    same protocol by mypy. It is not safe now: the seam is test scaffolding,
    `mypy --strict` runs over `src` and `tools` only, and `RunbookSelector` is a
    plain `Protocol` — so a keyword added to the real `select()` would leave
    ninety-odd tests passing against a seam that never learned about it.

    Compared as SIGNATURES rather than by `isinstance`: `RunbookSelector` is not
    `runtime_checkable`, and even a runtime protocol check only verifies the
    method NAME exists, which is the half of this that was never in doubt.
    """
    import inspect

    from fakes import ScriptedSelector

    from daintree_assistant_server.runbooks.local.selector import LocalSelector, RunbookSelector

    protocol = inspect.signature(RunbookSelector.select)
    production = inspect.signature(LocalSelector.select)
    seam = inspect.signature(ScriptedSelector.select)

    def shape(sig: inspect.Signature) -> list[tuple[str, str]]:
        # Names and kinds, not annotations: the seam is duck-typed test code and
        # annotating it against the real Catalog/ChatMessage types would import
        # production types into a fake for no benefit. What must not drift is
        # which arguments exist and how they may be passed.
        return [(name, str(p.kind)) for name, p in sig.parameters.items() if name != "self"]

    assert shape(seam) == shape(protocol), (
        "ScriptedSelector.select no longer matches the RunbookSelector protocol. Every "
        "orchestration test drives selection through it, so a mismatch means those "
        "tests are asserting against a selector the orchestrator cannot actually call."
    )
    assert shape(production) == shape(protocol), (
        "LocalSelector.select no longer matches the RunbookSelector protocol it is the "
        "only production implementation of."
    )
