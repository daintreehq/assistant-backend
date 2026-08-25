"""What the caller's account is entitled to, asked of the website.

**Identity and entitlement are two questions with two failure modes, and this
module owns only the second.** ``auth.py`` establishes that a token is genuine;
this establishes that the account behind it has paid. Nothing here re-verifies a
signature and nothing here decides who is calling — it is handed an already
verified principal and the bearer that produced it, and it asks one HTTP question
about that subject.

**The bearer is transient request material and never becomes state.** It is not
on ``Principal``, not on the verdict, not in a cache key, not in a log line and
not in an exception. It is re-extracted from the request AFTER the identity
dependency has accepted that same request, forwarded once to the one configured
origin, and dropped. The cache is keyed by ``(user_id, oauth_client_id)`` —
verified claims rather than credential text — so two tokens for the same session
share an entry and a leaked key never becomes a lookup handle.

**A dependency failure is never a verdict.** Every uncertain outcome resolves to
``entitlement_unavailable`` (503, retryable), and the one translation this module
refuses to make is uncertainty into ``subscription_required``: telling a paying
customer to buy a second plan during a billing outage is the single worst thing
the account taxonomy exists to prevent. The website's own 401 IS informative,
though, and is the piece the local JWKS check cannot supply: the website performs
a fresh Supabase user check, so a token that verified here and is refused there
is a session that no longer exists — ``auth_session_revoked``, which the CLI
already maps to "delete the local credential".

**The cache is the revocation window, so it is small and explicit.** A granted
answer is held for at most 60 seconds and a known-missing one for at most 15;
nothing uncertain is held at all. Any rejection purges the subject's entry
outright, so the window closes on the first refusal rather than at expiry.

It is per-INSTANCE and in-memory on purpose, and the bound survives that. Every
entry anywhere is at most its TTL old, so "a revoked grant can be honoured for up
to 60 seconds" is true whether one instance is warm or ten are — the instance
count changes how many lookups are made, never how stale the oldest usable answer
is. A shared cache (Redis) would therefore cut request volume against the website;
it would not narrow this window, which is why it is not a prerequisite for
turning enforcement on.
"""

from __future__ import annotations

import asyncio
import json
import re
import time
from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta
from email.utils import parsedate_to_datetime
from typing import Any, Literal, cast
from uuid import UUID

import httpx

from ..errors import (
    DaintreeError,
    account_rate_limited,
    auth_client_not_allowed,
    auth_permission_denied,
    auth_session_revoked,
    entitlement_unavailable,
)
from ..observability import get_logger
from ..text_utils import sanitize_line

logger = get_logger("daintree.entitlement")

#: The contract version this client understands. A body announcing anything else
#: is `entitlement_unavailable` rather than a best-effort read: the fields are the
#: input to a spend decision, and guessing at a shape we were told we do not know
#: is exactly where a silent misread becomes a wrong refusal.
ENTITLEMENT_CONTRACT_VERSION = 1

#: One small JSON call. The whole budget, not a per-phase one — a paid request is
#: waiting behind it, and a lookup that takes longer than this has already cost
#: more than the answer is worth.
ENTITLEMENT_TIMEOUT_SECONDS = 5.0
ENTITLEMENT_CONNECT_TIMEOUT_SECONDS = 2.0

#: Refuse to hold more than this before decoding. The real body is a few hundred
#: bytes; the cap is what stops a misconfigured URL that returns an HTML page —
#: or a hostile one that streams forever — from being read into memory on the
#: first-byte path of every paid turn.
ENTITLEMENT_RESPONSE_MAX_BYTES = 64 * 1024

#: How long a verdict may be reused, by kind. The positive TTL IS the revocation
#: window on a warm instance, which is why it is the smaller-looking number of the
#: two decisions here rather than the convenient one. The negative TTL is shorter
#: still: somebody who has just paid should not wait out a cache to start working,
#: and a missing plan is the state most likely to change in the next few seconds.
ENTITLEMENT_GRANTED_TTL_SECONDS = 60.0
ENTITLEMENT_DENIED_TTL_SECONDS = 15.0

#: Bound what a `Retry-After` may ask of us. The header is upstream-controlled and
#: reaches the client as authoritative — the CLI replaces its own jittered
#: schedule with a server-supplied value — so an implausible number would pin a
#: caller for its duration. Anything outside this range is dropped rather than
#: clamped: a value we do not believe is not evidence of a better one.
_MAX_RETRY_AFTER_SECONDS = 300

#: How far into the future a `checked_at` may sit before the answer is treated as
#: incoherent. Clock skew between two hosts is seconds; minutes is a bug.
_MAX_CHECKED_AT_SKEW = timedelta(minutes=5)

#: And how far back. An answer stamped days ago is not a fresh billing check,
#: whatever `stale` says about it.
_MAX_CHECKED_AT_AGE = timedelta(days=1)

#: RFC 3339, and nothing else. ``datetime.fromisoformat`` is much more permissive
#: — it accepts ISO week dates and basic-format strings this contract does not
#: define — so the shape is pinned before it is parsed. A timestamp is an input to
#: a freshness decision, and "we parsed something" is not the same as "the
#: dependency sent what it agreed to send".
_RFC3339 = re.compile(
    r"^\d{4}-\d{2}-\d{2}[Tt ]\d{2}:\d{2}:\d{2}(\.\d+)?([Zz]|[+-]\d{2}:\d{2})$"
)

#: How many verdicts one instance may hold. Expiry alone is NOT a bound: an entry
#: is examined only when its own key comes back, so a process serving many
#: distinct callers keeps every dead entry for its whole life. Eviction here is
#: safe in a way most caches' is not — the worst case is one extra lookup — so the
#: limit can be small enough to be obviously harmless.
ENTITLEMENT_CACHE_MAX_ENTRIES = 4096

_ACCESS_VALUES = frozenset({"granted", "subscription_required", "subscription_inactive"})
_PLAN_VALUES = frozenset({"standard", "pro"})
_SOURCE_VALUES = frozenset({"polar", "cache"})

#: Bounded provider metadata. It is diagnostic — nothing branches on it — but it
#: is rendered by the CLI and logged here, so it is capped like any other
#: upstream-controlled string.
_MAX_STATUS_LENGTH = 64

Access = Literal["granted", "subscription_required", "subscription_inactive"]


class EntitlementContractError(Exception):
    """The website answered, and the answer is not one we can act on.

    Separate from :class:`~..errors.DaintreeError` because it names a fault in the
    dependency rather than a verdict about the caller. Every caller maps it to
    ``entitlement_unavailable``; keeping it a distinct type is what stops a future
    edit from mapping it to something about the account instead.
    """


@dataclass(frozen=True)
class Entitlement:
    """One verdict about one account, already validated.

    Carries no credential and no raw identity — the subject it describes is the
    principal the caller already holds, so repeating it here would put a user
    UUID on an object that gets logged and serialized.
    """

    access: Access
    plan_id: str | None
    subscription_status: str | None
    source: str
    stale: bool
    checked_at: datetime

    @property
    def granted(self) -> bool:
        return self.access == "granted"

    def log_fields(self) -> dict[str, object]:
        """The verdict as metadata. Deliberately has nowhere to put a subject.

        ``subscription_status`` is CARRIED but never logged, and the difference
        matters: every other field here is drawn from a closed vocabulary this
        module validated, while that one is free-form text the website chose. It
        reaches the account endpoint because the contract puts it there; it does
        not reach a log line, because a log line is where upstream text that
        happened to echo a credential would come to rest.
        """
        return {
            "access": self.access,
            "plan_id": self.plan_id,
            "entitlement_source": self.source,
            "entitlement_stale": self.stale,
        }


@dataclass(frozen=True)
class _CacheEntry:
    verdict: Entitlement
    expires_at: float


class EntitlementService:
    """Asks the website one question, and remembers the answer briefly.

    Transport is injected rather than constructed, so a test drives the whole
    contract — every response variant, every malformed body, the timeout, the
    redirect, the body cap — through a fake without a socket. ``url`` is resolved
    per call from settings for the same reason ``DocsMcpClient`` does: the module
    is constructed once and the configuration it reads is not.
    """

    def __init__(
        self,
        client: httpx.AsyncClient,
        *,
        url: str,
        timeout: float = ENTITLEMENT_TIMEOUT_SECONDS,
    ) -> None:
        self._client = client
        self._url = url
        self._timeout = timeout
        self._cache: dict[tuple[UUID, str], _CacheEntry] = {}
        #: Bumped whenever a key's entry is invalidated. A fetch records the value
        #: it started under and refuses to store its result if it has moved — see
        #: :meth:`resolve`.
        self._generation: dict[tuple[UUID, str], int] = {}

    @property
    def url(self) -> str:
        return self._url

    async def aclose(self) -> None:
        await self._client.aclose()

    def _key(self, *, user_id: UUID, oauth_client_id: str | None) -> tuple[UUID, str]:
        # The client id is part of the key rather than decoration: the same person
        # signed in through two OAuth clients is two grants, and the website may
        # answer differently for each. `""` stands in for an absent one so the key
        # type stays hashable and total.
        return (user_id, oauth_client_id or "")

    def invalidate(self, *, user_id: UUID, oauth_client_id: str | None) -> None:
        """Drop this subject's cached verdict.

        Called on every rejection, which is what makes the positive TTL an upper
        bound rather than a floor: a revoked session stops being authorized at the
        first refusal the instance sees, not when the entry would have expired.
        """
        self._invalidate(self._key(user_id=user_id, oauth_client_id=oauth_client_id))

    def _invalidate(self, key: tuple[UUID, str]) -> None:
        """Remove the entry AND advance the key's generation.

        Removing alone is not enough under concurrency. This server runs at
        concurrency 8, so two requests for one account can miss together and call
        the website together. If the second is refused and purges while the first
        is still in flight, the first would then insert a fresh 60-second grant
        *after* the refusal that contradicted it — funding turns on a session that
        has just been revoked. The generation is what makes the purge win: a fetch
        that started before it is no longer allowed to write.
        """
        self._cache.pop(key, None)
        self._generation[key] = self._generation.get(key, 0) + 1

    def _store(self, key: tuple[UUID, str], entry: _CacheEntry) -> None:
        """Insert, evicting first so the dictionary cannot grow without bound.

        Expiry is not a bound on its own: an entry is only ever examined when its
        own key returns, so a process serving many distinct callers keeps every
        dead entry for its whole life. Expired entries go first — they cost
        nothing to lose — and only if that frees nothing does the oldest live
        entry go, which costs one extra lookup and nothing else.
        """
        if len(self._cache) >= ENTITLEMENT_CACHE_MAX_ENTRIES:
            now = time.monotonic()
            for stale_key in [k for k, v in self._cache.items() if v.expires_at <= now]:
                self._cache.pop(stale_key, None)
            if len(self._cache) >= ENTITLEMENT_CACHE_MAX_ENTRIES:
                oldest = min(self._cache, key=lambda k: self._cache[k].expires_at)
                self._cache.pop(oldest, None)
        self._cache[key] = entry

    async def resolve(
        self,
        *,
        token: str,
        user_id: UUID,
        oauth_client_id: str | None,
        request_id: str,
        subject_hash: str,
    ) -> Entitlement:
        """The caller's entitlement, from cache or from the website.

        Raises the typed account errors directly — ``auth_session_revoked``,
        ``auth_client_not_allowed``, ``account_rate_limited``,
        ``entitlement_unavailable`` — because every one of them is already the
        exact answer this server owes the caller. Wrapping them in a local type
        and re-mapping at the call site would be one more place for
        "we could not check" to become "you have not paid".
        """
        key = self._key(user_id=user_id, oauth_client_id=oauth_client_id)
        cached = self._cache.get(key)
        now = time.monotonic()
        if cached is not None and cached.expires_at > now:
            logger.info(
                "entitlement_resolved",
                request_id=request_id,
                subject_hash=subject_hash,
                cached=True,
                duration_ms=0,
                **cached.verdict.log_fields(),
            )
            return cached.verdict

        # Recorded BEFORE the call, and compared after. See `_invalidate`.
        generation = self._generation.get(key, 0)
        started = time.monotonic()
        unavailable: DaintreeError | None = None
        try:
            # ONE wall-clock deadline for the whole exchange. httpx's own timeouts
            # are per-operation inactivity bounds — its read timeout is the gap
            # between chunks — so a host dribbling one byte every four seconds
            # stays inside them indefinitely. A paid request is waiting behind
            # this, so the bound has to be on the elapsed time, not on the pauses.
            async with asyncio.timeout(self._timeout):
                verdict = await self._fetch(token=token, user_id=user_id)
        except DaintreeError:
            # Any refusal about this caller invalidates whatever we were holding.
            # A stale grant surviving the refusal that contradicts it is the one
            # cache bug that would matter here.
            self._invalidate(key)
            raise
        except EntitlementContractError as exc:
            self._invalidate(key)
            logger.warning(
                "entitlement_unusable_response",
                request_id=request_id,
                subject_hash=subject_hash,
                # The reason names the FIELD, never the value: the body is a
                # description of somebody's account.
                reason=str(exc),
                duration_ms=round((time.monotonic() - started) * 1000),
            )
            unavailable = entitlement_unavailable()
        except Exception as exc:  # noqa: BLE001 - see below
            # Everything else this call can fail with, mapped to the one honest
            # answer: we could not check. Deliberately broad, because the
            # alternative is a 500 on a paid request. The narrow spelling
            # (`httpx.HTTPError`) missed several real cases — `httpx.StreamError`
            # and a closed client raise `RuntimeError`, `httpx.InvalidURL` is
            # outside the `HTTPError` tree, and deep JSON raises `RecursionError`
            # — and every one of them would have surfaced to a caller as a server
            # fault rather than a retryable dependency outage.
            #
            # `except Exception` does not catch `CancelledError`, which is a
            # `BaseException` in 3.12: a disconnected client must keep unwinding.
            # `asyncio.timeout` raises `TimeoutError`, which IS caught here.
            self._invalidate(key)
            logger.warning(
                "entitlement_lookup_failed",
                request_id=request_id,
                subject_hash=subject_hash,
                # The TYPE only. `str(exc)` is not logged and the exception is not
                # chained: an `httpx.RequestError` carries `.request`, whose
                # headers hold the caller's bearer, so both the message and the
                # cause are routes out for a credential.
                error=type(exc).__name__,
                duration_ms=round((time.monotonic() - started) * 1000),
            )
            unavailable = entitlement_unavailable()
        if unavailable is not None:
            # Raised OUTSIDE the handler, and that placement is the whole point.
            # `raise ... from None` clears `__cause__` and leaves `__context__`
            # pointing at the original — so an `httpx.RequestError`, whose
            # `.request` headers hold the caller's bearer, stays reachable from
            # anything that walks an exception graph. Once the handler has exited
            # there is no exception being handled, so the new error is born with
            # no links at all. The diagnostic half is already in the log above.
            raise unavailable

        if self._generation.get(key, 0) != generation:
            # Something invalidated this key while the request was in flight — a
            # concurrent refusal, or an explicit purge. Return the answer to THIS
            # caller, who asked for it and whose request is still correct, but do
            # not resurrect it in the cache for anybody else.
            return verdict

        ttl = (
            ENTITLEMENT_GRANTED_TTL_SECONDS
            if verdict.granted
            else ENTITLEMENT_DENIED_TTL_SECONDS
        )
        self._store(key, _CacheEntry(verdict=verdict, expires_at=time.monotonic() + ttl))
        logger.info(
            "entitlement_resolved",
            request_id=request_id,
            subject_hash=subject_hash,
            cached=False,
            duration_ms=round((time.monotonic() - started) * 1000),
            **verdict.log_fields(),
        )
        return verdict

    async def _fetch(self, *, token: str, user_id: UUID) -> Entitlement:
        """One request, one response, no retry.

        **Deliberately not retried here.** Both the CLI and this server already
        own retry ladders, and a third one nested inside a user request multiplies
        rather than adds: a single caller-visible failure becomes N requests
        against a dependency that is by definition already struggling. Uncertainty
        surfaces as the retryable 503 instead, where the ladder that owns the
        decision can see it.
        """
        request = self._client.build_request(
            "GET",
            self._url,
            headers={
                "Authorization": f"Bearer {token}",
                "Accept": "application/json",
                # IDENTITY, and this is a size bound rather than a preference.
                # `aiter_bytes` yields DECODED bytes, so a compressed reply is
                # expanded before anything can measure it — a few KiB of gzip
                # becomes hundreds of megabytes in memory on the first-byte path
                # of a paid turn. Refusing the encoding makes the wire bytes and
                # the decoded bytes the same thing, which is what lets the cap
                # below actually cap. The body is a few hundred bytes; there is
                # nothing to compress.
                "Accept-Encoding": "identity",
            },
            # A Timeout OBJECT, not a scalar. A bare float replaces every phase,
            # including the connect bound the client was built with — so passing
            # `5.0` here silently widened connect from two seconds to five.
            timeout=httpx.Timeout(
                self._timeout, connect=ENTITLEMENT_CONNECT_TIMEOUT_SECONDS
            ),
        )
        # STREAMED rather than buffered, so the size bound is a bound: `get()`
        # reads and decompresses the whole body before returning, so a check on
        # the result would stop us PARSING an oversized reply without stopping it
        # reaching memory first.
        response = await self._client.send(
            request,
            stream=True,
            # Set here as well as on the client. A redirect is refused rather than
            # followed because the next hop would receive the caller's account
            # credential, and pinning the URL to a Daintree host in configuration
            # is undone by a 302 nobody validated.
            follow_redirects=False,
        )
        try:
            status = response.status_code

            if 300 <= status < 400:
                raise EntitlementContractError("redirect")
            if status == 401:
                # The one thing local JWKS verification cannot tell us. The
                # website performed a fresh user check and refused, so the session
                # behind a signature that still verifies is gone.
                raise auth_session_revoked()
            if status == 403:
                # The endpoint may name the narrower reason itself; honour it
                # rather than telling somebody their OAuth client is unregistered
                # when it is.
                if await _error_code_of(response) == "auth_permission_denied":
                    raise auth_permission_denied()
                raise auth_client_not_allowed()
            if status == 429:
                raise account_rate_limited(retry_after=_retry_after(response))
            if status != 200:
                raise EntitlementContractError(f"status_{status}")

            body = await _read_bounded(response)
        finally:
            await response.aclose()
        verdict = _parse(body, expected_subject=user_id)
        return _without_echoed_secrets(verdict, token=token, user_id=user_id)


async def _error_code_of(response: httpx.Response) -> str | None:
    """The upstream's own error code, when it published one in our envelope shape.

    Read through the same bounded reader as a success body — an error body is no
    less upstream-controlled than a good one, and this is the path where a
    misconfigured URL is most likely to be returning an HTML page.
    """
    try:
        payload: Any = json.loads(await _read_bounded(response))
    except (EntitlementContractError, ValueError, httpx.HTTPError):
        # A 403 with no JSON body is ordinary; the caller has a correct answer
        # either way and only loses the narrower of two messages.
        return None
    if not isinstance(payload, dict):
        return None
    error = payload.get("error")
    if isinstance(error, dict):
        code = error.get("code")
        return code if isinstance(code, str) else None
    return None


def _retry_after(response: httpx.Response) -> str | None:
    """A ``Retry-After`` we are willing to pass on, or nothing.

    Only the delta-seconds form, only when it is plausible. An HTTP-date is
    ignored rather than converted: the conversion needs a trusted clock on both
    ends, and the fallback — the client's own jittered backoff — is better than a
    wrong number presented as authoritative.
    """
    raw = response.headers.get("retry-after")
    if raw is None:
        return None
    value = raw.strip()
    seconds: int | None = None
    if value.isdigit():
        try:
            seconds = int(value)
        except ValueError:
            # `isdigit()` is true for characters `int()` refuses — Unicode digits
            # like "٣" — and for digit strings past the interpreter's conversion
            # limit. Neither is a delay we can act on.
            return None
    else:
        # The other legal form. Converted here rather than ignored, because a
        # CDN in front of the website will send this one and dropping it would
        # mean discarding the only real recovery estimate available.
        try:
            when = parsedate_to_datetime(value)
        except (TypeError, ValueError):
            return None
        if when is None:
            return None
        if when.tzinfo is None:
            when = when.replace(tzinfo=UTC)
        seconds = int((when - datetime.now(UTC)).total_seconds())
    if seconds is None or not 0 <= seconds <= _MAX_RETRY_AFTER_SECONDS:
        return None
    return str(seconds)


async def _read_bounded(response: httpx.Response) -> bytes:
    """Read the body, refusing to hold more than the cap.

    The declared length is checked first so an honest oversized response costs
    nothing, and the stream is measured anyway so a dishonest one costs the cap
    and not the body.
    """
    encoding = response.headers.get("content-encoding", "").strip().lower()
    if encoding and encoding != "identity":
        # We asked for identity. A reply that compressed anyway is one whose
        # decoded size we cannot bound before allocating it, so it is refused
        # rather than decoded — the cap has to hold against a hostile or merely
        # misconfigured peer, not only a cooperative one.
        raise EntitlementContractError("unexpected_content_encoding")
    declared = response.headers.get("content-length")
    if declared and declared.isdigit() and int(declared) > ENTITLEMENT_RESPONSE_MAX_BYTES:
        raise EntitlementContractError("body_too_large")
    chunks: list[bytes] = []
    total = 0
    # With identity enforced ABOVE — before a single byte is iterated — the
    # decoded bytes and the wire bytes are the same thing, so counting these is
    # counting what actually arrived. The header check is the real bound here;
    # this loop only has to hold a body that is already known to be undecoded.
    async for chunk in response.aiter_bytes():
        total += len(chunk)
        if total > ENTITLEMENT_RESPONSE_MAX_BYTES:
            raise EntitlementContractError("body_too_large")
        chunks.append(chunk)
    return b"".join(chunks)


def _parse(body: bytes, *, expected_subject: UUID) -> Entitlement:
    """Validate a v1 body into a verdict, or refuse it.

    Every rule here refuses rather than repairs. A field of the wrong type, a
    contradiction between two fields, an unknown contract version — each is a
    dependency that is not behaving as agreed, and the honest answer to "what does
    this account own?" in that state is that we do not know. Unknown FIELDS are
    the one thing ignored, so the website can add one without a lockstep release.
    """
    try:
        payload: Any = json.loads(
            body,
            # Duplicate names are REFUSED rather than resolved. Python's default
            # keeps the last one, so a body carrying both `"access":"granted"` and
            # `"access":"subscription_required"` would be acted on as whichever
            # came second — an ambiguous answer becoming a confident verdict about
            # somebody's money. There is no reading of "the dependency sent two
            # different answers" that is safe to guess at.
            object_pairs_hook=_no_duplicate_keys,
            # `NaN`, `Infinity` and `-Infinity` are Python extensions, not JSON.
            # Accepting them means accepting a body no conforming encoder produced.
            parse_constant=_reject_constant,
        )
    except ValueError as exc:
        raise EntitlementContractError("invalid_json") from exc
    if not isinstance(payload, dict):
        raise EntitlementContractError("not_an_object")

    # Every v1 field must be PRESENT. `.get()` alone cannot tell a missing key
    # from an explicit null, and the two are different claims: one is a dependency
    # that answered, the other is one that did not finish the sentence.
    for required in ("version", "subject", "access", "plan_id", "source", "stale", "checked_at"):
        if required not in payload:
            raise EntitlementContractError(f"missing_{required}")

    version = payload.get("version")
    if not isinstance(version, int) or isinstance(version, bool):
        raise EntitlementContractError("version")
    if version != ENTITLEMENT_CONTRACT_VERSION:
        raise EntitlementContractError("unknown_version")

    # The subject is compared against the principal we ALREADY verified. A
    # mismatch is not another account's answer to be reported — it is a protocol
    # or security failure at the dependency, and acting on it would attribute one
    # person's plan to another.
    subject = payload.get("subject")
    if not isinstance(subject, str):
        raise EntitlementContractError("subject")
    try:
        if UUID(subject) != expected_subject:
            raise EntitlementContractError("subject_mismatch")
    except ValueError as exc:
        raise EntitlementContractError("subject") from exc

    access = payload.get("access")
    if not isinstance(access, str) or access not in _ACCESS_VALUES:
        raise EntitlementContractError("access")

    plan_id = payload.get("plan_id")
    if plan_id is not None and (not isinstance(plan_id, str) or plan_id not in _PLAN_VALUES):
        raise EntitlementContractError("plan_id")
    if access == "granted" and plan_id is None:
        # Granted access with no plan is a contradiction, and the direction it
        # would fail in is the expensive one: it reads as "let them work" while
        # naming nothing that could later be attributed or capped.
        raise EntitlementContractError("granted_without_plan")

    source = payload.get("source")
    if not isinstance(source, str) or source not in _SOURCE_VALUES:
        raise EntitlementContractError("source")

    stale = payload.get("stale")
    if not isinstance(stale, bool):
        raise EntitlementContractError("stale")
    if stale and source != "cache":
        raise EntitlementContractError("stale_without_cache")

    status = payload.get("subscription_status")
    if status is not None and not isinstance(status, str):
        raise EntitlementContractError("subscription_status")
    if isinstance(status, str):
        if len(status) > _MAX_STATUS_LENGTH:
            raise EntitlementContractError("subscription_status")
        # The one free-form field in the contract, and it is rendered by the CLI
        # and returned by the account endpoint. Collapsed to a single safe line so
        # upstream text cannot carry control characters or newlines into either.
        status = sanitize_line(status, _MAX_STATUS_LENGTH) or None

    checked_at = _checked_at(payload.get("checked_at"))

    return Entitlement(
        access=cast(Access, access),
        plan_id=plan_id,
        subscription_status=status,
        source=source,
        stale=stale,
        checked_at=checked_at,
    )


def _without_echoed_secrets(
    verdict: Entitlement, *, token: str, user_id: UUID
) -> Entitlement:
    """Drop ``subscription_status`` if it echoes the credential or the subject.

    It is the ONE free-form field in the contract, and it is returned to the
    client and rendered in a terminal. Some providers default a status or label
    string to the identifier it describes — `daintree_auth._safe_label` already
    guards the same shape for a provider key label — so this is the field through
    which an upstream mistake would put a bearer fragment or a raw user id back on
    somebody's screen.

    Dropped rather than refused: the field is diagnostic, nothing branches on it,
    and refusing the whole answer would convert a cosmetic upstream bug into a
    billing outage. Losing a status string costs nothing.
    """
    status = verdict.subscription_status
    if not status:
        return verdict
    subject = str(user_id)
    if status in token or token in status or subject in status or status in subject:
        return replace(verdict, subscription_status=None)
    return verdict


def _no_duplicate_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    seen: dict[str, Any] = {}
    for name, value in pairs:
        if name in seen:
            raise ValueError(f"duplicate key: {name}")
        seen[name] = value
    return seen


def _reject_constant(name: str) -> Any:
    raise ValueError(f"non-JSON constant: {name}")


def _checked_at(value: object) -> datetime:
    """An RFC 3339 timestamp that is timezone-aware and plausible.

    Naive values are refused rather than assumed UTC: an assumption here silently
    shifts the freshness of a billing answer by whatever the website's timezone
    happens to be, and the failure would look like clock skew for as long as
    anyone cared to investigate it.
    """
    if not isinstance(value, str):
        raise EntitlementContractError("checked_at")
    # Shape first. `fromisoformat` accepts far more than RFC 3339 — ISO week
    # dates, basic-format strings — so parsing alone would let through a value the
    # contract does not define and read it as agreement.
    if not _RFC3339.match(value):
        raise EntitlementContractError("checked_at")
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError as exc:
        raise EntitlementContractError("checked_at") from exc
    if parsed.tzinfo is None:
        # Unreachable through the regex above, which requires an offset, and kept
        # as the assertion that the two rules agree: a naive value must never be
        # assumed UTC, because the assumption shifts a billing answer's freshness
        # by whatever the website's timezone happens to be.
        raise EntitlementContractError("checked_at_naive")
    now = datetime.now(UTC)
    if parsed > now + _MAX_CHECKED_AT_SKEW:
        raise EntitlementContractError("checked_at_future")
    if parsed < now - _MAX_CHECKED_AT_AGE:
        raise EntitlementContractError("checked_at_stale")
    return parsed
