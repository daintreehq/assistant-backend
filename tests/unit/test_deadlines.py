"""Request-scoped deadlines.

The property that matters: **a turn cannot outlive its total budget**, no matter
how the phases and retries compose. An `httpx` read timeout bounds inactivity,
not wall clock, so a response that trickles one byte resets it forever — and one
provider retry used to double whatever bound did exist.
"""

from __future__ import annotations

import asyncio
import time

import pytest

from daintree_assistant_server.errors import DaintreeError
from daintree_assistant_server.providers.retry import retry_upstream
from daintree_assistant_server.services.deadlines import (
    BudgetPolicy,
    DeadlineExceeded,
    RequestBudget,
    TurnPhase,
)


def _budget(**kwargs: float) -> RequestBudget:
    policy = BudgetPolicy(**{**{"preparation": 10, "first_output": 20, "stream_idle": 30, "total": 60}, **kwargs})
    return RequestBudget(policy=policy)


# ---------------------------------------------------------------------------
# Phase limits are always clamped by the total.
# ---------------------------------------------------------------------------


def test_a_fresh_budget_offers_each_phase_its_own_limit() -> None:
    budget = _budget()
    assert budget.remaining(TurnPhase.PREPARATION) == pytest.approx(10, abs=0.1)
    assert budget.remaining(TurnPhase.FIRST_OUTPUT) == pytest.approx(20, abs=0.1)
    assert budget.remaining(TurnPhase.TOTAL) == pytest.approx(60, abs=0.1)
    assert budget.remaining_idle(time.monotonic()) == pytest.approx(30, abs=0.1)


def test_start_relative_phases_do_not_renew() -> None:
    """The bug this replaced.

    `remaining()` used to return the full phase limit on every call, so a caller
    consulting it once per chunk handed out a fresh first-output allowance every
    time — a model emitting a reasoning fragment every 29s could think forever
    without tripping the deadline that exists to measure exactly that.
    """
    aged = RequestBudget(
        started_at=time.monotonic() - 15,
        policy=BudgetPolicy(preparation=10, first_output=20, stream_idle=30, total=60),
    )
    # 15s of a 20s allowance is gone, and it does not come back.
    assert aged.remaining(TurnPhase.FIRST_OUTPUT) == pytest.approx(5, abs=0.2)
    # Preparation is already past its 10s limit.
    assert aged.remaining(TurnPhase.PREPARATION) == 0.0


def test_the_idle_gap_is_the_one_limit_that_legitimately_resets() -> None:
    """A chunk arriving IS the evidence the stream is alive, so each new gap gets
    a fresh allowance — that is what distinguishes a slow generation from a dead
    one."""
    aged = RequestBudget(
        started_at=time.monotonic() - 40,
        policy=BudgetPolicy(preparation=10, first_output=20, stream_idle=15, total=300),
    )
    assert aged.remaining_idle(time.monotonic()) == pytest.approx(15, abs=0.2)
    assert aged.remaining_idle(time.monotonic() - 10) == pytest.approx(5, abs=0.2)


def test_asking_remaining_for_the_idle_phase_is_a_programming_error() -> None:
    """Serving it from `remaining()` is precisely how the renewal bug happened;
    make the wrong call impossible rather than merely discouraged."""
    with pytest.raises(ValueError, match="per gap"):
        _budget().remaining(TurnPhase.STREAM_IDLE)


def test_a_phase_can_never_outlive_the_turn() -> None:
    """The clamp is the whole point: a 30s idle allowance means nothing when 5s
    of total budget is left."""
    budget = RequestBudget(
        started_at=time.monotonic() - 55,
        policy=BudgetPolicy(preparation=10, first_output=20, stream_idle=30, total=60),
    )
    for phase in (TurnPhase.PREPARATION, TurnPhase.FIRST_OUTPUT, TurnPhase.TOTAL):
        assert budget.remaining(phase) <= 5.1, phase
    assert budget.remaining_idle(time.monotonic()) <= 5.1


def test_an_exhausted_budget_reports_zero_not_negative() -> None:
    budget = RequestBudget(
        started_at=time.monotonic() - 500, policy=BudgetPolicy(total=60)
    )
    assert budget.expired()
    for phase in (TurnPhase.PREPARATION, TurnPhase.FIRST_OUTPUT, TurnPhase.TOTAL):
        assert budget.remaining(phase) == 0.0
    assert budget.remaining_idle(time.monotonic()) == 0.0


def test_a_nonsense_policy_is_rejected_at_construction() -> None:
    with pytest.raises(ValueError):
        BudgetPolicy(total=0)
    with pytest.raises(ValueError):
        BudgetPolicy(preparation=-1)
    for kwargs in (
        {"first_output": 120, "total": 60},
        {"preparation": 120, "total": 60},
        {"stream_idle": 120, "total": 60},
    ):
        with pytest.raises(ValueError, match="could never be the phase that trips"):
            BudgetPolicy(**kwargs)  # type: ignore[arg-type]
    # Non-finite values would make every comparison silently false.
    for bad in (float("nan"), float("inf")):
        with pytest.raises(ValueError):
            BudgetPolicy(total=bad)


def test_the_budget_uses_a_monotonic_clock() -> None:
    """A wall-clock adjustment mid turn must not extend or collapse a deadline."""
    budget = RequestBudget()
    assert abs(budget.started_at - time.monotonic()) < 1.0
    # A monotonic timestamp is not a POSIX epoch value; this would be ~1.7e9.
    assert budget.started_at < 1_000_000_000


# ---------------------------------------------------------------------------
# Retry attempts share the budget rather than multiplying it.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_retries_stop_when_the_shared_budget_is_nearly_gone() -> None:
    """The regression this guards: N attempts at T seconds each cost N x T, which
    is exactly how a per-attempt timeout composes into an unbounded turn."""
    attempts = 0

    async def _always_fails() -> None:
        nonlocal attempts
        attempts += 1
        raise DaintreeError(
            status_code=503, message="transient", code="upstream_unavailable", retryable=True
        )

    spent = RequestBudget(started_at=time.monotonic() - 59.5, policy=BudgetPolicy(total=60))
    with pytest.raises(DaintreeError):
        await retry_upstream(
            _always_fails,
            retries=5,
            backoff_base_seconds=0.0,
            remaining_seconds=spent.remaining_total,
        )
    assert attempts == 1, "a retry started despite there being no budget for one"


@pytest.mark.asyncio
async def test_retries_still_happen_when_there_is_budget() -> None:
    attempts = 0

    async def _fails_once() -> str:
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise DaintreeError(
                status_code=503,
                message="transient",
                code="upstream_unavailable",
                retryable=True,
            )
        return "ok"

    fresh = RequestBudget(policy=BudgetPolicy(total=60))
    result = await retry_upstream(
        _fails_once,
        retries=2,
        backoff_base_seconds=0.0,
        remaining_seconds=fresh.remaining_total,
    )
    assert result == "ok"
    assert attempts == 2


@pytest.mark.asyncio
async def test_retry_without_a_budget_is_unchanged() -> None:
    """Standalone tooling (benchmarks, live tests) retries without inventing a
    budget; that path must keep working exactly as before."""
    attempts = 0

    async def _fails_once() -> str:
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise DaintreeError(
                status_code=503, message="t", code="upstream_unavailable", retryable=True
            )
        return "ok"

    assert await retry_upstream(_fails_once, retries=1, backoff_base_seconds=0.0) == "ok"
    assert attempts == 2


# ---------------------------------------------------------------------------
# attempt_allowance
# ---------------------------------------------------------------------------


def test_attempt_allowance_refuses_a_hopeless_attempt() -> None:
    nearly_done = RequestBudget(
        started_at=time.monotonic() - 59.8, policy=BudgetPolicy(total=60)
    )
    assert nearly_done.attempt_allowance(TurnPhase.TOTAL) is None


def test_attempt_allowance_grants_what_remains() -> None:
    budget = _budget()
    allowance = budget.attempt_allowance(TurnPhase.PREPARATION)
    assert allowance is not None
    assert allowance == pytest.approx(10, abs=0.1)


# ---------------------------------------------------------------------------
# DeadlineExceeded carries the diagnosis.
# ---------------------------------------------------------------------------


def test_the_exception_names_the_phase_and_the_elapsed_time() -> None:
    """"The turn timed out" sends an operator nowhere; "it timed out in
    preparation" says the model was never even reached."""
    exc = DeadlineExceeded(TurnPhase.PREPARATION, 12.34)
    assert exc.phase is TurnPhase.PREPARATION
    assert exc.elapsed == pytest.approx(12.34)
    assert "preparation" in str(exc)
    assert "12.3" in str(exc)


# ---------------------------------------------------------------------------
# A trickling stream cannot escape the total budget.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_a_slow_trickle_is_bounded_by_the_total_not_the_idle_gap() -> None:
    """The exact hole an inactivity timeout leaves open.

    Chunks keep arriving well inside the idle allowance, so an `httpx` read
    timeout would never fire — but the turn is still over its wall clock, and the
    total budget is what stops it.
    """
    budget = RequestBudget(
        policy=BudgetPolicy(
            preparation=0.3, first_output=0.3, stream_idle=0.3, total=0.3
        )
    )

    async def _trickle() -> None:
        while True:
            await asyncio.sleep(0.01)

    with pytest.raises(TimeoutError):
        async with asyncio.timeout(budget.remaining_idle(time.monotonic())):
            await _trickle()
    assert budget.expired()


# ---------------------------------------------------------------------------
# Utility-task budgets follow the PROFILE, not the shared default.
# ---------------------------------------------------------------------------


def test_a_heavy_task_budget_follows_its_own_timeout_not_the_shared_default() -> None:
    """The heavy transcript/graph profiles carry an explicit, larger per-call
    timeout so a future cut to the shared default cannot starve them.

    Deriving the total budget from that default instead would reintroduce exactly
    the starvation those explicit values exist to prevent — and it would stay
    invisible today, because the two numbers happen to be equal.
    """
    from daintree_assistant_server.config import Settings
    from daintree_assistant_server.services.task_runner import (
        _TASK_BUDGET_FACTOR,
        TaskProfile,
    )

    lean = Settings(STATE_SIGNING_KEY_CURRENT="x" * 32, UTILITY_TIMEOUT_SECONDS=10.0)

    class _Runner:
        settings = lean
        _budget_policy = None  # type: ignore[assignment]

    from pydantic import BaseModel

    from daintree_assistant_server.services.task_runner import TaskRunner

    class _Empty(BaseModel):
        pass

    heavy = TaskProfile(
        id="heavy",
        input_model=_Empty,
        output_mode="json",
        build_user=lambda *_: "",
        parse_output=lambda *_: {},
        timeout_seconds=60.0,
    )
    policy = TaskRunner._budget_policy(_Runner(), heavy)  # type: ignore[arg-type]
    assert policy.total == pytest.approx(60.0 * _TASK_BUDGET_FACTOR)
    assert policy.total > lean.utility_timeout_seconds * _TASK_BUDGET_FACTOR
