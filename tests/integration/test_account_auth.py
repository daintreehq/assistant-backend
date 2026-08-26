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
from uuid import UUID, uuid4

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


# ---- the private-staging subject gate --------------------------------------
#
# `enforce` proves a token is genuine. Website signup is public and deliberate,
# so on its own that admits every verified account of the one allowlisted OAuth
# client — which is the correct posture for the product and the wrong one for a
# private staging deployment. These cover the second question: did the operator
# name this person, and does a "no" land before anything is spent?
#
# No real digest appears here. Every value is computed from a synthetic UUID
# through the shipped helper, which proves the algorithm as well as the gate.

GATE_SUBJECT = UUID("deadbeef-0b0a-4a9b-8c7d-feedfacecafe")
STRANGER = UUID("0badc0de-0b0a-4a9b-8c7d-feedfacecafe")
GATE_REVISION = "beta-01"
GATE_ENTITLEMENT_URL = "https://staging.daintree.org/api/assistant/entitlement"


def _respond_body() -> dict[str, Any]:
    return {
        "protocol_version": 3,
        "session": {"id": "s", "turn_id": "t"},
        "startup": {},
        "input": {"messages": [{"role": "user", "content": "hello"}]},
    }


def _task_body() -> dict[str, Any]:
    return {"task": "terminal_summarize", "input": {"output": "some output"}}


FUNDED_ROUTES = [
    ("/v1/daintree/respond", _respond_body()),
    ("/v1/daintree/tasks", _task_body()),
]


class _Website:
    """Counts what was ASKED, not just what was concluded.

    The gate's whole claim is that a refusal lands before the entitlement lookup
    opens, and only a call counter can tell "we refused" from "we asked and then
    refused" — the second of which has already forwarded the caller's bearer to
    another service.
    """

    def __init__(self, body: dict[str, Any]) -> None:
        self._body = body
        self.calls = 0

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.calls += 1
        return httpx.Response(200, json=self._body, request=request)


def _entitlement_body(subject: UUID, **overrides: Any) -> dict[str, Any]:
    body: dict[str, Any] = {
        "version": 1,
        "subject": str(subject),
        "access": "granted",
        "plan_id": "standard",
        "subscription_status": "active",
        "source": "polar",
        "stale": False,
        "checked_at": datetime.now(UTC).isoformat().replace("+00:00", "Z"),
    }
    body.update(overrides)
    return body


@pytest.fixture
def make_gate_client(app: Any, fake_provider: Any) -> Any:
    """A staging deployment with enforcement on and a named membership."""

    from daintree_assistant_server.services.entitlement import EntitlementService

    def build(
        jwk: dict[str, Any],
        *,
        digests: str,
        revision: str = GATE_REVISION,
        website: _Website | None = None,
        complete: bool = True,
    ) -> TestClient:
        settings = Settings(
            ENVIRONMENT="staging",
            TOGETHER_API_KEY="tgp_v1_test-key",
            CEREBRAS_API_KEY="csk-test-key",
            STATE_SIGNING_KEY_CURRENT="test-signing-key",
            STATE_SIGNING_KID="k1",
            STATE_SALT="test-salt",
            RETRY_BACKOFF_BASE_SECONDS=0.0,
            AUTH_MODE="enforce",
            STAGING_ALLOWED_SUBJECT_DIGESTS=digests,
            STAGING_SUBJECT_GATE_REVISION=revision,
            ENTITLEMENT_MODE="enforce" if website else "off",
            **_OAUTH,
            **({"DAINTREE_ENTITLEMENT_URL": GATE_ENTITLEMENT_URL} if website else {}),
        )
        # The configuration a real staging deployment would have to satisfy before
        # it could serve at all: the gate is complete, so nothing below is testing
        # a posture readiness would have refused. `complete=False` is for the one
        # case that is ABOUT an unready deployment — `/version` answers while the
        # service is red, which is the whole reason it exists.
        assert (settings.subject_gate_problems() == []) is complete
        application = create_app(
            settings=settings, provider=fake_provider, selector=app.state.injected_selector
        )
        jwks_client = httpx.AsyncClient(
            transport=httpx.MockTransport(lambda _r: httpx.Response(200, json={"keys": [jwk]}))
        )
        application.state.token_verifier = SupabaseTokenVerifier(
            issuer=ISSUER,
            allowed_client_ids=frozenset({CLIENT_ID}),
            jwks=JwksCache(url=_OAUTH["SUPABASE_JWKS_URL"], client=jwks_client),
        )
        application.state.jwks_client = jwks_client
        if website is not None:
            application.state.entitlement_service = EntitlementService(
                httpx.AsyncClient(transport=httpx.MockTransport(website.handler)),
                url=GATE_ENTITLEMENT_URL,
            )
        application.state.container = app.state.container
        application.state.ready = True
        return TestClient(application, raise_server_exceptions=False)

    return build


def _member_list() -> str:
    """A membership of exactly one, written the way an operator would write it."""
    from daintree_assistant_server.auth import subject_digest

    return subject_digest(str(GATE_SUBJECT)) + "\n"


def test_the_gate_admits_the_subject_the_operator_named(
    make_gate_client: Any, signing_key: Any
) -> None:
    private, jwk = signing_key
    client = make_gate_client(jwk, digests=_member_list())
    response = client.get(
        PROTECTED, headers={"Authorization": f"Bearer {_token(private, sub=str(GATE_SUBJECT))}"}
    )
    assert response.status_code == 200


def test_the_gate_refuses_a_verified_subject_nobody_approved(
    make_gate_client: Any, signing_key: Any
) -> None:
    """403, not 401, and no challenge.

    The credential is genuine and current, so it must be KEPT: refreshing mints
    another token refused in exactly the same way, and what is missing is an
    operator's approval rather than a login.
    """
    private, jwk = signing_key
    client = make_gate_client(jwk, digests=_member_list())
    response = client.get(
        PROTECTED, headers={"Authorization": f"Bearer {_token(private, sub=str(STRANGER))}"}
    )
    assert response.status_code == 403
    assert response.json()["error"]["code"] == "auth_permission_denied"
    assert response.json()["error"]["type"] == "authentication_error"
    assert "WWW-Authenticate" not in response.headers


def test_an_uppercase_spelling_of_a_listed_subject_is_still_admitted(
    make_gate_client: Any, signing_key: Any
) -> None:
    """The canonicalization hazard, on the wire.

    `_require_uuid` compares the `sub` claim against `value.lower()`, so an
    uppercase-hex UUID is ACCEPTED as canonical while spelling itself differently
    from the `UUID` object's own form. A door that hashed the raw claim would
    compute a second digest for one account and refuse the person it just
    admitted, depending on nothing but how their token was minted.
    """
    private, jwk = signing_key
    client = make_gate_client(jwk, digests=_member_list())
    response = client.get(
        PROTECTED,
        headers={"Authorization": f"Bearer {_token(private, sub=str(GATE_SUBJECT).upper())}"},
    )
    assert response.status_code == 200


def test_a_list_that_holds_the_uncanonical_digest_admits_nobody(
    make_gate_client: Any, signing_key: Any
) -> None:
    """The same hazard from the other side: a digest of the UPPERCASE spelling is
    not the key the door computes, so a list written that way is a list of one
    account that can never sign in."""
    from daintree_assistant_server.auth import subject_digest

    private, jwk = signing_key
    client = make_gate_client(jwk, digests=subject_digest(str(GATE_SUBJECT).upper()))
    for spelling in (str(GATE_SUBJECT), str(GATE_SUBJECT).upper()):
        response = client.get(
            PROTECTED, headers={"Authorization": f"Bearer {_token(private, sub=spelling)}"}
        )
        assert response.status_code == 403, spelling


@pytest.mark.parametrize("path,body", FUNDED_ROUTES)
def test_a_refused_subject_reaches_neither_the_website_nor_the_provider(
    make_gate_client: Any, signing_key: Any, fake_provider: Any, path: str, body: dict[str, Any]
) -> None:
    """Why the check sits where it sits.

    Everything downstream of it spends something: the entitlement lookup forwards
    the caller's own bearer to the website, and past that the turn opens a
    provider call funded by Daintree's key. A refusal has to land while there is
    still nothing to undo.
    """
    private, jwk = signing_key
    website = _Website(_entitlement_body(STRANGER))
    client = make_gate_client(jwk, digests=_member_list(), website=website)
    response = client.post(
        path, json=body, headers={"Authorization": f"Bearer {_token(private, sub=str(STRANGER))}"}
    )
    assert response.status_code == 403
    assert response.json()["error"]["code"] == "auth_permission_denied"
    assert website.calls == 0
    assert fake_provider.requests == []
    assert fake_provider.stream_requests == []


@pytest.mark.parametrize("path,body", FUNDED_ROUTES)
def test_an_admitted_subject_reaches_entitlement_and_is_served(
    make_gate_client: Any, signing_key: Any, path: str, body: dict[str, Any]
) -> None:
    """The gate narrows WHO may ask the billing question; it does not answer it."""
    private, jwk = signing_key
    website = _Website(_entitlement_body(GATE_SUBJECT))
    client = make_gate_client(jwk, digests=_member_list(), website=website)
    response = client.post(
        path,
        json=body,
        headers={"Authorization": f"Bearer {_token(private, sub=str(GATE_SUBJECT))}"},
    )
    assert response.status_code == 200
    assert website.calls == 1


def test_an_admitted_subject_without_a_plan_still_gets_the_billing_answer(
    make_gate_client: Any, signing_key: Any
) -> None:
    """Membership is not a subscription. Someone the operator invited who has not
    subscribed must be told to subscribe, not told they were not invited — the two
    have entirely different remedies."""
    private, jwk = signing_key
    website = _Website(
        _entitlement_body(GATE_SUBJECT, access="subscription_required", plan_id=None)
    )
    client = make_gate_client(jwk, digests=_member_list(), website=website)
    response = client.post(
        "/v1/daintree/respond",
        json=_respond_body(),
        headers={"Authorization": f"Bearer {_token(private, sub=str(GATE_SUBJECT))}"},
    )
    assert response.status_code == 402
    assert response.json()["error"]["code"] == "subscription_required"


def test_a_wrong_oauth_client_is_still_refused_as_a_client(
    make_gate_client: Any, signing_key: Any
) -> None:
    """Order matters, and it is guaranteed by construction rather than by luck:
    the verifier refuses an unallowlisted client before it even parses `sub`, so
    the gate cannot reach a token it should never have seen. Telling someone their
    subject is not approved when their OAuth client is the actual problem sends
    them to the wrong person."""
    private, jwk = signing_key
    client = make_gate_client(jwk, digests=_member_list())
    response = client.get(
        PROTECTED,
        headers={
            "Authorization": f"Bearer {_token(private, sub=str(STRANGER), client_id='website')}"
        },
    )
    assert response.status_code == 403
    assert response.json()["error"]["code"] == "auth_client_not_allowed"


def test_an_anonymous_request_is_still_auth_required_under_the_gate(
    make_gate_client: Any, signing_key: Any
) -> None:
    _, jwk = signing_key
    response = make_gate_client(jwk, digests=_member_list()).get(PROTECTED)
    assert response.status_code == 401
    assert response.json()["error"]["code"] == "auth_required"


@pytest.mark.parametrize("path,body", FUNDED_ROUTES)
def test_the_funded_routes_require_a_credential_under_enforcement(
    make_client: Any, signing_key: Any, fake_provider: Any, path: str, body: dict[str, Any]
) -> None:
    """`auth_required` was proven on the read-only routes and nowhere near the two
    that spend money — which are the only ones where being wrong costs anything.
    No gate involved: this is plain `AUTH_MODE=enforce`."""
    _, jwk = signing_key
    response = make_client("enforce", jwk).post(path, json=body)
    assert response.status_code == 401
    assert response.json()["error"]["code"] == "auth_required"
    assert response.headers["WWW-Authenticate"] == "Bearer"
    assert fake_provider.requests == []
    assert fake_provider.stream_requests == []


@pytest.mark.parametrize(
    "path", ["/healthz", "/readyz", "/version", "/v1/daintree/auth/config"]
)
def test_the_public_surface_stays_public_under_the_gate(
    make_gate_client: Any, signing_key: Any, path: str
) -> None:
    """A private deployment still has to be probeable by Cloud Run, and a client
    still has to be able to read how to sign in — otherwise the only people who
    could discover they are not approved are the ones already approved."""
    _, jwk = signing_key
    assert make_gate_client(jwk, digests=_member_list()).get(path).status_code == 200


def test_version_publishes_only_the_safe_projection(
    make_gate_client: Any, signing_key: Any
) -> None:
    """Three facts and no fourth. A public unauthenticated endpoint that could be
    asked about membership would be a membership oracle, which is worse than
    publishing the list because nobody would notice it being read."""
    from daintree_assistant_server.auth import subject_digest

    _, jwk = signing_key
    client = make_gate_client(jwk, digests=_member_list())
    response = client.get("/version")
    posture = response.json()["posture"]
    assert posture["subject_gate"] == {
        "enabled": True,
        "entries": 1,
        "revision": GATE_REVISION,
    }
    assert subject_digest(str(GATE_SUBJECT)) not in response.text
    assert str(GATE_SUBJECT) not in response.text


def test_version_reports_the_gate_off_where_it_decides_nothing(
    make_client: Any, signing_key: Any
) -> None:
    _, jwk = signing_key
    posture = make_client("enforce", jwk).get("/version").json()["posture"]
    assert posture["subject_gate"] == {"enabled": False, "entries": 0, "revision": ""}


def test_the_auth_manifest_never_mentions_the_gate(
    make_gate_client: Any, signing_key: Any
) -> None:
    """`/v1/daintree/auth/config` is a CLIENT contract about how to obtain a
    credential. Whether a particular subject will then be admitted is not
    something an unauthenticated caller may enumerate."""
    from daintree_assistant_server.auth import subject_digest

    _, jwk = signing_key
    response = make_gate_client(jwk, digests=_member_list()).get("/v1/daintree/auth/config")
    assert subject_digest(str(GATE_SUBJECT)) not in response.text
    assert "subject" not in response.text.lower()


def test_a_refusal_logs_the_support_hash_and_nothing_else(
    make_gate_client: Any, signing_key: Any
) -> None:
    """The 16-character support hash is the only identifier that may travel: the
    raw subject stays on the principal, and the full digest is the gate's own key,
    so logging it would publish a membership token to every log reader."""
    from structlog.testing import capture_logs

    from daintree_assistant_server.auth import subject_digest, subject_hash

    private, jwk = signing_key
    client = make_gate_client(jwk, digests=_member_list())
    token = _token(private, sub=str(STRANGER))
    with capture_logs() as logs:
        assert client.get(PROTECTED, headers={"Authorization": f"Bearer {token}"}).status_code == 403

    refusals = [entry for entry in logs if entry.get("event") == "auth_subject_not_allowed"]
    assert refusals, "a refusal nobody can correlate is a refusal nobody can support"
    assert refusals[0]["subject_hash"] == subject_hash(str(STRANGER))
    assert refusals[0]["gate_revision"] == GATE_REVISION

    rendered = repr(logs)
    assert str(STRANGER) not in rendered
    assert subject_digest(str(STRANGER)) not in rendered
    assert subject_digest(str(GATE_SUBJECT)) not in rendered
    assert token not in rendered


def test_a_refusal_body_carries_neither_the_subject_nor_a_digest(
    make_gate_client: Any, signing_key: Any
) -> None:
    from daintree_assistant_server.auth import subject_digest

    private, jwk = signing_key
    client = make_gate_client(jwk, digests=_member_list())
    response = client.get(
        PROTECTED, headers={"Authorization": f"Bearer {_token(private, sub=str(STRANGER))}"}
    )
    assert str(STRANGER) not in response.text
    assert subject_digest(str(STRANGER)) not in response.text
    assert subject_digest(str(GATE_SUBJECT)) not in response.text
    assert not any(str(STRANGER) in value for value in response.headers.values())


def test_version_withholds_a_label_that_never_validated(
    make_gate_client: Any, signing_key: Any
) -> None:
    """`/version` answers while the service is UNREADY, and that is the point of it.

    So "readiness refuses a malformed label" is not on its own what makes the
    label safe to publish: the deployment whose label is wrong is exactly the one
    an operator is reading this endpoint to understand. A digest pasted into the
    wrong variable must not be published by the mistake that made it unsafe — it
    reads as absent instead.
    """
    from daintree_assistant_server.auth import subject_digest

    _, jwk = signing_key
    leaked = subject_digest(str(GATE_SUBJECT))
    client = make_gate_client(
        jwk, digests=_member_list(), revision=leaked, complete=False
    )
    response = client.get("/version")
    assert response.json()["posture"]["subject_gate"]["revision"] == ""
    assert leaked not in response.text
