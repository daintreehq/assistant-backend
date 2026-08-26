"""Verifying a Supabase access token, entirely offline.

Every token here is minted in-process against keys generated in-process, and the
JWKS is served by a `MockTransport`. That is not a compromise for want of a live
provider: it is what lets the awkward cases exist at all. Key rotation, an unknown
`kid`, a signature that does not match, a JWKS outage with and without a warm
cache, a token from the neighbouring environment — none of those can be produced
on demand against a real identity provider, and each is a path where getting it
wrong means either rejecting every legitimate login or accepting a forged one.
"""

from __future__ import annotations

import json
import time
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import uuid4

import httpx
import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric import ec, rsa

from daintree_assistant_server.services.supabase_auth import (
    ACCEPTED_ALGORITHMS,
    CLOCK_SKEW_SECONDS,
    JwksCache,
    SupabaseTokenVerifier,
    TokenError,
)

ISSUER = "https://abcdefghij.supabase.co/auth/v1"
JWKS_URL = f"{ISSUER}/.well-known/jwks.json"
CLIENT_ID = "daintree-assistant"


# ---------------------------------------------------------------------------
# Keys and tokens, minted here so the awkward cases can exist.
# ---------------------------------------------------------------------------


def _rsa_key(kid: str) -> tuple[Any, dict[str, Any]]:
    private = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    jwk = json.loads(jwt.algorithms.RSAAlgorithm.to_jwk(private.public_key()))
    jwk.update({"kid": kid, "alg": "RS256", "use": "sig"})
    return private, jwk


def _ec_key(kid: str) -> tuple[Any, dict[str, Any]]:
    private = ec.generate_private_key(ec.SECP256R1())
    jwk = json.loads(jwt.algorithms.ECAlgorithm.to_jwk(private.public_key()))
    jwk.update({"kid": kid, "alg": "ES256", "use": "sig"})
    return private, jwk


def _claims(**overrides: Any) -> dict[str, Any]:
    now = datetime.now(tz=UTC)
    claims: dict[str, Any] = {
        "iss": ISSUER,
        "aud": "authenticated",
        "sub": str(uuid4()),
        "session_id": str(uuid4()),
        "role": "authenticated",
        "is_anonymous": False,
        "client_id": CLIENT_ID,
        "aal": "aal1",
        "email": "person@example.com",
        "iat": int(now.timestamp()),
        "exp": int((now + timedelta(hours=1)).timestamp()),
    }
    claims.update(overrides)
    return {k: v for k, v in claims.items() if v is not _OMIT}


class _Omit:
    """Sentinel: `_claims(email=OMIT)` removes the claim rather than nulling it."""


_OMIT = _Omit()
OMIT: Any = _OMIT


def _sign(private: Any, claims: dict[str, Any], *, kid: str, algorithm: str = "RS256") -> str:
    return jwt.encode(claims, private, algorithm=algorithm, headers={"kid": kid})


def _verifier(
    jwks_documents: list[dict[str, Any]] | None = None,
    *,
    status: int = 200,
    client_ids: frozenset[str] = frozenset({CLIENT_ID}),
    **cache_kwargs: Any,
) -> tuple[SupabaseTokenVerifier, list[int]]:
    """A verifier over a scripted JWKS endpoint, plus a per-call fetch counter."""
    documents = list(jwks_documents or [])
    calls: list[int] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(1)
        if status != 200:
            return httpx.Response(status)
        document = documents[min(len(calls) - 1, len(documents) - 1)] if documents else {"keys": []}
        return httpx.Response(200, json=document)

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    cache = JwksCache(url=JWKS_URL, client=client, **cache_kwargs)
    verifier = SupabaseTokenVerifier(
        issuer=ISSUER, allowed_client_ids=client_ids, jwks=cache
    )
    return verifier, calls


# ---------------------------------------------------------------------------
# The happy paths, on both algorithms.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize("algorithm", ACCEPTED_ALGORITHMS)
async def test_a_genuine_token_verifies(algorithm: str) -> None:
    private, jwk = (_rsa_key("k1") if algorithm == "RS256" else _ec_key("k1"))
    verifier, _ = _verifier([{"keys": [jwk]}])
    claims = _claims()
    result = await verifier.verify(_sign(private, claims, kid="k1", algorithm=algorithm))
    assert str(result.user_id) == claims["sub"]
    assert str(result.session_id) == claims["session_id"]
    assert result.oauth_client_id == CLIENT_ID
    assert result.email == "person@example.com"
    assert result.assurance_level == "aal1"


@pytest.mark.asyncio
async def test_an_absent_optional_claim_is_tolerated() -> None:
    """`email` and `aal` are conveniences. A token without them is a token from a
    project configured slightly differently, not a forgery."""
    private, jwk = _rsa_key("k1")
    verifier, _ = _verifier([{"keys": [jwk]}])
    result = await verifier.verify(
        _sign(private, _claims(email=OMIT, aal=OMIT), kid="k1")
    )
    assert result.email is None
    assert result.assurance_level == "aal1"


# ---------------------------------------------------------------------------
# The signature itself.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_a_token_signed_by_another_key_is_refused() -> None:
    """The whole point. The attacker's key is well-formed and the token is
    perfectly shaped; only the signature disagrees."""
    _, jwk = _rsa_key("k1")
    attacker, _ = _rsa_key("k1")
    verifier, _ = _verifier([{"keys": [jwk]}])
    with pytest.raises(TokenError) as exc:
        await verifier.verify(_sign(attacker, _claims(), kid="k1"))
    assert exc.value.code == "auth_token_invalid"


@pytest.mark.asyncio
async def test_an_unsigned_token_is_refused() -> None:
    """`alg: none` is the bluntest member of the algorithm-confusion family, and
    the one a verifier that trusted the token's own header would accept."""
    _, jwk = _rsa_key("k1")
    verifier, _ = _verifier([{"keys": [jwk]}])
    unsigned = jwt.encode(_claims(), key="", algorithm="none", headers={"kid": "k1"})
    with pytest.raises(TokenError) as exc:
        await verifier.verify(unsigned)
    assert exc.value.code == "auth_token_invalid"


@pytest.mark.asyncio
async def test_an_hmac_token_is_refused_even_with_a_matching_kid() -> None:
    """Algorithm confusion proper: the classic attack signs with HMAC using the
    RSA PUBLIC key as the shared secret, so a verifier that let the token pick its
    algorithm would validate a token anyone could mint from public data.

    Refused at the header allowlist, before any key is fetched — so this asserts
    the OUTCOME rather than exercising the deeper defence. That second layer is
    real and independent: `decode` is handed a hardcoded algorithm list, and PyJWT
    raises `InvalidAlgorithmError` for an HS256 token even when the key passed is
    an RSA public key. Both accepted algorithms are asymmetric for the same
    reason — an HMAC entry in the allowlist would make the verification key a
    signing key too.
    """
    _, jwk = _rsa_key("k1")
    verifier, _ = _verifier([{"keys": [jwk]}])
    forged = jwt.encode(_claims(), key="public-key-as-secret", algorithm="HS256",
                        headers={"kid": "k1"})
    with pytest.raises(TokenError) as exc:
        await verifier.verify(forged)
    assert exc.value.code == "auth_token_invalid"


@pytest.mark.asyncio
async def test_a_token_with_no_kid_is_refused() -> None:
    private, jwk = _rsa_key("k1")
    verifier, _ = _verifier([{"keys": [jwk]}])
    token = jwt.encode(_claims(), private, algorithm="RS256")
    with pytest.raises(TokenError) as exc:
        await verifier.verify(token)
    assert exc.value.code == "auth_token_invalid"


@pytest.mark.asyncio
async def test_an_unreadable_token_is_refused() -> None:
    verifier, _ = _verifier([{"keys": []}])
    for value in ("", "not-a-jwt", "a.b", "a.b.c.d"):
        with pytest.raises(TokenError) as exc:
            await verifier.verify(value)
        assert exc.value.code == "auth_token_invalid", value


# ---------------------------------------------------------------------------
# Key rotation, and its bound.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_an_unknown_kid_forces_exactly_one_refresh() -> None:
    """This is the ROTATION path, and it has to work: a provider that has rotated
    serves tokens signed by a key our cache predates, and refusing them would log
    out every user until the cache expired on its own."""
    old_private, old_jwk = _rsa_key("old")
    new_private, new_jwk = _rsa_key("new")
    # `min_refresh_seconds=0` because these two calls happen in the same
    # millisecond. In production the previous fetch is minutes old — the TTL is
    # ten minutes and the anti-amplification window is ten seconds — so the window
    # is never what stands between a rotation and its first successful verify. It
    # only bounds a BURST, which is the next test.
    verifier, calls = _verifier(
        [{"keys": [old_jwk]}, {"keys": [old_jwk, new_jwk]}], min_refresh_seconds=0.0
    )

    await verifier.verify(_sign(old_private, _claims(), kid="old"))
    assert len(calls) == 1
    # A token naming the new key: the cache has never seen `new`, so it refreshes.
    await verifier.verify(_sign(new_private, _claims(), kid="new"))
    assert len(calls) == 2


@pytest.mark.asyncio
async def test_a_stream_of_unknown_kids_does_not_hammer_the_provider() -> None:
    """Beyond one refresh, an unknown `kid` means the token is not ours. Fetching
    per token would turn a misconfigured client — or someone probing — into a
    request amplifier pointed at the identity provider, on our credentials."""
    _, jwk = _rsa_key("k1")
    verifier, calls = _verifier([{"keys": [jwk]}], min_refresh_seconds=60.0)
    for index in range(20):
        stranger, _ = _rsa_key(f"x{index}")
        with pytest.raises(TokenError) as exc:
            await verifier.verify(_sign(stranger, _claims(), kid=f"x{index}"))
        # Suppressed by the window, so we never got to ask — "we could not check",
        # not "your credential is bad".
        assert exc.value.code in {"auth_token_invalid", "auth_dependency_unavailable"}
    assert len(calls) <= 2, calls


@pytest.mark.asyncio
async def test_two_keys_claiming_one_kid_are_both_dropped() -> None:
    """Which one verifies a token would otherwise be a function of iteration
    order — a verifier whose answer depends on dict ordering.

    A third, unambiguous key is present so the set is not empty: that separates
    "this kid is unusable" from "we have no keys at all", which are different
    outcomes with different codes.
    """
    private_a, jwk_a = _rsa_key("dup")
    _, jwk_b = _rsa_key("dup")
    good_private, good_jwk = _rsa_key("good")
    verifier, _ = _verifier([{"keys": [jwk_a, jwk_b, good_jwk]}], min_refresh_seconds=0.0)
    with pytest.raises(TokenError) as exc:
        await verifier.verify(_sign(private_a, _claims(), kid="dup"))
    assert exc.value.code == "auth_token_invalid"
    # The unambiguous key beside them is untouched.
    assert await verifier.verify(_sign(good_private, _claims(), kid="good"))


@pytest.mark.asyncio
async def test_concurrent_verifies_during_a_rotation_all_succeed() -> None:
    """The bug this found, and the reason the refresh is serialised.

    Without a lock, concurrent requests arriving during a rotation (or at a cold
    start) all miss the cache, all enter the refresh, and all but the first are
    turned away by the anti-amplification window — then find the key still absent
    and report the token INVALID. Measured before the fix: of five concurrent
    verifies of ONE GENUINE TOKEN, one succeeded and four were told their
    credential was bad.

    That is a wrong verdict rather than a slow one, and a client acting on
    `auth_token_invalid` discards a perfectly good session. Both halves are
    asserted: everyone verifies, and the provider is asked exactly once.
    """
    import asyncio

    private, jwk = _rsa_key("k1")
    calls: list[int] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        calls.append(1)
        # A real fetch is not instantaneous, which is what opens the window.
        await asyncio.sleep(0.05)
        return httpx.Response(200, json={"keys": [jwk]})

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    verifier = SupabaseTokenVerifier(
        issuer=ISSUER,
        allowed_client_ids=frozenset({CLIENT_ID}),
        jwks=JwksCache(url=JWKS_URL, client=client),
    )
    token = _sign(private, _claims(), kid="k1")
    results = await asyncio.gather(*[verifier.verify(token) for _ in range(5)])
    assert len(results) == 5
    assert len(calls) == 1, f"the provider was asked {len(calls)} times for one key"


# ---------------------------------------------------------------------------
# A JWKS outage is not a verdict about the token.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_a_jwks_outage_with_no_cached_key_is_a_dependency_error() -> None:
    """`auth_dependency_unavailable`, not `auth_token_invalid`. We could not
    check — saying the credential is bad would tell a client to throw away a
    perfectly good session because our network blipped."""
    verifier, _ = _verifier(status=503)
    _, jwk = _rsa_key("k1")
    private, _ = _rsa_key("k1")
    with pytest.raises(TokenError) as exc:
        await verifier.verify(_sign(private, _claims(), kid="k1"))
    assert exc.value.code == "auth_dependency_unavailable"


@pytest.mark.asyncio
async def test_a_warm_cache_survives_a_jwks_outage() -> None:
    """Keys already held stay usable. The alternative is that a blip at the
    identity provider signs out every user at once."""
    private, jwk = _rsa_key("k1")
    documents = [{"keys": [jwk]}]
    calls: list[int] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(1)
        if len(calls) == 1:
            return httpx.Response(200, json=documents[0])
        return httpx.Response(500)

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    cache = JwksCache(url=JWKS_URL, client=client, ttl_seconds=0.0, min_refresh_seconds=0.0)
    verifier = SupabaseTokenVerifier(
        issuer=ISSUER, allowed_client_ids=frozenset({CLIENT_ID}), jwks=cache
    )
    assert await verifier.verify(_sign(private, _claims(), kid="k1"))
    # TTL of zero forces a refresh attempt on the next call; it fails, and the
    # warm key must still verify.
    assert await verifier.verify(_sign(private, _claims(), kid="k1"))
    assert len(calls) >= 2


@pytest.mark.asyncio
async def test_an_empty_key_set_with_nothing_cached_is_a_dependency_error() -> None:
    verifier, _ = _verifier([{"keys": []}])
    private, _ = _rsa_key("k1")
    with pytest.raises(TokenError) as exc:
        await verifier.verify(_sign(private, _claims(), kid="k1"))
    assert exc.value.code == "auth_dependency_unavailable"


@pytest.mark.asyncio
async def test_an_oversized_jwks_document_is_refused() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=b'{"keys":[' + b"0" * (300 * 1024) + b"]}")

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    cache = JwksCache(url=JWKS_URL, client=client)
    verifier = SupabaseTokenVerifier(
        issuer=ISSUER, allowed_client_ids=frozenset({CLIENT_ID}), jwks=cache
    )
    private, _ = _rsa_key("k1")
    with pytest.raises(TokenError) as exc:
        await verifier.verify(_sign(private, _claims(), kid="k1"))
    assert exc.value.code == "auth_dependency_unavailable"


@pytest.mark.asyncio
async def test_the_jwks_age_is_reportable() -> None:
    """`/doctor` reads this. A key set that has stopped refreshing is invisible
    until the day it is rotated — and then it is invisible as "every login
    suddenly fails"."""
    private, jwk = _rsa_key("k1")
    verifier, _ = _verifier([{"keys": [jwk]}])
    assert verifier.jwks_age_seconds is None
    await verifier.verify(_sign(private, _claims(), kid="k1"))
    age = verifier.jwks_age_seconds
    assert age is not None and age >= 0


# ---------------------------------------------------------------------------
# Claims. Every one of these is a token with a valid signature.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("overrides", "code"),
    [
        ({"iss": "https://other.supabase.co/auth/v1"}, "auth_token_invalid"),
        ({"aud": "anon"}, "auth_token_invalid"),
        ({"role": "anon"}, "auth_token_invalid"),
        ({"role": OMIT}, "auth_token_invalid"),
        ({"is_anonymous": True}, "auth_token_invalid"),
        ({"is_anonymous": OMIT}, "auth_token_invalid"),
        ({"is_anonymous": "false"}, "auth_token_invalid"),
        ({"is_anonymous": 0}, "auth_token_invalid"),
        ({"client_id": OMIT}, "auth_token_invalid"),
        ({"client_id": ""}, "auth_token_invalid"),
        ({"client_id": 1}, "auth_token_invalid"),
        ({"client_id": "some-other-app"}, "auth_client_not_allowed"),
        ({"sub": "not-a-uuid"}, "auth_token_invalid"),
        ({"sub": 1}, "auth_token_invalid"),
        ({"sub": "00000000-0000-0000-0000-000000000000"}, "auth_token_invalid"),
        ({"sub": "{6ba7b810-9dad-11d1-80b4-00c04fd430c8}"}, "auth_token_invalid"),
        ({"sub": "6ba7b8109dad11d180b400c04fd430c8"}, "auth_token_invalid"),
        ({"session_id": "not-a-uuid"}, "auth_token_invalid"),
        ({"session_id": OMIT}, "auth_token_invalid"),
        ({"email": 42}, "auth_token_invalid"),
        ({"aal": 2}, "auth_token_invalid"),
        ({"iat": True}, "auth_token_invalid"),
        ({"exp": OMIT}, "auth_token_invalid"),
        ({"iat": OMIT}, "auth_token_invalid"),
        ({"sub": OMIT}, "auth_token_invalid"),
    ],
    ids=[
        "wrong-issuer", "wrong-audience", "wrong-role", "no-role",
        "anonymous", "no-is-anonymous", "is-anonymous-string", "is-anonymous-zero",
        "no-client-id", "empty-client-id", "numeric-client-id", "disallowed-client",
        "sub-not-uuid", "sub-numeric", "sub-nil-uuid", "sub-braced", "sub-bare-hex",
        "session-not-uuid", "no-session-id", "email-numeric", "aal-numeric",
        "iat-boolean", "no-exp", "no-iat", "no-sub",
    ],
)
async def test_a_claim_that_is_wrong_or_wrongly_typed_is_refused(
    overrides: dict[str, Any], code: str
) -> None:
    private, jwk = _rsa_key("k1")
    verifier, _ = _verifier([{"keys": [jwk]}])
    with pytest.raises(TokenError) as exc:
        await verifier.verify(_sign(private, _claims(**overrides), kid="k1"))
    assert exc.value.code == code


@pytest.mark.asyncio
async def test_a_token_from_the_neighbouring_environment_is_refused() -> None:
    """Staging tokens must not work against production and vice versa. The issuer
    is compared for equality, so this holds even when both projects are Supabase
    and both tokens are perfectly valid where they were minted."""
    private, jwk = _rsa_key("k1")
    verifier, _ = _verifier([{"keys": [jwk]}])
    with pytest.raises(TokenError):
        await verifier.verify(
            _sign(private, _claims(iss="https://prod.supabase.co/auth/v1"), kid="k1")
        )


# ---------------------------------------------------------------------------
# Time.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_an_expired_token_is_its_own_outcome() -> None:
    """The one verification failure a client can fix silently — refresh once and
    replay — so it is the only one worth telling apart from the rest."""
    private, jwk = _rsa_key("k1")
    verifier, _ = _verifier([{"keys": [jwk]}])
    past = int(time.time()) - 3600
    with pytest.raises(TokenError) as exc:
        await verifier.verify(
            _sign(private, _claims(iat=past - 60, exp=past), kid="k1")
        )
    assert exc.value.code == "auth_token_expired"


@pytest.mark.asyncio
async def test_a_token_just_inside_the_skew_still_verifies() -> None:
    """Clock drift between this host and the provider must not reject every login
    on an unsynchronised container."""
    private, jwk = _rsa_key("k1")
    verifier, _ = _verifier([{"keys": [jwk]}])
    now = int(time.time())
    assert await verifier.verify(
        _sign(private, _claims(exp=now - (CLOCK_SKEW_SECONDS - 5)), kid="k1")
    )


@pytest.mark.asyncio
async def test_a_token_not_yet_valid_is_refused() -> None:
    private, jwk = _rsa_key("k1")
    verifier, _ = _verifier([{"keys": [jwk]}])
    future = int(time.time()) + 3600
    with pytest.raises(TokenError):
        await verifier.verify(_sign(private, _claims(nbf=future), kid="k1"))


# ---------------------------------------------------------------------------
# What a failure says.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_no_error_message_carries_the_token_or_a_claim_value() -> None:
    """These messages reach the log. The RESPONSE wording comes from the error
    factories and is uniform by design, but a log line that quoted the `sub` it
    was about to reject would be writing an attacker-controlled string into the
    pipeline — and claims are attacker-controlled until the signature verifies."""
    private, jwk = _rsa_key("k1")
    verifier, _ = _verifier([{"keys": [jwk]}])
    marker = "s3cr3t-marker-value"
    token = _sign(private, _claims(email=marker, client_id=marker), kid="k1")
    with pytest.raises(TokenError) as exc:
        await verifier.verify(token)
    assert marker not in str(exc.value)
    assert token not in str(exc.value)


@pytest.mark.asyncio
async def test_an_unknown_kid_we_could_not_look_up_is_a_dependency_error() -> None:
    """The distinction the 503 codes exist to preserve.

    "We asked the issuer and it does not publish this key" is a statement about
    the token. "We never managed to ask" is a statement about us — and reporting
    them alike tells a client its credential is bad on the strength of our own
    outage, at which point it discards a session that was never in question.

    Here the anti-amplification window suppresses the refresh, so the second call
    genuinely cannot know.
    """
    private, jwk = _rsa_key("k1")
    verifier, calls = _verifier([{"keys": [jwk]}], min_refresh_seconds=3600.0)
    # Warms the cache, and consumes the refresh budget.
    await verifier.verify(_sign(private, _claims(), kid="k1"))
    stranger, _ = _rsa_key("k2")
    with pytest.raises(TokenError) as exc:
        await verifier.verify(_sign(stranger, _claims(), kid="k2"))
    assert exc.value.code == "auth_dependency_unavailable"
    assert len(calls) == 1


@pytest.mark.asyncio
async def test_an_unknown_kid_we_did_look_up_is_a_token_verdict() -> None:
    """The other half. The refresh ran, the issuer answered, and the key is not
    there — so the token names a key nobody publishes."""
    private, jwk = _rsa_key("k1")
    verifier, calls = _verifier([{"keys": [jwk]}], min_refresh_seconds=0.0)
    await verifier.verify(_sign(private, _claims(), kid="k1"))
    stranger, _ = _rsa_key("k2")
    with pytest.raises(TokenError) as exc:
        await verifier.verify(_sign(stranger, _claims(), kid="k2"))
    assert exc.value.code == "auth_token_invalid"
    assert len(calls) == 2


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "exp", [None, [], {}, float("inf"), float("nan"), "0", True],
    ids=["null", "list", "object", "infinity", "nan", "string", "boolean"],
)
async def test_a_malformed_temporal_claim_is_refused_rather_than_crashing(exp: Any) -> None:
    """PyJWT compares the temporal claims arithmetically without typing them
    first, so a signed token carrying `"exp": null`, `[]` or `Infinity` raises out
    of `decode` as a plain TypeError or OverflowError — past every
    `except PyJWTError` and out of the request as a 500.

    Not a bypass: the signature still had to verify, so only our own issuer could
    mint one. But a 500 is the wrong answer to a bad token, and on an
    unauthenticated path it is a free way to make the server emit stack traces.
    Every shape must land as a typed refusal.
    """
    private, jwk = _rsa_key("k1")
    verifier, _ = _verifier([{"keys": [jwk]}])
    with pytest.raises(TokenError) as exc:
        await verifier.verify(_sign(private, _claims(exp=exp), kid="k1"))
    assert exc.value.code in {"auth_token_invalid", "auth_token_expired"}


@pytest.mark.asyncio
async def test_three_keys_sharing_one_kid_are_all_dropped() -> None:
    """Deleting on the SECOND occurrence and re-testing membership lets a THIRD
    key with the same id be accepted — the same order-dependence the rule exists
    to remove, just further along the list."""
    private_a, jwk_a = _rsa_key("dup")
    _, jwk_b = _rsa_key("dup")
    private_c, jwk_c = _rsa_key("dup")
    good_private, good_jwk = _rsa_key("good")
    verifier, _ = _verifier([{"keys": [jwk_a, jwk_b, jwk_c, good_jwk]}], min_refresh_seconds=0.0)
    for private in (private_a, private_c):
        with pytest.raises(TokenError) as exc:
            await verifier.verify(_sign(private, _claims(), kid="dup"))
        assert exc.value.code == "auth_token_invalid"
    assert await verifier.verify(_sign(good_private, _claims(), kid="good"))


@pytest.mark.asyncio
async def test_a_cold_outage_reports_a_dependency_error_on_every_request() -> None:
    """Not "dependency error, then invalid". The first request fails the fetch and
    the second is suppressed by the window — but neither one learned anything
    about the token, so neither may say the credential is bad."""
    verifier, calls = _verifier(status=503, min_refresh_seconds=3600.0)
    private, _ = _rsa_key("k1")
    token = _sign(private, _claims(), kid="k1")
    for _ in range(3):
        with pytest.raises(TokenError) as exc:
            await verifier.verify(token)
        assert exc.value.code == "auth_dependency_unavailable"
    assert len(calls) == 1


@pytest.mark.asyncio
async def test_a_warm_cache_stops_being_trusted_once_it_is_too_stale() -> None:
    """"Survives an outage" and "is trusted forever" are different promises, and
    only the first is safe.

    A provider retires a key precisely when it should stop verifying tokens. A
    cache that never gave up would keep honouring it for as long as the fetch kept
    failing — exactly the window someone holding a retired key wants. Past the
    bound this fails CLOSED, as a dependency error rather than a verdict.
    """
    private, jwk = _rsa_key("k1")
    calls: list[int] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(1)
        return httpx.Response(200, json={"keys": [jwk]}) if len(calls) == 1 else httpx.Response(503)

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    cache = JwksCache(
        url=JWKS_URL, client=client, ttl_seconds=0.0, min_refresh_seconds=0.0,
        max_stale_seconds=0.0,
    )
    verifier = SupabaseTokenVerifier(
        issuer=ISSUER, allowed_client_ids=frozenset({CLIENT_ID}), jwks=cache
    )
    token = _sign(private, _claims(), kid="k1")
    assert await verifier.verify(token)
    # The key is still in hand and still correct; it is simply no longer trusted.
    with pytest.raises(TokenError) as exc:
        await verifier.verify(token)
    assert exc.value.code == "auth_dependency_unavailable"


@pytest.mark.asyncio
async def test_the_jwks_fetch_does_not_follow_a_redirect() -> None:
    """A redirect here would fetch signing keys from a host the issuer does not
    name. Asserted rather than left to the client's default, because the client is
    injected and its default is not ours to assume."""
    seen: list[bool] = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("jwks.json"):
            return httpx.Response(302, headers={"location": "https://evil.example/keys"})
        seen.append(True)
        return httpx.Response(200, json={"keys": []})

    client = httpx.AsyncClient(
        transport=httpx.MockTransport(handler), follow_redirects=True
    )
    cache = JwksCache(url=JWKS_URL, client=client)
    verifier = SupabaseTokenVerifier(
        issuer=ISSUER, allowed_client_ids=frozenset({CLIENT_ID}), jwks=cache
    )
    private, _ = _rsa_key("k1")
    with pytest.raises(TokenError) as exc:
        await verifier.verify(_sign(private, _claims(), kid="k1"))
    assert exc.value.code == "auth_dependency_unavailable"
    assert not seen, "the redirect was followed to another host"


# ---------------------------------------------------------------------------
# Recovery from the hard staleness bound.
#
# "Never serve a key this old" and "never look for a new one" were one rule for
# as long as `get()` answered the over-age case before the refresh path, and only
# the first of them was ever wanted. The tests below pin both halves: the bound
# still fails closed on every path that could return a key, and a cache that has
# crossed it can still recover on a later request without the process restarting.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_an_over_age_cache_recovers_once_the_issuer_does() -> None:
    """The defect this section exists for: recovery used to require a restart.

    `_fetched_at` moves only inside `_refresh`, so refusing before the refresh
    path made the one call able to clear the condition the one call the condition
    prevented. From the outside that is indistinguishable from the outage that
    caused it — every request answers `auth_dependency_unavailable`, forever.
    """
    private, jwk = _rsa_key("k1")
    calls: list[int] = []
    healthy = [True]

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(1)
        if not healthy[0]:
            return httpx.Response(503)
        return httpx.Response(200, json={"keys": [jwk]})

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    # `max_stale_seconds=0.0` puts the cache over its bound the instant it is
    # filled, which is the only way to reach this path without waiting a day.
    cache = JwksCache(
        url=JWKS_URL, client=client, ttl_seconds=0.0, min_refresh_seconds=0.0,
        max_stale_seconds=0.0,
    )
    verifier = SupabaseTokenVerifier(
        issuer=ISSUER, allowed_client_ids=frozenset({CLIENT_ID}), jwks=cache
    )
    token = _sign(private, _claims(), kid="k1")
    assert await verifier.verify(token)

    healthy[0] = False
    with pytest.raises(TokenError) as exc:
        await verifier.verify(token)
    assert exc.value.code == "auth_dependency_unavailable"
    during_outage = len(calls)

    # The issuer comes back. Nothing else changes — no restart, no new object.
    healthy[0] = True
    cache.max_stale_seconds = 3600.0
    assert await verifier.verify(token), "an over-age cache never asked again"
    assert len(calls) > during_outage, "recovery served a key without re-fetching"


@pytest.mark.asyncio
async def test_an_over_age_cache_keeps_asking_rather_than_giving_up() -> None:
    """Each request past the bound attempts a refresh; none of them is answered
    from the keys already held.

    The rate limiter still applies — `min_refresh_seconds` is what stops this
    becoming an amplifier pointed at a provider that is already struggling — so
    the assertion is that asking HAPPENS, not that it happens every time.
    """
    private, jwk = _rsa_key("k1")
    calls: list[int] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(1)
        return httpx.Response(200, json={"keys": [jwk]}) if len(calls) == 1 else httpx.Response(503)

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    cache = JwksCache(
        url=JWKS_URL, client=client, ttl_seconds=0.0, min_refresh_seconds=0.0,
        max_stale_seconds=0.0,
    )
    verifier = SupabaseTokenVerifier(
        issuer=ISSUER, allowed_client_ids=frozenset({CLIENT_ID}), jwks=cache
    )
    token = _sign(private, _claims(), kid="k1")
    assert await verifier.verify(token)
    for _ in range(3):
        with pytest.raises(TokenError) as exc:
            await verifier.verify(token)
        assert exc.value.code == "auth_dependency_unavailable"
    assert len(calls) > 1, "the over-age cache stopped consulting the issuer"


@pytest.mark.asyncio
async def test_an_over_age_key_is_never_served_even_when_it_is_the_right_one() -> None:
    """The bound is enforced at the point of USE.

    `k1` is in hand, matches the token, and would verify. It is simply older than
    the deployment is willing to trust, and a refresh that was suppressed by the
    anti-amplification window earns no new permission to honour it — which is the
    case a naive "attempt a refresh, then return whatever we have" rewrite gets
    wrong, because the key is present on both sides of the failed attempt.
    """
    private, jwk = _rsa_key("k1")
    calls: list[int] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(1)
        return httpx.Response(200, json={"keys": [jwk]}) if len(calls) == 1 else httpx.Response(503)

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    cache = JwksCache(
        url=JWKS_URL, client=client, ttl_seconds=0.0,
        # An hour-long window: the second request's refresh is suppressed before
        # it reaches the network, so the ONLY thing standing between the caller
        # and a stale key is the age check on the return path.
        min_refresh_seconds=3600.0,
        max_stale_seconds=0.0,
    )
    verifier = SupabaseTokenVerifier(
        issuer=ISSUER, allowed_client_ids=frozenset({CLIENT_ID}), jwks=cache
    )
    token = _sign(private, _claims(), kid="k1")
    assert await verifier.verify(token)
    with pytest.raises(TokenError) as exc:
        await verifier.verify(token)
    assert exc.value.code == "auth_dependency_unavailable"
    assert len(calls) == 1, "the rate limiter did not suppress the second fetch"
    assert cache._keys.get("k1") is not None, "the test no longer exercises a held key"


@pytest.mark.asyncio
async def test_a_recovered_over_age_cache_reports_an_unknown_kid_as_a_token_verdict() -> None:
    """Refresh succeeded, the issuer does not publish this `kid`: that is a
    statement about the TOKEN, and it survives the over-age path.

    The distinction between "we could not ask" and "we asked and it is not there"
    is the whole reason these are different codes, and a recovery path that
    collapsed them would tell a user with a genuinely bad token to wait it out.
    """
    private_old, jwk_old = _rsa_key("old")
    _, jwk_new = _rsa_key("new")
    calls: list[int] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(1)
        keys = [jwk_old] if len(calls) == 1 else [jwk_new]
        return httpx.Response(200, json={"keys": keys})

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    cache = JwksCache(
        url=JWKS_URL, client=client, ttl_seconds=0.0, min_refresh_seconds=0.0,
        max_stale_seconds=3600.0,
    )
    verifier = SupabaseTokenVerifier(
        issuer=ISSUER, allowed_client_ids=frozenset({CLIENT_ID}), jwks=cache
    )
    token = _sign(private_old, _claims(), kid="old")
    assert await verifier.verify(token)
    # Over the bound, and the successful refresh drops the key this token names.
    cache.max_stale_seconds = 0.0
    with pytest.raises(TokenError) as exc:
        await verifier.verify(token)
    assert exc.value.code == "auth_token_invalid"


@pytest.mark.asyncio
async def test_concurrent_callers_recover_an_over_age_cache_with_one_refresh() -> None:
    """Five requests arrive against a cache past its bound. One fetch, five
    successes.

    The single-flight lock already existed for the rotation path; this asserts the
    over-age path goes through it rather than around it. Without the re-check
    inside the lock the four queued callers would each refresh again, and with a
    realistic `min_refresh_seconds` all four would then be refused for a key the
    first caller had just put in place — the same wrong-verdict failure the lock
    was introduced to remove.
    """
    import asyncio

    private, jwk = _rsa_key("k1")
    calls: list[int] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(1)
        return httpx.Response(200, json={"keys": [jwk]})

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    cache = JwksCache(
        url=JWKS_URL, client=client, ttl_seconds=3600.0, min_refresh_seconds=3600.0,
        max_stale_seconds=3600.0,
    )
    verifier = SupabaseTokenVerifier(
        issuer=ISSUER, allowed_client_ids=frozenset({CLIENT_ID}), jwks=cache
    )
    token = _sign(private, _claims(), kid="k1")
    assert await verifier.verify(token)
    assert len(calls) == 1

    # Push the one successful fetch back past the hard bound without touching
    # anything else, so the next callers meet an over-age cache and a provider
    # that will answer.
    cache._fetched_at = time.monotonic() - 7200.0
    cache._last_attempt_at = 0.0
    results = await asyncio.gather(*(verifier.verify(token) for _ in range(5)))
    assert all(results)
    assert len(calls) == 2, f"expected exactly one recovery fetch, saw {len(calls) - 1}"
