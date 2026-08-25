"""GET /v1/daintree/auth/config — public OAuth discovery.

This endpoint is the bootstrap: it is what a client reads to learn how to obtain a
credential, so it must answer to a caller that holds none. The CLI already encodes
that, listing this path beside the liveness probes as one it may call with no
Authorization header at all.
"""

from __future__ import annotations

from typing import Any

from fastapi.testclient import TestClient

from daintree_assistant_server.config import OAUTH_REDIRECT_URI, Settings
from daintree_assistant_server.main import create_app

URL = "/v1/daintree/auth/config"

_COMPLETE: dict[str, Any] = {
    "AUTH_MODE": "open",
    "SUPABASE_AUTH_ISSUER": "https://abcdefghij.supabase.co/auth/v1",
    "SUPABASE_JWKS_URL": "https://abcdefghij.supabase.co/auth/v1/.well-known/jwks.json",
    "SUPABASE_OAUTH_AUTHORIZE_URL": "https://abcdefghij.supabase.co/auth/v1/oauth/authorize",
    "SUPABASE_OAUTH_TOKEN_URL": "https://abcdefghij.supabase.co/auth/v1/oauth/token",
    "SUPABASE_OAUTH_CLIENT_ID": "daintree-assistant-staging",
    "DAINTREE_ACCOUNT_URL": "https://staging.daintree.org/account",
    "DAINTREE_SUBSCRIBE_URL": "https://staging.daintree.org/subscribe",
}


def _client(settings: Settings, provider: Any) -> TestClient:
    """A client over an app built from `settings`.

    Every call site uses it as a context manager, so the lifespan DOES run and
    normally marks the app ready. The deliberately-unready case has its own test
    below and does not go through here.
    """
    return TestClient(
        create_app(settings=settings, provider=provider), raise_server_exceptions=False
    )


def _configured_settings(**overrides: Any) -> Settings:
    values: dict[str, Any] = {
        "TOGETHER_API_KEY": "tgp_v1_test-key",
        "STATE_SIGNING_KEY_CURRENT": "test-signing-key",
        **_COMPLETE,
    }
    values.update(overrides)
    return Settings(**values)


def test_it_answers_without_any_credential(client: TestClient) -> None:
    r = client.get(URL)
    assert r.status_code == 200


def test_the_route_does_not_depend_on_authentication_at_all(client: TestClient) -> None:
    """`require_auth` currently accepts everyone, so merely calling the route
    without a header proves nothing — the assertion above would still pass if this
    route quietly gained `Depends(require_auth)`. Overriding the dependency with
    one that always fails is what actually proves discovery is outside the door,
    and it is the property that must survive the door being shut.
    """
    from daintree_assistant_server.auth import require_auth

    def _refuse() -> None:
        raise AssertionError("auth/config must not resolve the auth dependency")

    client.app.dependency_overrides[require_auth] = _refuse
    try:
        assert client.get(URL).status_code == 200
    finally:
        client.app.dependency_overrides.pop(require_auth, None)


def test_the_configured_fixture_actually_starts(fake_provider: Any) -> None:
    """Lifespan swallows every construction exception into a readiness failure and
    this route answers anyway — so an app whose startup silently broke would leave
    every other assertion in this file green. Pin that these fixtures really do
    come up."""
    with _client(_configured_settings(), fake_provider) as client:
        assert client.app.state.ready is True, client.app.state.ready_error


def test_an_unconfigured_deployment_reports_that_as_a_200(client: TestClient) -> None:
    """"There are no accounts here" is the accurate description of production
    today, not an error. A 404 or a 503 would make a working deployment
    indistinguishable from a broken one."""
    body = client.get(URL).json()
    assert body["version"] == 1
    assert body["configured"] is False
    assert body["required"] is False
    # No manifest at all, rather than a manifest of empty strings — a client must
    # not be handed an issuer it would then have to reject.
    assert "issuer" not in body
    assert "client_id" not in body


def test_the_always_present_fields_are_present_in_both_shapes(
    client: TestClient, fake_provider: Any
) -> None:
    """A client reads these unconditionally and only then decides whether the
    endpoints exist."""
    always = {"version", "environment", "configured", "required"}
    assert always <= set(client.get(URL).json())
    with _client(_configured_settings(), fake_provider) as configured:
        assert always <= set(configured.get(URL).json())


def test_the_manifest_version_is_published_in_both_shapes(fake_provider: Any) -> None:
    """The client checks `version` FIRST and refuses the whole manifest on a
    mismatch, so a body without it decodes as version 0 and every sign-in fails
    with "this backend describes auth configuration version 0".

    It is present in the unconfigured shape too: "a manifest I am too old to
    understand" and "a deployment with no accounts" are different answers, and a
    client that could not tell them apart would report the wrong one.

    This is the exception to CLAUDE.md's no-versioning rule, not a violation of
    it — that rule governs internal artifacts a lockstep client never negotiates
    over, and explicitly preserves wire metadata like `X-Daintree-Protocol`.
    """
    with _client(_configured_settings(), fake_provider) as client:
        assert client.get(URL).json()["version"] == 1
    unconfigured = Settings(
        TOGETHER_API_KEY="tgp_v1_test-key", STATE_SIGNING_KEY_CURRENT="test-signing-key"
    )
    with _client(unconfigured, fake_provider) as client:
        assert client.get(URL).json()["version"] == 1


def test_a_configured_deployment_publishes_the_manifest(fake_provider: Any) -> None:
    with _client(_configured_settings(), fake_provider) as client:
        body = client.get(URL).json()
    assert body["configured"] is True
    assert body["issuer"] == _COMPLETE["SUPABASE_AUTH_ISSUER"]
    assert body["authorization_endpoint"] == _COMPLETE["SUPABASE_OAUTH_AUTHORIZE_URL"]
    assert body["token_endpoint"] == _COMPLETE["SUPABASE_OAUTH_TOKEN_URL"]
    assert body["jwks_uri"] == _COMPLETE["SUPABASE_JWKS_URL"]
    assert body["client_id"] == _COMPLETE["SUPABASE_OAUTH_CLIENT_ID"]
    # Against the CONSTANT, not against a key in `_COMPLETE`. That dict is env,
    # and `Settings` is `extra="ignore"`, so a `SUPABASE_OAUTH_REDIRECT_URI` entry
    # was silently dropped on the way in and configured nothing. The assertion
    # still had force — it was an independent literal — but it described a setting
    # that does not exist, which is the opposite of what
    # `test_the_callback_is_a_constant_rather_than_a_setting` establishes, and it
    # is how a reader concludes the callback is per-deployment.
    assert body["redirect_uri"] == OAUTH_REDIRECT_URI
    assert body["account_url"] == _COMPLETE["DAINTREE_ACCOUNT_URL"]
    assert body["subscribe_url"] == _COMPLETE["DAINTREE_SUBSCRIBE_URL"]
    assert body["scopes"] == ["openid", "email"]
    assert body["session_policy"] == {
        "access_token_seconds": 3600,
        "session_max_age_seconds": 2592000,
    }


def test_a_configured_deployment_is_not_yet_required(fake_provider: Any) -> None:
    """`configured` and `required` are different questions. A deployment
    mid-rollout publishes a complete manifest while still serving anonymous
    requests; a client collapsing the two would either refuse to sign in when it
    could, or demand a sign-in nothing is enforcing.

    `AUTH_MODE=open` here, which is the posture a rollout publishes from. That it
    can only be false used to be a property of the BUILD — `enforce` was refused
    at readiness while nothing read a token — and it is now a property of this
    deployment's chosen mode, which the test below exercises the other half of.
    """
    with _client(_configured_settings(), fake_provider) as client:
        body = client.get(URL).json()
    assert body["configured"] is True
    assert body["required"] is False


def test_a_staging_deployment_publishes_the_contract_it_was_configured_with(
    fake_provider: Any,
) -> None:
    """The frozen staging contract, over HTTP, in the posture it will roll out in.

    The settings-level suite proves these values pass every readiness rule. This
    proves they come back OUT of the endpoint unchanged — the half a client
    actually reads, and the half a serializer or a field rename would break
    without failing anything above.

    `environment` matters on its own: the client checks it against a closed set
    and fails the whole manifest on a value outside it, so a staging deployment
    reporting `staging` is a cross-repo contract rather than a label.
    """
    staging = _configured_settings(ENVIRONMENT="staging", AUTH_MODE="observe")
    with _client(staging, fake_provider) as client:
        # Readiness is asserted, not assumed. This route deliberately answers
        # while the service is unready, so without this the test would pass
        # identically against a deployment that failed to start — reporting a
        # complete manifest for a revision that can serve no turns.
        assert client.app.state.ready is True, getattr(
            client.app.state, "ready_error", None
        )
        body = client.get(URL).json()
    assert body["environment"] == "staging"
    assert body["configured"] is True
    # `observe` verifies a token that is present and demands none, so a client is
    # told sign-in is possible and not yet mandatory.
    assert body["required"] is False
    assert body["redirect_uri"] == OAUTH_REDIRECT_URI
    assert body["account_url"] == "https://staging.daintree.org/account"
    assert body["subscribe_url"] == "https://staging.daintree.org/subscribe"
    # The four OAuth endpoints too, and NOT because they are tedious to leave out:
    # a body that sourced any of them from somewhere else — the account link, a
    # stale field — would be caught by nothing else here. The unit suite can only
    # see `Settings`, and this is the only test that reads a staging manifest off
    # the wire.
    assert body["issuer"] == _COMPLETE["SUPABASE_AUTH_ISSUER"]
    assert body["authorization_endpoint"] == _COMPLETE["SUPABASE_OAUTH_AUTHORIZE_URL"]
    assert body["token_endpoint"] == _COMPLETE["SUPABASE_OAUTH_TOKEN_URL"]
    assert body["jwks_uri"] == _COMPLETE["SUPABASE_JWKS_URL"]
    assert body["client_id"] == _COMPLETE["SUPABASE_OAUTH_CLIENT_ID"]


def test_the_browser_links_move_nothing_but_themselves(fake_provider: Any) -> None:
    """Where a browser is sent and where a token is minted are separate, proved
    by moving one and watching the other hold still.

    Asserting "the endpoints equal the Supabase settings" cannot establish this on
    its own — those are plain fields, so such a check passes as long as Pydantic
    stores what it was given. Two bodies differing ONLY in the link fields is what
    makes the claim falsifiable: if anything derived an OAuth endpoint from
    `DAINTREE_ACCOUNT_URL`, the two would differ somewhere they must not.

    It matters here because `daintree.org` is in BOTH trust anchors — it is a legal
    browser link and a legal issuer — so the separation cannot be inferred from the
    hostnames alone.
    """
    oauth_keys = (
        "issuer",
        "authorization_endpoint",
        "token_endpoint",
        "jwks_uri",
        "client_id",
        "redirect_uri",
        "scopes",
    )
    with _client(_configured_settings(ENVIRONMENT="staging"), fake_provider) as client:
        first = client.get(URL).json()
    moved = _configured_settings(
        ENVIRONMENT="staging",
        DAINTREE_ACCOUNT_URL="https://daintree.org/billing",
        DAINTREE_SUBSCRIBE_URL="https://daintree.org/plans",
    )
    with _client(moved, fake_provider) as client:
        second = client.get(URL).json()

    assert second["account_url"] == "https://daintree.org/billing"
    assert second["subscribe_url"] == "https://daintree.org/plans"
    assert first["account_url"] != second["account_url"]
    assert {k: first[k] for k in oauth_keys} == {k: second[k] for k in oauth_keys}


def test_enforce_is_the_one_posture_that_reports_required(fake_provider: Any) -> None:
    """`required` tracks the mode, and only `enforce` sets it.

    This is reachable over HTTP now. It was not while a non-`open` mode failed
    readiness, which is why the assertion beside it used to be phrased as a
    permanent truth about the build rather than a fact about one deployment.
    """
    enforcing = _configured_settings(ENVIRONMENT="staging", AUTH_MODE="enforce")
    with _client(enforcing, fake_provider) as client:
        assert client.app.state.ready is True, getattr(
            client.app.state, "ready_error", None
        )
        body = client.get(URL).json()
    assert body["configured"] is True
    assert body["required"] is True


def test_required_is_never_true_without_a_manifest_to_follow(fake_provider: Any) -> None:
    """Telling a client a credential is mandatory while withholding the only
    description of how to obtain one is a dead end. Asserted at the body builder,
    because no mode combination can currently produce it over HTTP."""
    from daintree_assistant_server.api.daintree_auth import _auth_config_body

    settings = Settings(
        TOGETHER_API_KEY="tgp_v1_test-key",
        STATE_SIGNING_KEY_CURRENT="test-signing-key",
        AUTH_MODE="enforce",
    )
    body = _auth_config_body(settings)
    assert body["configured"] is False
    assert body["required"] is False


def test_an_invalid_manifest_is_never_published(fake_provider: Any) -> None:
    """This endpoint answers while the service is unready, so readiness cannot be
    what filters it. A token endpoint typo'd onto another host must not be served
    to every client just because startup failed."""
    settings = _configured_settings(SUPABASE_OAUTH_TOKEN_URL="https://attacker.example/token")
    app = create_app(settings=settings, provider=fake_provider)
    client = TestClient(app, raise_server_exceptions=False)
    body = client.get(URL).json()
    assert body["configured"] is False
    assert "token_endpoint" not in body
    assert "attacker.example" not in client.get(URL).text


def test_it_answers_while_the_service_is_not_ready(fake_provider: Any) -> None:
    """Gating discovery on readiness would turn a bad upstream key into "you
    cannot log in either", which is both false and the least useful answer."""
    app = create_app(settings=_configured_settings(), provider=fake_provider)
    app.state.ready = False
    app.state.container = None
    client = TestClient(app, raise_server_exceptions=False)
    # No lifespan is entered, so nothing has ever marked this app ready.
    r = client.get(URL)
    assert r.status_code == 200
    assert r.json()["configured"] is True


def test_no_secret_reaches_the_body(fake_provider: Any) -> None:
    """Enforced by construction — every published field is a plain `str` setting,
    so there is no SecretStr on this path that COULD leak — and asserted anyway,
    because "by construction" is a claim a later edit can quietly break."""
    secrets = {
        "TOGETHER_API_KEY": "tgp_v1_together-secret",
        "OPENROUTER_API_KEY": "sk-or-openrouter-secret",
        "CEREBRAS_API_KEY": "csk-cerebras-secret",
        "STATE_SIGNING_KEY_CURRENT": "state-current-secret",
        "STATE_SIGNING_KEY_PREVIOUS": "state-previous-secret",
        "STATE_SALT": "state-salt-secret",
        "TRACE_IDENTITY_KEY": "trace-identity-secret",
    }
    with _client(_configured_settings(**secrets), fake_provider) as client:
        response = client.get(URL)
    raw = response.text
    for value in secrets.values():
        assert value not in raw, value
    assert not any(value in " ".join(response.headers.values()) for value in secrets.values())


def test_the_response_is_cacheable_with_a_content_derived_etag(fake_provider: Any) -> None:
    """`private` rather than `public`: the response also carries a per-request
    `X-Request-Id`, which a shared cache would replay to unrelated callers and
    make correlation ids meaningless. 60 seconds rather than 300 so a cached
    `configured: false` cannot keep clients from signing in long after a cutover.
    """
    with _client(_configured_settings(), fake_provider) as client:
        first = client.get(URL)
        second = client.get(URL)
    assert first.headers["Cache-Control"] == "private, max-age=60"
    assert first.headers["ETag"] == second.headers["ETag"]


def test_the_etag_is_taken_over_the_bytes_actually_served(fake_provider: Any) -> None:
    """A strong validator must change whenever the representation does. An ETag
    computed from a re-serialization of the same object describes bytes nobody
    ever sent — key order and unicode escaping are both free to differ."""
    import hashlib

    with _client(_configured_settings(), fake_provider) as client:
        response = client.get(URL)
    expected = '"' + hashlib.sha256(response.content).hexdigest()[:32] + '"'
    assert response.headers["ETag"] == expected


def test_a_matching_if_none_match_gets_a_304(fake_provider: Any) -> None:
    with _client(_configured_settings(), fake_provider) as client:
        etag = client.get(URL).headers["ETag"]
        conditional = client.get(URL, headers={"If-None-Match": etag})
    assert conditional.status_code == 304
    assert conditional.headers["ETag"] == etag
    assert not conditional.content


def test_a_stale_if_none_match_gets_the_body(fake_provider: Any) -> None:
    with _client(_configured_settings(), fake_provider) as client:
        response = client.get(URL, headers={"If-None-Match": '"stale"'})
    assert response.status_code == 200
    assert response.json()["configured"] is True


def test_a_changed_configuration_changes_the_etag(fake_provider: Any) -> None:
    """Content-derived rather than build-derived: two revisions with identical
    OAuth configuration should share an ETag, and a changed client id must not."""
    with _client(_configured_settings(), fake_provider) as a:
        first = a.get(URL).headers["ETag"]
    with _client(_configured_settings(SUPABASE_OAUTH_CLIENT_ID="other"), fake_provider) as b:
        second = b.get(URL).headers["ETag"]
    assert first != second
