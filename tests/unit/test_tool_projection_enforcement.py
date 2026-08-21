"""The enforcement gate: when a turn may send the projection instead of everything.

`project_tools` decides what a turn WOULD send. This decides whether it may — and
every branch here answers "no" by leaving the full inventory in place, because the
cost of wrongly enforcing is a turn that cannot do its job while the cost of
wrongly failing open is a request that is merely as large as it was yesterday.

The gate is pure, so every one of these runs without an app, a provider, a
settings cache or a request. That is the reason it lives outside the orchestrator:
the failure modes worth testing are all rare, and rare paths that need a whole
server stood up to reach do not get tested.
"""

from __future__ import annotations

import pytest

from daintree_assistant_server.contracts.openai import ChatMessage, FunctionCall, ToolCall
from daintree_assistant_server.skills.tool_projection import (
    REASON_COHORT_EXCLUDED,
    REASON_DISABLED,
    REASON_EMPTY_PROJECTION,
    REASON_ENFORCED,
    REASON_FORCED_TOOL_MISSING,
    REASON_INVALID_PROJECTION,
    REASON_NO_OFFERED_TOOLS,
    REASON_PROFILE_EXCLUDED,
    REASON_PROJECTION_ERROR,
    REASON_SELECTOR_DEGRADED,
    REASON_SHADOW,
    REASON_UNRESOLVED_TOOL_MISSING,
    ToolProjection,
    decide_enforcement,
    filter_provider_tools,
    session_bucket,
    tools_bytes,
)


def _projection(proposed: set[str], offered: set[str]) -> ToolProjection:
    return ToolProjection(
        proposed=frozenset(proposed),
        offered=frozenset(offered),
        reasons=dict.fromkeys(proposed, "core"),
    )


HEALTHY = _projection(
    {"fs__read", "context__snapshot"}, {"fs__read", "context__snapshot", "git__x"}
)


def _decide(**overrides: object) -> object:
    kwargs: dict[str, object] = {
        "mode": "enforced",
        "rollout_percent": 100,
        "session_id": "sess-1",
        "profile": "assistant",
        "selector_degraded": False,
        "projection": HEALTHY,
        "messages": [],
        "forced_tool": None,
    }
    kwargs.update(overrides)
    return decide_enforcement(**kwargs)  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# The three postures.
# ---------------------------------------------------------------------------


def test_the_healthy_case_enforces() -> None:
    decision = _decide()
    assert decision.enforce is True
    assert decision.reason == REASON_ENFORCED
    assert decision.fail_open is False
    assert decision.cohort_selected is True


@pytest.mark.parametrize(
    ("mode", "reason"),
    [("disabled", REASON_DISABLED), ("shadow", REASON_SHADOW)],
)
def test_the_non_enforcing_modes_never_enforce(mode: str, reason: str) -> None:
    decision = _decide(mode=mode)
    assert decision.enforce is False
    assert decision.reason == reason
    # Neither is a failure, and counting them as one would bury the reasons that
    # actually mean something under the everyday default.
    assert decision.fail_open is False


def test_shadow_is_decided_before_anything_can_go_wrong() -> None:
    """A broken projection in shadow mode is still just shadow mode.

    Ordering matters: if the correctness gates ran first, every shadow turn with
    a degraded selector would be logged as a fail-open, and the rate a rollback
    triggers on would be dominated by traffic that was never enforcing.
    """
    decision = _decide(mode="shadow", projection=None, selector_degraded=True)
    assert decision.reason == REASON_SHADOW
    assert decision.fail_open is False


# ---------------------------------------------------------------------------
# Cohort.
# ---------------------------------------------------------------------------


def test_zero_percent_enforces_nothing_and_is_not_a_failure() -> None:
    decision = _decide(rollout_percent=0)
    assert decision.enforce is False
    assert decision.reason == REASON_COHORT_EXCLUDED
    assert decision.fail_open is False
    assert decision.cohort_selected is False


def test_the_bucket_is_stable_for_a_session() -> None:
    assert session_bucket("sess-1") == session_bucket("sess-1")
    assert 0 <= session_bucket("sess-1") < 100


def test_enrollment_is_monotonic_in_the_percentage() -> None:
    """Raising the percentage only ever ADDS sessions.

    A promotion that dropped a session already enforcing would move a live
    conversation back to the full inventory mid-task — recoverable, but it would
    also make the rollout's own numbers unreadable, since the enforced population
    would no longer be a superset of the previous stage's.
    """
    sessions = [f"sess-{n}" for n in range(400)]
    enrolled: list[set[str]] = []
    for percent in (0, 1, 5, 25, 50, 100):
        enrolled.append(
            {s for s in sessions if _decide(session_id=s, rollout_percent=percent).enforce}
        )
    for index in range(len(enrolled) - 1):
        assert enrolled[index] <= enrolled[index + 1]
    assert enrolled[0] == set()
    assert enrolled[-1] == set(sessions)


def test_the_percentage_actually_splits_traffic() -> None:
    """Guard the guard: a bucket function that returned a constant would make
    every monotonicity assertion above pass while shipping an all-or-nothing
    switch dressed up as a percentage."""
    sessions = [f"sess-{n}" for n in range(1000)]
    enrolled = sum(1 for s in sessions if _decide(session_id=s, rollout_percent=10).enforce)
    assert 40 < enrolled < 170, f"10% of 1000 sessions enrolled {enrolled}"


# ---------------------------------------------------------------------------
# Fail-open.
# ---------------------------------------------------------------------------


def test_a_sub_agent_is_held_out() -> None:
    decision = _decide(profile="subagent")
    assert decision.enforce is False
    assert decision.reason == REASON_PROFILE_EXCLUDED
    assert decision.fail_open is False


def test_a_degraded_selector_fails_open() -> None:
    decision = _decide(selector_degraded=True)
    assert decision.enforce is False
    assert decision.reason == REASON_SELECTOR_DEGRADED
    assert decision.fail_open is True


def test_a_continuation_round_that_never_ran_the_selector_still_enforces() -> None:
    """The gate reads `degraded`, never `ran`.

    The selector deliberately does not run on ordinary tool-continuation rounds,
    so requiring a fresh run would mean either never enforcing a continuation or
    reselecting on every tool round purely to justify a filter. The prior set was
    authoritative when it was resolved; nothing about a continuation unmakes that.
    """
    assert _decide(selector_degraded=False).enforce is True


def test_an_uncomputed_projection_fails_open() -> None:
    decision = _decide(projection=None)
    assert decision.enforce is False
    assert decision.reason == REASON_PROJECTION_ERROR
    assert decision.fail_open is True


def test_no_offered_tools_is_not_a_failure() -> None:
    decision = _decide(projection=_projection(set(), set()))
    assert decision.enforce is False
    assert decision.reason == REASON_NO_OFFERED_TOOLS
    assert decision.fail_open is False


def test_an_empty_proposal_against_a_real_inventory_fails_open() -> None:
    """The single most dangerous state there is.

    An empty proposal means "withhold everything". The core alone guarantees a
    non-empty proposal whenever the inventory holds any of it, so reaching this
    means the projection is wrong about this request — and a request offering the
    model no tools is not a cheaper turn, it is a turn that cannot act.
    """
    decision = _decide(projection=ToolProjection(proposed=frozenset(), offered=frozenset({"a"})))
    assert decision.enforce is False
    assert decision.reason == REASON_EMPTY_PROJECTION
    assert decision.fail_open is True


@pytest.mark.parametrize(
    "broken",
    [
        # Proposes something never offered — the two sets describe different requests.
        ToolProjection(
            proposed=frozenset({"ghost"}), offered=frozenset({"a"}), reasons={"ghost": "core"}
        ),
        # Reasons and proposal disagree, so `reasons` cannot explain what survived.
        ToolProjection(proposed=frozenset({"a"}), offered=frozenset({"a"}), reasons={}),
    ],
    ids=["proposes-unoffered", "reasons-mismatch"],
)
def test_an_internally_inconsistent_projection_fails_open(broken: ToolProjection) -> None:
    decision = _decide(projection=broken)
    assert decision.enforce is False
    assert decision.reason == REASON_INVALID_PROJECTION
    assert decision.fail_open is True


def test_a_pinned_tool_the_proposal_lost_fails_open() -> None:
    """True by construction, re-checked because construction is what regresses.

    A `tool_choice` naming a tool the request does not carry is an immediate
    provider 400 — not a degraded turn, a failed one.
    """
    decision = _decide(forced_tool="git.push")
    assert decision.enforce is False
    assert decision.reason == REASON_FORCED_TOOL_MISSING
    assert decision.fail_open is True


def test_a_pinned_tool_the_proposal_kept_enforces() -> None:
    assert _decide(forced_tool="fs.read").enforce is True


def test_tool_choice_required_needs_no_particular_tool() -> None:
    """`"required"` forces A call without naming one, so `_forced_tool_name`
    yields None and any non-empty proposal satisfies it."""
    assert _decide(forced_tool=None).enforce is True


def test_an_unanswered_call_whose_tool_the_proposal_lost_fails_open() -> None:
    """A stranded continuation is a turn that can never finish what it started."""
    messages = [
        ChatMessage(
            role="assistant",
            tool_calls=[
                ToolCall(
                    id="c1", type="function", function=FunctionCall(name="git__x", arguments="{}")
                )
            ],
        )
    ]
    decision = _decide(
        projection=_projection({"fs__read"}, {"fs__read", "git__x"}), messages=messages
    )
    assert decision.enforce is False
    assert decision.reason == REASON_UNRESOLVED_TOOL_MISSING
    assert decision.fail_open is True


def test_an_answered_call_does_not_pin_its_tool() -> None:
    """Only UNRESOLVED calls constrain the request.

    No provider requires the tools array to name what a completed exchange used,
    and pinning every tool the transcript ever mentioned would make a long
    conversation converge on the full inventory — the saving would decay to zero
    exactly where the prompt is largest.
    """
    messages = [
        ChatMessage(
            role="assistant",
            tool_calls=[
                ToolCall(
                    id="c1", type="function", function=FunctionCall(name="git__x", arguments="{}")
                )
            ],
        ),
        ChatMessage(role="tool", tool_call_id="c1", content="done"),
    ]
    decision = _decide(
        projection=_projection({"fs__read"}, {"fs__read", "git__x"}), messages=messages
    )
    assert decision.enforce is True


def test_a_proposal_that_removes_nothing_still_enforces() -> None:
    """A client whose inventory is already minimal is not a failure.

    Requiring the filter to actually drop something would log every such turn as
    a fail-open and inflate the one rate a rollback triggers on.
    """
    same = _projection({"fs__read"}, {"fs__read"})
    assert _decide(projection=same).enforce is True


# ---------------------------------------------------------------------------
# The filter and the size metric.
# ---------------------------------------------------------------------------


def _tool(name: str) -> dict[str, object]:
    return {"type": "function", "function": {"name": name, "parameters": {"type": "object"}}}


def test_the_filter_keeps_the_proposal_in_the_original_order() -> None:
    """Order is part of the bytes.

    The tool region is cached on its literal prefix, so a reordering that changed
    nothing about WHICH tools are present would still read upstream as a
    different request.
    """
    tools = [_tool(n) for n in ("z__a", "fs__read", "m__b", "context__snapshot")]
    kept = filter_provider_tools(tools, HEALTHY)
    assert [t["function"]["name"] for t in kept] == ["fs__read", "context__snapshot"]


def test_the_filter_refuses_a_tool_it_cannot_name() -> None:
    """A malformed entry is raised, never skipped.

    Skipping would silently withhold exactly the tool nothing understood — the
    one failure this whole design is arranged around. The caller turns the raise
    into a fail-open with a reason attached.
    """
    with pytest.raises((KeyError, TypeError)):
        filter_provider_tools([{"type": "function"}], HEALTHY)
    with pytest.raises(TypeError):
        filter_provider_tools([{"type": "function", "function": {"name": 7}}], HEALTHY)


def test_the_size_metric_measures_the_compact_array() -> None:
    assert tools_bytes(None) == len(b"[]")
    assert tools_bytes([]) == len(b"[]")
    one = tools_bytes([_tool("fs__read")])
    two = tools_bytes([_tool("fs__read"), _tool("fs__list")])
    assert 0 < one < two
    # Compact separators: no whitespace padding the measurement.
    assert b", " not in str(tools_bytes).encode()
