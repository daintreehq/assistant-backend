"""What reaches the set-wise head, and what that guarantee does and does not mean.

``assemble_candidates`` is pure numpy over a similarity row, so all of this runs
without the ONNX artifacts and inside the fast unit sweep. The end-to-end
counterparts — a reserved candidate scored by the real head, and still evicted on
a real topic shift — live beside the other artifact-gated tests in
``test_local_selection.py``.

**The similarity fixture is deliberately non-monotonic.** An earlier version of
this file used ``linspace`` over ascending ids, which made "highest cosine" and
"highest id" the same ordering: an implementation that ranked by id would have
passed nearly every assertion here. ``_sims`` scrambles the two apart, and the
expected orderings are derived by an independent plain-Python sort rather than by
re-running the code under test.
"""

from __future__ import annotations

import numpy as np
import pytest

from daintree_assistant_server.runbooks.local.candidates import assemble_candidates


def _ids(n: int = 50) -> list[str]:
    return [f"runbook.{i:03d}" for i in range(n)]


def _sims(n: int = 50) -> np.ndarray:
    """A fixed, arbitrary similarity row. Id order and score order disagree."""
    rng = np.random.default_rng(20260821)
    return rng.permutation(np.linspace(-0.3, 0.95, n)).astype(np.float32)


def _oracle(sims: np.ndarray, k: int) -> list[int]:
    """The intended ordering, by a different implementation to the one tested.

    A plain Python sort on the same total key the assembler claims: descending
    cosine, ties broken by table position. Deriving expectations from
    ``np.argsort`` instead would be checking numpy against numpy.
    """
    return sorted(range(len(sims)), key=lambda i: (-float(sims[i]), i))[:k]


def _picked(chosen, ids: list[str]) -> list[str]:
    return [ids[int(i)] for i in chosen.indices]


# --------------------------------------------------------------------------
# the shortlist, unchanged
# --------------------------------------------------------------------------


def test_without_actives_it_is_the_plain_cosine_shortlist() -> None:
    """The no-active path must still be the policy this replaced."""
    ids, sims = _ids(), _sims()
    chosen = assemble_candidates(sims, ids, set(), 32)

    assert len(chosen) == 32
    assert _picked(chosen, ids) == [ids[i] for i in _oracle(sims, 32)]
    assert not chosen.reserved.any()
    assert chosen.rank_norm[0] == 0.0
    assert chosen.rank_norm[31] == 1.0


def test_a_catalog_smaller_than_k_is_fully_ranked_not_merely_included() -> None:
    """The whole-catalog branch still has to ORDER what it returns.

    Membership is trivially right when everything fits; the rank feature is what
    can silently be left in table order.
    """
    ids, sims = _ids(6), _sims(6)
    chosen = assemble_candidates(sims, ids, {ids[0]}, 32)

    assert _picked(chosen, ids) == [ids[i] for i in _oracle(sims, 6)]
    # Nothing was displaced, so nothing is flagged reserved even if an active
    # runbook sits at the bottom of the ranking.
    assert not chosen.reserved.any()


def test_rank_norm_is_scaled_by_k_not_by_the_candidate_count() -> None:
    """A short ballot must not stretch its ranks across the full 0..1 range.

    ``k`` is the head's slot count and the scale it trained against. Dividing by
    the number of candidates actually found would make rank 5 of 6 read as 1.0 —
    "retrieval's worst" — when it is nowhere near the weakest slot the head knows.
    """
    chosen = assemble_candidates(_sims(6), _ids(6), set(), 32)
    assert chosen.rank_norm[5] == np.float32(5.0 / 31.0)


# --------------------------------------------------------------------------
# reservation
# --------------------------------------------------------------------------


def test_an_active_runbook_below_the_shortlist_is_seated_anyway() -> None:
    """The defect this module exists for: retrieval misses the open runbook.

    The chosen runbook is the catalog's WORST match — the shape of a vague
    follow-up ("how's that going?") whose words sit nowhere near the runbook
    driving it — and must still reach the head.
    """
    ids, sims = _ids(), _sims()
    worst = ids[_oracle(sims, len(ids))[-1]]

    assert worst not in _picked(assemble_candidates(sims, ids, set(), 32), ids)

    chosen = assemble_candidates(sims, ids, {worst}, 32)
    assert worst in _picked(chosen, ids)
    assert len(chosen) == 32


def test_the_reserved_candidate_keeps_its_real_cosine_and_the_weakest_rank() -> None:
    ids, sims = _ids(), _sims()
    order = _oracle(sims, len(ids))
    # Not the catalog's worst, and deliberately NOT zero: a bug that zero-fills a
    # reserved candidate's cosine would pass against a runbook whose true score
    # happened to be 0.0.
    row = order[40]
    seated = ids[row]
    assert sims[row] != 0.0

    chosen = assemble_candidates(sims, ids, {seated}, 32)
    slot = _picked(chosen, ids).index(seated)

    assert chosen.reserved[slot]
    # Its ACTUAL similarity, not a flattering stand-in: the head is told
    # retrieval scored this one poorly.
    assert chosen.cosine[slot] == sims[row]
    # And the weakest rank the head has ever seen, rather than its true rank of
    # 40, which at this k would render as 1.29 — off the trained scale.
    assert chosen.rank_norm[slot] == 1.0


def test_reservation_displaces_the_weakest_retrieved_candidate() -> None:
    ids, sims = _ids(), _sims()
    order = _oracle(sims, len(ids))
    seated = ids[order[40]]
    # Named explicitly from the oracle rather than read back as "whatever the
    # baseline put last", which a wrong ranking policy would satisfy trivially.
    evicted = ids[order[31]]

    baseline = set(_picked(assemble_candidates(sims, ids, set(), 32), ids))
    chosen = set(_picked(assemble_candidates(sims, ids, {seated}, 32), ids))

    assert baseline - chosen == {evicted}
    assert chosen - baseline == {seated}


def test_an_active_runbook_already_retrieved_is_not_flagged_reserved() -> None:
    ids, sims = _ids(), _sims()
    best = ids[_oracle(sims, 1)[0]]
    chosen = assemble_candidates(sims, ids, {best}, 32)

    assert not chosen.reserved.any()
    assert len(chosen) == 32


def test_several_absent_actives_are_all_seated() -> None:
    ids, sims = _ids(), _sims()
    order = _oracle(sims, len(ids))
    active = {ids[order[35]], ids[order[42]], ids[order[49]]}

    chosen = assemble_candidates(sims, ids, active, 32)

    assert active <= set(_picked(chosen, ids))
    assert int(chosen.reserved.sum()) == 3
    assert len(chosen) == 32


def test_reservation_never_evicts_another_active_runbook() -> None:
    """Seating one open runbook must not unseat a different open runbook.

    The weakest RETRIEVED candidate is itself active here, so the displacement
    scan has to step over it and take the next one up instead.
    """
    ids, sims = _ids(), _sims()
    order = _oracle(sims, len(ids))
    absent, weakest_retrieved, next_up = ids[order[45]], ids[order[31]], ids[order[30]]

    chosen = set(_picked(assemble_candidates(sims, ids, {absent, weakest_retrieved}, 32), ids))

    assert absent in chosen
    assert weakest_retrieved in chosen
    assert next_up not in chosen


def test_when_every_retrieved_slot_is_active_nothing_is_displaced() -> None:
    """The degenerate end of the never-evict-an-active rule.

    With no displaceable slot left, an absent active simply does not fit. The
    alternative — evicting one open runbook to seat another — is the bug this
    module exists to remove, so not fitting is the correct outcome.
    """
    ids, sims = _ids(10), _sims(10)
    order = _oracle(sims, 10)
    retrieved_all_active = {ids[i] for i in order[:4]}
    absent = ids[order[9]]

    chosen = assemble_candidates(sims, ids, retrieved_all_active | {absent}, 4)

    assert set(_picked(chosen, ids)) == retrieved_all_active
    assert not chosen.reserved.any()


def test_when_slots_run_out_the_nearest_actives_win() -> None:
    """Contract, not a reachable state: totality when actives exceed the ballot.

    ``max_active_runbooks`` has no hard upper bound in ``config.py``, so this is
    the documented behaviour if it were ever raised past ``k`` — the actives the
    query is nearest are seated, by cosine and not by iteration order.
    """
    ids, sims = _ids(10), _sims(10)
    order = _oracle(sims, 10)
    active = {ids[order[7]], ids[order[8]], ids[order[9]]}

    chosen = _picked(assemble_candidates(sims, ids, active, 2), ids)

    assert set(chosen) == {ids[order[7]], ids[order[8]]}


def test_an_active_id_absent_from_the_table_is_ignored() -> None:
    """The pinned docs runbook is hidden from selection and has no table row.

    It can legitimately appear in the prior active set, so reservation must
    treat an unknown id as nothing to seat rather than as a lookup to force.
    """
    ids, sims = _ids(), _sims()
    chosen = assemble_candidates(sims, ids, {"daintree.docs.runbook"}, 32)

    assert not chosen.reserved.any()
    assert _picked(chosen, ids) == [ids[i] for i in _oracle(sims, 32)]


def test_active_may_be_any_container_not_only_a_set() -> None:
    """The parameter is typed ``Container[str]``; serving passes a set, and a
    caller passing a list must get the same ballot."""
    ids, sims = _ids(), _sims()
    seated = ids[_oracle(sims, len(ids))[45]]

    as_set = _picked(assemble_candidates(sims, ids, {seated}, 32), ids)
    as_list = _picked(assemble_candidates(sims, ids, [seated], 32), ids)

    assert as_set == as_list


def test_an_empty_table_returns_an_empty_ballot() -> None:
    chosen = assemble_candidates(np.zeros(0, dtype=np.float32), [], {"a"}, 32)
    assert len(chosen) == 0
    assert chosen.cosine.shape == chosen.rank_norm.shape == (0,)


@pytest.mark.parametrize("k", [0, -1])
def test_a_non_positive_k_returns_an_empty_ballot(k: int) -> None:
    chosen = assemble_candidates(_sims(), _ids(), set(), k)
    assert len(chosen) == 0


# --------------------------------------------------------------------------
# malformed input fails OPEN, rather than answering authoritatively
# --------------------------------------------------------------------------


def test_a_similarity_row_that_does_not_match_the_catalog_raises() -> None:
    """A wiring bug must not silently rank the wrong runbooks.

    ``InferencePool.run`` turns this into a degrade-open reuse of the prior
    active set. Returning a ballot built from a mismatched row instead would come
    back as a SUCCESSFUL selection — and a successful selection replaces the
    whole active set.
    """
    with pytest.raises(ValueError, match="one score per"):
        assemble_candidates(_sims(10), _ids(50), set(), 32)


def test_a_two_dimensional_similarity_row_raises() -> None:
    with pytest.raises(ValueError, match="one score per"):
        assemble_candidates(_sims(50).reshape(5, 10), _ids(50), set(), 32)


@pytest.mark.parametrize("bad", [np.nan, np.inf, -np.inf])
def test_a_non_finite_similarity_raises_rather_than_reaching_the_head(bad: float) -> None:
    """NaN is the dangerous one: it does not raise anywhere downstream.

    It ranks arbitrarily, propagates through the head as a garbage probability,
    and returns as a confident answer. Refusing here converts it into the
    fail-open the selector already has.
    """
    sims = _sims().copy()
    sims[7] = bad
    with pytest.raises(ValueError, match="not finite"):
        assemble_candidates(sims, _ids(), set(), 32)


# --------------------------------------------------------------------------
# invariants the head depends on
# --------------------------------------------------------------------------


def test_every_array_stays_parallel_and_correctly_typed() -> None:
    ids, sims = _ids(), _sims()
    seated = ids[_oracle(sims, len(ids))[45]]
    chosen = assemble_candidates(sims, ids, {seated}, 32)

    assert chosen.indices.shape == chosen.cosine.shape == chosen.rank_norm.shape
    assert chosen.reserved.shape == chosen.indices.shape
    # ``indices`` is used to index the ranking table and ``reserved`` to filter
    # it; a float or object dtype would fail far from here.
    assert np.issubdtype(chosen.indices.dtype, np.integer)
    assert chosen.cosine.dtype == np.float32
    assert chosen.rank_norm.dtype == np.float32
    assert chosen.reserved.dtype == np.bool_
    # Every reported cosine is the table's own value for that row — the feature
    # cannot drift from the candidate it describes.
    for slot, row in enumerate(chosen.indices):
        assert chosen.cosine[slot] == sims[int(row)]


def test_cosine_and_rank_norm_are_not_the_same_array() -> None:
    """They are written independently at serving; aliasing would couple them."""
    chosen = assemble_candidates(np.zeros(0, dtype=np.float32), [], set(), 32)
    assert chosen.cosine is not chosen.rank_norm


def test_the_ballot_never_repeats_a_runbook() -> None:
    """A duplicated row would give one runbook two votes in the set-wise pass."""
    ids, sims = _ids(), _sims()
    order = _oracle(sims, len(ids))
    active = {ids[order[0]], ids[order[31]], ids[order[47]]}

    picked = _picked(assemble_candidates(sims, ids, active, 32), ids)
    assert len(picked) == len(set(picked))


def test_tied_scores_are_broken_by_table_position() -> None:
    """The tie-break has to be a TOTAL order, not merely a repeatable one.

    numpy's default sort is unstable and may order equal keys differently
    between versions and architectures. The trainer and the server would then
    hand the same two tied runbooks different ``rank_norm`` values — a train/serve
    skew with no error attached. Table position is id order, so it is stable
    across processes; asserted against that rather than against "two calls in
    one process agreed", which unstable sorting also satisfies.
    """
    ids = _ids(40)
    sims = np.full(40, 0.5, dtype=np.float32)
    picked = _picked(assemble_candidates(sims, ids, set(), 32), ids)

    assert picked == ids[:32]
