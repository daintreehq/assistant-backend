"""The OAuth deployment configuration, and the readiness rules over it.

These values are the only thing standing between a client and a login it cannot
complete. The CLI is instructed to REFUSE a discovery manifest whose endpoints are
not HTTPS, do not share the issuer's origin, or whose callback is not its own
compiled loopback URI — so a malformed value here does not degrade the login, it
makes every client throw the manifest away. That is why validation runs in every
mode, including the ``open`` posture that reads no tokens at all.
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from daintree_assistant_server.config import (
    OAUTH_REDIRECT_URI,
    Settings,
    _loopback_redirect_problem,
)

_ISSUER = "https://abcdefghij.supabase.co/auth/v1"
_JWKS = "https://abcdefghij.supabase.co/auth/v1/.well-known/jwks.json"
_AUTHORIZE = "https://abcdefghij.supabase.co/auth/v1/oauth/authorize"
_TOKEN = "https://abcdefghij.supabase.co/auth/v1/oauth/token"


#: The values a manifest must actually have. The callback is absent because it is
#: a CONSTANT rather than a setting, and the two links are absent because the
#: client treats them as optional — requiring them would have withheld an entire
#: working manifest over a missing subscribe page.
_MANIFEST_ENV = (
    "SUPABASE_AUTH_ISSUER",
    "SUPABASE_JWKS_URL",
    "SUPABASE_OAUTH_AUTHORIZE_URL",
    "SUPABASE_OAUTH_TOKEN_URL",
    "SUPABASE_OAUTH_CLIENT_ID",
)


def _settings(**overrides: object) -> Settings:
    base: dict[str, object] = {
        "STATE_SIGNING_KEY_CURRENT": "x" * 32,
        "OPENAI_API_KEY": "sk-server-owned",
        "CEREBRAS_API_KEY": "csk-server-owned",
    }
    base.update(overrides)
    return Settings(**base)  # type: ignore[arg-type]


def _complete(**overrides: object) -> Settings:
    base: dict[str, object] = {
        "SUPABASE_AUTH_ISSUER": _ISSUER,
        "SUPABASE_JWKS_URL": _JWKS,
        "SUPABASE_OAUTH_AUTHORIZE_URL": _AUTHORIZE,
        "SUPABASE_OAUTH_TOKEN_URL": _TOKEN,
        "SUPABASE_OAUTH_CLIENT_ID": "daintree-assistant-staging",
        "DAINTREE_ACCOUNT_URL": "https://staging.daintree.org/account",
        "DAINTREE_SUBSCRIBE_URL": "https://staging.daintree.org/subscribe",
    }
    base.update(overrides)
    return _settings(**base)


# ---- The default posture must not move -------------------------------------


def test_auth_mode_defaults_to_open() -> None:
    """A deployment that sets nothing keeps today's behaviour exactly.

    The rollback story for this whole feature is "set the mode back and roll a
    config-only revision"; a default of anything else would make the feature's
    end state the fleet's starting state.
    """
    assert _settings().auth_mode == "open"


def test_an_unconfigured_deployment_is_ready() -> None:
    """Production runs the open door today. Requiring OAuth configuration to be
    present would fail `/readyz` on the very next release, and the revision would
    never take traffic.

    Asserted through `local_only_problems()` as well, because that is the list
    lifespan actually reads — `auth_problems()` being empty proves nothing if
    something else refuses the same deployment for an auth-shaped reason.
    """
    assert _settings().auth_problems() == []
    assert _settings().local_only_problems() == []
    production = _settings(ENVIRONMENT="production")
    assert production.auth_problems() == []
    assert not [p for p in production.local_only_problems() if "AUTH" in p or "SUPABASE" in p]


@pytest.mark.parametrize("mode", ["open", "observe", "enforce"])
def test_every_documented_mode_parses(mode: str) -> None:
    assert _complete(AUTH_MODE=mode).auth_mode == mode


def test_the_manifest_itself_is_not_condemned_by_an_unsupported_mode() -> None:
    """`auth_manifest_problems` answers "are these values safe to publish", which
    has nothing to do with whether this build can enforce. Collapsing the two
    would make /auth/config withhold a perfectly good manifest."""
    settings = _complete(AUTH_MODE="enforce")
    assert settings.auth_manifest_problems() == []
    assert settings.auth_is_configured


def test_an_unknown_mode_is_rejected_rather_than_defaulted() -> None:
    """Closed for the reason every mode enum here is closed: each gate keys on
    this value, so a typo would fall through to the permissive branch."""
    with pytest.raises(ValidationError):
        _complete(AUTH_MODE="enforced")


# ---- Completeness is required only once the mode is not `open` -------------


@pytest.mark.parametrize("mode", ["open", "observe", "enforce"])
def test_a_complete_configuration_is_ready_in_every_mode(mode: str) -> None:
    assert _complete(AUTH_MODE=mode).auth_problems() == []


@pytest.mark.parametrize("missing", _MANIFEST_ENV)
def test_enforce_requires_every_manifest_field(missing: str) -> None:
    """A deployment checking tokens must also be able to tell a client how to get
    one. Half a manifest is a sign-in that cannot be started."""
    problems = _complete(AUTH_MODE="enforce", **{missing: ""}).auth_problems()
    assert any(missing in p and "requires" in p for p in problems), problems


@pytest.mark.parametrize("missing", _MANIFEST_ENV)
def test_observe_requires_the_same_completeness_as_enforce(missing: str) -> None:
    problems = _complete(AUTH_MODE="observe", **{missing: ""}).auth_problems()
    assert any(missing in p and "requires" in p for p in problems), problems


@pytest.mark.parametrize("missing", _MANIFEST_ENV)
def test_a_missing_field_leaves_the_manifest_unpublishable(missing: str) -> None:
    """Every field, not just the client id: a property that checked seven of eight
    would publish a manifest with a hole in it."""
    assert not _complete(**{missing: ""}).auth_is_configured


def test_a_whitespace_only_value_does_not_count_as_present() -> None:
    """`" "` is truthy. A client id of one space would satisfy a bare truthiness
    check and be published as a real one."""
    assert not _complete(SUPABASE_OAUTH_CLIENT_ID="   ").auth_is_configured
    problems = _complete(AUTH_MODE="enforce", SUPABASE_OAUTH_CLIENT_ID="  ").auth_problems()
    assert any("SUPABASE_OAUTH_CLIENT_ID" in p and "requires" in p for p in problems), problems


# ---- Whatever is set is validated, in every mode ---------------------------


def test_a_malformed_url_fails_readiness_even_under_the_open_door() -> None:
    """`open` does not make a broken URL harmless: it is still published by
    `/auth/config`, and still rejected by every client that reads it."""
    problems = _settings(AUTH_MODE="open", SUPABASE_AUTH_ISSUER="not a url").auth_problems()
    assert any("SUPABASE_AUTH_ISSUER" in p for p in problems), problems


def test_plain_http_is_refused_in_production() -> None:
    problems = _complete(
        ENVIRONMENT="production",
        SUPABASE_AUTH_ISSUER="http://abcdefghij.supabase.co/auth/v1",
    ).auth_problems()
    assert any("https" in p for p in problems), problems


def test_loopback_http_is_allowed_in_development_only() -> None:
    """A local Supabase issues over `http://127.0.0.1:54321`; requiring HTTPS
    everywhere would make the feature undevelopable."""
    local = {
        "SUPABASE_AUTH_ISSUER": "http://127.0.0.1:54321/auth/v1",
        "SUPABASE_JWKS_URL": "http://127.0.0.1:54321/auth/v1/.well-known/jwks.json",
        "SUPABASE_OAUTH_AUTHORIZE_URL": "http://127.0.0.1:54321/auth/v1/oauth/authorize",
        "SUPABASE_OAUTH_TOKEN_URL": "http://127.0.0.1:54321/auth/v1/oauth/token",
    }
    assert _complete(ENVIRONMENT="development", **local).auth_problems() == []
    hardened = _complete(ENVIRONMENT="staging", **local).auth_problems()
    assert any("SUPABASE_AUTH_ISSUER" in p and "publicly reachable" in p for p in hardened), hardened


def test_a_non_loopback_http_url_is_refused_even_in_development() -> None:
    """The development exemption is for loopback, not for plaintext generally."""
    problems = _settings(SUPABASE_AUTH_ISSUER="http://example.com/auth/v1").auth_problems()
    assert any("SUPABASE_AUTH_ISSUER" in p for p in problems), problems


def test_credentials_embedded_in_a_url_are_refused() -> None:
    """This value is served to anyone who asks `/auth/config`."""
    problems = _settings(
        SUPABASE_AUTH_ISSUER="https://user:pw@abcdefghij.supabase.co/auth/v1"
    ).auth_problems()
    assert any("credentials" in p for p in problems), problems


def test_a_non_numeric_port_fails_readiness_rather_than_a_request() -> None:
    """`urlsplit(...).port` raises ValueError here. It must become a problem
    string, not a lifespan exception with a stack trace for an operator."""
    problems = _settings(
        SUPABASE_AUTH_ISSUER="https://abc.supabase.co:port/auth/v1"
    ).auth_problems()
    assert any("SUPABASE_AUTH_ISSUER" in p and "usable absolute URL" in p for p in problems), (
        problems
    )


# ---- Same-origin, measured against the ISSUER ------------------------------


@pytest.mark.parametrize(
    "field",
    ["SUPABASE_JWKS_URL", "SUPABASE_OAUTH_AUTHORIZE_URL", "SUPABASE_OAUTH_TOKEN_URL"],
)
def test_an_endpoint_on_another_origin_is_refused(field: str) -> None:
    """The half-edited-configuration case: three URLs moved to a new project and
    one left pointing at the old one. A token endpoint the issuer does not name is
    where an authorization code would be exchanged with a stranger."""
    problems = _complete(**{field: "https://elsewhere.supabase.co/auth/v1/x"}).auth_problems()
    assert any(field in p and "same authority" in p for p in problems), problems


def test_an_explicit_default_port_is_still_a_different_authority() -> None:
    """`https://h` and `https://h:443` are the same origin by any semantic reading
    and are two different strings to a textual one — and the client compares them
    textually. So a configuration mixing the two spellings must be refused HERE,
    where it is an operator's typo, rather than on a user's machine, where it is
    an unexplained login failure.

    Being the stricter of the two implementations is the only way this endpoint's
    promise — that it never publishes what the client would reject — holds.
    """
    problems = _complete(
        SUPABASE_JWKS_URL="https://abcdefghij.supabase.co:443/auth/v1/.well-known/jwks.json"
    ).auth_problems()
    assert any("same authority" in p for p in problems), problems


def test_a_different_port_is_a_different_authority() -> None:
    problems = _complete(
        SUPABASE_JWKS_URL="https://abcdefghij.supabase.co:8443/auth/v1/.well-known/jwks.json"
    ).auth_problems()
    assert any("same authority" in p for p in problems), problems


# ---- The loopback callback -------------------------------------------------


def test_the_callback_is_a_constant_rather_than_a_setting() -> None:
    """The client compiles this exact string in and compares the manifest's value
    against it for EQUALITY, so a backend that could vary it could only ever be
    wrong. It is the same binary against every environment, and the same loopback
    URI is registered for each OAuth client, so there is nothing here for a
    deployment to change.

    Asserted as an absence too: an env var over it would be a way to break a login
    and no way to fix one.
    """
    assert OAUTH_REDIRECT_URI == "http://127.0.0.1:42813/oauth/callback"
    assert "supabase_oauth_redirect_uri" not in Settings.model_fields


def test_the_callback_constant_satisfies_the_rules_it_would_be_held_to() -> None:
    """It is hardcoded, not unchecked. IPv4 loopback (not `localhost`, which
    resolves through the host's own name lookup), an explicit port (the client
    matches redirect URIs exactly), and a real path."""
    assert _loopback_redirect_problem(
        value=OAUTH_REDIRECT_URI, env_name="OAUTH_REDIRECT_URI"
    ) is None


@pytest.mark.parametrize(
    "value",
    [
        "http://localhost:42813/oauth/callback",
        "http://0.0.0.0:42813/oauth/callback",
        "http://192.168.1.10:42813/oauth/callback",
        "https://staging.daintree.org/oauth/callback",
        "http://127.0.0.1/oauth/callback",
        "http://127.0.0.1:42813/",
        "http://u:p@127.0.0.1:42813/oauth/callback",
        "http://127.0.0.1:42813/oauth/callback#x",
    ],
    ids=[
        "localhost", "wildcard", "lan", "https-remote",
        "no-port", "no-path", "userinfo", "fragment",
    ],
)
def test_the_callback_validator_still_refuses_every_wrong_shape(value: str) -> None:
    """The validator outlives the setting it was written for: it is what proves
    the constant above, and it is what a future per-client callback would be held
    to. Each case is a different way of getting an authorization code delivered
    somewhere it should not go."""
    assert _loopback_redirect_problem(value=value, env_name="X") is not None


# ---- The staged deployment contract ----------------------------------------
#
# Everything above tests one RULE against one crafted value. This section tests
# one DEPLOYMENT against all of them at once, and the two are not substitutes.
# Every property below is already covered by some property test — but each of
# those runs under `development`, or `production`, or with a single field made
# wrong, and none of them ever asserts that the exact set of values a staging
# revision will actually carry survives the exact set of rules a hardened
# deployment actually applies. That gap is the one an operator falls into: a
# configuration where each value is individually legal and the combination fails
# readiness, discovered on the deploy rather than here.
#
# The values are the frozen contract for `assistant.daintree.org`. The Supabase
# project ref and the OAuth client id are placeholders on purpose — the real ones
# are deployment configuration and never belong in source — but their SHAPE is
# real, and shape is all these rules read.

_STAGING_CONTRACT: dict[str, object] = {
    "ENVIRONMENT": "staging",
    "SUPABASE_AUTH_ISSUER": _ISSUER,
    "SUPABASE_JWKS_URL": _JWKS,
    "SUPABASE_OAUTH_AUTHORIZE_URL": _AUTHORIZE,
    "SUPABASE_OAUTH_TOKEN_URL": _TOKEN,
    "SUPABASE_OAUTH_CLIENT_ID": "daintree-assistant-staging",
    "DAINTREE_ACCOUNT_URL": "https://staging.daintree.org/account",
    "DAINTREE_SUBSCRIBE_URL": "https://staging.daintree.org/subscribe",
}


def _staging(**overrides: object) -> Settings:
    return _settings(**{**_STAGING_CONTRACT, **overrides})


@pytest.mark.parametrize("mode", ["open", "observe", "enforce"])
def test_the_staged_contract_is_accepted_in_every_posture(mode: str) -> None:
    """The whole point of the environment/posture split, over the real values.

    `ENVIRONMENT` says where this is and `AUTH_MODE` says what it does about
    credentials, and they are independent — there is deliberately no fourth
    `staging` auth mode. So one staging deployment must be able to sit in any of
    the three postures without its OAuth configuration becoming the reason it
    cannot start, and the posture must be movable by a config-only revision.

    Named for what it checks. This is the AUTH configuration being accepted, not
    the deployment being ready: readiness also weighs secrets and the salt, which
    this fixture does not supply, and `test_auth_problems_reach_the_readiness_
    aggregate` below is what holds the two lists connected.
    """
    assert _staging(AUTH_MODE=mode).auth_problems() == []


def test_staging_is_hardened_exactly_as_production_is() -> None:
    """Staging is not a softer tier, and this is the assertion that says so.

    Every relaxation the URL rules make — plaintext on a loopback literal, a
    machine-local hostname — is withdrawn for `production` and `staging`
    TOGETHER, keyed on one property. A staging deployment is read by clients on
    other machines exactly as production is, so the reasons those relaxations
    exist do not apply to it.
    """
    # The WHOLE closed set, not two of it. Asserting only that staging and
    # production are hardened leaves `environment != "development"` passing, which
    # would harden `test` and break every offline fixture that relies on it.
    hardened = {
        env: _settings(ENVIRONMENT=env).is_hardened
        for env in ("development", "test", "staging", "production")
    }
    assert hardened == {
        "development": False,
        "test": False,
        "staging": True,
        "production": True,
    }


@pytest.mark.parametrize(
    ("issuer", "expected"),
    [
        # Each specimen isolates ONE hardened rule. A composite like
        # `http://127.0.0.1/auth/v1` trips the private-host check first, so it
        # would keep passing with the HTTPS requirement deleted.
        ("http://abcdefghij.supabase.co/auth/v1", "https://"),
        ("https://localhost/auth/v1", "publicly reachable"),
        ("https://10.0.0.5/auth/v1", "publicly reachable"),
        ("http://127.0.0.1:54321/auth/v1", "publicly reachable"),
    ],
    ids=["public-plaintext", "machine-local-name", "private-address", "loopback"],
)
def test_staging_withdraws_every_relaxation_development_allows(
    issuer: str, expected: str
) -> None:
    """Each of these is legal, or nearly so, in development. None is in staging.

    A staging manifest is read by clients on other machines exactly as a
    production one is, so a host naming the READER's own machine sends each of
    them somewhere different and none of those places is us.
    """
    problems = _staging(
        SUPABASE_AUTH_ISSUER=issuer,
        SUPABASE_JWKS_URL=issuer + "/.well-known/jwks.json",
        SUPABASE_OAUTH_AUTHORIZE_URL=issuer + "/oauth/authorize",
        SUPABASE_OAUTH_TOKEN_URL=issuer + "/oauth/token",
    ).auth_problems()
    assert any(expected in p for p in problems), (issuer, problems)


def test_the_staged_contract_publishes_a_manifest_before_it_enforces() -> None:
    """`configured` and `required` are two questions, and a rollout needs them apart.

    The staging plan is to publish a complete manifest while still serving
    anonymous requests, so a client can complete a real sign-in against a real
    identity provider before anybody's request depends on the answer. A build that
    collapsed the two would make that step impossible: the manifest would appear
    only once the door had already shut.
    """
    for mode in ("open", "observe", "enforce"):
        assert _staging(AUTH_MODE=mode).auth_is_configured, mode


def test_the_staging_browser_links_survive_hardening() -> None:
    """`staging.daintree.org` is a label-boundary subdomain of the pinned anchor.

    Worth its own case because the anchor is a SUFFIX rule, and the failure it
    exists to stop — `daintree.org.evil.com`, `notdaintree.org` — is a string that
    also ends in the anchor's characters. The rule has to accept this and refuse
    those, and only checking one direction leaves the other free to break.
    """
    assert _staging().auth_manifest_problems() == []
    for lookalike in (
        "https://staging.daintree.org.evil.com/account",
        "https://notdaintree.org/account",
        "https://daintree.org.evil.com/account",
    ):
        problems = _staging(DAINTREE_ACCOUNT_URL=lookalike).auth_problems()
        assert any("Daintree site" in p for p in problems), (lookalike, problems)


def test_the_link_host_is_not_admitted_as_an_oauth_endpoint() -> None:
    """`staging.daintree.org` is where a BROWSER goes, and that is all it is.

    The trap this closes is that `daintree.org` sits in BOTH trust anchors — it is
    a legal browser link and a legal issuer suffix — so a token endpoint moved onto
    the link host looks plausible against the anchor rules alone. What refuses it
    is the separate requirement that the three OAuth endpoints share the ISSUER's
    exact authority, which a browser link does not.

    The other half of this claim — that nothing DERIVES an endpoint from a link —
    cannot be made here, because these are plain fields and a settings-level check
    would only prove Pydantic stores what it is given. It is made over the wire in
    `test_the_browser_links_move_nothing_but_themselves`.
    """
    problems = _staging(
        SUPABASE_OAUTH_TOKEN_URL="https://staging.daintree.org/auth/v1/oauth/token"
    ).auth_problems()
    assert any("SUPABASE_OAUTH_TOKEN_URL" in p and "authority" in p for p in problems), problems


#: Every published URL, with a value of the right SHAPE for its role. Used to
#: sweep one prohibited shape across all of them at once.
_STAGING_URL_FIELDS: dict[str, str] = {
    "SUPABASE_AUTH_ISSUER": _ISSUER,
    "SUPABASE_JWKS_URL": _JWKS,
    "SUPABASE_OAUTH_AUTHORIZE_URL": _AUTHORIZE,
    "SUPABASE_OAUTH_TOKEN_URL": _TOKEN,
    "DAINTREE_ACCOUNT_URL": "https://staging.daintree.org/account",
    "DAINTREE_SUBSCRIBE_URL": "https://staging.daintree.org/subscribe",
}

_OAUTH_URL_FIELDS = tuple(f for f in _STAGING_URL_FIELDS if f.startswith("SUPABASE_"))
_LINK_FIELDS = ("DAINTREE_ACCOUNT_URL", "DAINTREE_SUBSCRIBE_URL")


@pytest.mark.parametrize("field", tuple(_STAGING_URL_FIELDS))
def test_no_staged_url_may_embed_credentials(field: str) -> None:
    """Userinfo, on every field, under staging.

    The rules are tested one crafted value at a time elsewhere, and mostly under
    `development` — which proves the rule EXISTS without proving it still applies
    to the deployment that will actually publish these values. Sweeping the shape
    across every field is what catches a rule that was weakened for one of them.
    """
    value = _STAGING_URL_FIELDS[field].replace("https://", "https://u:p@", 1)
    problems = _staging(**{field: value}).auth_problems()
    assert any(field in p and "credentials" in p for p in problems), problems


@pytest.mark.parametrize("field", _OAUTH_URL_FIELDS)
def test_no_staged_oauth_endpoint_may_carry_a_fragment(field: str) -> None:
    """A fragment is meaningless on an issuer or a token endpoint. Only the two
    browser links may carry one, and they are excluded here rather than merely
    untested — `test_a_browser_link_may_carry_a_fragment` holds that side."""
    problems = _staging(**{field: _STAGING_URL_FIELDS[field] + "#x"}).auth_problems()
    assert any(field in p and "fragment" in p for p in problems), problems


@pytest.mark.parametrize("field", _OAUTH_URL_FIELDS)
def test_no_staged_oauth_path_may_percent_encode(field: str) -> None:
    """Every way of HANDLING a percent-escape in these paths diverges from the
    client, so they are refused outright rather than normalised. A real OAuth path
    never needs one, which is what makes refusing free."""
    value = _STAGING_URL_FIELDS[field].replace("/auth/v1", "/auth/v1/%2e%2e/x", 1)
    problems = _staging(**{field: value}).auth_problems()
    assert any(field in p for p in problems), (value, problems)


@pytest.mark.parametrize("field", _LINK_FIELDS)
@pytest.mark.parametrize(
    "host",
    ["staging.daintree.org.evil.com", "notdaintree.org", "daintree.org.evil.com"],
)
def test_no_staged_browser_link_accepts_a_lookalike(field: str, host: str) -> None:
    """BOTH links, not just the account one.

    The anchor is a suffix rule and every one of these ends in its characters, so
    a weakening to a bare `endswith` admits them. Parameterised across both fields
    because the two are separate call sites: one of them could be relaxed alone,
    and a test covering only `DAINTREE_ACCOUNT_URL` would not notice.
    """
    problems = _staging(**{field: f"https://{host}/x"}).auth_problems()
    assert any(field in p and "Daintree site" in p for p in problems), problems


@pytest.mark.parametrize("field", ("SUPABASE_JWKS_URL", "SUPABASE_OAUTH_AUTHORIZE_URL"))
def test_no_staged_oauth_endpoint_may_leave_the_issuers_authority(field: str) -> None:
    """A half-edited configuration — three URLs moved to a new project and one
    left behind — is what this catches, and it is the shape that would exchange an
    authorization code with the previous project."""
    value = _STAGING_URL_FIELDS[field].replace("abcdefghij", "zzzzzzzzzz", 1)
    problems = _staging(**{field: value}).auth_problems()
    assert any(field in p and "authority" in p for p in problems), problems


# ---- Readiness aggregation and secret hygiene ------------------------------


def test_auth_problems_reach_the_readiness_aggregate() -> None:
    """`local_only_problems()` is what lifespan actually calls; an `auth_problems`
    nothing aggregated would gate nothing.

    Non-emptiness is asserted FIRST: a subset check alone passes vacuously the
    moment `auth_problems()` regresses to returning nothing, which is precisely
    the regression this test exists to catch.
    """
    settings = _settings(SUPABASE_AUTH_ISSUER="not a url")
    problems = settings.auth_problems()
    assert problems, "the fixture is meant to be misconfigured"
    assert set(problems) <= set(settings.local_only_problems())


def test_no_oauth_value_is_a_required_secret() -> None:
    """None of these is a secret — they are published by `/auth/config`. Listing
    them in `missing_secrets()` would tell an operator to mount public
    configuration out of Secret Manager, which `test_infra_drift` then enforces."""
    assert _settings().missing_secrets() == []
    required = _complete(AUTH_MODE="enforce").missing_secrets()
    for env_name in _MANIFEST_ENV:
        assert env_name not in required, env_name
    assert "AUTH_MODE" not in required


def test_auth_is_configured_tracks_completeness() -> None:
    assert not _settings().auth_is_configured
    assert _complete().auth_is_configured


# ---- URL hygiene, run before parsing ---------------------------------------
#
# `urlsplit` normalises several of these away — it strips leading/trailing spaces
# and C0 controls and deletes embedded tab/CR/LF — so the cleaned value validates
# while the RAW value is what gets published. The check has to happen first.


@pytest.mark.parametrize(
    "value",
    [
        " https://abcdefghij.supabase.co/auth/v1",
        "https://abcdefghij.supabase.co/auth/v1 ",
        "https://abcdefghij.supabase.co/auth\n/v1",
        "https://abcdefghij.supabase.co/auth\t/v1",
        "https://abcdefghij.supabase.co/auth\r/v1",
        "https://abcdefghij.supabase.co\\evil/auth/v1",
    ],
    ids=["lead-space", "trail-space", "newline", "tab", "cr", "backslash"],
)
def test_whitespace_and_control_characters_are_refused_before_parsing(value: str) -> None:
    problems = _settings(SUPABASE_AUTH_ISSUER=value).auth_problems()
    assert any("whitespace or a control character" in p for p in problems), problems


@pytest.mark.parametrize(
    "field",
    ["SUPABASE_AUTH_ISSUER", "SUPABASE_JWKS_URL", "SUPABASE_OAUTH_TOKEN_URL"],
)
def test_an_oauth_endpoint_may_not_carry_a_fragment(field: str) -> None:
    values = {
        "SUPABASE_AUTH_ISSUER": _ISSUER + "#x",
        "SUPABASE_JWKS_URL": _JWKS + "#x",
        "SUPABASE_OAUTH_TOKEN_URL": _TOKEN + "#x",
    }
    problems = _complete(**{field: values[field]}).auth_problems()
    assert any(field in p and "fragment" in p for p in problems), problems


@pytest.mark.parametrize("field", ["DAINTREE_ACCOUNT_URL", "DAINTREE_SUBSCRIBE_URL"])
def test_a_browser_link_may_carry_a_fragment(field: str) -> None:
    """The one rule that varies by role. A fragment is meaningless on an issuer
    and suspicious on a callback, but an account page may legitimately be a
    single-page app whose route lives in one — and the client permits it there, so
    refusing it here would reject a configuration that works."""
    value = "https://staging.daintree.org/account#/billing"
    assert _complete(**{field: value}).auth_manifest_problems() == []


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("SUPABASE_AUTH_ISSUER", "https://u:p@abcdefghij.supabase.co/auth/v1"),
        ("DAINTREE_ACCOUNT_URL", "https://u:p@staging.daintree.org/account"),
        ("SUPABASE_JWKS_URL", "https://u:p@abcdefghij.supabase.co/auth/v1/jwks"),
    ],
)
def test_no_published_url_may_embed_credentials(field: str, value: str) -> None:
    """Every published URL, through one helper. The redirect URI was the one that
    escaped this originally — it went through a helper of its own that never
    checked userinfo, so a password in it passed readiness and was then served to
    anyone who asked `/auth/config`."""
    problems = _complete(**{field: value}).auth_problems()
    assert any(field in p and "credentials" in p for p in problems), problems


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("SUPABASE_AUTH_ISSUER", "https://abcdefghij.supabase.co:0/auth/v1"),
        ("SUPABASE_JWKS_URL", "https://abcdefghij.supabase.co:0/auth/v1/jwks"),
    ],
)
def test_port_zero_is_refused(field: str, value: str) -> None:
    """Port 0 parses, and means "pick one for me" — which is precisely the shape
    Supabase's exact redirect matching cannot accept, and is meaningless for a
    remote endpoint."""
    problems = _complete(**{field: value}).auth_problems()
    assert any(field in p and "port" in p for p in problems), problems


def test_a_problem_string_never_echoes_the_offending_url() -> None:
    """These strings reach the startup log, and outside production a readiness
    response. The input most likely to be malformed is one with a credential
    pasted into it, so the value must not travel with the complaint."""
    secret = "s3cr3t-pasted-by-mistake"
    problems = _settings(SUPABASE_AUTH_ISSUER=f"https://x:{secret}@h:port/auth/v1").auth_problems()
    assert problems
    assert not any(secret in p for p in problems), problems


# ---- Hardened deployments must name a reachable host -----------------------


@pytest.mark.parametrize(
    "host",
    ["127.0.0.1", "localhost", "[::1]", "10.0.0.5", "192.168.1.10", "169.254.1.1"],
)
def test_a_local_or_private_host_is_refused_in_production(host: str) -> None:
    """`https://` does not redeem it. A production manifest is read by clients on
    other machines, so a host naming the reader's own machine — or an address only
    reachable inside one network — sends each of them somewhere different, and
    none of those places is us."""
    problems = _complete(
        ENVIRONMENT="production", SUPABASE_AUTH_ISSUER=f"https://{host}/auth/v1"
    ).auth_problems()
    assert any("publicly reachable" in p for p in problems), problems


def test_a_public_host_is_accepted_in_production() -> None:
    assert _complete(ENVIRONMENT="production").auth_problems() == []


# ---- The issuer path is part of the credential check -----------------------


@pytest.mark.parametrize(
    "issuer",
    [
        "https://abcdefghij.supabase.co",
        "https://abcdefghij.supabase.co/",
        "https://abcdefghij.supabase.co/auth",
        "https://abcdefghij.supabase.co/auth/v2",
    ],
)
def test_an_issuer_that_is_not_the_auth_endpoint_is_refused(issuer: str) -> None:
    """Pasting the bare project URL is the common way to get this wrong, and it
    would present as "every login is rejected" rather than as a config error —
    the issuer is compared for equality against the token's `iss`."""
    problems = _settings(SUPABASE_AUTH_ISSUER=issuer).auth_problems()
    assert any("/auth/v1" in p for p in problems), problems


def test_a_trailing_slash_on_a_correct_issuer_is_tolerated() -> None:
    assert _complete(SUPABASE_AUTH_ISSUER=_ISSUER + "/").auth_manifest_problems() == []


# ---- A malformed manifest is never publishable -----------------------------


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("SUPABASE_AUTH_ISSUER", "not a url"),
        ("SUPABASE_OAUTH_TOKEN_URL", "https://attacker.example/token"),
        ("SUPABASE_JWKS_URL", "https://attacker.example/jwks"),
        ("SUPABASE_OAUTH_AUTHORIZE_URL", "https://abcdefghij.supabase.co/elsewhere"),
        ("DAINTREE_ACCOUNT_URL", "http://plain.example/account"),
    ],
)
def test_a_complete_but_invalid_manifest_is_not_published(field: str, value: str) -> None:
    """The sharpest version of this bug: `/auth/config` deliberately answers while
    the service is unready, so readiness cannot be what filters it. A token
    endpoint typo'd onto another host would otherwise fail startup AND be
    published to every client — the one shape that could send an authorization
    code to a stranger.
    """
    settings = _complete(**{field: value})
    assert settings.auth_manifest_problems(), "the fixture is meant to be invalid"
    assert not settings.auth_is_configured


# ---- The trust anchors, pinned rather than configured -----------------------
#
# The client refuses a manifest naming anything outside these lists. Duplicating
# them here is not redundancy: it turns "the client will reject this" into "this
# server never published it", which is the difference between a login failing
# confusingly on a user's machine and a deploy failing at readiness.


@pytest.mark.parametrize(
    "issuer",
    [
        "https://abcdefghij.supabase.co/auth/v1",
        "https://auth.daintree.org/auth/v1",
        "https://staging.daintree.org/auth/v1",
    ],
)
def test_a_daintree_identity_provider_is_accepted(issuer: str) -> None:
    same_origin = issuer.rsplit("/auth/v1", 1)[0]
    assert (
        _complete(
            SUPABASE_AUTH_ISSUER=issuer,
            SUPABASE_JWKS_URL=f"{same_origin}/auth/v1/.well-known/jwks.json",
            SUPABASE_OAUTH_AUTHORIZE_URL=f"{same_origin}/auth/v1/oauth/authorize",
            SUPABASE_OAUTH_TOKEN_URL=f"{same_origin}/auth/v1/oauth/token",
        ).auth_manifest_problems()
        == []
    )


@pytest.mark.parametrize(
    "issuer",
    [
        "https://evil.example/auth/v1",
        # The near-misses a plain suffix test would wave through.
        "https://supabase.co.evil.example/auth/v1",
        "https://daintree.org.evil.example/auth/v1",
        "https://notsupabase.co/auth/v1",
    ],
)
def test_an_issuer_outside_the_trust_anchor_is_refused(issuer: str) -> None:
    """The anchor check is the one rule a self-consistent hostile manifest cannot
    satisfy: everything else about it can be made internally coherent."""
    problems = _settings(SUPABASE_AUTH_ISSUER=issuer).auth_problems()
    assert any("identity provider" in p for p in problems), problems


def test_a_trailing_dot_does_not_evade_the_anchor() -> None:
    """`daintree.org.` and `daintree.org` are the same name to DNS, and only one of
    them matches a plain string comparison."""
    assert (
        _complete(
            SUPABASE_AUTH_ISSUER="https://abcdefghij.supabase.co./auth/v1",
            SUPABASE_JWKS_URL="https://abcdefghij.supabase.co./auth/v1/jwks",
            SUPABASE_OAUTH_AUTHORIZE_URL="https://abcdefghij.supabase.co./auth/v1/authorize",
            SUPABASE_OAUTH_TOKEN_URL="https://abcdefghij.supabase.co./auth/v1/token",
        ).auth_manifest_problems()
        == []
    )


@pytest.mark.parametrize("field", ["DAINTREE_ACCOUNT_URL", "DAINTREE_SUBSCRIBE_URL"])
def test_a_browser_link_outside_daintree_is_refused(field: str) -> None:
    """These open in the user's browser under the assistant's own recommendation —
    "manage your account", "buy a plan" — so a free choice here is a phishing page
    with the product vouching for it."""
    problems = _complete(**{field: "https://evil.example/account"}).auth_problems()
    assert any(field in p and "Daintree site" in p for p in problems), problems


def test_a_daintree_subdomain_link_is_accepted() -> None:
    assert (
        _complete(
            DAINTREE_ACCOUNT_URL="https://staging.daintree.org/account",
            DAINTREE_SUBSCRIBE_URL="https://staging.daintree.org/subscribe",
        ).auth_manifest_problems()
        == []
    )


# ---- Endpoints live under the issuer, not merely beside it -----------------


@pytest.mark.parametrize(
    "jwks",
    [
        # A sibling handler on the issuer's own host is still not the issuer.
        "https://abcdefghij.supabase.co/jwks",
        "https://abcdefghij.supabase.co/other/v1/jwks",
        # Prefix, not containment: a different route a sibling handler can own.
        "https://abcdefghij.supabase.co/auth/v1malicious/jwks",
        # Resolves to /capture in anything that normalises dot segments.
        "https://abcdefghij.supabase.co/auth/v1/../capture",
    ],
    ids=["off-path", "sibling-namespace", "prefix-not-segment", "dot-segment"],
)
def test_an_endpoint_outside_the_issuer_path_is_refused(jwks: str) -> None:
    problems = _complete(SUPABASE_JWKS_URL=jwks).auth_problems()
    assert any("SUPABASE_JWKS_URL" in p and "path" in p for p in problems), problems


def test_an_endpoint_under_the_issuer_path_is_accepted() -> None:
    assert (
        _complete(
            SUPABASE_JWKS_URL=_ISSUER + "/.well-known/jwks.json"
        ).auth_manifest_problems()
        == []
    )


# ---- The OAuth client id ---------------------------------------------------


def test_surrounding_whitespace_on_the_client_id_is_refused_not_trimmed() -> None:
    """The client trims what it receives and validates the trimmed copy. Trimming
    here too would mean validating one value and publishing another — which is
    exactly how `" client "` reaches a URL query string with its spaces intact."""
    problems = _complete(SUPABASE_OAUTH_CLIENT_ID=" abc ").auth_problems()
    assert any("whitespace" in p for p in problems), problems


def test_an_implausibly_long_client_id_is_refused() -> None:
    problems = _complete(SUPABASE_OAUTH_CLIENT_ID="a" * 201).auth_problems()
    assert any("plausible identifier" in p for p in problems), problems
    assert _complete(SUPABASE_OAUTH_CLIENT_ID="a" * 200).auth_manifest_problems() == []


@pytest.mark.parametrize("value", ["a b", "a/b", "a?b", "a#b", "a%2e", "aé"])
def test_a_client_id_outside_the_charset_is_refused(value: str) -> None:
    """It goes into a URL query string and, on the client, into a credential-store
    account key. Neither has any business holding an arbitrary wire value."""
    problems = _complete(SUPABASE_OAUTH_CLIENT_ID=value).auth_problems()
    assert problems, value


@pytest.mark.parametrize("value", ["daintree-assistant", "abc_123", "a.b~c", "A-Z0"])
def test_an_ordinary_client_id_is_accepted(value: str) -> None:
    assert _complete(SUPABASE_OAUTH_CLIENT_ID=value).auth_manifest_problems() == []


@pytest.mark.parametrize(
    "jwks",
    [
        "https://abcdefghij.supabase.co/auth/v1/%2e%2e/capture",
        "https://abcdefghij.supabase.co/auth/v1/%2E%2E/capture",
        "https://abcdefghij.supabase.co/auth/v1/%2e%2e%2fcapture",
    ],
    ids=["lower", "upper", "encoded-slash"],
)
def test_a_percent_encoded_dot_segment_does_not_evade_containment(jwks: str) -> None:
    """`urlsplit` leaves `.path` ENCODED, so `%2e%2e` splits into an innocuous
    segment and the dot-segment rule never sees it — while every server and proxy
    downstream decodes it and resolves the URL to `/capture`, outside the issuer.

    Go's `url.Parse` decodes on the way in, so the client catches this for free.
    This side had to be told, and a check that only caught the already-decoded
    spelling would have been the more dangerous kind of green test.
    """
    problems = _complete(SUPABASE_JWKS_URL=jwks).auth_problems()
    assert any("SUPABASE_JWKS_URL" in p and "path" in p for p in problems), problems


@pytest.mark.parametrize("host", ["localhost", "evil.localhost", "127.0.0.1."])
def test_plaintext_is_refused_on_a_loopback_NAME_even_in_development(host: str) -> None:
    """The http exemption exists because there is no network to intercept, and a
    NAME does not establish that: `evil.localhost` resolves through DNS or
    /etc/hosts to wherever someone points it, and `127.0.0.1.` is a fully-qualified
    DNS name that merely looks like an IP literal.

    A plaintext token exchange carries the authorization code AND the PKCE
    verifier, so a name-based exemption would let both travel to a remote host.
    The client applies exactly this rule, so accepting a name here would publish a
    manifest it refuses.
    """
    problems = _settings(
        ENVIRONMENT="development", SUPABASE_AUTH_ISSUER=f"http://{host}:54321/auth/v1"
    ).auth_problems()
    assert any("SUPABASE_AUTH_ISSUER" in p and "https" in p for p in problems), problems


@pytest.mark.parametrize("host", ["127.0.0.1", "[::1]"])
def test_plaintext_is_allowed_on_a_loopback_LITERAL_in_development(host: str) -> None:
    assert (
        _settings(
            ENVIRONMENT="development", SUPABASE_AUTH_ISSUER=f"http://{host}:54321/auth/v1"
        ).auth_manifest_problems()
        == []
    )


# ---- Staying the STRICTER of the two implementations -----------------------
#
# Everything below exists because the client would refuse it. Each is a
# configuration that would otherwise pass here and fail on a user's machine,
# where the person hitting it can do nothing about it and the operator never sees
# it. The endpoint's whole promise is that this list is empty in production.


@pytest.mark.parametrize(
    "jwks",
    [
        # Decoding sees this as a segment boundary; the client's parser refuses
        # the URL outright.
        "https://abcdefghij.supabase.co/auth/v1%2Fmalicious",
        # `unquote` leaves a malformed escape untouched; the client's parser does
        # not, and rejects the whole URL.
        "https://abcdefghij.supabase.co/auth/v1/%zz",
    ],
    ids=["encoded-slash", "malformed-escape"],
)
def test_a_percent_escape_in_an_endpoint_path_is_refused(jwks: str) -> None:
    """Refused outright rather than interpreted, because every way of HANDLING a
    percent-escape here diverges from the client. A real issuer, authorize, token
    or JWKS path never contains one."""
    problems = _complete(SUPABASE_JWKS_URL=jwks).auth_problems()
    assert any("SUPABASE_JWKS_URL" in p and "percent-encode" in p for p in problems), problems


def test_a_browser_link_may_still_percent_encode() -> None:
    """The rule is scoped to the OAuth endpoints. An account page may legitimately
    carry an encoded query, and the client applies no path-containment rule to
    these at all."""
    assert (
        _complete(
            DAINTREE_ACCOUNT_URL="https://staging.daintree.org/account?next=%2Fbilling"
        ).auth_manifest_problems()
        == []
    )


def test_more_than_one_trailing_dot_does_not_match_the_trust_anchor() -> None:
    """The client removes a single trailing dot, so `...co..` matches here and is
    refused there if this strips them all."""
    problems = _settings(
        SUPABASE_AUTH_ISSUER="https://abcdefghij.supabase.co../auth/v1"
    ).auth_problems()
    assert any("identity provider" in p for p in problems), problems


def test_a_value_that_cannot_be_encoded_fails_readiness_rather_than_a_request() -> None:
    """A lone surrogate — which the environment can carry in through
    surrogateescape — survives every structural check and then raises while the
    response is being serialized, turning a configuration mistake into a 500 on a
    public endpoint."""
    problems = _settings(
        SUPABASE_AUTH_ISSUER="https://abcdefghij.supabase.co/auth/v1\udc80"
    ).auth_problems()
    assert any("UTF-8" in p for p in problems), problems


def test_an_unbounded_url_is_refused() -> None:
    """The client caps the whole manifest body, so one enormous value does not
    merely bloat the response — it makes every other field unreadable, and the
    failure names the size rather than the field that caused it."""
    long_url = "https://staging.daintree.org/account?x=" + "a" * 4000
    problems = _complete(DAINTREE_ACCOUNT_URL=long_url).auth_problems()
    assert any("longer than" in p for p in problems), problems


@pytest.mark.parametrize("field", ["AUTH_ACCESS_TOKEN_SECONDS", "AUTH_SESSION_MAX_AGE_SECONDS"])
def test_a_session_duration_is_bounded_at_both_ends(field: str) -> None:
    """The client decodes these into a fixed-width integer, so an unbounded Python
    int is not merely implausible — it fails to decode and takes the whole manifest
    with it."""
    for bad in (0, 59, 10**20):
        with pytest.raises(ValidationError):
            _complete(**{field: bad})
    assert _complete(**{field: 31_536_000}).auth_manifest_problems() == []


# ---- `required` cannot outrun the implementation ---------------------------


def test_required_needs_both_the_mode_and_a_manifest_to_follow() -> None:
    """`required` is derived from two things, not from the mode alone.

    Telling a client a credential is mandatory while withholding the only
    description of how to obtain one is a dead end — and `/auth/config` answers
    while the service is unready, so readiness cannot be what prevents it.
    """
    from daintree_assistant_server.api.daintree_auth import _auth_config_body

    assert _auth_config_body(_complete(AUTH_MODE="enforce"))["required"] is True
    assert _auth_config_body(_complete(AUTH_MODE="observe"))["required"] is False
    assert _auth_config_body(_complete(AUTH_MODE="open"))["required"] is False
    # No manifest to follow means no demand for one, whatever the mode says.
    unconfigured = _settings(AUTH_MODE="enforce")
    body = _auth_config_body(unconfigured)
    assert body["configured"] is False
    assert body["required"] is False


# ---- the defensive guards, which are reachable and worth reaching ----------


def test_a_scheme_with_no_default_port_is_not_given_a_fabricated_one() -> None:
    """`_origin_of` returns None rather than inventing a port for an unknown
    scheme. A fabricated one would make two unrelated origins compare equal."""
    problems = _settings(SUPABASE_AUTH_ISSUER="ftp://abcdefghij.supabase.co/auth/v1").auth_problems()
    assert any("SUPABASE_AUTH_ISSUER" in p for p in problems), problems


def test_an_unparseable_endpoint_is_reported_once_not_twice() -> None:
    """The origin and path checks skip a value the shape check already refused, so
    one broken URL produces one complaint rather than three."""
    problems = _complete(SUPABASE_JWKS_URL="https://:notaport/x").auth_problems()
    jwks = [p for p in problems if "SUPABASE_JWKS_URL" in p]
    assert len(jwks) == 1, jwks


def test_an_unparseable_link_is_reported_once_too() -> None:
    problems = _complete(DAINTREE_ACCOUNT_URL="https://:notaport/x").auth_problems()
    links = [p for p in problems if "DAINTREE_ACCOUNT_URL" in p]
    assert len(links) == 1, links


def test_an_issuer_with_no_path_does_not_break_containment() -> None:
    """A parent path of "" contains everything, which is correct — and the issuer
    is separately refused for not ending in /auth/v1, so the configuration still
    fails rather than passing on a technicality."""
    problems = _complete(SUPABASE_AUTH_ISSUER="https://abcdefghij.supabase.co").auth_problems()
    assert any("/auth/v1" in p for p in problems), problems
