"""``GET /v1/daintree/account`` end to end.

The three properties this file exists to hold:

* a known missing or inactive subscription is **200 data**, not an error — the
  route that explains a refusal must be reachable by the people being refused;
* a dependency failure is reported as itself, never flattened into "no plan";
* the raw subject, the email in a log, and the bearer never leave the process.
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
from structlog.testing import capture_logs

from daintree_assistant_server.auth import subject_hash
from daintree_assistant_server.config import Settings
from daintree_assistant_server.main import create_app
from daintree_assistant_server.services.entitlement import EntitlementService
from daintree_assistant_server.services.supabase_auth import (
    JwksCache,
    SupabaseTokenVerifier,
)

ISSUER = "https://abcdefghij.supabase.co/auth/v1"
CLIENT_ID = "daintree-assistant"
ENTITLEMENT_URL = "https://staging.daintree.org/api/assistant/entitlement"

_OAUTH: dict[str, Any] = {
    "SUPABASE_AUTH_ISSUER": ISSUER,
    "SUPABASE_JWKS_URL": f"{ISSUER}/.well-known/jwks.json",
    "SUPABASE_OAUTH_AUTHORIZE_URL": f"{ISSUER}/oauth/authorize",
    "SUPABASE_OAUTH_TOKEN_URL": f"{ISSUER}/oauth/token",
    "SUPABASE_OAUTH_CLIENT_ID": CLIENT_ID,
    "DAINTREE_ACCOUNT_URL": "https://staging.daintree.org/account",
    "DAINTREE_SUBSCRIBE_URL": "https://staging.daintree.org/subscribe",
}

SUBJECT = UUID("8f14e45f-ea8b-4c1d-9b2a-0000feedface")
EMAIL = "person@example.com"


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
        "sub": str(SUBJECT),
        "session_id": str(uuid4()),
        "role": "authenticated",
        "is_anonymous": False,
        "client_id": CLIENT_ID,
        "email": EMAIL,
        "aal": "aal1",
        "iat": int(now.timestamp()),
        "exp": int((now + timedelta(hours=1)).timestamp()),
    }
    claims.update(overrides)
    return jwt.encode(claims, private, algorithm="RS256", headers={"kid": "k1"})


def _entitlement_body(**overrides: Any) -> dict[str, Any]:
    body: dict[str, Any] = {
        "version": 1,
        "subject": str(SUBJECT),
        "access": "granted",
        "plan_id": "standard",
        "subscription_status": "active",
        "source": "polar",
        "stale": False,
        "checked_at": datetime.now(UTC).isoformat().replace("+00:00", "Z"),
    }
    body.update(overrides)
    return body


def _build(
    *,
    mode: str,
    jwk: dict[str, Any] | None,
    provider: Any,
    selector: Any,
    entitlement: httpx.Response | Exception | None = None,
) -> Any:
    settings = Settings(
        OPENAI_API_KEY="tgp_v1_test-key",
        STATE_SIGNING_KEY_CURRENT="test-signing-key",
        STATE_SIGNING_KID="k1",
        STATE_SALT="test-salt",
        RETRY_BACKOFF_BASE_SECONDS=0.0,
        AUTH_MODE=mode,
        **(_OAUTH if mode != "open" else {}),
        **({"DAINTREE_ENTITLEMENT_URL": ENTITLEMENT_URL} if entitlement else {}),
    )
    app = create_app(settings=settings, provider=provider, selector=selector)
    if jwk is not None:
        jwks_client = httpx.AsyncClient(
            transport=httpx.MockTransport(
                lambda _request: httpx.Response(200, json={"keys": [jwk]})
            )
        )
        app.state.token_verifier = SupabaseTokenVerifier(
            issuer=ISSUER,
            allowed_client_ids=frozenset({CLIENT_ID}),
            jwks=JwksCache(url=_OAUTH["SUPABASE_JWKS_URL"], client=jwks_client),
        )
        app.state.jwks_client = jwks_client
    if entitlement is not None:
        def handler(request: httpx.Request) -> httpx.Response:
            if isinstance(entitlement, Exception):
                raise entitlement
            return httpx.Response(
                status_code=entitlement.status_code,
                headers=entitlement.headers,
                content=entitlement.content,
                request=request,
            )

        app.state.entitlement_service = EntitlementService(
            httpx.AsyncClient(transport=httpx.MockTransport(handler)),
            url=ENTITLEMENT_URL,
        )
    app.state.ready = True
    return app


@pytest.fixture
def make_client(app: Any, fake_provider: Any) -> Any:
    def build(
        mode: str,
        jwk: dict[str, Any] | None = None,
        entitlement: httpx.Response | Exception | None = None,
    ) -> TestClient:
        application = _build(
            mode=mode,
            jwk=jwk,
            provider=fake_provider,
            selector=app.state.injected_selector,
            entitlement=entitlement,
        )
        application.state.container = app.state.container
        return TestClient(application, raise_server_exceptions=False)

    return build


def _json(response: httpx.Response) -> dict[str, Any]:
    body: dict[str, Any] = response.json()
    return body


# --- who may call it -------------------------------------------------------


@pytest.mark.parametrize("mode", ["open", "observe"])
def test_a_mode_with_no_authenticated_principal_refuses(
    make_client: Any, signing_key: Any, mode: str
) -> None:
    """The bar is not lowered to make this route callable earlier.

    Under ``open`` the header is not read at all; under ``observe`` a verified
    caller is deliberately returned as anonymous. In both there is no subject to
    describe, and inventing one would make ``observe`` change what a caller can
    observe — which is the single thing that mode promises not to do.
    """
    private, jwk = signing_key
    client = make_client(mode, jwk)
    response = client.get(
        "/v1/daintree/account",
        headers={"Authorization": f"Bearer {_token(private)}"},
    )
    assert response.status_code == 401
    assert _json(response)["error"]["code"] == "auth_required"
    # A GENUINE token, deliberately. The refusal must come from the mode's
    # anonymous principal rather than from the credential being bad — otherwise
    # this test would pass on a build that verified the header and then discarded
    # the result, which is a different (and wrong) reason to say no.
    assert _json(response)["error"]["code"] != "auth_token_invalid"


def test_enforce_without_a_credential_refuses(make_client: Any, signing_key: Any) -> None:
    _, jwk = signing_key
    response = make_client("enforce", jwk).get("/v1/daintree/account")
    assert response.status_code == 401
    assert _json(response)["error"]["code"] == "auth_required"


# --- the identity-only rollout ---------------------------------------------


def test_no_entitlement_url_reports_unverified_rather_than_failing(
    make_client: Any, signing_key: Any
) -> None:
    """The state of every deployment doing identity before billing.

    ``unverified`` says nothing was asked. A client must not read it as "no plan":
    nothing was asked, so nothing was refused.
    """
    private, jwk = signing_key
    response = make_client("enforce", jwk).get(
        "/v1/daintree/account",
        headers={"Authorization": f"Bearer {_token(private)}"},
    )
    assert response.status_code == 200
    body = _json(response)
    assert body["version"] == 1
    assert body["access"] == "unverified"
    assert body["email"] == EMAIL
    assert body["subject_hash"] == subject_hash(str(SUBJECT))
    # The entitlement-only fields are ABSENT, not defaulted. `plan_id: null` beside
    # `entitlement_stale: false` would read as three claims none of which were made.
    for field in (
        "plan_id",
        "subscription_status",
        "entitlement_source",
        "entitlement_stale",
        "checked_at",
    ):
        assert field not in body


def test_an_omitted_email_is_absent_rather_than_invented(
    make_client: Any, signing_key: Any
) -> None:
    private, jwk = signing_key
    token = _token(private, email=None)
    response = make_client("enforce", jwk).get(
        "/v1/daintree/account", headers={"Authorization": f"Bearer {token}"}
    )
    assert response.status_code == 200
    assert "email" not in _json(response)


# --- the entitlement verdicts ----------------------------------------------


def test_a_granted_account_reports_its_plan(make_client: Any, signing_key: Any) -> None:
    private, jwk = signing_key
    client = make_client("enforce", jwk, httpx.Response(200, json=_entitlement_body()))
    response = client.get(
        "/v1/daintree/account",
        headers={"Authorization": f"Bearer {_token(private)}"},
    )
    assert response.status_code == 200
    body = _json(response)
    assert body["access"] == "granted"
    assert body["plan_id"] == "standard"
    assert body["subscription_status"] == "active"
    assert body["entitlement_source"] == "polar"
    # Explicitly `false`, not absent: `exclude_none` must drop only None, or a
    # client reading "is this answer stale?" gets no answer at all.
    assert body["entitlement_stale"] is False
    # RFC 3339 with an offset, matching the documented example.
    assert datetime.fromisoformat(body["checked_at"]).tzinfo is not None


@pytest.mark.parametrize("access", ["subscription_required", "subscription_inactive"])
def test_a_missing_or_inactive_plan_is_two_hundred_data(
    make_client: Any, signing_key: Any, access: str
) -> None:
    """The property this endpoint exists for.

    These become typed 402s on a PAID request. Here they are status, because
    somebody with no plan has to be able to read their own account to discover
    that — gating the explanation behind the refusal is a loop with no exit.
    """
    private, jwk = signing_key
    client = make_client(
        "enforce",
        jwk,
        httpx.Response(200, json=_entitlement_body(access=access, plan_id=None)),
    )
    response = client.get(
        "/v1/daintree/account",
        headers={"Authorization": f"Bearer {_token(private)}"},
    )
    assert response.status_code == 200
    assert _json(response)["access"] == access
    assert "plan_id" not in _json(response)


def test_a_revoked_session_is_reported_as_itself(make_client: Any, signing_key: Any) -> None:
    """The website performs a fresh user check that local JWKS cannot.

    Flattening this into ``unverified`` would tell somebody whose session had just
    been revoked that their identity is fine and their plan merely unknown.
    """
    private, jwk = signing_key
    client = make_client("enforce", jwk, httpx.Response(401))
    response = client.get(
        "/v1/daintree/account",
        headers={"Authorization": f"Bearer {_token(private)}"},
    )
    assert response.status_code == 401
    assert _json(response)["error"]["code"] == "auth_session_revoked"


@pytest.mark.parametrize(
    "response_or_error",
    [
        pytest.param(httpx.Response(503), id="website_down"),
        pytest.param(httpx.Response(200, content=b"nonsense"), id="unreadable_body"),
        pytest.param(httpx.ConnectError("refused"), id="unreachable"),
    ],
)
def test_an_outage_is_never_reported_as_having_no_plan(
    make_client: Any, signing_key: Any, response_or_error: Any
) -> None:
    private, jwk = signing_key
    client = make_client("enforce", jwk, response_or_error)
    response = client.get(
        "/v1/daintree/account",
        headers={"Authorization": f"Bearer {_token(private)}"},
    )
    assert response.status_code == 503
    code = _json(response)["error"]["code"]
    assert code == "entitlement_unavailable"
    assert code != "subscription_required"


def test_the_entitlement_is_resolved_even_while_the_mode_is_off(
    make_client: Any, signing_key: Any
) -> None:
    """A mode makes a verdict BINDING; it does not make one obtainable.

    This is what lets the contract be exercised against the real website, by a
    real CLI, before it can deny anybody a turn.
    """
    private, jwk = signing_key
    client = make_client("enforce", jwk, httpx.Response(200, json=_entitlement_body()))
    assert client.app.state.settings.entitlement_mode == "off"
    response = client.get(
        "/v1/daintree/account",
        headers={"Authorization": f"Bearer {_token(private)}"},
    )
    assert _json(response)["access"] == "granted"


# --- headers, protocol and what never escapes -------------------------------


def test_the_response_is_never_stored(make_client: Any, signing_key: Any) -> None:
    """It describes one person's billing state, and the honest answer changes the
    moment they pay or their session ends."""
    private, jwk = signing_key
    response = make_client("enforce", jwk).get(
        "/v1/daintree/account",
        headers={"Authorization": f"Bearer {_token(private)}"},
    )
    assert response.headers["cache-control"] == "private, no-store"


def test_it_carries_the_request_id(make_client: Any, signing_key: Any) -> None:
    private, jwk = signing_key
    response = make_client("enforce", jwk).get(
        "/v1/daintree/account",
        headers={
            "Authorization": f"Bearer {_token(private)}",
            "X-Request-Id": "safe-id-123",
        },
    )
    # The SUPPLIED id, adopted — that is what makes a caller's logs joinable to
    # ours. Merely asserting non-emptiness would pass on a minted one.
    assert response.headers["x-request-id"] == "safe-id-123"


def test_an_unsupported_protocol_is_refused(make_client: Any, signing_key: Any) -> None:
    private, jwk = signing_key
    response = make_client("enforce", jwk).get(
        "/v1/daintree/account",
        headers={
            "Authorization": f"Bearer {_token(private)}",
            "X-Daintree-Protocol": "999",
        },
    )
    assert response.status_code == 426
    assert _json(response)["error"]["code"] == "unsupported_daintree_protocol"


def test_the_raw_subject_never_reaches_the_response_or_the_log(
    make_client: Any, signing_key: Any
) -> None:
    """This body is rendered in terminals and pasted into issues."""
    private, jwk = signing_key
    token = _token(private)
    client = make_client("enforce", jwk, httpx.Response(200, json=_entitlement_body()))
    with capture_logs() as logs:
        response = client.get(
            "/v1/daintree/account", headers={"Authorization": f"Bearer {token}"}
        )
    rendered = response.text + json.dumps(logs)
    assert str(SUBJECT) not in rendered
    assert token not in rendered
    # The email is display data in the BODY and is deliberately never logged.
    assert EMAIL in response.text
    assert EMAIL not in json.dumps(logs)


# --- the cases the first pass missed ----------------------------------------


def _envelope(code: str, status: int, **headers: str) -> httpx.Response:
    """A website answer that names its own error code in the Daintree envelope."""
    return httpx.Response(
        status,
        content=json.dumps({"error": {"code": code}}).encode("utf-8"),
        headers={"content-type": "application/json", **headers},
    )


@pytest.mark.parametrize(
    "entitlement,expected_status",
    [
        pytest.param(httpx.Response(401), 401, id="revoked"),
        pytest.param(httpx.Response(403), 403, id="client_not_allowed"),
        pytest.param(
            _envelope("account_rate_limited", 429, **{"retry-after": "30"}),
            429,
            id="rate_limited",
        ),
        pytest.param(httpx.Response(503), 503, id="unavailable"),
        # Neither of these is a fact about the caller. The website refusing a
        # subject this server's own staging gate already admitted means the two
        # configured allowlists have drifted; an opaque 429 with no Daintree
        # envelope is an edge limit counting this server's shared egress address.
        # Both are retryable dependency uncertainty.
        pytest.param(_envelope("auth_permission_denied", 403), 503, id="gate_drift"),
        pytest.param(
            httpx.Response(429, headers={"retry-after": "60"}, content=b"<html>429</html>"),
            503,
            id="edge_throttle",
        ),
    ],
)
def test_every_response_carries_the_cache_policy(
    make_client: Any, signing_key: Any, entitlement: Any, expected_status: int
) -> None:
    """A policy that applies only when things go well is the one that gets
    forgotten. These describe the state of somebody's session."""
    private, jwk = signing_key
    client = make_client("enforce", jwk, entitlement)
    response = client.get(
        "/v1/daintree/account",
        headers={"Authorization": f"Bearer {_token(private)}"},
    )
    assert response.status_code == expected_status
    assert response.headers["cache-control"] == "private, no-store"


def test_a_refusal_before_authentication_still_carries_the_cache_policy(
    make_client: Any, signing_key: Any
) -> None:
    _, jwk = signing_key
    response = make_client("enforce", jwk).get("/v1/daintree/account")
    assert response.status_code == 401
    assert response.headers["cache-control"] == "private, no-store"


def test_a_rate_limit_preserves_its_retry_after_alongside_the_cache_policy(
    make_client: Any, signing_key: Any
) -> None:
    """The cache header is MERGED, not assigned — an error's own headers matter."""
    private, jwk = signing_key
    client = make_client(
        "enforce", jwk, _envelope("account_rate_limited", 429, **{"retry-after": "30"})
    )
    response = client.get(
        "/v1/daintree/account",
        headers={"Authorization": f"Bearer {_token(private)}"},
    )
    assert response.headers["retry-after"] == "30"
    assert response.headers["cache-control"] == "private, no-store"
    assert _json(response)["error"]["code"] == "account_rate_limited"


def test_a_drifted_gate_is_retryable_uncertainty_rather_than_an_identity_verdict(
    make_client: Any, signing_key: Any
) -> None:
    """`/account` is where somebody looks to find out what is wrong, so it is the
    worst place to answer with a verdict that is not true.

    This server's own copy of the staging subject allowlist refused every
    non-member before the lookup opened, so the website disagreeing can only be
    configuration drift — and the credential must survive it untouched.
    """
    private, jwk = signing_key
    client = make_client("enforce", jwk, _envelope("auth_permission_denied", 403))
    response = client.get(
        "/v1/daintree/account",
        headers={"Authorization": f"Bearer {_token(private)}"},
    )
    assert response.status_code == 503
    assert _json(response)["error"]["code"] == "entitlement_unavailable"
    assert "www-authenticate" not in response.headers


def test_an_edge_throttle_does_not_borrow_the_caller_s_name_or_its_delay(
    make_client: Any, signing_key: Any
) -> None:
    """The address an edge limit counts is this server's shared Cloud Run egress
    IP, so the delay it hands back is about the whole instance. Passing it on
    would wake every caller behind this server at the same second."""
    private, jwk = signing_key
    client = make_client(
        "enforce",
        jwk,
        httpx.Response(429, headers={"retry-after": "60"}, content=b"<html>429</html>"),
    )
    response = client.get(
        "/v1/daintree/account",
        headers={"Authorization": f"Bearer {_token(private)}"},
    )
    assert response.status_code == 503
    assert _json(response)["error"]["code"] == "entitlement_unavailable"
    assert "retry-after" not in response.headers


def test_an_unready_service_still_answers(make_client: Any, signing_key: Any) -> None:
    """This route needs the verifier and the entitlement client, neither of which
    is part of the model-serving container.

    A missing upstream key would otherwise make "what does my account look like?"
    answer 503 — turning an unrelated outage into an apparent account problem at
    exactly the moment somebody is trying to tell the two apart.
    """
    private, jwk = signing_key
    client = make_client("enforce", jwk)
    client.app.state.ready = False
    client.app.state.container = None
    response = client.get(
        "/v1/daintree/account",
        headers={"Authorization": f"Bearer {_token(private)}"},
    )
    assert response.status_code == 200
    assert _json(response)["access"] == "unverified"


def test_a_configured_but_unbuilt_client_is_an_outage_not_a_rollout(
    make_client: Any, signing_key: Any
) -> None:
    """A wiring failure must not wear the same 200 as a deliberate identity-only
    rollout — `unverified` would report the outage as a settled fact and nobody
    would go looking."""
    private, jwk = signing_key
    client = make_client("enforce", jwk, httpx.Response(200, json=_entitlement_body()))
    client.app.state.entitlement_service = None
    response = client.get(
        "/v1/daintree/account",
        headers={"Authorization": f"Bearer {_token(private)}"},
    )
    assert response.status_code == 503
    assert _json(response)["error"]["code"] == "entitlement_unavailable"


def test_an_implausibly_long_email_is_omitted_rather_than_crashing(
    make_client: Any, signing_key: Any
) -> None:
    """The verifier accepts whatever the signed token carries; the response model
    caps it. Without a guard the mismatch is a 500 whose log quotes the email."""
    private, jwk = signing_key
    token = _token(private, email="x" * 400 + "@example.com")
    with capture_logs() as logs:
        response = make_client("enforce", jwk).get(
            "/v1/daintree/account", headers={"Authorization": f"Bearer {token}"}
        )
    assert response.status_code == 200
    assert "email" not in _json(response)
    assert "xxxxxxxxxx" not in json.dumps(logs)


def test_a_pro_plan_and_a_stale_cached_answer_are_both_reported(
    make_client: Any, signing_key: Any
) -> None:
    """`stale` is the website telling us its own lookup was not fresh. It is a
    legal, useful answer and must reach the client rather than be refused."""
    private, jwk = signing_key
    client = make_client(
        "enforce",
        jwk,
        httpx.Response(200, json=_entitlement_body(plan_id="pro", source="cache", stale=True)),
    )
    body = _json(
        client.get(
            "/v1/daintree/account",
            headers={"Authorization": f"Bearer {_token(private)}"},
        )
    )
    assert body["plan_id"] == "pro"
    assert body["entitlement_source"] == "cache"
    assert body["entitlement_stale"] is True


def test_an_answer_about_another_subject_is_refused(
    make_client: Any, signing_key: Any
) -> None:
    """A mismatch is an upstream protocol or security failure, never another
    account's answer to be reported as this one's."""
    private, jwk = signing_key
    client = make_client(
        "enforce",
        jwk,
        httpx.Response(200, json=_entitlement_body(subject=str(uuid4()))),
    )
    response = client.get(
        "/v1/daintree/account",
        headers={"Authorization": f"Bearer {_token(private)}"},
    )
    assert response.status_code == 503
    assert _json(response)["error"]["code"] == "entitlement_unavailable"


def test_two_authorization_headers_never_reach_the_billing_authority(
    make_client: Any, signing_key: Any
) -> None:
    """Refused at the door, before any credential is forwarded anywhere."""
    private, jwk = signing_key
    token = _token(private)
    client = make_client("enforce", jwk, httpx.Response(200, json=_entitlement_body()))
    response = client.get(
        "/v1/daintree/account",
        headers=[("Authorization", f"Bearer {token}"), ("Authorization", f"Bearer {token}")],
    )
    assert response.status_code == 401
    assert not service_calls(client)


def service_calls(client: TestClient) -> list[Any]:
    service = client.app.state.entitlement_service
    return list(service._cache) if service is not None else []


def test_a_token_pasted_into_the_protocol_header_is_never_echoed(
    make_client: Any, signing_key: Any
) -> None:
    """The realistic way that header is malformed is a client-side mix-up.

    The request authenticates fine and then gets a 426 — whose body must not
    contain the credential it just accepted.
    """
    private, jwk = signing_key
    token = _token(private)
    response = make_client("enforce", jwk).get(
        "/v1/daintree/account",
        headers={"Authorization": f"Bearer {token}", "X-Daintree-Protocol": token},
    )
    assert response.status_code == 426
    assert token not in response.text
