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

from daintree_assistant_server.config import Settings

_ISSUER = "https://abcdefghij.supabase.co/auth/v1"
_JWKS = "https://abcdefghij.supabase.co/auth/v1/.well-known/jwks.json"
_AUTHORIZE = "https://abcdefghij.supabase.co/auth/v1/oauth/authorize"
_TOKEN = "https://abcdefghij.supabase.co/auth/v1/oauth/token"
_REDIRECT = "http://127.0.0.1:42813/oauth/callback"


def _settings(**overrides: object) -> Settings:
    base: dict[str, object] = {
        "STATE_SIGNING_KEY_CURRENT": "x" * 32,
        "TOGETHER_API_KEY": "sk-server-owned",
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
        "SUPABASE_OAUTH_REDIRECT_URI": _REDIRECT,
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


@pytest.mark.parametrize("mode", ["observe", "enforce"])
def test_a_mode_whose_behaviour_does_not_exist_yet_is_refused(mode: str) -> None:
    """The footgun this closes: nothing reads a token on any path yet, so a
    deployment that set `enforce` would pass readiness, tell every client a
    credential was required, and go on serving unauthenticated requests. An
    operator would believe the door was shut while it stood open.

    Failing readiness is the fail-closed answer — the revision never takes
    traffic. Lifted by the change that makes the modes real, not before.
    """
    problems = _complete(AUTH_MODE=mode).auth_problems()
    assert any("not implemented in this build" in p for p in problems), problems


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


def test_a_complete_configuration_is_ready_under_the_implemented_mode() -> None:
    assert _complete(AUTH_MODE="open").auth_problems() == []


@pytest.mark.parametrize("mode", ["observe", "enforce"])
def test_a_complete_configuration_fails_only_on_the_unimplemented_mode(mode: str) -> None:
    """The completeness and validity rules must not also be firing — otherwise the
    refusal above would be masking a second defect, and lifting it later would
    surface one nobody had seen."""
    problems = _complete(AUTH_MODE=mode).auth_problems()
    assert len(problems) == 1, problems
    assert "not implemented in this build" in problems[0]


@pytest.mark.parametrize(
    "missing",
    [
        "SUPABASE_AUTH_ISSUER",
        "SUPABASE_JWKS_URL",
        "SUPABASE_OAUTH_AUTHORIZE_URL",
        "SUPABASE_OAUTH_TOKEN_URL",
        "SUPABASE_OAUTH_CLIENT_ID",
        "SUPABASE_OAUTH_REDIRECT_URI",
        "DAINTREE_ACCOUNT_URL",
        "DAINTREE_SUBSCRIBE_URL",
    ],
)
def test_enforce_requires_every_manifest_field(missing: str) -> None:
    """A deployment checking tokens must also be able to tell a client how to get
    one. Half a manifest is a sign-in that cannot be started."""
    problems = _complete(AUTH_MODE="enforce", **{missing: ""}).auth_problems()
    assert any(missing in p and "requires" in p for p in problems), problems


_MANIFEST_ENV = (
    "SUPABASE_AUTH_ISSUER",
    "SUPABASE_JWKS_URL",
    "SUPABASE_OAUTH_AUTHORIZE_URL",
    "SUPABASE_OAUTH_TOKEN_URL",
    "SUPABASE_OAUTH_CLIENT_ID",
    "SUPABASE_OAUTH_REDIRECT_URI",
    "DAINTREE_ACCOUNT_URL",
    "DAINTREE_SUBSCRIBE_URL",
)


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
    assert any(field in p and "origin" in p for p in problems), problems


def test_an_explicit_default_port_is_the_same_origin() -> None:
    """`https://h` and `https://h:443` are one origin; rejecting the pair would
    fail readiness on a correct configuration."""
    assert (
        _complete(
            SUPABASE_JWKS_URL="https://abcdefghij.supabase.co:443/auth/v1/.well-known/jwks.json"
        ).auth_problems()
        == []
    )


def test_a_different_port_is_a_different_origin() -> None:
    problems = _complete(
        SUPABASE_JWKS_URL="https://abcdefghij.supabase.co:8443/auth/v1/.well-known/jwks.json"
    ).auth_problems()
    assert any("origin" in p for p in problems), problems


# ---- The loopback callback -------------------------------------------------


def test_localhost_is_refused_as_a_callback_host() -> None:
    """`localhost` resolves through the host's own name lookup, so /etc/hosts or
    an IPv6-first resolver can hand the authorization code to a listener that is
    not the CLI's. The spec binds IPv4 loopback; a name is not a binding."""
    problems = _complete(
        SUPABASE_OAUTH_REDIRECT_URI="http://localhost:42813/oauth/callback"
    ).auth_problems()
    assert any("SUPABASE_OAUTH_REDIRECT_URI" in p for p in problems), problems


@pytest.mark.parametrize(
    "value",
    [
        "http://0.0.0.0:42813/oauth/callback",
        "http://192.168.1.10:42813/oauth/callback",
        "https://staging.daintree.org/oauth/callback",
    ],
)
def test_a_non_loopback_callback_is_refused(value: str) -> None:
    problems = _complete(SUPABASE_OAUTH_REDIRECT_URI=value).auth_problems()
    assert any("SUPABASE_OAUTH_REDIRECT_URI" in p for p in problems), problems


def test_a_callback_without_an_explicit_port_is_refused() -> None:
    """Supabase matches redirect URIs by exact string, so a port the client picks
    at runtime can never match a registered URI."""
    problems = _complete(
        SUPABASE_OAUTH_REDIRECT_URI="http://127.0.0.1/oauth/callback"
    ).auth_problems()
    assert any("port" in p for p in problems), problems


def test_a_callback_without_a_path_is_refused() -> None:
    problems = _complete(SUPABASE_OAUTH_REDIRECT_URI="http://127.0.0.1:42813/").auth_problems()
    assert any("path" in p for p in problems), problems


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
    ["SUPABASE_AUTH_ISSUER", "DAINTREE_ACCOUNT_URL", "SUPABASE_OAUTH_REDIRECT_URI"],
)
def test_no_published_url_may_carry_a_fragment(field: str) -> None:
    values = {
        "SUPABASE_AUTH_ISSUER": _ISSUER + "#x",
        "DAINTREE_ACCOUNT_URL": "https://staging.daintree.org/account#x",
        "SUPABASE_OAUTH_REDIRECT_URI": _REDIRECT + "#x",
    }
    problems = _complete(**{field: values[field]}).auth_problems()
    assert any(field in p and "fragment" in p for p in problems), problems


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("SUPABASE_AUTH_ISSUER", "https://u:p@abcdefghij.supabase.co/auth/v1"),
        ("DAINTREE_ACCOUNT_URL", "https://u:p@staging.daintree.org/account"),
        ("SUPABASE_OAUTH_REDIRECT_URI", "http://u:p@127.0.0.1:42813/oauth/callback"),
    ],
)
def test_no_published_url_may_embed_credentials(field: str, value: str) -> None:
    """The redirect URI is the one that was missed: it went through its own helper,
    which never checked userinfo, so a password in it passed readiness and was then
    served to anyone who asked `/auth/config`."""
    problems = _complete(**{field: value}).auth_problems()
    assert any(field in p and "credentials" in p for p in problems), problems


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("SUPABASE_AUTH_ISSUER", "https://abcdefghij.supabase.co:0/auth/v1"),
        ("SUPABASE_OAUTH_REDIRECT_URI", "http://127.0.0.1:0/oauth/callback"),
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
        ("SUPABASE_OAUTH_REDIRECT_URI", "https://attacker.example/callback"),
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
