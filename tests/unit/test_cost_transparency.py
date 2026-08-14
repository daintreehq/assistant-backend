"""Cost reporting — the tester pays this bill.

Every upstream call is funded by the caller's own OpenRouter key, so what a turn
costs is their money, not a hosting expense we absorb. That makes the figure a
product surface rather than an internal metric, and makes two failure modes
unacceptable: reporting a cost we did not observe, and reporting zero for one we
could not observe.

The wire fact these tests rest on was verified against the live API on
2026-08-14: OpenRouter returns `cost` in the usage object unconditionally — the
`usage: {include: true}` opt-in is deprecated and has no effect — on both the
non-streaming body and the final chunk of a stream.
"""

from __future__ import annotations

import pytest

from daintree_assistant_server.providers.base import ProviderUsage
from daintree_assistant_server.services.turn_accounting import (
    response_usage,
    sum_usage,
    turn_cost,
    turn_cost_with_speculation,
)

# Captured verbatim from a live deepseek/deepseek-v4-flash-0731 call.
LIVE_USAGE = {
    "prompt_tokens": 6,
    "completion_tokens": 5,
    "total_tokens": 11,
    "cost": 1.739976e-06,
    "is_byok": False,
    "prompt_tokens_details": {
        "cached_tokens": 0,
        "cache_write_tokens": 0,
        "audio_tokens": 0,
        "video_tokens": 0,
    },
    "cost_details": {
        "upstream_inference_cost": 1.739976e-06,
        "upstream_inference_prompt_cost": 4.79976e-07,
        "upstream_inference_completions_cost": 1.26e-06,
    },
}


def test_cost_is_parsed_from_a_real_usage_payload() -> None:
    usage = ProviderUsage.from_wire(LIVE_USAGE)
    assert usage.cost == 1.739976e-06
    assert usage.prompt_tokens == 6
    assert usage.cached_tokens == 0


def test_an_unreported_cost_stays_none_rather_than_zero() -> None:
    """Zero is a claim; None is the truth.

    A provider that omits cost has told us nothing. Reporting 0.0 would tell the
    tester the turn was free, which is the one wrong answer that looks right.
    """
    usage = ProviderUsage.from_wire({"prompt_tokens": 6, "completion_tokens": 5})
    assert usage.cost is None
    assert response_usage(usage).cost is None


def test_a_non_numeric_cost_does_not_break_the_response() -> None:
    """The usage record is on the response path; a malformed field must degrade."""
    assert ProviderUsage.from_wire({"cost": "1.23"}).cost is None
    assert ProviderUsage.from_wire({"cost": None}).cost is None


def test_a_discarded_attempt_is_still_billed_to_the_caller() -> None:
    """A re-rolled round charged the caller twice; reporting once under-bills.

    Same reason the token counts are summed rather than replaced — the caller's
    key funded the generation we threw away.
    """
    summed = sum_usage(
        ProviderUsage(prompt_tokens=100, cost=0.002),
        ProviderUsage(prompt_tokens=120, cost=0.003),
    )
    assert summed.cost == 0.005
    assert summed.prompt_tokens == 220


def test_summing_preserves_not_reported() -> None:
    """`None` must survive when NEITHER side reported, and defer when one did."""
    assert sum_usage(ProviderUsage(), ProviderUsage()).cost is None
    assert sum_usage(ProviderUsage(cost=0.002), ProviderUsage()).cost == 0.002
    assert sum_usage(ProviderUsage(), ProviderUsage(cost=0.003)).cost == 0.003


def test_the_response_usage_carries_cost_through() -> None:
    out = response_usage(ProviderUsage(prompt_tokens=10, cached_tokens=8, cost=0.004))
    assert out.cost == 0.004
    assert out.cached_tokens == 8


# ---------------------------------------------------------------------------
# Partial totals. A sum missing a component must not be labelled complete.
# ---------------------------------------------------------------------------


def _meta(selector_usage=None):  # type: ignore[no-untyped-def]
    from daintree_assistant_server.contracts.daintree_api import SkillsBlock
    from daintree_assistant_server.contracts.extensions import SelectorMeta
    from daintree_assistant_server.services.chat_orchestrator import _RespondMeta

    return _RespondMeta(
        request_id="r",
        state=None,
        catalog_revision="c",
        prompt_version="p",
        skills=SkillsBlock(selector=SelectorMeta(usage=selector_usage)),
        warnings=[],
    )


def test_a_selector_that_ran_without_reporting_cost_makes_the_total_incomplete() -> None:
    """The failure the whole 'absent means unknown' rule exists to prevent.

    A selector call that happened but reported no cost would otherwise
    contribute 0.0 to a field named `total` — a partial sum presented as a
    complete one, which is the same lie as reporting zero for an unknown, just
    one level up.
    """
    from daintree_assistant_server.contracts.extensions import SelectorUsage

    cost = turn_cost(
        ProviderUsage(cost=0.002), _meta(SelectorUsage(prompt_tokens=3000, cost=None))
    )
    assert cost is not None
    assert cost.total == 0.002
    assert cost.complete is False, "a partial sum must not be labelled complete"


def test_a_turn_that_skipped_selection_is_still_complete() -> None:
    """No selector usage at all means no selector CALL — a truthful zero.

    The no-task fast path and continuation rounds skip selection entirely. Their
    missing selector cost is not a gap and must not degrade the turn's total.
    """
    cost = turn_cost(ProviderUsage(cost=0.002), _meta(None))
    assert cost is not None
    assert cost.total == 0.002
    assert cost.complete is True


def test_both_components_known_is_complete() -> None:
    from daintree_assistant_server.contracts.extensions import SelectorUsage

    cost = turn_cost(
        ProviderUsage(cost=0.002), _meta(SelectorUsage(cost=0.0004))
    )
    assert cost is not None
    assert cost.total == pytest.approx(0.0024)
    assert cost.complete is True


def test_a_selector_repair_call_is_billed_into_the_selector_cost() -> None:
    """A malformed selector response costs TWICE — the attempt and the repair.

    The selector has its own `_sum_usage`; it summed tokens but dropped cost, so
    a repaired selection reported less than the caller paid.
    """
    from daintree_assistant_server.skills.selector import _sum_usage

    summed = _sum_usage(ProviderUsage(cost=0.0004), ProviderUsage(cost=0.0005))
    assert summed is not None
    assert summed.cost == pytest.approx(0.0009)


def test_a_task_repair_call_is_billed_into_the_task_cost() -> None:
    """Same defect, same fix, in the task runner's copy of the helper."""
    from daintree_assistant_server.services.task_runner import _sum_usage

    assert _sum_usage(
        ProviderUsage(cost=0.0002), ProviderUsage(cost=0.0003)
    ).cost == pytest.approx(0.0005)


# ---------------------------------------------------------------------------
# Malformed provider values must never become a claim of "free".
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("raw", [False, True])
def test_a_boolean_cost_is_rejected_not_read_as_zero(raw: bool) -> None:
    """`bool` subclasses `int`, so a naive numeric check turns `false` into 0.0.

    That is the single worst wrong value: it reports "this call was free" with
    full confidence, on the one field that is about someone's money.
    """
    assert ProviderUsage.from_wire({"cost": raw}).cost is None


@pytest.mark.parametrize("raw", [float("nan"), float("inf"), float("-inf")])
def test_a_non_finite_cost_is_rejected(raw: float) -> None:
    """NaN and Infinity are not valid JSON.

    Beyond being meaningless as money, letting one through produces a response
    body that a strict client cannot parse — turning a cosmetic provider quirk
    into a failed turn.
    """
    assert ProviderUsage.from_wire({"cost": raw}).cost is None


def test_a_losing_speculation_is_billed_into_the_turn() -> None:
    """A speculative generation the user never saw was still charged to them.

    When it finished before losing, its cost is known and belongs in the total.
    """
    from daintree_assistant_server.contracts.extensions import SelectorUsage

    cost = turn_cost_with_speculation(
        ProviderUsage(cost=0.002),
        _meta(SelectorUsage(cost=0.0004)),
        discarded_usage=ProviderUsage(cost=0.001),
        speculation_billed=True,
    )
    assert cost is not None
    assert cost.total == pytest.approx(0.0034)
    assert cost.complete is True


def test_a_speculation_cancelled_before_reporting_makes_the_total_a_floor() -> None:
    """Real spend that nothing can measure.

    OpenRouter reports usage only in a stream's final chunk, which a cancelled
    stream never sends. The turn is still billed, so the honest report is a
    total that admits it is incomplete — not one that pretends the discarded
    generation was free.
    """
    cost = turn_cost_with_speculation(
        ProviderUsage(cost=0.002),
        _meta(None),
        discarded_usage=ProviderUsage(),
        speculation_billed=True,
    )
    assert cost is not None
    assert cost.total == 0.002
    assert cost.complete is False
