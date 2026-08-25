"""``GET /v1/daintree/account`` — who is calling, and what their plan permits.

**Identity-protected, and never a paid route.** It invokes no model, spends
nothing upstream, and is deliberately outside the entitlement guard: somebody with
no subscription must be able to read their own status and discover that. Gating
the one endpoint that explains a refusal behind the refusal itself is a loop with
no exit.

**A status READ.** A known missing or inactive subscription is a 200 whose
``access`` says so. The same fact becomes a typed 402 the moment it stops a paid
request, and the split is deliberate: an error here would force a client to handle
four exception paths to render an ordinary account page.

**It requires a real, authenticated principal, and does not lower that bar to be
callable sooner.** Under ``open`` the ``Authorization`` header is not read at all,
and under ``observe`` a verified caller is deliberately returned as anonymous — so
in both modes there is no subject to answer about, and this route says
``auth_required`` rather than inventing one. The CLI is told when to use it by
``auth/config``'s ``required``, which is the field that exists for exactly this.

**Entitlement is resolved whenever a URL is configured, including while
``ENTITLEMENT_MODE`` is ``off``.** The mode decides whether a verdict may REFUSE a
turn, not whether it can be obtained — so the whole contract can be exercised
against the real website, by a real CLI, before it is able to deny anybody.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse

from ..auth import Principal, authenticate
from ..contracts.account import AccountResponse
from ..errors import DaintreeError, auth_required, entitlement_unavailable
from ..observability import get_logger
from ..services.bearer import authorization_values, parse_bearer
from ..services.entitlement import Entitlement, EntitlementService
from .deps import check_protocol_header, request_id

logger = get_logger("daintree.account")

router = APIRouter()


#: Stamped on EVERY response from this route, not only the 200. The body of a
#: success describes one person's billing state; the errors describe the state of
#: their session. Neither is something an intermediary should hold, and a policy
#: that applies only when things go well is the one that gets forgotten.
_NO_STORE = {"Cache-Control": "private, no-store"}

#: RFC 5321's maximum forward-path length, and the same bound the response model
#: declares. Kept beside the check that USES it so the two cannot drift into a
#: state where the route builds a value the model refuses.
_MAX_EMAIL_LENGTH = 320


async def account_principal(request: Request) -> Principal:
    """Resolve the caller, and require that there actually is one.

    A dependency of its own rather than a check inside the handler, because the
    refusal has to be stamped with this route's cache policy — and an
    ``auth_required`` raised by ``authenticate`` itself never reaches the handler
    to be stamped there. Doing it here catches both shapes: no credential at all,
    and a mode that resolved one to nobody.
    """
    try:
        principal = await authenticate(request)
    except DaintreeError as exc:
        raise _no_store(exc) from None
    if not principal.is_authenticated:
        # Open and observe both land here. Not a failure of the request — a
        # deployment that is not enforcing identity has no authenticated caller to
        # describe, and saying so is more useful than a fabricated anonymous
        # account.
        raise _no_store(auth_required())
    return principal


@router.get("/v1/daintree/account")
async def account(
    request: Request,
    principal: Principal = Depends(account_principal),
    _protocol: None = Depends(check_protocol_header),
) -> JSONResponse:
    """The caller's account, as far as this deployment can establish it.

    **Deliberately not gated on readiness**, and for the reason the token verifier
    is built outside the container in the first place: this route needs the
    verifier and the entitlement client, and neither is part of the model-serving
    machinery. A missing upstream key or a malformed runbook would otherwise make
    "what does my account look like?" answer 503 — turning an unrelated outage
    into an apparent account problem, at exactly the moment someone is trying to
    work out which of the two they have.
    """
    settings = request.app.state.settings
    service: EntitlementService | None = getattr(
        request.app.state, "entitlement_service", None
    )
    correlation = request_id(request)

    if service is None and settings.entitlement_is_configured:
        # Configured, and the client could not be built (see the lifespan's
        # guarded construction). That is a wiring failure, and it must not wear
        # the same 200 as a deployment that deliberately has no billing authority
        # — `unverified` would report the outage as a settled fact about the
        # rollout, and nobody would go looking.
        logger.error("entitlement_service_unavailable", request_id=correlation)
        raise _no_store(entitlement_unavailable())

    entitlement: Entitlement | None = None
    if service is not None:
        # Re-extracted HERE, after the identity dependency has already accepted
        # this same request. The token is never carried on `Principal` — it is
        # request material, not identity — so this is the one place it can be
        # picked up, and it is picked up through `services.bearer` rather than a
        # single-value header read, so two `Authorization` headers cannot let a
        # different credential through than the one that was verified.
        parsed = parse_bearer(authorization_values(request.scope.get("headers") or []))
        if parsed.token is not None:
            assert principal.user_id is not None  # is_authenticated, checked above
            # Failures propagate as themselves. `auth_session_revoked`,
            # `account_rate_limited` and `entitlement_unavailable` are each the
            # exact answer this endpoint owes, and flattening them into
            # `unverified` would tell somebody whose session had just been revoked
            # that their identity is fine and their plan merely unknown.
            try:
                entitlement = await service.resolve(
                    token=parsed.token,
                    user_id=principal.user_id,
                    oauth_client_id=principal.oauth_client_id,
                    request_id=correlation,
                    subject_hash=principal.subject_hash,
                )
            except DaintreeError as exc:
                raise _no_store(exc) from None

    body = _body(principal, entitlement)
    logger.info(
        "account_status",
        request_id=correlation,
        # The correlation id, never the subject. Everything else here is drawn
        # from a closed vocabulary; the email and the raw user id are not logged
        # at all.
        subject_hash=principal.subject_hash,
        access=body.access,
        plan_id=body.plan_id,
        entitlement_configured=settings.entitlement_is_configured,
    )
    return JSONResponse(
        content=body.model_dump(mode="json", exclude_none=True),
        headers={"X-Request-Id": correlation, **_NO_STORE},
    )


def _display_email(email: str | None) -> str | None:
    """The email, or nothing when it is not a plausible one.

    The verifier accepts whatever the signed token carries, and the response model
    caps it at the RFC 5321 maximum — so a token with a longer claim would raise a
    validation error, and the unhandled-exception log would then record Pydantic's
    message, which QUOTES the offending input. An email leaking into logs by way
    of a crash is a poor trade for a display-only field.

    Omitted rather than truncated, which is the same rule the contract already
    states for an absent claim: a half an email address is not a shorter fact, it
    is a wrong one.
    """
    if email is None or len(email) > _MAX_EMAIL_LENGTH:
        return None
    return email


def _no_store(error: DaintreeError) -> DaintreeError:
    """Add the cache policy to an error on its way out.

    `no-store`, not merely `private`. A cached copy of any of these is a stale
    claim about somebody's account, on a route whose whole purpose is to be
    current. Merged rather than assigned so an error's own headers — the
    `WWW-Authenticate` challenge, a `Retry-After` — survive.
    """
    error.headers = {**error.headers, **_NO_STORE}
    return error


def _body(principal: Principal, entitlement: Entitlement | None) -> AccountResponse:
    """Assemble the response, omitting what was never established.

    The entitlement-only fields are absent rather than defaulted when no lookup
    happened. A `plan_id: null` beside `entitlement_stale: false` would read as
    "we checked, you have nothing, and that answer is fresh" — three claims, none
    of them made.
    """
    email = _display_email(principal.email)
    if entitlement is None:
        return AccountResponse(
            email=email,
            subject_hash=principal.subject_hash,
            access="unverified",
        )
    return AccountResponse(
        email=email,
        subject_hash=principal.subject_hash,
        access=entitlement.access,
        plan_id=entitlement.plan_id,
        subscription_status=entitlement.subscription_status,
        entitlement_source=entitlement.source,
        entitlement_stale=entitlement.stale,
        checked_at=entitlement.checked_at,
    )

