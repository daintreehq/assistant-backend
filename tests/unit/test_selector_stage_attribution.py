"""Which stage lost a required skill — the decomposition, tested without a model.

Every case here runs the REAL ``admit_with_reasons`` over a hand-built ranking
and hands its output to ``_attribute``. That is the whole point of the design:
the diagnosis is produced by the routine that made the decision, so a test that
fabricated a ``rejected`` map would be testing a fiction and would go on passing
after the two came apart.

It is also how the first version was found to be wrong. Attribution was
reconstructed from outside — probability, cap, count — and got two cases
backwards, both pointing at the wrong subsystem: with the count head answering
zero, ``admit`` never evaluates a floor at all, yet the reconstruction blamed
calibration; and a candidate the bonus scan stopped short of at the cap was
blamed on a floor it never reached. Neither showed up in the end-to-end number.
"""

from __future__ import annotations

from benchmarks.skill_selection.cases import BenchCase
from benchmarks.skill_selection.local_runner import STAGES, _attribute
from daintree_assistant_server.skills.local.setwise import (
    SetwiseClassifier,
    admit,
    admit_with_reasons,
)

NEWCOMER_FLOOR = SetwiseClassifier.NEWCOMER_FLOOR_WHEN_ACTIVE
BONUS_FLOOR = SetwiseClassifier.BONUS_FLOOR


def _case(required: list[str], active: list[str] | None = None) -> BenchCase:
    return BenchCase(
        id="c", category="routing-trap", source="t", messages=[],
        active=list(active or []), required=required, optional=[],
        turn_context=None, available_tools=[], note="",
    )


def _stage(
    ranked: list[tuple[str, float]],
    *,
    want: str,
    active: set[str] | None = None,
    count: int = 1,
    max_active: int = 3,
) -> str:
    """Run the real admission, then attribute the loss it produced."""
    active = active or set()
    picked, rejected = admit_with_reasons(ranked, active, count, max_active)
    assert want not in picked, "fixture does not exercise a loss: the skill was admitted"
    loss = _attribute(
        _case([want], sorted(active)), want, ranked=dict(ranked), rejected=rejected
    )
    return loss.stage


# --------------------------------------------------------------------------
# the five stages
# --------------------------------------------------------------------------


def test_a_skill_absent_from_the_ballot_is_a_stage_one_loss() -> None:
    """The distinction the whole suite exists for.

    Absent is not "ranked low". No admission floor and no better head can
    recover a candidate that never reached the head, so this must never be
    reported as a ranking problem.
    """
    loss = _attribute(_case(["want"]), "want", ranked={"other": 0.9}, rejected={})
    assert loss.stage == "candidate_miss"
    assert loss.probability is None


def test_a_scored_but_low_skill_is_a_stage_two_loss() -> None:
    assert _stage([("other", 0.9), ("want", 0.02)], want="want") == "rank_miss"


def test_a_newcomer_below_its_floor_is_a_calibration_loss() -> None:
    """Scored well enough to be reached, and refused on the way past.

    Attributed to the floor rather than to the head, because the head did rank
    it — the earn-your-place rule is what dropped it, and retraining would not
    help.
    """
    stage = _stage(
        [("want", NEWCOMER_FLOOR - 0.01), ("running", 0.5)],
        want="want", active={"running"}, count=1,
    )
    assert stage == "floor_reject"


def test_an_active_skill_is_not_charged_the_newcomer_floor() -> None:
    """The floor applies only to a skill the conversation is NOT already running.

    Charging an active skill to ``floor_reject`` would point every continuation
    failure at calibration when the head is what scored it down.
    """
    stage = _stage(
        [("newcomer", 0.99), ("want", 0.02)],
        want="want", active={"want"}, count=1,
    )
    assert stage == "rank_miss"


def test_a_confident_skill_lost_to_a_zero_count_is_a_cardinality_loss() -> None:
    """The failure the shipped artifact produces on a reserved candidate.

    The skill scores far above the bonus floor and still does not load, because
    the count head called the turn empty — and the bonus never fires on an empty
    answer. Reporting this as ``rank_miss`` would send someone to retrain the
    ranking head over a problem in the count head.
    """
    assert _stage([("want", 0.98)], want="want", count=0) == "cardinality_short"


def test_a_confident_newcomer_lost_to_a_zero_count_is_NOT_a_floor_loss() -> None:
    """The misattribution that reconstruction-from-outside got wrong.

    With ``count == 0`` the admission scan stops before evaluating any floor, so
    the newcomer floor is not what rejected this — the count is. The earlier
    version checked floors first and blamed calibration for a cardinality
    failure, which is a whole subsystem away from the fix.
    """
    stage = _stage(
        [("running", 0.9), ("want", 0.60)],
        want="want", active={"running"}, count=0,
    )
    assert NEWCOMER_FLOOR > 0.60 > BONUS_FLOOR, "fixture must straddle the two floors"
    assert stage == "cardinality_short"


def test_a_confident_skill_lost_to_a_full_answer_is_a_cap_loss() -> None:
    ranked = [("a", 0.99), ("b", 0.98), ("c", 0.97), ("want", 0.96)]
    assert _stage(ranked, want="want", count=3, max_active=3) == "cap_full"


def test_a_capped_out_newcomer_is_NOT_a_floor_loss() -> None:
    """The second misattribution. The bonus scan exits at the cap check, above
    the floor check, so a sub-floor newcomer beyond a full answer was never
    refused by the floor — it was never reached."""
    ranked = [("a", 0.99), ("b", 0.98), ("c", 0.97), ("want", NEWCOMER_FLOOR - 0.01)]
    stage = _stage(ranked, want="want", active={"a"}, count=3, max_active=3)
    assert stage == "cap_full"


def test_a_below_floor_skill_on_an_empty_answer_is_still_a_rank_miss() -> None:
    """Cardinality is only to blame when the skill would otherwise have ridden.

    Below the bonus floor the count head's answer is irrelevant — the bonus
    would not have admitted it at any count, so the score is the problem.
    """
    assert _stage([("want", BONUS_FLOOR - 0.01)], want="want", count=0) == "rank_miss"


# --------------------------------------------------------------------------
# invariants
# --------------------------------------------------------------------------


def test_every_stage_the_attributor_can_report_is_a_declared_one() -> None:
    """``STAGES`` drives the histogram and the printed report, so a stage the
    attributor can emit but the tuple does not name is dropped from both."""
    emitted = {
        _attribute(_case(["w"]), "w", ranked={"o": 1.0}, rejected={}).stage,
        _stage([("o", 0.9), ("w", 0.02)], want="w"),
        _stage([("w", NEWCOMER_FLOOR - 0.01), ("r", 0.5)], want="w", active={"r"}),
        _stage([("w", 0.98)], want="w", count=0),
        _stage([("a", 0.99), ("b", 0.98), ("c", 0.97), ("w", 0.96)], want="w", count=3),
    }
    # `background_reuse` is emitted by the runner for a turn that never reaches
    # the model at all, not by `_attribute`.
    assert emitted == set(STAGES) - {"background_reuse"}


def test_reporting_reasons_did_not_change_what_admit_admits() -> None:
    """``admit_with_reasons`` replaced ``admit``'s early breaks with full scans.

    That is only safe because neither loop can append after the condition it
    used to break on — once the count is met the first loop cannot grow, and the
    ranking is descending so every score after the first sub-floor one is also
    sub-floor. Asserted rather than reasoned about, across the shapes where the
    two loops interact.
    """
    fixtures: list[tuple[list[tuple[str, float]], set[str], int, int]] = [
        ([("a", 0.9), ("b", 0.8), ("c", 0.2)], set(), 1, 3),
        ([("a", 0.9), ("b", 0.8), ("c", 0.7), ("d", 0.6)], set(), 2, 3),
        ([("a", 0.9), ("b", BONUS_FLOOR - 0.01), ("c", 0.9)], set(), 1, 3),
        ([("new", 0.60), ("run", 0.55)], {"run"}, 1, 3),
        ([("run", 0.4), ("new", 0.99)], {"run"}, 0, 3),
        ([("a", 0.99), ("b", 0.98), ("c", 0.97), ("d", 0.96)], {"a"}, 3, 3),
        ([], set(), 2, 3),
    ]
    for ranked, active, count, cap in fixtures:
        assert admit(ranked, active, count, cap) == admit_with_reasons(
            ranked, active, count, cap
        )[0]


def test_a_rejected_reason_is_recorded_for_every_candidate_not_admitted() -> None:
    """The runner reconciles stages against the miss count, so a candidate with
    no reason at all would silently fall through to the default branch."""
    ranked = [("a", 0.9), ("b", 0.44), ("c", 0.2), ("d", 0.1)]
    picked, rejected = admit_with_reasons(ranked, set(), 1, 3)
    assert set(rejected) | set(picked) == {sid for sid, _ in ranked}
    assert not set(picked) & set(rejected)
