"""The three auth postures, on the wire.

Unit tests cover the verifier and the parser. These cover the DOOR: what a real
request gets, through the real middleware stack, in each mode. The properties that
matter here are not "is this token valid" but "does the posture do what its name
says" — and in particular that `open` and `observe` are byte-for-byte the same
experience for a caller, which is the only thing that makes `observe` safe to turn
on in front of real traffic.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import uuid4

import httpx
import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric import rsa
from fastapi.testclient import TestClient

from daintree_assistant_server.config import Settings
from daintree_assistant_server.main import create_app
from daintree_assistant_server.services.supabase_auth import (
    JwksCache,
    SupabaseTokenVerifier,
)

ISSUER = "https://abcdefghij.supabase.co/auth/v1"
CLIENT_ID = "daintree-assistant"

_OAUTH: dict[str, Any] = {
    "SUPABASE_AUTH_ISSUER": ISSUER,
    "SUPABASE_JWKS_URL": f"{ISSUER}/.well-known/jwks.json",
    "SUPABASE_OAUTH_AUTHORIZE_URL": f"{ISSUER}/oauth/authorize",
    "SUPABASE_OAUTH_TOKEN_URL": f"{ISSUER}/oauth/token",
    "SUPABASE_OAUTH_CLIENT_ID": CLIENT_ID,
    "DAINTREE_ACCOUNT_URL": "https://staging.daintree.org/account",
    "DAINTREE_SUBSCRIBE_URL": "https://staging.daintree.org/subscribe",
}


@pytest.fixture
def signing_key() -> tuple[Any, dict[str, Any]]:
    private = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    jwk = json.loads(jwt.algorithms.RSAAlgorithm.to_jwk(private.public_key()))
    jwk.update({"kid": "k1", "alg": "RS256", "use": "sig"})
    return private, jwk


def _token(private: Any, **overrides: Any) -> str:
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
        "iat": int(now.timestamp()),
        "exp": int((now + timedelta(hours=1)).timestamp()),
    }
    claims.update(overrides)
    return jwt.encode(claims, private, algorithm="RS256", headers={"kid": "k1"})


def _app(mode: str, jwk: dict[str, Any] | None, provider: Any, selector: Any) -> Any:
    settings = Settings(
        TOGETHER_API_KEY="tgp_v1_test-key",
        STATE_SIGNING_KEY_CURRENT="test-signing-key",
        STATE_SIGNING_KID="k1",
        STATE_SALT="test-salt",
        RETRY_BACKOFF_BASE_SECONDS=0.0,
        AUTH_MODE=mode,
        **(_OAUTH if mode != "open" else {}),
    )
    app = create_app(settings=settings, provider=provider, selector=selector)
    if jwk is not None:
        client = httpx.AsyncClient(
            transport=httpx.MockTransport(
                lambda _request: httpx.Response(200, json={"keys": [jwk]})
            )
        )
        app.state.token_verifier = SupabaseTokenVerifier(
            issuer=ISSUER,
            allowed_client_ids=frozenset({CLIENT_ID}),
            jwks=JwksCache(url=_OAUTH["SUPABASE_JWKS_URL"], client=client),
        )
        # Owned where the real one is owned, so the fixture models the production
        # lifecycle rather than leaving a client nothing is responsible for.
        app.state.jwks_client = client
    app.state.ready = True
    return app


@pytest.fixture
def make_client(app: Any, fake_provider: Any) -> Any:
    """A client in a chosen mode, reusing the container the `app` fixture built.

    Reused rather than rebuilt because the selector artifact takes seconds to
    load, and none of these tests is about the selector.
    """

    def build(mode: str, jwk: dict[str, Any] | None = None) -> TestClient:
        application = _app(mode, jwk, fake_provider, app.state.injected_selector)
        application.state.container = app.state.container
        return TestClient(application, raise_server_exceptions=False)

    return build


PROTECTED = "/v1/daintree/capabilities"


# ---- open: the header is not read ------------------------------------------


def test_open_serves_a_request_with_no_credential(make_client: Any) -> None:
    assert make_client("open").get(PROTECTED).status_code == 200


def test_open_serves_a_request_with_a_nonsense_credential(make_client: Any) -> None:
    """The migration case. A client still sending an old key must be served, not
    have that key forwarded upstream and rejected as its own."""
    client = make_client("open")
    for header in ("Bearer sk-or-v1-stale", "Bearer", "Basic x", "not-a-scheme"):
        assert client.get(PROTECTED, headers={"Authorization": header}).status_code == 200, header


# ---- observe: verified, recorded, and behaviourally inert ------------------


def test_observe_serves_every_request_open_would_have_served(
    make_client: Any, signing_key: Any
) -> None:
    """The property that makes `observe` safe to turn on in front of real traffic.

    A caller must not be able to tell the difference. Anything else and the mode
    is enforcement arriving a release early, without anyone having decided to
    switch it on.
    """
    private, jwk = signing_key
    client = make_client("observe", jwk)
    cases = [
        {},
        {"Authorization": "Bearer sk-or-v1-stale"},
        {"Authorization": "Bearer"},
        {"Authorization": f"Bearer {_token(private)}"},
        {"Authorization": f"Bearer {_token(private, client_id='someone-else')}"},
        {"Authorization": f"Bearer {_token(private, exp=1)}"},
    ]
    for headers in cases:
        assert client.get(PROTECTED, headers=headers).status_code == 200, headers


def test_observe_does_not_grant_a_verified_caller_anything_extra(
    make_client: Any, signing_key: Any
) -> None:
    """A verified identity in observe mode is recorded and DISCARDED. Returning
    the real principal would make the mode change authorization, billing
    attribution and rate limiting — which is enforcement."""
    private, jwk = signing_key
    client = make_client("observe", jwk)
    with_token = client.get(PROTECTED, headers={"Authorization": f"Bearer {_token(private)}"})
    without = client.get(PROTECTED)
    assert with_token.status_code == without.status_code == 200
    assert with_token.json() == without.json()


# ---- enforce: a verified credential is required ----------------------------


def test_enforce_requires_a_credential(make_client: Any, signing_key: Any) -> None:
    _, jwk = signing_key
    response = make_client("enforce", jwk).get(PROTECTED)
    assert response.status_code == 401
    assert response.json()["error"]["code"] == "auth_required"
    assert response.headers["WWW-Authenticate"] == "Bearer"


def test_enforce_accepts_a_genuine_token(make_client: Any, signing_key: Any) -> None:
    private, jwk = signing_key
    response = make_client("enforce", jwk).get(
        PROTECTED, headers={"Authorization": f"Bearer {_token(private)}"}
    )
    assert response.status_code == 200


@pytest.mark.parametrize(
    ("header", "status", "code"),
    [
        ("Bearer sk-or-v1-stale", 401, "auth_token_invalid"),
        ("Bearer", 401, "auth_token_invalid"),
        ("Basic abc", 401, "auth_token_invalid"),
    ],
    ids=["not-a-jwt", "scheme-only", "wrong-scheme"],
)
def test_enforce_refuses_an_unusable_credential(
    make_client: Any, signing_key: Any, header: str, status: int, code: str
) -> None:
    """Present-but-unreadable is `auth_token_invalid`, not `auth_required`. The
    client did try, and telling it nothing arrived would send someone to a sign-in
    when their token store handed the request an empty string."""
    _, jwk = signing_key
    response = make_client("enforce", jwk).get(PROTECTED, headers={"Authorization": header})
    assert response.status_code == status
    assert response.json()["error"]["code"] == code


def test_enforce_refuses_an_expired_token_with_its_own_code(
    make_client: Any, signing_key: Any
) -> None:
    """The one failure a client can fix silently, so it is the one that must be
    distinguishable: refresh once and replay."""
    private, jwk = signing_key
    response = make_client("enforce", jwk).get(
        PROTECTED,
        headers={"Authorization": f"Bearer {_token(private, iat=1, exp=2)}"},
    )
    assert response.json()["error"]["code"] == "auth_token_expired"


def test_enforce_refuses_a_token_from_another_oauth_client(
    make_client: Any, signing_key: Any
) -> None:
    """403, not 401, and no challenge: the credential is valid and current, so a
    refresh mints another token wrong in exactly the same way."""
    private, jwk = signing_key
    response = make_client("enforce", jwk).get(
        PROTECTED,
        headers={"Authorization": f"Bearer {_token(private, client_id='website')}"},
    )
    assert response.status_code == 403
    assert response.json()["error"]["code"] == "auth_client_not_allowed"
    assert "WWW-Authenticate" not in response.headers


def test_enforce_refuses_two_authorization_headers(
    make_client: Any, signing_key: Any
) -> None:
    """Which one an upstream proxy forwards is not knowable here, so validating
    either would be validating a credential the upstream might never have seen."""
    private, jwk = signing_key
    token = _token(private)
    response = make_client("enforce", jwk).get(
        PROTECTED,
        headers=[("Authorization", f"Bearer {token}"), ("Authorization", f"Bearer {token}")],
    )
    assert response.status_code == 401
    assert response.json()["error"]["code"] == "auth_token_invalid"


def test_a_refusal_never_echoes_the_credential(make_client: Any, signing_key: Any) -> None:
    """Not in the body, not in a header, not in the request id."""
    _, jwk = signing_key
    secret = "sk-or-v1-a-real-looking-credential"
    response = make_client("enforce", jwk).get(
        PROTECTED, headers={"Authorization": f"Bearer {secret}"}
    )
    assert secret not in response.text
    assert not any(secret in value for value in response.headers.values())


def test_every_failure_still_carries_a_request_id(
    make_client: Any, signing_key: Any
) -> None:
    """A 401 is the response a tester is most likely to be reporting, so it is the
    one that can least afford to be uncorrelatable."""
    _, jwk = signing_key
    client = make_client("enforce", jwk)
    for headers in ({}, {"Authorization": "Bearer nonsense"}):
        assert client.get(PROTECTED, headers=headers).headers["X-Request-Id"]


# ---- what stays public ------------------------------------------------------


@pytest.mark.parametrize(
    "path", ["/healthz", "/readyz", "/version", "/v1/daintree/auth/config"]
)
def test_the_public_surface_stays_public_under_enforcement(
    make_client: Any, signing_key: Any, path: str
) -> None:
    """The probes because Cloud Run calls them, and `/auth/config` for a sharper
    reason: it is what a client reads to learn how to obtain a credential, so
    requiring one is a bootstrap nothing could satisfy."""
    _, jwk = signing_key
    assert make_client("enforce", jwk).get(path).status_code == 200


def test_doctor_needs_identity_but_not_a_permission(
    make_client: Any, signing_key: Any
) -> None:
    """A user must be able to ask why their access is blocked. Gating the
    diagnostic behind the thing being diagnosed is how someone gets stuck with no
    way to find out why."""
    private, jwk = signing_key
    client = make_client("enforce", jwk)
    assert client.post("/v1/daintree/doctor", json={}).status_code == 401
    response = client.post(
        "/v1/daintree/doctor", json={}, headers={"Authorization": f"Bearer {_token(private)}"}
    )
    assert response.status_code == 200


# ---- the wiring failures, which must not read as the caller's fault --------


def test_a_missing_verifier_under_enforce_is_a_dependency_error(make_client: Any) -> None:
    """Readiness refuses a non-open mode without complete configuration, so a
    verifier that is absent anyway is a WIRING failure. It must not be reported as
    a bad credential: the caller's token was never looked at, and a client acting
    on `auth_token_invalid` would discard a session that was never in question.
    """
    private = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    response = make_client("enforce", None).get(
        PROTECTED, headers={"Authorization": f"Bearer {_token(private)}"}
    )
    assert response.status_code == 503
    assert response.json()["error"]["code"] == "auth_dependency_unavailable"


def test_a_missing_verifier_under_observe_still_serves(make_client: Any) -> None:
    """Observe mode fails open on its own machinery, like every other part of it.
    A misconfigured observer must not become an outage."""
    private = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    response = make_client("observe", None).get(
        PROTECTED, headers={"Authorization": f"Bearer {_token(private)}"}
    )
    assert response.status_code == 200


def test_a_verifier_bug_does_not_break_an_observed_request(make_client: Any) -> None:
    """Observe mode's entire promise is that a caller cannot tell it is on. An
    unexpected failure inside the verifier — a bug, not a verdict — must not
    escape as a 500, which would break that promise in the loudest possible way
    and turn a measurement into an outage."""
    private = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    client = make_client("observe", None)

    class _Exploding:
        async def verify(self, token: str) -> None:
            raise RuntimeError("a bug in the verifier")

    client.app.state.token_verifier = _Exploding()
    response = client.get(PROTECTED, headers={"Authorization": f"Bearer {_token(private)}"})
    assert response.status_code == 200


def test_a_verifier_bug_under_enforce_is_a_dependency_error(make_client: Any) -> None:
    """Still not the caller's fault, so still not a stack trace — the same
    reasoning that keeps a JWKS outage from reading as an invalid credential."""
    private = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    client = make_client("enforce", None)

    class _Exploding:
        async def verify(self, token: str) -> None:
            raise RuntimeError("a bug in the verifier")

    client.app.state.token_verifier = _Exploding()
    response = client.get(PROTECTED, headers={"Authorization": f"Bearer {_token(private)}"})
    assert response.status_code == 503
    assert response.json()["error"]["code"] == "auth_dependency_unavailable"
