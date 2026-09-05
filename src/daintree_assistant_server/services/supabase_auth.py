"""Verifying a Supabase access token, locally, against a cached JWKS.

**Locally is the design.** With asymmetric signing keys a signature check is
arithmetic over bytes we already hold, so asking the identity provider about every
request would add a network round trip to the first-byte path of every turn to
learn something we can compute. The only thing we cannot compute is whether the
session behind the token still exists — logout does not reach back into an already
minted JWT — and that is a separate check with its own cache, deliberately not
folded in here (``TODO.md``).

**Everything about what a valid token looks like is hardcoded**, and only the
deployment's identity is configured. The audience, the accepted algorithms, the
clock skew, the required claims: each has one right answer we have decided, and an
environment variable over any of them is a way to weaken verification from outside
the code review that should have approved it. What varies per environment — which
Supabase project, which OAuth client — lives in ``Settings``.

The verifier is built once and held for the process. It is **owned by the app
rather than by the service container**, because ``/v1/daintree/doctor``
deliberately answers when the container failed to build, and an authenticated
diagnostic that cannot authenticate during exactly that outage is the diagnostic
being unavailable when it is needed.
"""

from __future__ import annotations

import asyncio
import json
import time
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any
from uuid import UUID

import httpx
import jwt
from jwt import PyJWK, PyJWKSet

from ..observability import get_logger

logger = get_logger("daintree.auth.supabase")

#: The only signature algorithms accepted, and a closed list rather than anything
#: read from the token. A JWT names its own algorithm in an unprotected header, so
#: a verifier that honoured that field would let the token choose how it is
#: checked — the algorithm-confusion family, of which `alg: none` is only the
#: bluntest. Both entries are asymmetric on purpose: an HMAC algorithm here would
#: mean a leaked verification key could also MINT tokens.
ACCEPTED_ALGORITHMS = ("RS256", "ES256")

#: Every Supabase access token carries this audience. Hardcoded because it is a
#: property of the provider's tokens rather than of our deployment.
EXPECTED_AUDIENCE = "authenticated"

#: Tolerance for clock drift between this host and the identity provider, applied
#: to `exp`, `iat` and `nbf`. Small enough that an expired token is not usefully
#: extended, large enough that an unsynchronised container does not reject every
#: login.
CLOCK_SKEW_SECONDS = 60

#: How long a fetched key set is trusted before a refresh is considered. JWKS is
#: public, cacheable data and rotation is measured in weeks, so this is a bound on
#: staleness rather than a cache tuned for hit rate.
JWKS_CACHE_SECONDS = 600

#: Shortest gap between two JWKS fetches. The unknown-`kid` path forces a refresh,
#: and without this a stream of tokens naming keys that do not exist — which is
#: what a misconfigured client, or someone probing, produces — becomes a request
#: amplifier pointed at the identity provider.
JWKS_MIN_REFRESH_SECONDS = 10

#: A JWKS document is a handful of public keys. Anything larger is not one.
MAX_JWKS_BYTES = 256 * 1024

# Bound the whole exchange, including a peer that keeps sending small chunks
# inside httpx's per-read timeout. Refresh holds the shared authentication lock.
JWKS_FETCH_TIMEOUT_SECONDS = 5.0

#: How long a key set may go WITHOUT a successful refresh and still be trusted.
#:
#: The warm cache deliberately survives an outage — a blip at the identity
#: provider must not sign out every user at once — but "survives an outage" and
#: "is trusted forever" are different promises, and only the first one is safe. A
#: provider retires a key precisely when it should stop verifying tokens, and a
#: cache that never gives up would keep honouring it for as long as the fetch kept
#: failing, which is exactly the window an attacker holding a retired key wants.
#:
#: Twenty-four hours: long enough that no realistic outage locks anyone out,
#: short enough to be a bound rather than a formality.
JWKS_MAX_STALE_SECONDS = 24 * 60 * 60


class TokenError(Exception):
    """Verification failed. ``code`` names WHICH account error this becomes.

    The message is for our logs. It is never the text a caller sees: the mapping
    to a response goes through the error factories, whose wording is deliberately
    uniform so the door cannot be used to discover what this deployment accepts.
    """

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


@dataclass(frozen=True)
class VerifiedClaims:
    """What a verified token establishes. Identity only — never entitlement.

    A valid signature proves who is calling. It does not prove they have paid, and
    it does not prove the session still exists; both are separate questions with
    separate failure modes, and merging them here is how an outage in one starts
    presenting as a verdict about the other.
    """

    user_id: UUID
    session_id: UUID
    oauth_client_id: str
    email: str | None
    assurance_level: str
    issued_at: datetime
    expires_at: datetime


@dataclass
class JwksCache:
    """The signing keys, with a bounded refresh.

    Not a general cache: it holds one key set, because there is one issuer.
    """

    url: str
    client: httpx.AsyncClient
    ttl_seconds: float = JWKS_CACHE_SECONDS
    min_refresh_seconds: float = JWKS_MIN_REFRESH_SECONDS
    max_stale_seconds: float = JWKS_MAX_STALE_SECONDS
    _keys: dict[str, PyJWK] = field(default_factory=dict)
    _fetched_at: float = 0.0
    _last_attempt_at: float = 0.0
    #: Serialises refreshes. Without it, concurrent requests arriving during a
    #: rotation all miss the cache, all enter the refresh, and all but the first
    #: are turned away by the anti-amplification window — then find the key still
    #: absent and report the token INVALID. Measured before this existed: of five
    #: concurrent verifies of one genuine token, one succeeded and four were told
    #: their credential was bad. A wrong verdict, not a slow one, and exactly the
    #: shape of failure a cold start or a key rotation produces.
    _lock: asyncio.Lock = field(default_factory=asyncio.Lock)

    @property
    def age_seconds(self) -> float | None:
        """Seconds since the last successful fetch, or ``None`` if never fetched.

        Surfaced by ``/doctor``: a key set that has stopped refreshing is invisible
        until the day it is rotated, and then it is invisible as "every login
        suddenly fails".
        """
        if not self._fetched_at:
            return None
        return time.monotonic() - self._fetched_at

    async def get(self, kid: str) -> PyJWK:
        """The key named by ``kid``, fetching or refreshing at most as often as allowed.

        The unknown-``kid`` path is **key rotation**, not a retry loop. A provider
        that has rotated serves tokens signed by a key our cache predates, so
        exactly one forced refresh turns a token we would wrongly reject into one
        we correctly accept. Beyond that, an unknown ``kid`` means the token is not
        ours, and re-fetching on each one would let a stream of invented ids
        hammer the provider on our behalf.

        Which failure it raises turns on whether we actually MANAGED to ask. A
        refresh that ran and did not produce the key says something about the
        token; one that was suppressed or failed says only that we could not
        check, and reporting those alike would tell a client its credential was
        bad on the strength of our own outage.

        Crossing the HARD bound is a reason to distrust the keys we hold, not a
        reason to stop asking for new ones. Those two rules were one for as long
        as this method answered the over-age case before the refresh path, and
        the result was that an over-age cache could never recover: `_fetched_at`
        moves only inside :meth:`_refresh`, so the one call able to clear the
        condition was the one call the condition prevented. A provider that came
        back healthy stayed locked out until somebody restarted the process,
        which is not a failure mode an operator can diagnose from the outside —
        every request answers `auth_dependency_unavailable`, exactly as it does
        during the outage that caused it.
        """
        # Read here and re-read twice below. Every `return` on this path is
        # guarded by it, so an over-age key is never served no matter which
        # branch reaches the caller — the bound is enforced at the point of USE
        # rather than by refusing to enter the function.
        over_age = self._too_stale_to_trust()
        key = self._keys.get(kid)
        if key is not None and not over_age and not self._stale():
            return key
        async with self._lock:
            # Re-checked INSIDE the lock. Whoever held it may have just fetched
            # the very key this caller is waiting for, and without this second
            # look every queued request would go on to refresh again — or be
            # refused by the rate limiter for a key that is now present. The age
            # is re-read with it: a caller that queued while the cache was over
            # its bound must see the recovery that whoever held the lock just
            # performed, rather than acting on the reading it took outside.
            over_age = self._too_stale_to_trust()
            key = self._keys.get(kid)
            if key is not None and not over_age and not self._stale():
                return key
            refreshed = await self._refresh()
            # `refreshed`, not the clock, is what clears the bound here. A
            # successful fetch replaced the key set moments ago, so it is fresh
            # BY CONSTRUCTION, and re-reading the age would instead ask whether
            # the keys we just received are already too old — which at a max age
            # of zero they trivially are. That reading turns a deployment that
            # trusts keys briefly into one that cannot verify anything at all,
            # rejecting its own fetch on the way out of the call that made it.
            over_age = not refreshed and self._too_stale_to_trust()
            key = self._keys.get(kid)
        if key is not None and not over_age:
            return key
        # Still over its bound means the refresh did not happen: it failed, or
        # the anti-amplification window suppressed it. A key with this id may
        # well be sitting in `_keys` — it is simply too old to honour, and the
        # guard above is the branch that says so instead of serving it.
        if refreshed:
            # We asked the issuer and it does not publish this key. That is a
            # statement about the TOKEN.
            raise TokenError("auth_token_invalid", "no signing key matches the token's kid")
        # We did NOT manage to ask — the fetch failed, or the anti-amplification
        # window suppressed it. The key may well exist. Saying the credential is
        # invalid here would be a verdict we have not earned, and a client acting
        # on it discards a session that was never in question; this is precisely
        # the distinction the 503 codes exist to preserve.
        raise TokenError(
            "auth_dependency_unavailable", "the signing keys could not be consulted"
        )

    def _stale(self) -> bool:
        """Due a refresh. Still usable in the meantime."""
        return not self._fetched_at or (time.monotonic() - self._fetched_at) > self.ttl_seconds

    def _too_stale_to_trust(self) -> bool:
        """Past the point where a key we hold may still be honoured.

        Distinct from :meth:`_stale`, which only means "worth re-fetching". This
        one is the security boundary, and crossing it fails CLOSED.
        """
        if not self._fetched_at:
            return False  # never fetched: the refresh below decides
        return (time.monotonic() - self._fetched_at) > self.max_stale_seconds

    async def _refresh(self) -> bool:
        """Whether the issuer was successfully consulted."""
        now = time.monotonic()
        if self._last_attempt_at and (now - self._last_attempt_at) < self.min_refresh_seconds:
            return False
        self._last_attempt_at = now
        try:
            # `follow_redirects` explicitly, not by relying on the client's
            # default: this client is injected, and a redirect here would fetch
            # signing keys from a host the issuer does not name.
            async with asyncio.timeout(JWKS_FETCH_TIMEOUT_SECONDS):
                async with self.client.stream("GET", self.url, follow_redirects=False) as response:
                    response.raise_for_status()
                    declared = response.headers.get("content-length")
                    if declared and declared.isdigit() and int(declared) > MAX_JWKS_BYTES:
                        raise ValueError("jwks document announces an implausible size")
                    body = bytearray()
                    async for chunk in response.aiter_bytes():
                        if len(body) + len(chunk) > MAX_JWKS_BYTES:
                            raise ValueError("jwks document is implausibly large")
                        body.extend(chunk)
                    document = json.loads(body)
            key_set = PyJWKSet.from_dict(document)
        except Exception as exc:
            # A fetch failure is NOT a verdict about the token. Keys we already
            # hold stay usable — the alternative is that a blip at the identity
            # provider logs out every signed-in user at once — and a request that
            # needs a key we do not have becomes a dependency error rather than an
            # invalid credential.
            logger.warning("jwks_refresh_failed", error=type(exc).__name__)
            if not self._keys:
                raise TokenError(
                    "auth_dependency_unavailable", "the signing keys could not be fetched"
                ) from exc
            return False
        keys: dict[str, PyJWK] = {}
        # Tracked separately rather than by absence from `keys`: deleting on the
        # second occurrence and testing membership again lets a THIRD key with the
        # same id be accepted, which is the same order-dependence the rule exists
        # to remove, just further along the list.
        duplicated: set[str] = set()
        for key in key_set.keys:
            kid = key.key_id
            if not kid:
                continue
            if kid in duplicated:
                continue
            if kid in keys:
                # Two keys claiming one id: which one verifies a token would be a
                # function of iteration order. All of them are dropped rather than
                # one being picked.
                del keys[kid]
                duplicated.add(kid)
                continue
            keys[kid] = key
        if duplicated:
            logger.warning("jwks_duplicate_kid", count=len(duplicated))
        if not keys:
            logger.warning("jwks_refresh_empty")
            if not self._keys:
                raise TokenError(
                    "auth_dependency_unavailable", "the signing key set was empty"
                )
            return False
        self._keys = keys
        self._fetched_at = now
        logger.info("jwks_refreshed", keys=len(keys))
        return True


class SupabaseTokenVerifier:
    """Turns a bearer credential into :class:`VerifiedClaims`, or raises.

    One instance per process, built during lifespan. The JWKS client is shared and
    async: PyJWK's own ``PyJWKClient`` is synchronous, and calling it from here
    would block the event loop for the duration of a key fetch — on the first-byte
    path of whatever request happened to trigger the rotation.
    """

    def __init__(
        self,
        *,
        issuer: str,
        allowed_client_ids: frozenset[str],
        jwks: JwksCache,
    ) -> None:
        self._issuer = issuer
        self._allowed_client_ids = allowed_client_ids
        self._jwks = jwks

    @property
    def jwks_age_seconds(self) -> float | None:
        return self._jwks.age_seconds

    async def verify(self, token: str) -> VerifiedClaims:
        """Verify a token completely, or raise :class:`TokenError`.

        Order matters and is not incidental: the SIGNATURE is checked before any
        claim is read or logged. Claims from an unverified token are attacker-
        controlled strings, and a verifier that logged the `sub` it was about to
        check would be writing whatever it was handed into the log pipeline.
        """
        try:
            header = jwt.get_unverified_header(token)
        except jwt.PyJWTError as exc:
            raise TokenError("auth_token_invalid", "the token header is unreadable") from exc

        algorithm = header.get("alg")
        if algorithm not in ACCEPTED_ALGORITHMS:
            # Read only to SELECT a key, never to choose the algorithm: the
            # allowlist below is what `decode` is given.
            raise TokenError("auth_token_invalid", "unsupported signature algorithm")
        kid = header.get("kid")
        if not isinstance(kid, str) or not kid:
            raise TokenError("auth_token_invalid", "the token names no signing key")

        key = await self._jwks.get(kid)
        try:
            claims: dict[str, Any] = jwt.decode(
                token,
                key=key.key,
                algorithms=list(ACCEPTED_ALGORITHMS),
                issuer=self._issuer,
                audience=EXPECTED_AUDIENCE,
                leeway=CLOCK_SKEW_SECONDS,
                options={
                    "require": ["exp", "iat", "sub", "aud", "iss"],
                    "verify_signature": True,
                    "verify_exp": True,
                    "verify_iat": True,
                    "verify_nbf": True,
                    "verify_aud": True,
                    "verify_iss": True,
                },
            )
        except jwt.ExpiredSignatureError as exc:
            # The one verification failure a client can fix silently, so it is the
            # one worth telling apart.
            raise TokenError("auth_token_expired", "the token has expired") from exc
        except jwt.PyJWTError as exc:
            raise TokenError(
                "auth_token_invalid", f"verification failed: {type(exc).__name__}"
            ) from exc
        except (TypeError, OverflowError, ValueError) as exc:
            # PyJWT compares the temporal claims arithmetically and does not type
            # them first, so a signed token carrying `"exp": null`, `"exp": []` or
            # `"exp": Infinity` raises out of `decode` as a plain TypeError or
            # OverflowError — past every `except PyJWTError` and out of the request
            # as a 500. Measured: three of four malformed shapes did exactly that.
            #
            # Not an authentication bypass (the signature still had to verify), but
            # a 500 is the wrong answer to a bad token, and on an unauthenticated
            # path it is a free way to make the server produce stack traces.
            raise TokenError(
                "auth_token_invalid", f"malformed claim: {type(exc).__name__}"
            ) from exc

        return self._read_claims(claims)

    def _read_claims(self, claims: dict[str, Any]) -> VerifiedClaims:
        """Type-check what the signature has established.

        PyJWT's ``require`` proves a claim is PRESENT, not that it holds the type
        we expect — and `True` is an `int` in Python, so a `sub` of `true` would
        satisfy a naive numeric check. Every field is therefore checked for its
        actual type here rather than trusted because the signature verified: a
        genuine token from a misconfigured project is still a token we cannot
        build a principal from.
        """
        if claims.get("role") != "authenticated":
            raise TokenError("auth_token_invalid", "the token is not an authenticated role")
        if claims.get("is_anonymous") is not False:
            # `is not False` rather than `not ...`: an absent claim, `None`, or a
            # string would all pass a falsy test, and an anonymous session must
            # never reach a paid path.
            raise TokenError("auth_token_invalid", "anonymous sessions are not accepted")

        client_id = claims.get("client_id")
        if not isinstance(client_id, str) or not client_id:
            raise TokenError("auth_token_invalid", "the token names no OAuth client")
        if client_id not in self._allowed_client_ids:
            # A VALID, current token from a client this deployment does not accept.
            # Its own code, because refreshing cannot help: every refresh mints
            # another token wrong in exactly the same way.
            raise TokenError("auth_client_not_allowed", "the OAuth client is not allowlisted")

        user_id = _require_uuid(claims.get("sub"), "sub")
        session_id = _require_uuid(claims.get("session_id"), "session_id")

        email = claims.get("email")
        if email is not None and not isinstance(email, str):
            raise TokenError("auth_token_invalid", "the email claim is not a string")

        assurance_level = claims.get("aal")
        if assurance_level is not None and not isinstance(assurance_level, str):
            raise TokenError("auth_token_invalid", "the aal claim is not a string")

        return VerifiedClaims(
            user_id=user_id,
            session_id=session_id,
            oauth_client_id=client_id,
            email=email,
            assurance_level=assurance_level or "aal1",
            issued_at=_require_timestamp(claims.get("iat"), "iat"),
            expires_at=_require_timestamp(claims.get("exp"), "exp"),
        )


def _require_uuid(value: object, name: str) -> UUID:
    """A canonical UUID, or a refusal.

    Canonical spelling is required rather than merely parseable. ``UUID()``
    accepts braces, URNs and a bare 32-hex string, so the same identity has
    several spellings — and an id that is a database key and a cache key must have
    exactly one, or two spellings of one user become two users to anything that
    compares strings. The nil UUID is refused as an identity nobody has.
    """
    if not isinstance(value, str):
        raise TokenError("auth_token_invalid", f"the {name} claim is not a string")
    try:
        parsed = UUID(value)
    except ValueError as exc:
        raise TokenError("auth_token_invalid", f"the {name} claim is not a UUID") from exc
    if str(parsed) != value.lower():
        raise TokenError("auth_token_invalid", f"the {name} claim is not a canonical UUID")
    if parsed.int == 0:
        raise TokenError("auth_token_invalid", f"the {name} claim is the nil UUID")
    return parsed


def _require_timestamp(value: object, name: str) -> datetime:
    """A NumericDate, as a timezone-aware datetime.

    ``bool`` is rejected explicitly because it is a subclass of ``int``: without
    that, a claim of ``true`` becomes the epoch plus one second and a token would
    carry a perfectly well-typed nonsense expiry.
    """
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise TokenError("auth_token_invalid", f"the {name} claim is not a timestamp")
    try:
        return datetime.fromtimestamp(value, tz=UTC)
    except (OverflowError, OSError, ValueError) as exc:
        raise TokenError("auth_token_invalid", f"the {name} claim is out of range") from exc
