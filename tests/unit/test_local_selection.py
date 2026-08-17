"""Unit tests for local (in-process) skill selection.

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

import pytest

from daintree_assistant_server.skills.local.classifier import SkillClassifier, admit
from daintree_assistant_server.skills.local.documents import (
    query_document,
    skill_document,
    split_when_to_use,
)
from daintree_assistant_server.skills.local.runtime import InferencePool


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
    meta = _Meta("x", "Title", "Summary.", "Use for A. Do not use for B — use the B skill.")
    assert "Do not use" not in skill_document(meta, variant="short")
    assert "Do not use" not in skill_document(meta, variant="trigger")
    assert "Do not use" in skill_document(meta, variant="full")


def test_unknown_document_variant_raises() -> None:
    with pytest.raises(ValueError, match="unknown skill document variant"):
        skill_document(_Meta("x", "T", "S", "W"), variant="nope")


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
    """The bonus never turns an empty answer into a loaded skill —
    polluting a no-task turn is how loading noise becomes the norm."""
    ranked = [("a", 0.9), ("b", 0.6)]
    assert admit(ranked, set(), 0, 3) == []


def test_admit_rejected_newcomer_does_not_consume_the_slot() -> None:
    """Cardinality says one; rank 1 is a filtered newcomer; rank 2 is the
    active skill. The scan must continue to rank 2 instead of returning an
    authoritative empty set that evicts a live workflow."""
    ranked = [("newcomer", 0.60), ("running", 0.55)]
    assert admit(ranked, {"running"}, 1, 3) == ["running"]


def test_admit_bonus_rides_above_the_floor_up_to_the_cap() -> None:
    """Every candidate here clears the bonus floor, so the CAP is what stops the
    scan — not the floor. The earlier fixture used 0.51 for the fourth entry,
    which fell below the floor once it moved to 0.55: the assertion still passed
    but for the wrong reason, and a removed cap check would not have failed it.
    """
    ranked = [("a", 0.90), ("b", 0.80), ("c", 0.70), ("d", 0.60)]
    # cardinality said one; all four clear the floor, so only the cap of 3 binds
    assert admit(ranked, set(), 1, 3) == ["a", "b", "c"]


def test_admit_bonus_floor_excludes_the_band_below_it() -> None:
    """Pins the FLOOR'S VALUE, not just the rule.

    Every other test here is written relative to the constant, so reverting
    BONUS_FLOOR to 0.50 kept the whole suite green while silently restoring the
    false positive the move exists to stop: "create a new claude code terminal"
    loading create-plain-worktree at 0.546 alongside a correct, cardinality-1
    answer. This is the regression that notices.
    """
    assert admit([("correct", 0.91), ("marginal", 0.546)], set(), 1, 3) == ["correct"]
    # ...and the floor still admits what it is meant to admit
    assert admit([("correct", 0.91), ("worth_it", 0.56)], set(), 1, 3) == [
        "correct",
        "worth_it",
    ]


def test_admit_bonus_stops_at_the_first_below_floor_score() -> None:
    ranked = [("a", 0.9), ("b", 0.45), ("c", 0.44)]
    assert admit(ranked, set(), 1, 3) == ["a"]


def test_admit_bonus_does_not_readmit_an_active_skill_below_the_floor() -> None:
    """The bonus floor applies to EVERY candidate, active ones included.

    The rule the code comment calls load-bearing: the bonus exists to rescue a
    near-miss RANKING, not to re-admit a skill the model scored below a coin flip
    just because it used to be loaded. The sibling below-floor test drives this
    with nothing active, so it would still pass a refactor that exempted active
    skills from the floor — which is exactly the drift this pins.
    """
    ranked = [("newcomer", 0.9), ("stale", 0.45)]
    assert admit(ranked, {"stale"}, 1, 3) == ["newcomer"]


def test_admit_floors_are_inclusive_at_their_exact_boundary() -> None:
    """Both floors are `>=` comparisons, so a score landing exactly on one is
    admitted.

    Driven off the constants rather than literals: both floors are tuned values
    (BONUS_FLOOR moved 0.50 -> 0.55 to stop sub-0.55 noise riding along), and a
    boundary test written with a hardcoded number stops testing the boundary the
    moment one moves — it just becomes another mid-band case.
    """
    newcomer_floor = SkillClassifier.NEWCOMER_FLOOR_WHEN_ACTIVE
    bonus_floor = SkillClassifier.BONUS_FLOOR
    # a newcomer exactly ON the newcomer floor, with a workflow active
    assert admit([("running", 0.9), ("newcomer", newcomer_floor)], {"running"}, 2, 3) == [
        "running",
        "newcomer",
    ]
    # a bonus candidate exactly ON the bonus floor
    assert admit([("a", 0.9), ("b", bonus_floor)], set(), 1, 3) == ["a", "b"]


def test_admit_bonus_newcomer_still_owes_the_higher_floor() -> None:
    """With a workflow active, a 0.60 newcomer clears the bonus floor but not
    the newcomer floor — it stays out; a 0.58 ACTIVE skill rides."""
    ranked = [("running", 0.9), ("newcomer", 0.60), ("also_running", 0.58)]
    got = admit(ranked, {"running", "also_running"}, 1, 3)
    assert got == ["running", "also_running"]


def test_admit_respects_max_active_over_count() -> None:
    ranked = [("a", 0.9), ("b", 0.8), ("c", 0.7), ("d", 0.6)]
    assert admit(ranked, set(), 3, 2) == ["a", "b"]


# --------------------------------------------------------------------------
# LocalSelector — the serving contract
# --------------------------------------------------------------------------


class _StubClassifier:
    """Duck-typed SkillClassifier: fixed answer, recorded inputs."""

    def __init__(self, revision: str | None = None, skill_ids: list[str] | None = None,
                 docs: bool = False, fail: bool = False) -> None:
        # None -> "match whatever catalog the selector holds"; the fixture fills
        # it in with the real routing hash of the stub catalog.
        self.routing_revision = revision or ""
        self._ids = skill_ids if skill_ids is not None else ["daintree.terminals.rename"]
        self._docs = docs
        self._fail = fail
        self.calls: list[tuple[str, list[str]]] = []

    def select(self, query: str, active_ids=(), *, max_active: int = 3,
               docs_threshold: float = 0.5):
        if self._fail:
            raise RuntimeError("bad graph")
        self.calls.append((query, list(active_ids)))
        from daintree_assistant_server.skills.local.classifier import Selection

        return Selection(skill_ids=list(self._ids), docs_wanted=self._docs,
                         cardinality=len(self._ids),
                         scores=dict.fromkeys(self._ids, 0.9))


class _StubSkill:
    def __init__(self, sid: str) -> None:
        self.meta = _Meta(sid, f"Title {sid}", "Summary.", "Use when X.")


class _StubCatalog:
    def __init__(self, selectable: set[str]) -> None:
        self.selectable_ids = selectable
        self.skills = [_StubSkill(sid) for sid in sorted(selectable)]


class _StubManager:
    def __init__(self, catalog: _StubCatalog) -> None:
        self.catalog = catalog


class _StubSettings:
    max_active_skills = 3


def _local_selector(classifier: _StubClassifier, *, stale: bool = False):
    from daintree_assistant_server.skills.local.documents import routing_revision
    from daintree_assistant_server.skills.local.selector import LocalSelector

    catalog = _StubCatalog({"daintree.terminals.rename", "daintree.foundation"})
    if not classifier.routing_revision:
        classifier.routing_revision = (
            "routing:STALE" if stale else routing_revision(catalog)
        )
    pool = InferencePool(workers=1, queue_depth=2, timeout_seconds=5.0)
    return LocalSelector(classifier, pool, _StubManager(catalog), _StubSettings()), pool  # type: ignore[arg-type]


def test_local_selector_returns_the_classifier_answer() -> None:
    async def go() -> None:
        clf = _StubClassifier()
        sel, pool = _local_selector(clf)
        decision = await sel.select([_Msg("user", "rename my terminals")], [])
        assert decision.skill_ids == ["daintree.terminals.rename"]
        assert decision.ran and decision.authoritative and not decision.degraded
        assert decision.endpoint == "local"
        pool.close()

    asyncio.run(go())


def test_local_selector_degrades_open_on_a_stale_head() -> None:
    """A head trained against a different catalog must never serve its answer.

    Its skill rows point at documents that no longer exist, and the failure
    would otherwise be silent and confident — the worst kind.
    """

    async def go() -> None:
        clf = _StubClassifier()
        sel, pool = _local_selector(clf, stale=True)
        decision = await sel.select([_Msg("user", "rename my terminals")], ["prior.skill"])
        assert decision.degraded
        assert decision.warnings == ["local_skill_selector_stale_catalog"]
        assert clf.calls == [], "a stale head must not be consulted at all"
        pool.close()

    asyncio.run(go())


def test_local_selector_fails_open_when_the_model_raises() -> None:
    async def go() -> None:
        clf = _StubClassifier(fail=True)
        sel, pool = _local_selector(clf)
        decision = await sel.select([_Msg("user", "rename my terminals")], [])
        assert decision.degraded
        assert decision.warnings == ["local_skill_selector_failed_open"]
        pool.close()

    asyncio.run(go())


def test_local_selector_background_wake_keeps_the_active_set() -> None:
    async def go() -> None:
        clf = _StubClassifier()
        sel, pool = _local_selector(clf)
        convo = [_Msg("user", "watch it"), _Msg("assistant", "watcher armed, ending turn")]
        decision = await sel.select(convo, ["daintree.foundation"])
        assert decision.skill_ids == ["daintree.foundation"]
        assert decision.task_type == "background_wake"
        assert clf.calls == [], "a wake has no new instruction to classify"
        pool.close()

    asyncio.run(go())


def test_local_selector_filters_ids_the_live_catalog_does_not_have() -> None:
    async def go() -> None:
        clf = _StubClassifier(skill_ids=["daintree.terminals.rename", "skill.ghost"])
        sel, pool = _local_selector(clf)
        decision = await sel.select([_Msg("user", "rename")], [])
        assert decision.skill_ids == ["daintree.terminals.rename"]
        pool.close()

    asyncio.run(go())


def test_local_selector_degrades_when_no_named_id_resolves() -> None:
    """Named-but-unresolvable is a mismatch, not an authoritative clear."""

    async def go() -> None:
        clf = _StubClassifier(skill_ids=["skill.ghost", "skill.phantom"])
        sel, pool = _local_selector(clf)
        decision = await sel.select([_Msg("user", "rename")], ["daintree.foundation"])
        assert decision.degraded
        assert decision.warnings == ["local_skill_selector_unknown_ids"]
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
# SkillClassifier — against the real shipped artifact (skips without LFS)
# --------------------------------------------------------------------------

_ARTIFACT = (
    Path(__file__).resolve().parents[2]
    / "src/daintree_assistant_server/skills/local/artifacts/classifier"
)


def _artifact_present() -> bool:
    """True when the int8 graph is real bytes, not an unfetched LFS pointer."""
    graph = _ARTIFACT / "model_int8.onnx"
    return graph.is_file() and graph.stat().st_size > 1_000_000


@pytest.mark.skipif(not _artifact_present(), reason="classifier artifact not present (LFS)")
def test_shipped_classifier_selects_a_plausible_skill() -> None:
    from daintree_assistant_server.skills.local.classifier import SkillClassifier

    clf = SkillClassifier(_ARTIFACT)
    sel = clf.select("rename my terminals so i can tell the agents apart", [])
    assert "daintree.terminals.rename" in sel.skill_ids
    assert 0 < sel.cardinality <= 3


@pytest.mark.skipif(not _artifact_present(), reason="classifier artifact not present (LFS)")
def test_shipped_classifier_returns_empty_on_chatter() -> None:
    from daintree_assistant_server.skills.local.classifier import SkillClassifier

    clf = SkillClassifier(_ARTIFACT)
    assert clf.select("thanks, that's perfect", []).skill_ids == []


@pytest.mark.skipif(not _artifact_present(), reason="classifier artifact not present (LFS)")
def test_shipped_classifier_head_matches_the_live_catalog() -> None:
    """Every label row must be a skill the live catalog can actually load —
    and the recorded revision must match, or the retrain runbook is overdue."""
    from daintree_assistant_server.skills.catalog import default_skills_dir, load_catalog
    from daintree_assistant_server.skills.local.classifier import SkillClassifier
    from daintree_assistant_server.skills.local.documents import routing_revision

    catalog = load_catalog(default_skills_dir())
    clf = SkillClassifier(_ARTIFACT)
    assert set(clf.labels) == set(catalog.selectable_ids)
    assert clf.routing_revision == routing_revision(catalog)


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
