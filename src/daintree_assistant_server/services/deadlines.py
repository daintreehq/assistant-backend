"""Request-scoped deadlines — a finite end-to-end budget for a turn.

The gap this closes: **an `httpx` read timeout is an inactivity bound, not a
wall-clock one.** It measures the gap between chunks, so a response that keeps
trickling a byte resets it forever. Combined with one provider retry, a stuck
turn could previously approach the Cloud Run request ceiling with nothing in the
application able to stop it — and a request that runs until the platform kills it
produces no useful error, no phase attribution, and no released capacity.

Four distinct limits, because "it took too long" is four different bugs with four
different fixes:

===================  ===========================================================
Phase                What it bounds
===================  ===========================================================
``preparation``      Validation, state decode and skill selection — everything
                     before the upstream model is opened. Slow here means *our*
                     work is slow (or the selector is), and the user has seen
                     nothing at all.
``first_output``     Arrival to the first visible content or tool call. This is
                     the number a user actually experiences as "is it broken?".
``stream_idle``      The longest gap between upstream chunks once generation has
                     started. Distinguishes a slow generation from a dead one.
``total``            Hard wall-clock for the whole turn. The backstop that makes
                     the other three unable to compose into something unbounded.
===================  ===========================================================

All timing uses :func:`time.monotonic`, never wall time: a clock adjustment mid
turn must not extend or collapse a deadline.
"""

from __future__ import annotations

import math
import time
from dataclasses import dataclass, field
from enum import StrEnum


class TurnPhase(StrEnum):
    """Which budget ran out. Reported in logs and in the error payload.

    The value is the diagnostic: "the turn timed out" sends an operator nowhere,
    while "it timed out in preparation" says the model was never even reached.
    """

    PREPARATION = "preparation"
    FIRST_OUTPUT = "first_output"
    STREAM_IDLE = "stream_idle"
    TOTAL = "total"


@dataclass(frozen=True)
class BudgetPolicy:
    """The four limits, in seconds.

    Defaults are the shipped posture. They are deliberately generous against
    measured behaviour rather than aspirational: the job here is to bound the
    pathological case, not to fail healthy slow turns. A turn that trips one of
    these is broken, not merely unlucky.
    """

    preparation: float = 25.0
    first_output: float = 30.0
    stream_idle: float = 60.0
    total: float = 300.0

    def __post_init__(self) -> None:
        for name in ("preparation", "first_output", "stream_idle", "total"):
            value = getattr(self, name)
            if not math.isfinite(value) or value <= 0:
                raise ValueError(
                    f"{name} deadline must be a positive finite number, got {value!r}"
                )
        # Every non-total phase, not just first_output. A phase limit above the
        # total is clamped away and so can never be the phase reported — which
        # silently costs the attribution the phase exists to provide. Rejecting
        # one and tolerating the others was just inconsistent.
        for name in ("preparation", "first_output", "stream_idle"):
            if getattr(self, name) > self.total:
                raise ValueError(
                    f"{name} deadline exceeds the total budget, so it could never "
                    "be the phase that trips"
                )


DEFAULT_BUDGET_POLICY = BudgetPolicy()


@dataclass(frozen=True)
class RequestBudget:
    """One turn's monotonic clock, shared by every phase and every retry attempt.

    Created once at request arrival and threaded down. The critical property is
    that retries **share** it: three attempts that each get a fresh per-attempt
    timeout multiply, which is precisely how a bounded-looking system becomes
    unbounded.
    """

    started_at: float = field(default_factory=time.monotonic)
    policy: BudgetPolicy = DEFAULT_BUDGET_POLICY

    # ---- elapsed / remaining -------------------------------------------

    def elapsed(self) -> float:
        return time.monotonic() - self.started_at

    def remaining_total(self) -> float:
        """Seconds left in the whole turn. Never negative."""
        return max(0.0, self.policy.total - self.elapsed())

    def remaining(self, phase: TurnPhase) -> float:
        """Seconds left for ``phase``, measured from the START of the turn.

        **Absolute, not renewable.** An earlier version returned the full phase
        limit on every call, so a caller that consulted it once per chunk handed
        out a fresh 30-second first-output allowance every time — meaning a model
        emitting a reasoning fragment every 29 seconds could think indefinitely
        without ever tripping the deadline that exists to measure exactly that.

        ``STREAM_IDLE`` is the one genuinely renewable limit and is deliberately
        not served here; use :meth:`remaining_idle`.
        """
        if phase is TurnPhase.TOTAL:
            return self.remaining_total()
        if phase is TurnPhase.STREAM_IDLE:
            raise ValueError(
                "stream_idle is measured per gap, not from the start of the turn; "
                "use remaining_idle(since=...)"
            )
        limit = float(getattr(self.policy, phase.value))
        return max(0.0, min(limit - self.elapsed(), self.remaining_total()))

    def remaining_idle(self, since: float) -> float:
        """Seconds left in the current inter-chunk gap.

        The only limit that legitimately resets: each new chunk starts a fresh
        allowance, which is what distinguishes a slow generation (chunks still
        arriving) from a dead one. Still clamped by the total, so a generous idle
        allowance cannot outlive the turn.
        """
        gap_left = float(self.policy.stream_idle) - (time.monotonic() - since)
        return max(0.0, min(gap_left, self.remaining_total()))

    def expired(self) -> bool:
        return self.remaining_total() <= 0.0

    # ---- retry integration ---------------------------------------------

    def attempt_allowance(self, phase: TurnPhase, *, minimum: float = 1.0) -> float | None:
        """The budget one retry attempt may consume, or ``None`` to stop trying.

        Returning ``None`` when too little time remains is deliberate: starting an
        attempt that cannot plausibly finish wastes the caller's remaining budget
        and replaces a clean deadline error with a confusing upstream one.
        """
        left = self.remaining(phase)
        if left < minimum:
            return None
        return left


class DeadlineExceeded(Exception):
    """A phase ran out of budget.

    Carries the phase because that is the actionable part, and the elapsed time
    because "300s" and "0.4s" mean very different things when the same phase
    trips.
    """

    def __init__(self, phase: TurnPhase, elapsed: float) -> None:
        super().__init__(f"{phase.value} deadline exceeded after {elapsed:.1f}s")
        self.phase = phase
        self.elapsed = elapsed
