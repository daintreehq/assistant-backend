"""Cost reporting — the tester pays this bill.

Cost is the provider's own reported figure, never a token-price estimate
computed here — so what a turn
costs is their money, not a hosting expense we absorb. That makes the figure a
product surface rather than an internal metric, and makes two failure modes
unacceptable: reporting a cost we did not observe, and reporting zero for one we
could not observe.

The wire fact these tests rest on was verified against the live API on
2026-08-14, against OpenRouter: `cost` arrived in the usage object unconditionally — the
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


def test_cached_tokens_are_read_from_the_nested_shape() -> None:
    usage = ProviderUsage.from_wire(
        {"prompt_tokens": 1000, "prompt_tokens_details": {"cached_tokens": 768}}
    )
    assert usage.cached_tokens == 768
    assert usage.cached_tokens_source == "nested"


def test_cached_tokens_are_read_from_the_flat_shape_too() -> None:
    """Together documents both, model-dependent.

    Reading only the nested one turns a host that uses the flat spelling into one
    that appears to have no prefix cache — a wrong number that reads as a caching
    problem rather than a parser bug, which is why it would survive review.
    """
    usage = ProviderUsage.from_wire({"prompt_tokens": 1000, "cached_tokens": 768})
    assert usage.cached_tokens == 768
    assert usage.cached_tokens_source == "flat"


def test_the_flat_shape_is_a_fallback_and_never_a_supplement() -> None:
    """Both present means one number reported twice, not two numbers to add."""
    usage = ProviderUsage.from_wire(
        {
            "prompt_tokens": 1000,
            "cached_tokens": 768,
            "prompt_tokens_details": {"cached_tokens": 768},
        }
    )
    assert usage.cached_tokens == 768


def test_a_nested_skeleton_zero_does_not_mask_a_real_flat_count() -> None:
    """An OpenAI-shaped host emits `prompt_tokens_details` as a fixed skeleton.

    Preferring the nested value unconditionally would let a zero the host never
    filled in hide a real flat count — the exact under-report the flat fallback
    exists to fix, arriving through the fix itself. A report beats a zero.
    """
    usage = ProviderUsage.from_wire(
        {
            "prompt_tokens": 1000,
            "cached_tokens": 900,
            "prompt_tokens_details": {"cached_tokens": 0, "audio_tokens": 0},
        }
    )
    assert usage.cached_tokens == 900
    assert usage.cached_tokens_source == "flat"


def test_a_flat_skeleton_zero_does_not_mask_a_real_nested_count() -> None:
    """The mirror. Neither field is privileged; a report beats a zero either way."""
    usage = ProviderUsage.from_wire(
        {"prompt_tokens": 1000, "cached_tokens": 0, "prompt_tokens_details": {"cached_tokens": 900}}
    )
    assert usage.cached_tokens == 900
    assert usage.cached_tokens_source == "nested"


def test_a_genuine_zero_is_reported_as_zero_not_as_unreported() -> None:
    """Both saying "no reuse" is a measurement, and must not decay to `None`."""
    usage = ProviderUsage.from_wire(
        {"prompt_tokens": 1000, "cached_tokens": 0, "prompt_tokens_details": {"cached_tokens": 0}}
    )
    assert usage.cached_tokens == 0
    assert usage.cached_tokens_source == "nested"

    lone = ProviderUsage.from_wire(
        {"prompt_tokens": 1000, "prompt_tokens_details": {"cached_tokens": 0}}
    )
    assert lone.cached_tokens == 0
    assert lone.cached_tokens_source == "nested"


def test_an_unparseable_nested_count_falls_through_to_the_flat_one() -> None:
    """`"768 tokens"` needs a judgement call, so it is not a count at all."""
    usage = ProviderUsage.from_wire(
        {"cached_tokens": 768, "prompt_tokens_details": {"cached_tokens": "768 tokens"}}
    )
    assert usage.cached_tokens == 768
    assert usage.cached_tokens_source == "flat"


def test_an_absent_cached_count_reports_no_source() -> None:
    usage = ProviderUsage.from_wire({"prompt_tokens": 1000})
    assert usage.cached_tokens is None
    assert usage.cached_tokens_source is None


@pytest.mark.parametrize(
    "value",
    [
        " 1024",  # whitespace needs a judgement call; a count does not
        "+1024",
        "1_024",
        "1e3",
        "\u00b9\u2070\u00b2\u2074",  # str.isdigit() says yes; int() says no
        True,  # bool subclasses int, so this would otherwise land as 1
        -5,  # a negative token count is not a token count
        float("nan"),
        float("inf"),
        1024.5,
        None,
        [1024],
        {"value": 1024},
    ],
)
def test_a_malformed_token_count_is_unreported_rather_than_wrong(value: object) -> None:
    """`turn_accounting.cache_ratio` divides by `prompt_tokens`, mid-turn.

    A string there raised on the paid path. `None` means "not reported", which
    every consumer already handles.
    """
    usage = ProviderUsage.from_wire(
        {"prompt_tokens": value, "completion_tokens": value, "cached_tokens": value}
    )
    assert usage.prompt_tokens is None
    assert usage.completion_tokens is None
    assert usage.cached_tokens is None


@pytest.mark.parametrize("value", [1024.0, "1024", 0, "0"])
def test_an_unambiguous_count_is_read_rather_than_refused(value: object) -> None:
    """Refusing here does not surface as "not reported" — it surfaces as zero.

    Every consumer projects a `None` count with `or 0` on the way to the client,
    so a form we could have read exactly one way and chose not to becomes a
    confident zero. `1024.0` and `"1024"` each have one reading; take it.
    """
    assert ProviderUsage.from_wire({"prompt_tokens": value}).prompt_tokens == int(value)


def test_a_summed_usage_claims_no_single_cached_token_source() -> None:
    summed = sum_usage(
        ProviderUsage(cached_tokens=10, cached_tokens_source="nested"),
        ProviderUsage(cached_tokens=20, cached_tokens_source="nested"),
    )
    assert summed.cached_tokens == 30
    assert summed.cached_tokens_source is None


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


# ---------------------------------------------------------------------------
# Partial SUMS. `None` is visibly unknown; a short float is not.
# ---------------------------------------------------------------------------


def test_a_sum_with_one_side_unreported_is_marked_partial() -> None:
    """The number survives, the fact that it is short must survive with it.

    Two calls were billed and one reported. `cost` is a real figure below what
    the caller paid, and nothing about the float itself says so — which is
    exactly why the bit travels alongside it.
    """
    assert sum_usage(ProviderUsage(cost=0.002), ProviderUsage()).cost_complete is False
    assert sum_usage(ProviderUsage(), ProviderUsage(cost=0.003)).cost_complete is False


def test_a_sum_with_both_sides_reported_stays_complete() -> None:
    summed = sum_usage(ProviderUsage(cost=0.002), ProviderUsage(cost=0.003))
    assert summed.cost == pytest.approx(0.005)
    assert summed.cost_complete is True


def test_neither_side_reporting_is_unknown_not_partial() -> None:
    """`cost is None` already reads as unknown; it needs no second flag."""
    summed = sum_usage(ProviderUsage(), ProviderUsage())
    assert summed.cost is None
    assert summed.cost_complete is True


def test_an_empty_accumulator_is_the_identity_not_a_silent_call() -> None:
    """`None` means "nothing summed yet"; `ProviderUsage()` means "a call told us nothing".

    The streaming path accumulates from `None` for exactly this reason. If the
    empty accumulator were a `ProviderUsage()`, the very first sum would look
    like a call that reported nothing and every turn would be marked partial.
    """
    assert sum_usage(None, ProviderUsage(cost=0.002)).cost_complete is True
    assert sum_usage(ProviderUsage(cost=0.002), None).cost_complete is True


def test_a_partial_propagates_through_a_further_sum() -> None:
    """Folding a third, fully-reported call must not launder an earlier gap."""
    partial = sum_usage(ProviderUsage(cost=0.002), ProviderUsage())
    assert sum_usage(partial, ProviderUsage(cost=0.001)).cost_complete is False


def test_three_fully_reported_calls_stay_complete() -> None:
    """The positive control for the case above: depth alone is not doubt."""
    two = sum_usage(ProviderUsage(cost=0.002), ProviderUsage(cost=0.001))
    three = sum_usage(two, ProviderUsage(cost=0.003))
    assert three.cost == pytest.approx(0.006)
    assert three.cost_complete is True


def test_two_empty_accumulators_sum_to_an_unknown_not_a_partial() -> None:
    """Nothing plus nothing is still nothing — not a gap in something."""
    summed = sum_usage(None, None)
    assert summed.cost is None
    assert summed.cost_complete is True


def test_a_wire_usage_record_is_never_partial() -> None:
    """One call is reported or not; only aggregation can be short."""
    assert ProviderUsage.from_wire(LIVE_USAGE).cost_complete is True
    assert ProviderUsage.from_wire({"prompt_tokens": 6}).cost_complete is True


def test_the_response_usage_carries_cost_through() -> None:
    out = response_usage(ProviderUsage(prompt_tokens=10, cached_tokens=8, cost=0.004))
    assert out.cost == 0.004
    assert out.cached_tokens == 8


# ---------------------------------------------------------------------------
# Partial totals. A sum missing a component must not be labelled complete.
# ---------------------------------------------------------------------------


def _meta(selector_usage=None, *, selector_cost_complete=True):  # type: ignore[no-untyped-def]
    from daintree_assistant_server.contracts.daintree_api import RunbooksBlock
    from daintree_assistant_server.contracts.extensions import SelectorMeta
    from daintree_assistant_server.services.chat_orchestrator import _RespondMeta

    return _RespondMeta(
        request_id="r",
        state=None,
        catalog_revision="c",
        prompt_version="p",
        runbooks=RunbooksBlock(selector=SelectorMeta(usage=selector_usage)),
        warnings=[],
        selector_cost_complete=selector_cost_complete,
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


def test_a_partial_main_sum_is_not_reported_as_a_complete_total() -> None:
    """The re-roll case: `main` is non-None and short, so `complete` must clear.

    A bare-intent re-roll bills the main slot twice. When only one attempt
    reported, `main.cost is not None` is true and used to be the whole test —
    so the turn went out as `complete: true` carrying a figure below what the
    caller was charged. The one case where the flag said "complete" and the
    number underneath it was not.
    """
    from daintree_assistant_server.contracts.extensions import SelectorUsage

    main = sum_usage(ProviderUsage(cost=0.002), ProviderUsage())
    cost = turn_cost(main, _meta(SelectorUsage(cost=0.0004)))
    assert cost is not None
    assert cost.total == pytest.approx(0.0024)
    assert cost.complete is False, "a partial main sum must not be labelled complete"


def test_a_partial_selector_sum_is_not_reported_as_a_complete_total() -> None:
    """Same defect one slot over: a repaired selection that half-reported."""
    from daintree_assistant_server.contracts.extensions import SelectorUsage

    cost = turn_cost(
        ProviderUsage(cost=0.002),
        _meta(SelectorUsage(cost=0.0004), selector_cost_complete=False),
    )
    assert cost is not None
    assert cost.complete is False


def test_a_repair_where_both_calls_reported_stays_complete() -> None:
    """The fix must not make every repaired turn a floor — only the short ones.

    Read against the TASK runner's summation. This used to exercise the prompt
    selector's copy of the same function; that selector is gone (selection is
    local and makes no billed call, so it has no repair to bill), and the JSON
    repair retry is now the only place in the server where two completions get
    summed into one reported figure.
    """
    from daintree_assistant_server.services.task_runner import _sum_usage

    summed = _sum_usage(ProviderUsage(cost=0.0004), ProviderUsage(cost=0.0005))
    assert summed is not None
    assert summed.cost_complete is True


def test_a_repair_that_half_reported_is_marked_partial() -> None:
    from daintree_assistant_server.services.task_runner import _sum_usage

    summed = _sum_usage(ProviderUsage(cost=0.0004), ProviderUsage())
    assert summed is not None
    assert summed.cost_complete is False


def test_a_repair_call_is_billed_into_the_reported_cost() -> None:
    """A malformed JSON response costs TWICE — the attempt and the repair.

    The bug this pins is a summation that added tokens and dropped cost, so a
    repaired call reported less than was actually paid.
    """
    from daintree_assistant_server.services.task_runner import _sum_usage

    summed = _sum_usage(ProviderUsage(cost=0.0004), ProviderUsage(cost=0.0005))
    assert summed is not None
    assert summed.cost == pytest.approx(0.0009)


def test_a_task_repair_call_is_billed_into_the_task_cost() -> None:
    """Same defect, same fix, in the task runner's copy of the helper."""
    from daintree_assistant_server.services.task_runner import _sum_usage

    summed = _sum_usage(ProviderUsage(cost=0.0002), ProviderUsage(cost=0.0003))
    assert summed.cost == pytest.approx(0.0005)
    assert summed.cost_complete is True


def test_a_half_reported_task_repair_is_marked_partial() -> None:
    """`/tasks` has no `complete` flag, so the runner needs the bit to withhold on."""
    from daintree_assistant_server.services.task_runner import _sum_usage

    assert _sum_usage(ProviderUsage(cost=0.0002), ProviderUsage()).cost_complete is False


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


def test_a_reported_zero_is_a_measurement_not_a_gap() -> None:
    """`0.0` is falsy, and every check here has to be `is None` rather than truthy.

    A genuinely-free call (a promo endpoint, a fully-cached prompt) reports
    `0.0`. Treating that as "unreported" would flip a turn we measured exactly
    into a floor — the mirror image of the bug this file is about.
    """
    summed = sum_usage(ProviderUsage(cost=0.0), ProviderUsage(cost=0.002))
    assert summed.cost == pytest.approx(0.002)
    assert summed.cost_complete is True

    cost = turn_cost(ProviderUsage(cost=0.0), _meta(None))
    assert cost is not None
    assert cost.total == 0.0
    assert cost.complete is True


def test_a_reported_zero_beside_a_silent_call_is_still_partial() -> None:
    """...and the same `is None` discipline must not let the gap slip through."""
    assert sum_usage(ProviderUsage(cost=0.0), ProviderUsage()).cost_complete is False


def test_a_wire_zero_is_parsed_as_a_zero_not_as_absent() -> None:
    """The falsy-value check has to start at the parser, or nothing after it helps."""
    assert ProviderUsage.from_wire({"cost": 0.0}).cost == 0.0
    assert ProviderUsage.from_wire({"cost": 0}).cost == 0.0




def test_a_free_local_selection_does_not_make_the_turn_incomplete() -> None:
    """The case that used to be an edge and is now EVERY turn.

    `LocalSelector` runs in process and reports `usage=None`. That is not "a
    selector call whose cost we failed to observe" — the distinction the rule
    above turns on — it is "no cost was incurred", and a turn whose only unknown
    is a cost that does not exist must still report a complete total.

    Untested until the prompt-based selector was deleted, because until then a
    selection that reported nothing genuinely WAS a billed call we had lost
    track of, and the suite's coverage of `selector_usage=None` was all about
    the skipped-continuation case. Now every non-degraded turn takes this path,
    so the assertion is load-bearing on the ordinary route rather than an edge.
    """
    cost = turn_cost(ProviderUsage(cost=0.002), _meta(None))
    assert cost is not None
    assert cost.complete is True, (
        "a free in-process selection was treated as an unobserved cost, which floors "
        "the reported total of every ordinary turn"
    )
    assert cost.total == pytest.approx(0.002)


def test_a_degraded_local_selection_is_still_a_complete_total() -> None:
    """Fail-open selection costs nothing either — degrading is not spending.

    `SelectionDecision.degraded_open` is reachable on the local path (a
    classifier error, or ids the catalog cannot resolve). It carries no usage,
    so the same rule applies: nothing was billed, and nothing is unknown.
    """
    from daintree_assistant_server.runbooks.decision import SelectionDecision

    decision = SelectionDecision.degraded_open(["local_runbook_selector_failed_open"])
    assert decision.usage is None
    assert decision.calls == 0

    # Built the way the orchestrator builds it from that decision, so the test
    # distinguishes "ran locally and degraded" from "skipped selection" — the
    # two states that both arrive here as `selector_usage=None` and that the
    # deleted prompt-selector tests kept apart by the cost each one billed.
    meta = _meta(None)
    meta.runbooks.selector.ran = True
    meta.runbooks.selector.degraded = True
    cost = turn_cost(ProviderUsage(cost=0.002), meta)
    assert cost is not None
    assert cost.complete is True, (
        "a degraded LOCAL selection floored the turn's total. Degrading costs "
        "nothing in process; only an unobserved BILL is a gap."
    )
    assert cost.selector is None
