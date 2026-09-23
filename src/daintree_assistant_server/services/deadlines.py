"""Request-scoped deadlines — bounding DEAD AIR, not turn length.

The gap this closes: **an `httpx` read timeout is an inactivity bound, not a
wall-clock one.** It measures the gap between chunks, so a response that keeps
trickling a byte resets it forever. Combined with one provider retry, a stuck
turn could previously approach the Cloud Run request ceiling with nothing in the
application able to stop it — and a request that runs until the platform kills it
produces no useful error, no phase attribution, and no released capacity.

**The posture, which is the part that is easy to get wrong:** every limit here
exists to detect a turn that has STOPPED, never to cap one that is merely long.
A round may legitimately run for minutes; what it may never do is sit silent
against a dead upstream until the platform kills the request. So each limit below
bounds an interval in which *nothing arrived*, and the single wall-clock number is
a backstop sized to fire only in place of Cloud Run's own — which would report no
phase, free no capacity until it fired, and tell a paying user nothing.

The other half of that posture lives in the orchestrator: **a stall detected
before any model output is a retryable upstream failure, not a fatal one.**
Nothing of the turn is public at that point, so replaying it is invisible. The
incident that set this shape: an upstream accepted the connection, never sent a
first event, and killed the turn at 30 s with 90% of the total budget unspent and
the retry that would have opened in ~1 s never attempted, because the deadline
raised something :func:`~..providers.retry.retry_upstream` does not catch.

===================  ===========================================================
Phase                What it bounds
===================  ===========================================================
``preparation``      Validation, state decode and runbook selection — everything
                     before the upstream model is opened. Slow here means *our*
                     work is slow (or the selector is), and the user has seen
                     nothing at all. Absolute, from the start of the turn.
``open_stall``       Per ATTEMPT: opening the upstream stream and reading its
                     first event. Retryable, so it is sized to be generous about
                     a cold prefill rather than tight about latency — aborting a
                     legitimate 100k-token prefill only pays for it twice.
``first_output``     From a SUCCESSFUL open to the first visible content or tool
                     call. Cumulative over that stream, so chunks that carry
                     nothing a user can see cannot renew it forever.
``stream_idle``      The longest gap between chunks once the stream is live. Per
                     gap, because a chunk arriving IS the evidence the stream is
                     alive — so a long generation never trips it, and a dead one
                     trips it in a minute.
``total``            Hard wall-clock backstop for the whole turn. Sized under the
                     platform request timeout so the failure is ours (typed and
                     phase-attributed) instead of the platform's.
===================  ===========================================================

``first_output`` is anchored to **the open that succeeded**, not to the start of
the turn, and that anchor is the whole correction. Measured from the turn start it
composed badly with the retry above it: an attempt that stalled for its full
allowance handed the live stream that followed an already-spent budget, and the
deadline would guillotine a perfectly healthy generation for being late rather
than dead. Anchored to the open, a retried turn gets the same honest allowance an
un-retried one does, while a stream that trickles reasoning fragments or
keepalives without ever saying anything still hits a wall well short of ``total``.

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

    ``FIRST_OUTPUT`` carries no limit of its own (see the module docstring): it is
    the label the per-gap idle deadline reports when the stall happened before
    anything visible had arrived.
    """

    PREPARATION = "preparation"
    OPEN_STALL = "open_stall"
    FIRST_OUTPUT = "first_output"
    STREAM_IDLE = "stream_idle"
    TOTAL = "total"


# Every field the policy carries, in one place so validation cannot drift from
# what it is meant to cover.
_LIMIT_NAMES = ("preparation", "open_stall", "first_output", "stream_idle", "total")


@dataclass(frozen=True)
class BudgetPolicy:
    """The four limits, in seconds.

    Defaults are the shipped posture. They are deliberately generous against
    measured behaviour rather than aspirational: the job here is to bound the
    pathological case, not to fail healthy slow turns. A turn that trips one of
    these is broken, not merely unlucky.

    Reference points behind the numbers, measured on the serving path:
    ``open_stall`` at 45 s against an open-to-first-event of ~1.2 s and a
    cold-prefill tail in the single-digit seconds — with one retry, a stalled open
    costs a pause, not the turn. ``first_output`` at 120 s against a main model reasoning
    at `medium`, whose first visible token follows seconds after the open at
    most: it can only bite a stream that is producing nothing a user can
    see. ``total`` at 900 s answers to the Cloud Run request timeout of 960 s
    (``cloudbuild.yaml``), which is the only number it has to stay under; move one
    and move the other.

    None of these has to fit inside the CLIENT's stream-idle window, because the
    server is not silent while they run: ``_stream_with_heartbeat`` emits an SSE
    comment every 8 s through selection, the provider open, the retries and any
    generation gap, and the CLI's watchdog resets on that read progress. The
    cadence is the coupling that matters, and
    ``tests/wire/test_stream_silence_budget.py`` pins it against the CLI's source.
    """

    preparation: float = 25.0
    open_stall: float = 45.0
    first_output: float = 120.0
    stream_idle: float = 60.0
    total: float = 900.0

    def __post_init__(self) -> None:
        for name in _LIMIT_NAMES:
            value = getattr(self, name)
            if not math.isfinite(value) or value <= 0:
                raise ValueError(
                    f"{name} deadline must be a positive finite number, got {value!r}"
                )
        # Every non-total limit. A limit above the total is clamped away and so can
        # never be the phase reported — which silently costs the attribution the
        # phase exists to provide. Rejecting one and tolerating the others was just
        # inconsistent.
        for name in _LIMIT_NAMES:
            if name != "total" and getattr(self, name) > self.total:
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
        out a fresh allowance every time — meaning a model emitting a fragment
        every 29 seconds could run indefinitely without ever tripping the deadline
        that exists to measure exactly that.

        Only ``PREPARATION`` and ``TOTAL`` are start-relative. The others are
        anchored to something later in the turn and are deliberately not served
        here: use :meth:`remaining_idle` for a stream gap,
        :meth:`remaining_open_attempt` for one open attempt, and
        :meth:`remaining_first_visible` for the wait after a successful open.
        Asking for them through this method is how the renewal bug happened, so it
        raises rather than quietly returning something plausible.
        """
        if phase is TurnPhase.TOTAL:
            return self.remaining_total()
        if phase is TurnPhase.STREAM_IDLE:
            raise ValueError(
                "stream_idle is measured per gap, not from the start of the turn; "
                "use remaining_idle(since=...)"
            )
        if phase is TurnPhase.FIRST_OUTPUT:
            raise ValueError(
                "first_output is measured from the successful open, not from the "
                "start of the turn; use remaining_first_visible(since=...)"
            )
        if phase is TurnPhase.OPEN_STALL:
            raise ValueError(
                "open_stall is measured per attempt, not from the start of the "
                "turn; use remaining_open_attempt()"
            )
        limit = float(getattr(self.policy, phase.value))
        return max(0.0, min(limit - self.elapsed(), self.remaining_total()))

    def remaining_first_visible(self, since: float) -> float:
        """Seconds left to produce the first VISIBLE token, measured from ``since``.

        ``since`` is when the stream that is now being read was opened — not the
        start of the turn, so an open that had to be retried does not spend this
        allowance before the stream it belongs to exists.

        Cumulative over that stream and NOT renewed by arriving chunks, which is
        the whole point: a per-gap allowance treats any chunk as evidence of
        progress, and an endpoint emitting a role-only delta or a reasoning
        fragment just inside every gap would hold a user answerless until the
        total. Progress the user cannot see is not progress.
        """
        left = float(self.policy.first_output) - (time.monotonic() - since)
        return max(0.0, min(left, self.remaining_total()))

    def remaining_open_attempt(self) -> float:
        """The allowance for ONE stream-open + first-event attempt.

        Per attempt rather than start-relative, which is what makes the retry
        meaningful: a stalled open that consumed the whole allowance must leave the
        next attempt a full one, or the retry is decorative — it starts with zero
        budget and fails instantly. Multiplication is not a risk here the way it is
        for a renewable in-stream limit, because the count is bounded by
        ``MAX_UPSTREAM_RETRIES`` and every attempt is still clamped by the total.
        """
        return max(0.0, min(float(self.policy.open_stall), self.remaining_total()))

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
