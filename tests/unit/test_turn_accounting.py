"""Endpoint attribution for the number the prompt-cache work exists to move.

`cache_ratio` is the metric the assembler's whole layout is built to protect,
and until now it was logged without saying WHICH of OpenRouter's ~24 endpoints
produced it. Each keeps its own node-local prefix cache, so under
throughput-sorted routing with fallbacks a re-rolled round can be answered by a
host that has never seen this prefix — and a prompt layout regression and a
routing switch then look identical in the logs.

These tests pin the fields that tell those two apart, and the deliberate
exclusions that keep the signal worth reading.
"""

from __future__ import annotations

from typing import Any

from structlog.testing import capture_logs

from daintree_assistant_server.contracts.daintree_api import TurnCost
from daintree_assistant_server.contracts.extensions import SelectorUsage
from daintree_assistant_server.providers.base import ProviderUsage
from daintree_assistant_server.services.turn_accounting import cache_ratio, log_turn_usage


def _log(**overrides: Any) -> dict[str, Any]:
    """Emit one `turn_usage` record with sensible defaults and return it."""
    kwargs: dict[str, Any] = {
        "usage": ProviderUsage(prompt_tokens=1000, completion_tokens=50, cached_tokens=750),
        "request_id": "req_1",
        "session_id": "sess_1",
        "turn_id": "turn_1",
        "session_round": 0,
        "stream": False,
        "finish_reason": "stop",
        "endpoint": "deepinfra",
        "observed_endpoints": ("deepinfra",),
        "selector_usage": None,
        "selector_endpoint": None,
        "turn_cost": None,
    }
    kwargs.update(overrides)
    usage = kwargs.pop("usage")
    with capture_logs() as logs:
        log_turn_usage(usage, **kwargs)
    entries = [entry for entry in logs if entry["event"] == "turn_usage"]
    assert len(entries) == 1
    return entries[0]


# ---- the ratio helper ---------------------------------------------------


def test_cache_ratio_is_the_cached_share_of_the_prompt() -> None:
    assert cache_ratio(1000, 750) == 0.75


def test_cache_ratio_rounds_to_three_places() -> None:
    """Enough to see a cache degrade, not so much that noise reads as movement."""
    assert cache_ratio(3, 1) == 0.333


def test_an_unmeasurable_ratio_is_none_rather_than_zero() -> None:
    """A missing number cannot support "the cache did nothing".

    Same rule as an unreported cost, with a sharper edge: an absent cost merely
    sits there, but a fabricated 0.0 here lands in whatever average a dashboard
    takes and drags it down — so the absence of a measurement would MOVE the one
    number this work exists to watch.
    """
    assert cache_ratio(None, 750) is None
    assert cache_ratio(1000, None) is None
    assert cache_ratio(0, 0) is None


def test_a_genuinely_cold_prompt_still_reports_zero() -> None:
    """The counterpart: an endpoint that counted its hits and found none is a
    real 0.0, and must not be blurred into the "unknown" case above."""
    assert cache_ratio(1000, 0) == 0.0


# ---- main-call attribution ----------------------------------------------


def test_the_endpoint_is_logged_beside_the_ratio_it_produced() -> None:
    entry = _log()
    assert entry["cache_ratio"] == 0.75
    assert entry["endpoint"] == "deepinfra"
    assert entry["distinct_endpoints"] == 1


def test_one_endpoint_named_twice_is_still_one_cache() -> None:
    """A re-roll that landed back on the same host did not blend anything."""
    entry = _log(observed_endpoints=("deepinfra", "deepinfra"))
    assert entry["distinct_endpoints"] == 1


def test_a_blended_ratio_identifies_itself() -> None:
    """The measurement the issue exists for: two hosts contributed tokens to one
    ratio, so a drop here may be routing rather than prompt layout."""
    entry = _log(observed_endpoints=("deepinfra", "novita"))
    assert entry["distinct_endpoints"] == 2


def test_unreported_endpoints_count_as_none_rather_than_one() -> None:
    """`None` is "not reported", not a host. Counting it would invent an
    endpoint switch out of a provider that simply stayed quiet."""
    entry = _log(endpoint=None, observed_endpoints=(None, None))
    assert entry["endpoint"] is None
    assert entry["distinct_endpoints"] == 0


def test_a_partially_reported_pair_counts_only_what_was_named() -> None:
    entry = _log(observed_endpoints=(None, "novita"))
    assert entry["distinct_endpoints"] == 1


# ---- selector attribution -----------------------------------------------


def test_the_selector_gets_its_own_ratio_and_endpoint() -> None:
    """A separate prefix on a separately-routed call: it regresses and is fixed
    independently, so reading it off the main ratio would read the wrong
    number."""
    entry = _log(
        selector_usage=SelectorUsage(prompt_tokens=400, cached_tokens=100),
        selector_endpoint="novita",
    )
    assert entry["selector_endpoint"] == "novita"
    assert entry["selector_prompt_tokens"] == 400
    assert entry["selector_cached_tokens"] == 100
    assert entry["selector_cache_ratio"] == 0.25


def test_a_turn_that_skipped_selection_reports_nothing_rather_than_zero() -> None:
    """A continuation round runs no selector. Zeros here would read as a
    selector call whose cache did nothing, which is a different fact."""
    entry = _log(selector_usage=None, selector_endpoint=None)
    assert entry["selector_prompt_tokens"] is None
    assert entry["selector_cached_tokens"] is None
    assert entry["selector_cache_ratio"] is None
    assert entry["selector_endpoint"] is None


def test_the_selectors_endpoint_is_excluded_from_the_distinct_count() -> None:
    """The exclusion that keeps `distinct_endpoints` worth reading.

    The selector sends a different prompt prefix, so it shares no cache with the
    main call and routinely lands elsewhere under throughput sorting. Folding it
    in would make `> 1` fire on nearly every turn and mean nothing.
    """
    entry = _log(
        endpoint="deepinfra",
        observed_endpoints=("deepinfra",),
        selector_usage=SelectorUsage(prompt_tokens=400, cached_tokens=100),
        selector_endpoint="novita",
    )
    assert entry["distinct_endpoints"] == 1


# ---- the turn key -------------------------------------------------------


def test_the_turn_key_is_logged_so_rounds_can_be_grouped() -> None:
    """A turn outlives this request — its tool-continuation rounds are separate
    calls sharing a `turn_id`. This server is stateless and cannot count across
    them, so the key is what hands that job to the query."""
    entry = _log(turn_id="turn_abc", session_round=3)
    assert entry["turn_id"] == "turn_abc"
    assert entry["round"] == 3


def test_the_session_is_logged_because_turn_ids_are_not_globally_unique() -> None:
    """`turn_id` is a caller-supplied string, unique only WITHIN a session.

    Grouping on it alone would merge unrelated turns from different callers that
    happened to pick the same name — and then report their two endpoints as an
    endpoint switch inside one turn, manufacturing exactly the finding this
    field exists to detect.
    """
    first = _log(session_id="sess_alice", turn_id="turn-1", endpoint="deepinfra")
    second = _log(session_id="sess_bob", turn_id="turn-1", endpoint="novita")
    assert first["session_id"] == "sess_alice"
    assert second["session_id"] == "sess_bob"
    assert first["turn_id"] == second["turn_id"], "the collision this key disambiguates"


# ---- honesty about what was not reported --------------------------------


def test_a_call_that_reported_no_tokens_declines_to_state_a_ratio() -> None:
    """An empty usage record is what a real call that told us nothing looks like.

    The token counts fall back to zero (they are counts of what we saw), but the
    ratio must not: 0.0 would report a stone-cold cache on a turn where nothing
    was measured at all.
    """
    entry = _log(usage=ProviderUsage())
    assert entry["prompt_tokens"] == 0
    assert entry["completion_tokens"] == 0
    assert entry["cached_tokens"] == 0
    assert entry["cache_ratio"] is None


def test_a_counted_prompt_with_uncounted_hits_declines_too() -> None:
    """The subtler half: the denominator arrived and the numerator did not.

    A ratio here would look perfectly well-formed and be pure invention.
    """
    entry = _log(usage=ProviderUsage(prompt_tokens=1000, cached_tokens=None))
    assert entry["prompt_tokens"] == 1000
    assert entry["cache_ratio"] is None


# ---- what was already there ---------------------------------------------


def test_the_existing_cost_fields_are_untouched() -> None:
    entry = _log(
        usage=ProviderUsage(
            prompt_tokens=1000, completion_tokens=50, cached_tokens=750, cost=0.002
        ),
        turn_cost=TurnCost(total=0.003, main=0.002, selector=0.001, complete=True),
    )
    assert entry["prompt_tokens"] == 1000
    assert entry["completion_tokens"] == 50
    assert entry["cached_tokens"] == 750
    assert entry["cost_usd"] == 0.002
    assert entry["turn_cost_usd"] == 0.003
    assert entry["selector_cost_usd"] == 0.001


def test_an_absent_turn_cost_reports_no_total() -> None:
    entry = _log(turn_cost=None)
    assert entry["turn_cost_usd"] is None
    assert entry["selector_cost_usd"] is None


def test_the_stream_flag_and_finish_reason_survive() -> None:
    entry = _log(stream=True, finish_reason="tool_calls")
    assert entry["stream"] is True
    assert entry["finish_reason"] == "tool_calls"
    assert entry["request_id"] == "req_1"
