"""The entitlement guard on the routes that spend money.

**Identity and entitlement stay separate all the way down.** ``auth.py`` answers
who is calling and deliberately knows nothing about plans — merging the two is how
a billing outage starts presenting as a signed-out user. This module is the join,
and it lives at the ROUTE boundary rather than inside either half, because the one
thing it must guarantee is about ordering: the verdict is reached before any
upstream call is opened and before a single byte is streamed.

**A FastAPI dependency, and that is what buys the ordering.** Dependencies resolve
before the handler body runs, so a refusal happens while there is still nothing to
undo — no provider call in flight, no SSE stream already open with a `meta` event
on it. Calling this from inside `_stream_body` would mean discovering somebody has
no plan after the response had already begun, and the only way to report it would
be a terminal `error` event on a stream that had already promised an answer.

It also resolves exactly ONCE per request, which is the property a hand-rolled
call in two code paths would keep losing.

**Which routes.** Only ``/respond`` and ``/tasks``. Health, version, auth
discovery, account status, capabilities and the runbook catalog are not gated —
none of them reaches a provider, and gating account status in particular would put
the explanation of a refusal behind the refusal. ``/runbooks/resolve`` is
deliberately NOT gated either: runbook selection is a local ONNX classifier and the
route reaches no provider at all, which
``tests/integration/test_entitlement_guard.py`` pins rather than leaves as a
claim.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable

from fastapi import Request

from ..auth import Principal, require_permission
from ..errors import (
    DaintreeError,
    entitlement_unavailable,
    subscription_inactive,
    subscription_required,
)
from ..observability import get_logger
from ..services.bearer import authorization_values, parse_bearer
from ..services.entitlement import Entitlement, EntitlementService
from .deps import request_id

logger = get_logger("daintree.paid_access")

#: The verdict → refusal mapping, and the reason each is its own code. Both are
#: 402s that a refresh cannot fix, and they differ in the REMEDY: a first purchase
#: versus the billing portal. Offering a fresh checkout to somebody whose card
#: merely bounced is how a person ends up paying for two subscriptions.
_REFUSALS: dict[str, Callable[[], DaintreeError]] = {
    "subscription_required": subscription_required,
    "subscription_inactive": subscription_inactive,
}


def require_paid_access(permission: str) -> Callable[[Request], Awaitable[Principal]]:
    """Resolve the caller, check the permission, then check they may spend.

    Wraps ``require_permission`` rather than replacing it, so the identity half
    keeps its own tests and its own failure modes. Built as a factory for the same
    reason that one is: the permission is named at the route, beside the thing it
    protects.
    """
    identity = require_permission(permission)

    async def dependency(request: Request) -> Principal:
        principal = await identity(request)
        await _check(request, principal)
        return principal

    return dependency


async def _check(request: Request, principal: Principal) -> None:
    """Apply this deployment's entitlement posture to one paid request."""
    settings = request.app.state.settings
    mode = settings.entitlement_mode
    if mode == "off":
        # Today's behaviour, unchanged: nothing is asked, nothing is billed for
        # the asking, and no latency is added to a turn's first byte.
        return

    service: EntitlementService | None = getattr(
        request.app.state, "entitlement_service", None
    )
    correlation = request_id(request)

    try:
        verdict = await _resolve(request, principal, service, correlation)
    except DaintreeError as exc:
        if mode == "observe":
            # **Observe must not change the RESPONSE.** That is the mode's whole
            # promise and the property the account-auth suite pins hardest: a
            # deployment measuring entitlement returns the same status, body and
            # headers as one that is not. A refusal escaping here would turn a
            # measurement into an outage.
            logger.info(
                "entitlement_observed",
                request_id=correlation,
                subject_hash=principal.subject_hash,
                result="error",
                code=exc.code,
            )
            return
        raise
    except Exception as exc:  # noqa: BLE001 - see below
        # Anything the client did not already normalise: a bug here, a resolver a
        # test or a future deployment substituted, a failure in the cache
        # bookkeeping that sits outside the fetch's own boundary. Caught in BOTH
        # modes, for two different reasons.
        #
        # In `observe` the mode's promise is that the response does not change;
        # letting a bug escape as a 500 breaks it in the loudest possible way. In
        # `enforce` it is still not the caller's fault, and a 500 would be the
        # wrong SHAPE as well as the wrong blame — the CLI retries a
        # `entitlement_unavailable` and does not retry an internal error, so a
        # transient bug would end a turn that a retry would have completed.
        #
        # `except Exception` deliberately does not catch `CancelledError`, which
        # is a `BaseException` in 3.12: a disconnected client must keep unwinding
        # rather than have its abandonment recorded as a billing outage.
        logger.error(
            "entitlement_gate_failed",
            request_id=correlation,
            mode=mode,
            # The TYPE only — `str(exc)` on an unexpected error can quote
            # anything that was in scope where it was raised.
            error=type(exc).__name__,
        )
        if mode == "observe":
            return
        raise entitlement_unavailable() from None

    if verdict is None:
        # Enforce reaching this means the door is open (no authenticated caller)
        # or no client was built. Readiness refuses both configurations, so it is
        # a wiring failure rather than a caller's problem — and it must fail
        # CLOSED, because the alternative is spending money on a turn nobody
        # checked.
        if mode == "observe":
            logger.info(
                "entitlement_observed",
                request_id=correlation,
                subject_hash=principal.subject_hash,
                result="unresolvable",
            )
            return
        logger.error("entitlement_unresolvable", request_id=correlation)
        raise entitlement_unavailable()

    if mode == "observe":
        logger.info(
            "entitlement_observed",
            request_id=correlation,
            subject_hash=principal.subject_hash,
            result="resolved",
            would_refuse=not verdict.granted,
            **verdict.log_fields(),
        )
        return

    if verdict.granted:
        return

    refusal = _REFUSALS.get(verdict.access)
    if refusal is None:  # pragma: no cover - _ACCESS_VALUES is closed
        # An access value that is neither granted nor a known refusal. The only
        # safe reading is that we do not know, which is a 503 and never a claim
        # about somebody's plan.
        logger.error(
            "entitlement_unknown_access",
            request_id=correlation,
            access=verdict.access,
        )
        raise entitlement_unavailable()
    logger.info(
        "entitlement_refused",
        request_id=correlation,
        subject_hash=principal.subject_hash,
        **verdict.log_fields(),
    )
    raise refusal()


async def _resolve(
    request: Request,
    principal: Principal,
    service: EntitlementService | None,
    correlation: str,
) -> Entitlement | None:
    """The verdict, or ``None`` when this deployment cannot obtain one.

    ``None`` is a wiring state, never an answer about the caller. Every real
    failure — a revoked session, a rate limit, an outage — arrives as its own
    typed error from the client and is not caught here.
    """
    if service is None or not principal.is_authenticated or principal.user_id is None:
        return None
    # Re-extracted here, after the identity dependency has accepted this same
    # request. The token is request material rather than identity, so it is never
    # carried on `Principal` — and it is read through `services.bearer` so two
    # `Authorization` headers cannot forward a different credential than the one
    # that was verified.
    parsed = parse_bearer(authorization_values(request.scope.get("headers") or []))
    if parsed.token is None:
        return None
    return await service.resolve(
        token=parsed.token,
        user_id=principal.user_id,
        oauth_client_id=principal.oauth_client_id,
        request_id=correlation,
        subject_hash=principal.subject_hash,
    )
