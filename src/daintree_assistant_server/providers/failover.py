"""Two hosts for one model: Baseten first, OpenRouter when it cannot answer.

Before this, a Baseten outage was a Daintree outage. One host held the only copy
of the executor, so a 5xx, a refused connection or a socket that went quiet
became a failed turn as soon as ``MAX_UPSTREAM_RETRIES`` was spent — and retrying
the same dead host harder is not robustness, it is the same failure taken more
slowly. Nothing about ``zai-org/GLM-5.3-Flash`` requires Baseten specifically;
the same weights are served by twenty other endpoints behind OpenRouter, so the
single point of failure was an accident of how the host was chosen.

:class:`FailoverProvider` is a :class:`~providers.base.ChatProvider` that fronts
two of them. It is transparent above the provider boundary: the orchestrator, the
task runner, the retry layer and both response paths see one provider and one
model, and a caller is never told which host answered.

**Failover is only ever attempted before any model output has been forwarded**,
and that is a structural property rather than a rule this module has to remember.
There are four sites, all of them pre-output: ``complete``,
``structured_tool_call``, ``open_stream``, and the first-event prefetch on the
stream it returns. The first event CLOSES the window (``_can_hand_off``), so a
mid-generation failure stays terminal exactly as it did before — conversation
content has already been delivered and must not be replayed on a second host.

That is a statement about REPLAY, not about health. A late break still counts
against the primary, because a host that keeps dying mid-generation has to become
visible to the breaker even though no individual turn may be rescued.

**What counts as "the host could not answer" is defined by exclusion**, and the
inversion is deliberate. The eligible set is not a list of transient errors: it is
everything EXCEPT the errors that are about our REQUEST — a rejected body, a
prompt past the context window, a runaway generation. Those three would fail
identically on any host, so sending them again is a second bill for the same
answer. Everything else — a 5xx, a timeout, a rate limit, an unparseable
response, a dead credential, an exhausted balance — says something about THIS
host and nothing about the request, and is worth a second opinion. Defining the
small set rather than the large one is what stops a new error code defaulting to
"give up".

## The breaker, and the two halves of "do not overreact"

An outage is not one failed request, and a fallback that engages per-call gets
the worst of both readings. Left alone, every turn during a 10-minute incident
would pay the primary's failure — usually a TIMEOUT, so the connect-or-stall
budget lands in front of every single turn — before reaching the host that was
always going to answer. Tripping on the first failure would do the opposite:
move all traffic to a second host over a blip that has already passed.

:class:`FailoverHealth` is a three-state breaker sized against that:

* **closed** — every call tries the primary. A failure falls back for that call
  and increments a counter; any success resets it to zero. It takes
  ``EXECUTOR_FALLBACK_FAILURE_THRESHOLD`` (3) CONSECUTIVE failures to open, which
  is the "do not overreact to a short outage" half: the turns that hit a blip are
  each still answered by the fallback, and nothing durable changes.
* **open** — the primary is skipped outright and calls go straight to the
  fallback, waiting for nothing. This is the state that makes a long outage cheap
  instead of merely survivable.
* **half-open** — after ``EXECUTOR_FALLBACK_PROBE_INTERVAL_SECONDS`` (180s), the
  next call becomes a single PROBE against the primary. Success closes the
  breaker; failure re-arms another interval. That is the "retry every few
  minutes" half, and the reason it is one call rather than all of them: exactly
  one live turn pays a failed primary attempt per interval, and every other turn
  in flight is unaffected because ``_probe_in_flight`` keeps the second concurrent
  caller on the fallback.

A 10-to-15-minute incident therefore costs: up to three failed-then-recovered
turns to open the breaker, three-to-five probe turns spread across the outage,
and nothing else. Recovery is noticed within one probe interval of it happening.

**State is per-instance and per-process, like the entitlement cache and for the
same reason.** Cloud Run holds no shared state, so a second instance opens its
breaker on its own evidence. That is sound rather than merely tolerable: each
instance's evidence is about the connections IT is making, the cost of being
wrong is bounded by the interval above, and a shared store would buy fewer probes
without shortening the window it takes to notice recovery.
"""

from __future__ import annotations

import asyncio
import contextlib
import time
from collections.abc import Awaitable, Callable
from dataclasses import replace
from enum import Enum
from typing import Any

from pydantic import SecretStr

from ..config import (
    EXECUTOR_FALLBACK_FAILURE_THRESHOLD,
    EXECUTOR_FALLBACK_PROBE_INTERVAL_SECONDS,
    EXECUTOR_FALLBACK_STALL_HOLDOFF_SECONDS,
)
from ..errors import DaintreeError
from ..observability import get_logger
from .base import (
    ChatProvider,
    ProviderChatRequest,
    ProviderChatResult,
    ProviderStream,
    ProviderToolCallResult,
    ProviderUsage,
)

logger = get_logger("daintree.failover")

#: How long to wait for a FAILED primary's stream to close before opening the
#: fallback. Small, because this close talks to the host that just failed and the
#: fallback's open is queued behind it — see `_FailoverStream._hand_off`.
_STALE_CLOSE_SECONDS = 2.0

#: Errors that are about the REQUEST rather than about the host. Sending one of
#: these to a second host produces the same refusal and a second bill, so the
#: fallback is not attempted and the primary's health is not marked down — the
#: host answered, which is exactly what health measures.
#:
#: Everything not named here is eligible. See the module docstring on why the
#: small set is the one that is enumerated.
REQUEST_FAULT_CODES = frozenset(
    {
        # A 4xx that is not about length: a malformed body, an unknown field, a
        # bad tool schema. Almost always a server-side bug in what we sent.
        "upstream_request_rejected",
        # The prompt does not fit. It will not fit on the other host either —
        # both serve the same weights at the same window.
        "context_window_exceeded",
        # We stopped the generation ourselves. Not the host's doing, and
        # unreachable from the failover sites anyway (it is a mid-stream
        # verdict); listed so the classification is complete rather than
        # incidentally correct.
        "upstream_runaway_output",
        # A 403, and the ONE entry here that is not obviously about the request.
        # It is here because the code is AMBIGUOUS in a direction that only
        # resolves one way: it covers model permissions and spend limits, and it
        # also covers a host's own moderation refusal. There is nothing in the
        # status or the body that separates them, so failing over would mean
        # sometimes taking a prompt one host declined to serve and sending it,
        # whole, to a different one. The cost of the safe reading is that a
        # genuine permissions 403 does not fail over — a deterministic,
        # operator-fixable condition that a second host would very likely refuse
        # too. The cost of the unsafe reading is laundering a refusal. Split the
        # code before revisiting this.
        "provider_key_forbidden",
    }
)


def _incomplete(usage: ProviderUsage) -> ProviderUsage:
    """The same usage, marked as no longer a complete account of what was billed.

    A primary attempt that reached the host and then failed may already have been
    charged a prefill it never got to report. The fallback's own record is a
    confident number about the call that WORKED, and publishing it unqualified
    would present a partial sum as an exact one — "zero means free" wearing its
    subtlest hat, which is the failure the whole accounting chain is built to
    avoid. `cost_complete=False` is how that unknown travels.
    """
    return replace(usage, cost_complete=False)


def is_host_fault(exc: DaintreeError) -> bool:
    """Whether ``exc`` says the HOST could not answer, rather than that the
    request was bad. See :data:`REQUEST_FAULT_CODES`."""
    return exc.code not in REQUEST_FAULT_CODES


class PrimaryDecision(Enum):
    """What the breaker decided about one call, before it was made."""

    #: Breaker closed. An ordinary call; failures count toward opening it.
    USE = "use"
    #: Breaker half-open. THIS call is the single probe testing recovery; every
    #: other concurrent caller is on the fallback meanwhile.
    PROBE = "probe"
    #: Breaker open. The primary is not called at all.
    SKIP = "skip"


class FailoverHealth:
    """The three-state breaker described in the module docstring.

    Pure and synchronous. It is only ever touched from the event loop and every
    method returns without awaiting, so the read-decide-write sequences below are
    atomic without a lock — but note the decision and its outcome ARE separated by
    an await in the caller, which is why the probe slot is a held reservation
    (:attr:`_probe_in_flight`) rather than something recomputed from the clock.

    ``clock`` is injectable so tests can drive the probe interval without
    sleeping; it must be a MONOTONIC source, since the only thing measured is an
    elapsed interval and a wall clock stepping backwards would strand the breaker
    open.
    """

    def __init__(
        self,
        *,
        failure_threshold: int = EXECUTOR_FALLBACK_FAILURE_THRESHOLD,
        probe_interval_seconds: float = EXECUTOR_FALLBACK_PROBE_INTERVAL_SECONDS,
        stall_holdoff_seconds: float = EXECUTOR_FALLBACK_STALL_HOLDOFF_SECONDS,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._failure_threshold = max(1, failure_threshold)
        self._probe_interval = max(0.0, probe_interval_seconds)
        self._stall_holdoff = max(0.0, stall_holdoff_seconds)
        #: Until when a STALLED primary is held off, independently of the
        #: breaker. See `note_stall`.
        self._hold_until: float | None = None
        self._clock = clock
        self._consecutive_failures = 0
        #: When the breaker opened, or ``None`` while it is closed. The single
        #: piece of state that says which of the three modes we are in.
        self._opened_at: float | None = None
        self._probe_in_flight = False
        #: Diagnostics only, for the ``/doctor`` posture block. Never read by a
        #: decision.
        self._opened_count = 0

    # ---- state ----------------------------------------------------------

    @property
    def is_open(self) -> bool:
        return self._opened_at is not None

    @property
    def consecutive_failures(self) -> int:
        return self._consecutive_failures

    @property
    def opened_count(self) -> int:
        return self._opened_count

    def seconds_until_probe(self) -> float | None:
        """How long until the primary is tested again, or ``None`` when closed."""
        if self._opened_at is None:
            return None
        return max(0.0, self._probe_interval - (self._clock() - self._opened_at))

    # ---- the decision ---------------------------------------------------

    def before_call(self) -> PrimaryDecision:
        """Decide whether this call tries the primary, reserving the probe slot.

        MUST be paired with exactly one of :meth:`record_success`,
        :meth:`record_failure` or :meth:`release`, or a reserved probe slot is
        never returned and the breaker stops probing entirely.
        """
        # THE STALL HOLDOFF, and it is checked FIRST because it is the one state
        # that has to win against a closed breaker. A stall that has just
        # happened is being retried right now by the layer above, and letting
        # that retry re-enter the same silent host is how the whole feature
        # misses the one outage shape that produces no status code.
        if self._hold_until is not None:
            if self._clock() < self._hold_until:
                return PrimaryDecision.SKIP
            self._hold_until = None
        if self._opened_at is None:
            return PrimaryDecision.USE
        if self._probe_in_flight:
            # Another call is already testing the primary. A second one would
            # double the cost of being wrong about recovery for no extra
            # information.
            return PrimaryDecision.SKIP
        if self._clock() - self._opened_at < self._probe_interval:
            return PrimaryDecision.SKIP
        self._probe_in_flight = True
        return PrimaryDecision.PROBE

    def record_success(self, decision: PrimaryDecision) -> bool:
        """The primary COMPLETED a call. Closes the breaker.

        **"Completed" is stricter than "answered", and the difference is the
        whole reason a stream settles at its end rather than at its first
        frame.** A host can emit a role-only SSE event and then go quiet, or
        write one token and drop, on every single turn — and if the first frame
        settled the verdict, each of those turns would land here, zero the
        counter, and leave trafficselecting a host that has not finished a
        generation in hours. So the streaming path calls this from clean
        termination and :meth:`record_failure` from a late break;
        `_FailoverStream` holds its permit for the life of the stream to make
        that possible.

        Returns whether the breaker was OPEN and is now closed — i.e. whether
        this call is the recovery. Reported rather than inferred because the
        caller cannot ask afterwards: by the time it could look, the state is
        already closed and indistinguishable from a breaker that never opened.
        Recovery is the one transition an operator watching an incident is
        actually waiting for, so it has to be able to reach a log line.
        """
        if decision is PrimaryDecision.SKIP:  # pragma: no cover - defensive
            return False
        recovered = self._opened_at is not None
        # ONLY a probe may release the probe slot. A plain `USE` call that was
        # already in flight when the breaker opened settles LATER than the probe
        # that was reserved after it, and clearing the flag unconditionally lets
        # that stale outcome hand somebody else's reservation back — admitting a
        # second concurrent probe against a host we have just decided not to
        # spend turns on.
        if decision is PrimaryDecision.PROBE:
            self._probe_in_flight = False
        self._consecutive_failures = 0
        self._opened_at = None
        # A primary that answered is a primary that is reachable, so a stall
        # holdoff has been overtaken by evidence and is dropped rather than
        # allowed to keep diverting traffic.
        self._hold_until = None
        return recovered

    def record_failure(self, decision: PrimaryDecision) -> None:
        """The primary could not answer. Opens (or re-arms) the breaker."""
        if decision is PrimaryDecision.SKIP:  # pragma: no cover - defensive
            return
        if decision is PrimaryDecision.PROBE:
            self._probe_in_flight = False
            self._consecutive_failures += 1
            # A failed probe re-arms a FULL interval rather than leaving the
            # breaker immediately probe-eligible again, which would turn every
            # subsequent turn into a probe and undo the whole point of opening.
            self._opened_at = self._clock()
            return
        self._count_failure()

    def note_stall(self) -> None:
        """The primary took the request and produced nothing before the caller's
        clock ran out.

        Counts toward the breaker like any other failure AND holds the primary
        off for a short window. The second half is what actually matters: a stall
        arrives as a cancellation from the caller's own wall clock, the caller
        retries immediately, and without the holdoff that retry re-enters the
        same silent host — so the turn spends its entire budget stalling twice
        while a healthy second host is never called.

        Takes no permit. A stall is observed on the stream rather than at the
        decision that opened it, and the permit that opened it may already have
        been settled; the count is the honest thing to record either way.
        """
        self._hold_until = self._clock() + self._stall_holdoff
        self._count_failure()

    def _count_failure(self) -> None:
        """The shared accounting for a failure that was not a probe."""
        self._consecutive_failures += 1
        if self._opened_at is not None:
            # Already open. Re-arming the interval here would let a slow call
            # that started before the breaker opened push the next probe further
            # out, and bumping `_opened_count` again would report one incident as
            # several.
            return
        if self._consecutive_failures >= self._failure_threshold:
            self._opened_at = self._clock()
            self._opened_count += 1

    def release(self, decision: PrimaryDecision) -> None:
        """Abandon a reserved probe without a verdict.

        For an outcome that is not evidence either way — an abandoned stream, or
        an exception that is not a :class:`DaintreeError` and so is a bug in this
        process rather than a fact about the host.
        """
        if decision is PrimaryDecision.PROBE:
            self._probe_in_flight = False


class FailoverProvider:
    """A :class:`ChatProvider` that tries ``primary``, then ``fallback``.

    Constructed only when a fallback credential exists; ``main._build_provider``
    returns the bare primary otherwise, so there is no "wrapper with nothing to
    fall back to" state to reason about.
    """

    def __init__(
        self,
        primary: ChatProvider,
        fallback: ChatProvider,
        *,
        fallback_model: str,
        primary_name: str,
        fallback_name: str,
        health: FailoverHealth | None = None,
    ) -> None:
        self._primary = primary
        self._fallback = fallback
        self._fallback_model = fallback_model
        self._primary_name = primary_name
        self._fallback_name = fallback_name
        self.health = health if health is not None else FailoverHealth()

    # ---- helpers --------------------------------------------------------

    def _fallback_request(self, request: ProviderChatRequest) -> ProviderChatRequest:
        """The same request, addressed to the other host's id for these weights.

        A COPY, via ``dataclasses.replace``. Mutating in place would be visible to
        everything that re-reads the request afterwards — the retry layer, the
        bare-intent re-roll, both response paths — and would silently pin a turn
        to the fallback model for the rest of its life once it had failed over
        once.

        **``api_key`` is CLEARED, and that is a credential boundary rather than a
        tidy-up.** A per-call key is scoped to the host it was issued for. The
        adapter resolves the request's own key in preference to its configured
        default (``OpenRouterProvider._resolve_key``), so carrying it across
        would take a caller's Baseten credential and send it, as a Bearer token,
        to OpenRouter — disclosing a secret to a third party that has no business
        holding it, on a path nobody is watching because it only runs during an
        outage. Nothing on the serving path pins a key today (the server funds
        every turn from each adapter's configured default), but the field is kept
        live and tested precisely so a future account-mapped credential can
        occupy it, and that is the caller for whom this would be worst.

        Clearing it means the fallback adapter's own configured key pays, which
        is the correct credential for that host and the same one every other
        fallback call uses.
        """
        return replace(request, model=self._fallback_model, api_key=None)

    def _log(
        self,
        event: str,
        *,
        operation: str,
        decision: PrimaryDecision,
        error: DaintreeError | None = None,
        level: str = "warning",
    ) -> None:
        """One shape for every failover log line.

        ``level`` splits two things that are NOT the same event even though they
        look alike. Engaging the fallback is a surprise: something went wrong that
        the reader should see. SKIPPING a primary the breaker has already given up
        on is the mechanism working exactly as designed — and it happens on EVERY
        call for as long as the outage lasts, so at warning it would bury the one
        line that says why underneath minutes of lines that say nothing new.
        """
        getattr(logger, level)(
            event,
            operation=operation,
            primary=self._primary_name,
            fallback=self._fallback_name,
            breaker=("open" if self.health.is_open else "closed"),
            decision=decision.value,
            consecutive_failures=self.health.consecutive_failures,
            # The upstream's own reason is already redacted by the adapter that
            # mapped it (`_extract_upstream_error` scrubs an echoed credential),
            # so only the code travels here — enough to tell a rate limit from a
            # dead socket without putting a message of unknown provenance in a
            # log line.
            error_code=(error.code if error is not None else None),
        )

    def _record_success(self, decision: PrimaryDecision, *, operation: str) -> None:
        """Mark the primary healthy, and SAY SO when that closed the breaker.

        The only transition with no natural log line of its own: a failure logs
        `failover_engaged`, an open breaker logs `failover_primary_skipped` on
        every call, and then the noise simply stops. "It went quiet" is not a
        recovery signal — it is indistinguishable from traffic stopping — so the
        one line an operator is actually waiting for during an incident has to be
        emitted deliberately.
        """
        if self.health.record_success(decision):
            logger.warning(
                "failover_recovered",
                operation=operation,
                primary=self._primary_name,
                fallback=self._fallback_name,
                decision=decision.value,
                opened_count=self.health.opened_count,
            )

    async def _call_with_failover[T](
        self,
        request: ProviderChatRequest,
        call: Callable[[ChatProvider, ProviderChatRequest], Awaitable[T]],
        *,
        operation: str,
    ) -> tuple[T, bool]:
        """Run ``call`` on the primary, then on the fallback if it could not answer.

        The one place the breaker's decide/record contract is honoured, so a new
        operation gets the policy by construction rather than by remembering to
        copy it.

        Returns the result and whether a primary attempt was ABANDONED getting
        there — which is not diagnostics. An attempt that reached the host and
        then failed may already have been billed a prefill it never reported, so
        the caller owes the turn a cost marked incomplete rather than the
        fallback's confident total. A breaker SKIP is deliberately not that: no
        primary request was made, so there is nothing unreported.
        """
        decision = self.health.before_call()
        if decision is PrimaryDecision.SKIP:
            self._log(
                "failover_primary_skipped",
                operation=operation,
                decision=decision,
                level="info",
            )
            return await call(self._fallback, self._fallback_request(request)), False

        try:
            result = await call(self._primary, request)
        except DaintreeError as exc:
            if not is_host_fault(exc):
                # The host ANSWERED — it refused our request. That is a healthy
                # host giving a correct verdict, so it resets the failure count
                # (and closes a half-open breaker) rather than counting against
                # it, and the refusal propagates untouched.
                self._record_success(decision, operation=operation)
                raise
            self.health.record_failure(decision)
            self._log("failover_engaged", operation=operation, decision=decision, error=exc)
        except BaseException:
            # A cancellation, or a bug in this process. Neither is evidence about
            # the host, so the breaker learns nothing — but a reserved probe slot
            # must still come back or probing stops for good.
            self.health.release(decision)
            raise
        else:
            self._record_success(decision, operation=operation)
            return result, False

        return await call(self._fallback, self._fallback_request(request)), True

    # ---- ChatProvider ---------------------------------------------------

    async def complete(self, request: ProviderChatRequest) -> ProviderChatResult:
        result, failed_over = await self._call_with_failover(
            request,
            lambda provider, req: provider.complete(req),
            operation="complete",
        )
        if failed_over:
            result.usage = _incomplete(result.usage)
        return result

    async def structured_tool_call(
        self,
        request: ProviderChatRequest,
        *,
        function_name: str,
        function_schema: dict[str, Any],
    ) -> ProviderToolCallResult:
        result, failed_over = await self._call_with_failover(
            request,
            lambda provider, req: provider.structured_tool_call(
                req, function_name=function_name, function_schema=function_schema
            ),
            operation="structured_tool_call",
        )
        if failed_over:
            result.usage = _incomplete(result.usage)
        return result

    async def open_stream(self, request: ProviderChatRequest) -> ProviderStream:
        """Open on the primary, falling back if the POST itself fails.

        The stream that comes back is wrapped, because opening is only half of
        what can go wrong: a host can accept the connection, send headers, and
        then never complete an SSE event. That failure surfaces at the FIRST-EVENT
        PREFETCH, which is a separate call the orchestrator makes — so
        :class:`_FailoverStream` carries the same policy one step further, to the
        last moment before any model output exists.
        """
        decision = self.health.before_call()
        if decision is PrimaryDecision.SKIP:
            self._log(
                "failover_primary_skipped",
                operation="open_stream",
                decision=decision,
                level="info",
            )
            inner = await self._fallback.open_stream(self._fallback_request(request))
            return _FailoverStream(self, inner, request, decision, on_primary=False)

        try:
            inner = await self._primary.open_stream(request)
        except DaintreeError as exc:
            if not is_host_fault(exc):
                self._record_success(decision, operation="open_stream")
                raise
            self.health.record_failure(decision)
            self._log("failover_engaged", operation="open_stream", decision=decision, error=exc)
        except BaseException:
            self.health.release(decision)
            raise
        else:
            # NOT recorded as a success yet. Headers are not an answer on this
            # path: the prefetch below is what proves the host is producing
            # tokens, and marking the open healthy here is exactly what would
            # make a silently stalling endpoint invisible to the breaker.
            return _FailoverStream(self, inner, request, decision, on_primary=True)

        fallback_stream = await self._fallback.open_stream(self._fallback_request(request))
        return _FailoverStream(self, fallback_stream, request, decision, on_primary=False)

    async def verify_key(self, api_key: SecretStr | None = None) -> dict[str, Any]:
        """Probe the PRIMARY only.

        Readiness and ``/doctor`` ask "is this deployment configured to serve",
        and the answer that matters is about the host that serves. A fallback
        whose credential is bad should not be able to make a healthy deployment
        look broken, and — more importantly — a fallback whose credential is GOOD
        must not be able to make a deployment with a dead primary key look fine.
        ``/doctor`` reports the fallback's presence separately.
        """
        return await self._primary.verify_key(api_key)

    async def aclose(self) -> None:
        # Both, and the primary first only so that a raising close on one still
        # leaves the other closed: the calls are independent.
        try:
            await self._primary.aclose()
        finally:
            await self._fallback.aclose()


class _FailoverStream(ProviderStream):
    """A stream that can still change hosts, but only until its first event.

    Everything after the prefetch is a straight proxy. That is not laziness: once
    a model delta has been forwarded to the client, replaying the turn on another
    host would duplicate or contradict content the user has already seen, which is
    the same reason ``retry_upstream`` stops at the first chunk.
    """

    def __init__(
        self,
        owner: FailoverProvider,
        inner: ProviderStream,
        request: ProviderChatRequest,
        decision: PrimaryDecision,
        *,
        on_primary: bool,
    ) -> None:
        self._owner = owner
        self._inner = inner
        self._request = request
        self._decision = decision
        self._on_primary = on_primary
        # Whether this object's breaker permit has been settled. ONE permit, ONE
        # outcome — and it is HELD FOR THE LIFE OF THE STREAM rather than
        # released at the first frame, which is the load-bearing part.
        #
        # A first frame proves the host is responding; it does not prove it
        # finished a generation. A host that emits a role-only event and then
        # goes quiet, or one token and then drops, would otherwise settle
        # SUCCESSFULLY on every turn — zeroing the counter each time, so the
        # breaker could never open against a host that has not completed a reply
        # in hours. Clean termination settles success, a late break settles
        # failure, and `aclose` settles a release for everything else.
        #
        # That last path is not a nicety: the orchestrator disposes of an open
        # nobody consumed, and a half-open PROBE abandoned that way would hold
        # its reservation forever — the one failure mode of this design that
        # never heals on its own.
        self._settled = False
        # Whether failover is still available. Cleared by the first event: after
        # that, handing off would replay content the user has already seen.
        self._can_hand_off = on_primary
        # Opens THIS object abandoned before yielding anything. Read by the
        # orchestrator and folded into its own count of abandoned opens (see
        # `_hand_off`).
        self.abandoned_attempts = 0

    # `usage` and `endpoint` are read off the stream by the orchestrator after the
    # fact — usage for the turn's accounting, endpoint to name the host in a
    # slow-turn log — so they must follow whichever inner stream is CURRENT rather
    # than being copied at construction, because a handoff replaces it. Written
    # as properties with setters (rather than plain attributes) so the delegation
    # survives a caller that assigns, which the base class's mutable attributes
    # permit.
    @property
    def usage(self) -> ProviderUsage | None:
        return self._inner.usage

    @usage.setter
    def usage(self, value: ProviderUsage | None) -> None:
        self._inner.usage = value

    @property
    def endpoint(self) -> str | None:
        return self._inner.endpoint

    @endpoint.setter
    def endpoint(self, value: str | None) -> None:
        self._inner.endpoint = value

    async def prefetch_first_event(self) -> dict[str, Any] | None:
        # ONE-SHOT. A second call is not a second chance at failover: the first
        # event has been forwarded by then, so handing off would replay a turn
        # whose content the user has already seen. Nothing in the orchestrator
        # calls this twice; the guard is here so the class enforces its own rule
        # rather than depending on its only caller to.
        if not self._can_hand_off:
            return await self._inner.prefetch_first_event()

        try:
            first = await self._inner.prefetch_first_event()
        except DaintreeError as exc:
            self._settled = True
            self._can_hand_off = False
            if not is_host_fault(exc):
                self._owner._record_success(self._decision, operation="prefetch")
                raise
            self._owner.health.record_failure(self._decision)
            self._owner._log(
                "failover_engaged",
                operation="prefetch",
                decision=self._decision,
                error=exc,
            )
            await self._hand_off()
            return await self._inner.prefetch_first_event()
        except asyncio.CancelledError:
            # THE SILENT STALL, and the one judgement call in this module.
            #
            # The orchestrator bounds open-plus-prefetch with `asyncio.timeout`,
            # so a host that sends headers and then goes quiet arrives here as a
            # CANCELLATION rather than as an error we could classify. We cannot
            # hand off from inside a cancellation — there is no awaiting left to
            # do — so the holdoff is what carries the verdict forward: it counts
            # the failure AND keeps the primary out of the caller's immediate
            # retry, which is the attempt that actually reaches the fallback.
            #
            # Without it the breaker sees the stall but nothing acts on it in
            # time: the retry re-enters the same silent host, the turn spends its
            # whole budget stalling twice, and the healthy second host is never
            # called on the one outage shape that produces no status code.
            #
            # The known cost: a client hanging up mid-open is cancelled the same
            # way and is misread as a stall. Bounded and self-correcting — half a
            # minute of traffic on a healthy second host, any primary success
            # clears the holdoff outright.
            self._settled = True
            self._can_hand_off = False
            self._owner.health.release(self._decision)
            self._owner.health.note_stall()
            raise
        except BaseException:
            # NOT evidence about the host. An `AttributeError` or a `TypeError`
            # here is a bug in this process, and counting our own defects against
            # the primary would open the breaker on a code path that would fail
            # identically against the fallback.
            self._settled = True
            self._can_hand_off = False
            self._owner.health.release(self._decision)
            raise
        else:
            # Deliberately NOT settled. The first frame closes the failover
            # window, and nothing more: see `_settled` above on why "the host
            # responded" is not "the host completed a generation".
            self._can_hand_off = False
            return first

    async def __anext__(self) -> dict[str, Any]:
        try:
            return await self._inner.__anext__()
        except StopAsyncIteration:
            # THE stream finished. This is the only evidence that actually says
            # the primary works, so it is what closes a breaker and zeroes the
            # failure count.
            if self._on_primary and not self._settled:
                self._settled = True
                self._owner._record_success(self._decision, operation="stream")
            raise
        except DaintreeError as exc:
            # A LATE failure. The turn is terminal either way — content the user
            # has already seen must never be replayed on a second host — but that
            # is a fact about THIS turn, not about the primary, and a host that
            # keeps dying mid-generation has to become visible to the breaker.
            if self._on_primary and not self._settled and is_host_fault(exc):
                self._settled = True
                self._owner.health.record_failure(self._decision)
            raise

    async def _hand_off(self) -> None:
        """Close the primary's half-open stream and open one on the fallback.

        The close comes FIRST and is unconditional: the abandoned stream holds a
        pooled connection to a host that has just failed, and leaking it under an
        outage is how a connection pool empties.
        """
        stale = self._inner
        # BOUNDED, and the bound is the point. This close talks to the host that
        # just failed, and the fallback's open is queued behind it — so a close
        # that hangs on the same dead socket spends the caller's whole open
        # deadline and OpenRouter is never reached at all, which is the failure
        # this method exists to prevent. `TimeoutError` is caught alongside the
        # rest: an unclosed connection to a failing host is a smaller problem
        # than a turn that never reaches the host that works.
        with contextlib.suppress(Exception):  # pragma: no cover - already failing
            await asyncio.wait_for(stale.aclose(), timeout=_STALE_CLOSE_SECONDS)
        # An open attempt that reached the host and produced nothing usable may
        # still have been billed a prefill it never got to report, so the turn's
        # cost is no longer known to be complete. The orchestrator reads this and
        # marks it, exactly as it does for an attempt its own retry loop
        # abandoned.
        self.abandoned_attempts += 1
        # Opened into a local and assigned with `_on_primary` in one step. If the
        # fallback's open raises, this object is left holding the closed primary
        # stream and STILL flagged as being on the primary — which is the state a
        # second `prefetch_first_event` would handle correctly, rather than one
        # that quietly reads a dead stream with failover already spent. The
        # exception propagates either way; this just keeps the two fields from
        # disagreeing in between.
        opened = await self._owner._fallback.open_stream(
            self._owner._fallback_request(self._request)
        )
        self._inner = opened
        self._on_primary = False

    def __aiter__(self) -> _FailoverStream:
        return self

    async def aclose(self) -> None:
        """Close the current inner stream, and settle an unresolved permit.

        The second half is the one that matters. A stream can end without a
        verdict in more ways than is obvious: the orchestrator disposes of an
        open nobody consumed (`_abandon_open`), a client disconnects mid-reply,
        the runaway guard breaks out of the loop. In every one of those a
        half-open probe would otherwise hold its reservation forever, leaving
        every later request skipping a primary that may have recovered hours ago.

        RELEASED rather than recorded, in both directions: a stream that was
        abandoned is not evidence that the host failed, and it is not evidence
        that it succeeded either — nobody waited to find out.
        """
        if not self._settled:
            self._settled = True
            self._owner.health.release(self._decision)
        await self._inner.aclose()
