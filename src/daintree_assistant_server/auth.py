"""Request identity: who is calling, and what that lets them do.

**The posture is `AUTH_MODE`, and the default is still `open`.** Nothing about
this module changes what a deployment does until somebody sets that variable, and
that is deliberate — every mode below is reachable, tested and inert until chosen.

The three modes are three different jobs, not three settings of one:

``open``
    The ``Authorization`` header is **not read at all**. Not parsed, not verified,
    not rejected. That is stronger than "a credential is optional", and the
    difference is the whole reason the mode still exists: a client mid-migration
    may be sending a credential from the old world, and a door that *read* it
    would have to decide what to do with something it cannot check. Refusing to
    look is what makes an old client work rather than fail confusingly.

``observe``
    A credential that IS present is parsed and verified, and the outcome is
    recorded. **Authorization is unchanged**: the observed identity grants
    nothing, denies nothing, and a request with no token — or a bad one — is
    served exactly as ``open`` would serve it. It exists to produce evidence
    against real traffic before anyone's request depends on the answer, in the
    same shape the fast-response stage's ``shadow`` mode HAD, before that stage went
    single-mode and the mode was deleted.

``enforce``
    A verified credential is required. Failures are the account error family.

**Genuine is not the same as invited.** Website signup is public and deliberate,
so ``enforce`` alone admits every verified account of the allowlisted OAuth
client. A private staging deployment needs the second question, and
``STAGING_ALLOWED_SUBJECT_DIGESTS`` answers it: under ``ENVIRONMENT=staging`` with
``AUTH_MODE=enforce``, a subject the operator did not name is refused with
``auth_permission_denied`` before the entitlement lookup opens and before any
provider call is funded. It is a membership test on a SUBJECT, not a permission on
an OAuth client — ``require_permission`` below answers the other question — and it
is inert in every other environment.

**Identity is not entitlement.** A verified token establishes who is calling. It
does not establish that they have paid, and it does not establish that their
session still exists — nothing here reaches back into a minted JWT to ask, so
``enforce`` ALONE means "a genuine, unexpired token from a session that existed
when it was minted".

The entitlement lookup narrows that second gap where it runs, as a side effect
rather than as a revocation check: ``services/entitlement.py`` maps a 401 from
the website's endpoint — which is specified to perform a fresh Supabase user
check — to ``auth_session_revoked``. That error is raised and returned by the
entitlement path itself (``api/paid_access.py`` and the account endpoint simply
let it propagate); this module's own dispatch table covers ``TokenError`` from
local JWT verification, which is a different failure with the same vocabulary.
So the revocation window is the verdict cache's TTL on the
routes that ask, and ``exp`` on the ones that do not. All three remain separate
questions with separate failure modes, and merging any of them here is how an
outage in one starts presenting as a verdict about another.
"""

from __future__ import annotations

import hashlib
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import datetime
from typing import TYPE_CHECKING
from uuid import UUID

from fastapi import Request

from .errors import (
    DaintreeError,
    auth_client_not_allowed,
    auth_dependency_unavailable,
    auth_permission_denied,
    auth_required,
    auth_session_revoked,
    auth_token_expired,
    auth_token_invalid,
)
from .observability import get_logger
from .services.bearer import BearerOutcome, authorization_values, parse_bearer
from .services.supabase_auth import TokenError

if TYPE_CHECKING:  # pragma: no cover - typing only
    from .services.supabase_auth import SupabaseTokenVerifier

logger = get_logger("daintree.auth")

#: What a caller may do, as a closed vocabulary. Server-owned, and derived from
#: the OAuth client rather than from anything in the token.
#:
#: Supabase has no application-defined scopes, so there is no scope claim to read
#: — but the rule would hold even if there were. A permission the CALLER can ask
#: for is not a permission, and a verifier that honoured one would let the token
#: describe its own authority.
PERMISSION_CHAT = "chat"
PERMISSION_TASKS = "tasks"
PERMISSION_RUNBOOKS_READ = "runbooks:read"

#: Everything the Daintree Assistant client is allowed to do. Granted to any
#: OAuth client that passed the allowlist, which today is exactly one — the
#: allowlist IS the mapping while there is a single client, and this constant is
#: where a second one would earn a narrower set.
#:
#: `runbooks:write` is deliberately absent. The reload route it would gate is
#: mounted only for local runbook authoring and is refused in a hardened
#: deployment twice over already; a permission for it would be the first step
#: toward shipping the route.
ASSISTANT_PERMISSIONS = frozenset(
    {PERMISSION_CHAT, PERMISSION_TASKS, PERMISSION_RUNBOOKS_READ}
)


#: The domain separator the CLI hashes under. Changing either half of this
#: string, or the truncation below, silently stops the two sides agreeing about
#: which account a support report is about — and nothing would fail, because both
#: values are perfectly good hashes of something.
_SUBJECT_HASH_PREFIX = "daintree-assistant-subject:"

#: 8 bytes / 16 hex characters, matching `SubjectHash` in the CLI
#: (`cli/internal/auth/status.go`). Long enough that two accounts will not collide
#: in any realistic support queue, short enough to read aloud, and one-way.
_SUBJECT_HASH_BYTES = 8


def subject_hash(subject: str) -> str:
    """A stable, non-reversible correlation id for a subject.

    Byte-for-byte the CLI's ``auth.SubjectHash``, and pinned against it by test —
    the whole value of the id is that a person can quote the one their CLI printed
    and have it match a log line here.
    """
    if not subject:
        return ""
    digest = hashlib.sha256((_SUBJECT_HASH_PREFIX + subject).encode("utf-8")).digest()
    return digest[:_SUBJECT_HASH_BYTES].hex()


def subject_digest(subject: str) -> str:
    """The FULL digest of a canonical subject — the private-staging gate's key.

    Same domain separator and same hash as :func:`subject_hash`, untruncated. The
    two are not interchangeable and neither may stand in for the other: the
    16-character one is a support correlation id, printed in logs and by the CLI
    and therefore public by design, while this one is the value an operator writes
    into ``STAGING_ALLOWED_SUBJECT_DIGESTS``. Gating on the short one would mean
    membership was decided by a value already published everywhere.

    ``subject`` must ALREADY be canonical — ``str(claims.user_id)``, the ``UUID``
    object's own lowercase hyphenated spelling, never the raw ``sub`` string.
    ``_require_uuid`` compares against ``value.lower()``, so an uppercase-hex
    token is accepted as canonical while spelling itself differently; hashing what
    arrived would produce two digests for one account, and the gate would admit or
    refuse the same person depending on how their token was minted.
    """
    return hashlib.sha256((_SUBJECT_HASH_PREFIX + subject).encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class Principal:
    """The caller. Identity only, and never a credential.

    Carries no token, no refresh token and no provider key. The upstream
    credential is the server's own, resolved inside the provider — it never
    travels on a per-request object, because there is no per-request credential
    for it to travel on.

    ``user_id`` is ``None`` for the anonymous principal the open door produces,
    and that is the one field worth checking rather than inferring: an "anonymous"
    id string, which is what this used to carry, is indistinguishable from a real
    identity to everything downstream that only knows how to compare strings.
    """

    permissions: frozenset[str]
    user_id: UUID | None = None
    session_id: UUID | None = None
    oauth_client_id: str | None = None
    email: str | None = None
    assurance_level: str | None = None
    issued_at: datetime | None = None
    expires_at: datetime | None = None

    @property
    def is_authenticated(self) -> bool:
        return self.user_id is not None

    @property
    def subject_hash(self) -> str:
        """The support correlation id for this caller, or ``""`` when anonymous.

        The one identifier that may travel: stable enough to join two reports
        together, and useless for anything else. Everything that logs or returns
        an identity uses THIS — the raw user id stays on the principal and goes no
        further.
        """
        return subject_hash(str(self.user_id)) if self.user_id is not None else ""

    def has_permission(self, permission: str) -> bool:
        return permission in self.permissions


#: The open door's caller. Holds every permission ON PURPOSE: under ``open`` there
#: is no identity to narrow authority by, so a narrower set would refuse requests
#: this deployment is meant to serve — the mode would stop being "the header is
#: not read" and start being "everyone is a restricted user", which is a different
#: and much worse thing to ship by accident.
ANONYMOUS = Principal(permissions=ASSISTANT_PERMISSIONS)

#: `TokenError.code` is already an account error code, so this is a lookup rather
#: than a translation. Closed and exhaustive: a code with no entry would otherwise
#: fall through to whatever default the `.get` had, and the defaults available
#: here are "let them in" and "log them out", neither of which is safe to reach by
#: accident.
_ERROR_FOR_CODE: dict[str, Callable[[], DaintreeError]] = {
    "auth_token_invalid": auth_token_invalid,
    "auth_token_expired": auth_token_expired,
    "auth_session_revoked": auth_session_revoked,
    "auth_client_not_allowed": auth_client_not_allowed,
    "auth_dependency_unavailable": auth_dependency_unavailable,
}


def _error_for(code: str) -> DaintreeError:
    factory = _ERROR_FOR_CODE.get(code)
    if factory is None:  # pragma: no cover - guarded by test_auth_modes
        logger.error("auth_unmapped_error_code", code=code)
        return auth_token_invalid()
    return factory()


async def authenticate(request: Request) -> Principal:
    """Resolve the caller for this request, according to ``AUTH_MODE``.

    Reads the header through ``services.bearer`` rather than a single-value
    accessor. That is not fussiness: ``Headers.get`` returns the first of two
    ``Authorization`` headers, so a door using it would validate a credential the
    upstream might never have seen.
    """
    settings = request.app.state.settings
    mode = settings.auth_mode

    if mode == "open":
        # Nothing is read. See the module docstring — this is the whole mode.
        return ANONYMOUS

    values = authorization_values(request.scope.get("headers") or [])
    parsed = parse_bearer(values)
    verifier: SupabaseTokenVerifier | None = getattr(
        request.app.state, "token_verifier", None
    )

    if parsed.outcome is BearerOutcome.ABSENT:
        if mode == "observe":
            logger.info("auth_observed", result="absent")
            return ANONYMOUS
        raise auth_required()

    if not parsed.well_formed or parsed.token is None:
        if mode == "observe":
            logger.info("auth_observed", result="malformed", reason=parsed.reason)
            return ANONYMOUS
        # Present but unreadable is NOT `auth_required`: the client did try, and
        # telling it nothing arrived would send someone to a sign-in when their
        # token store handed the request an empty string.
        raise auth_token_invalid()

    if verifier is None:
        # Readiness refuses a non-open mode without complete configuration, so
        # this is a wiring failure rather than a caller's problem — and it must
        # not read as one.
        logger.error("auth_verifier_missing", mode=mode)
        if mode == "observe":
            return ANONYMOUS
        raise auth_dependency_unavailable()

    try:
        claims = await verifier.verify(parsed.token)
    except TokenError as exc:
        if mode == "observe":
            # Metadata only. The claims are unverified by definition on this
            # path, so nothing from the token is logged — a failed verification
            # is exactly when its contents are least trustworthy.
            logger.info("auth_observed", result="rejected", code=exc.code)
            return ANONYMOUS
        raise _error_for(exc.code) from exc
    except Exception as exc:  # noqa: BLE001 - see below
        # An unexpected failure in the verifier, which is a bug rather than a
        # verdict. Caught in BOTH modes, for two different reasons.
        #
        # In `observe` the mode's entire promise is that a caller cannot tell it
        # is on; letting a bug escape as a 500 breaks that promise in the loudest
        # possible way, and turns a measurement into an outage. In `enforce` it is
        # still not the caller's fault, so it is a dependency error rather than a
        # stack trace — the same reasoning that keeps a JWKS outage from reading
        # as an invalid credential.
        #
        # `except Exception` deliberately does not catch `CancelledError`, which
        # is a `BaseException`: a disconnected client must keep unwinding.
        logger.error("auth_verifier_failed", mode=mode, error=type(exc).__name__)
        if mode == "observe":
            return ANONYMOUS
        raise auth_dependency_unavailable() from exc

    if mode == "observe":
        # Verified, and deliberately DISCARDED. Returning the real principal here
        # would make observe mode change authorization, billing attribution and
        # rate limiting — which is enforcement, arriving a release early and
        # without anyone deciding to turn it on.
        logger.info("auth_observed", result="verified")
        return ANONYMOUS

    # The private-staging membership gate. Placed HERE, after every JWT and
    # OAuth-client check and before the principal exists, because everything
    # downstream of this line spends something: `api/paid_access.py` resolves
    # entitlement against the website with the caller's bearer, and past that the
    # turn opens a provider call funded by Daintree's own key. A refusal that
    # arrives here costs a signature verification and nothing else, and there is
    # nothing yet to undo.
    #
    # It is deliberately NOT a permission. `require_permission` asks what an OAuth
    # CLIENT may do and every allowlisted client holds the whole set; this asks
    # whether this deployment was opened to this PERSON, which is a question about
    # a subject and has no answer in the token.
    #
    # `str(claims.user_id)` and never the raw `sub` string: `_require_uuid`
    # compares the claim against `value.lower()`, so an uppercase-hex spelling is
    # accepted as canonical while hashing to a different digest, and the gate would
    # admit or refuse one account depending on how its token was minted.
    #
    # Fails closed by construction: `allowed_subject_digests` returns an empty set
    # for a list that does not parse, and nothing is a member of it. Readiness has
    # already refused such a deployment — this is the same answer given a second
    # time, where a wiring mistake still cannot open the door. The short-circuit
    # is what keeps every other deployment from hashing a subject and parsing a
    # list it has no use for.
    if (
        settings.staging_subject_gate_enabled
        and subject_digest(str(claims.user_id)) not in settings.allowed_subject_digests()
    ):
        # The 16-character support hash and nothing else. The raw subject never
        # leaves the principal, and the full digest is the gate's own key —
        # logging it would publish a membership token to every log reader.
        #
        # The revision goes through `published_gate_revision` rather than being
        # read off the settings: this route is reachable while the service is
        # unready, so a label that failed validation — a digest or a subject id
        # typed into the wrong variable — would otherwise be written into a log
        # line by exactly the mistake that made it unsafe.
        logger.warning(
            "auth_subject_not_allowed",
            subject_hash=subject_hash(str(claims.user_id)),
            gate_revision=settings.published_gate_revision,
        )
        # No message override. The factory's default says a credential this
        # deployment accepts may not do this, which is exactly true, and the
        # message is the only field on this error a value could ride out on.
        raise auth_permission_denied()

    return Principal(
        permissions=ASSISTANT_PERMISSIONS,
        user_id=claims.user_id,
        session_id=claims.session_id,
        oauth_client_id=claims.oauth_client_id,
        email=claims.email,
        assurance_level=claims.assurance_level,
        issued_at=claims.issued_at,
        expires_at=claims.expires_at,
    )


async def require_auth(request: Request) -> Principal:
    """FastAPI dependency: the caller's principal for this request."""
    return await authenticate(request)


def require_permission(permission: str) -> Callable[[Request], Awaitable[Principal]]:
    """A dependency that resolves the caller AND checks one permission.

    Built as a factory so the permission is named at the route, beside the thing
    it protects, rather than in a table somewhere else that has to be kept in step
    with the router.

    Under ``open`` and ``observe`` the anonymous principal holds every permission,
    so this is inert until enforcement is on — which is the point: the check ships
    and is exercised long before it can refuse anybody.
    """

    async def dependency(request: Request) -> Principal:
        principal = await authenticate(request)
        if not principal.has_permission(permission):
            # A verified caller whose client is not permitted this operation. Not
            # a credential problem — refreshing would mint another token with the
            # same authority — so a 403 rather than a 401, and its OWN 403: saying
            # the client is not accepted, when it is, sends whoever reads the log
            # hunting a configuration error that does not exist.
            logger.warning(
                "auth_permission_denied",
                permission=permission,
                client_id=principal.oauth_client_id,
            )
            raise auth_permission_denied()
        return principal

    return dependency
