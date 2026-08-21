"""Configuration and authentication tests."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from daintree_assistant_server.auth import authenticate
from daintree_assistant_server.config import Settings
from daintree_assistant_server.errors import DaintreeError


def _settings(**overrides: object) -> Settings:
    base = {"STATE_SIGNING_KEY_CURRENT": "sk", "CEREBRAS_API_KEY": "csk-test"}
    base.update(overrides)
    return Settings(**base)  # type: ignore[arg-type]


def test_missing_secrets_reports_both_server_owned_secrets() -> None:
    # Both are the SERVER's own material, which is the whole shape of the current
    # credential model: Daintree holds the upstream key and callers hold none.
    # CEREBRAS_API_KEY must fail READINESS — without it nothing can be answered,
    # and discovering that on the first turn surfaces as an upstream 401 that
    # reads like the caller's fault.
    s = Settings(CEREBRAS_API_KEY="", STATE_SIGNING_KEY_CURRENT="")
    assert s.missing_secrets() == ["STATE_SIGNING_KEY_CURRENT", "CEREBRAS_API_KEY"]


def test_missing_upstream_key_alone_still_fails_readiness() -> None:
    s = Settings(CEREBRAS_API_KEY="", STATE_SIGNING_KEY_CURRENT="sk")
    assert s.missing_secrets() == ["CEREBRAS_API_KEY"]


def test_missing_secrets_empty_when_present() -> None:
    assert _settings().missing_secrets() == []


def test_no_inbound_api_key_is_ever_required() -> None:
    # There is still no INBOUND key to configure — the door is open by design
    # until account auth lands. A deployment carrying only the two server-owned
    # secrets is complete, in development AND in production shape.
    for env in ("development", "production"):
        s = _settings(
            ENVIRONMENT=env,
            STATE_SIGNING_KEY_CURRENT="y" * 48,
            STATE_SALT="a-real-random-salt-value",
        )
        assert s.missing_secrets() == []
        assert not any("DAINTREE_API_KEY" in p for p in s.weak_secrets())


def test_server_provider_is_built_with_the_configured_credential() -> None:
    # THE production wiring guarantee, now inverted: the serving provider carries
    # the server's own key, because callers no longer bring one. A provider built
    # keyless would fail every request in `_resolve_key`.
    from daintree_assistant_server.main import _build_provider

    s = _settings(CEREBRAS_API_KEY="csk-server-owned")
    provider = _build_provider(s)
    assert provider._resolve_key(None).get_secret_value() == "csk-server-owned"  # noqa: SLF001


def test_production_requires_the_real_cerebras_origin() -> None:
    # THE SERVER's key rides every request to this origin, so a hardened
    # deployment must not be able to point it anywhere else (or downgrade to
    # plain http). The stake went UP with the credential move: a misdirected
    # request now leaks the one key that funds all of Daintree, not one caller's.
    base = {
        "ENVIRONMENT": "production",
        "STATE_SIGNING_KEY_CURRENT": "y" * 48,
        "STATE_SALT": "a-real-random-salt-value",
    }
    for bad in (
        "http://api.cerebras.ai/v1",
        "https://api.cerebras.ai.evil.example/v1",
        "https://api.cerebras.ai@evil.example/v1",
        "https://user@api.cerebras.ai/v1",  # userinfo never belongs here
        "https://api.cerebras.ai:444/v1",  # non-default port = different origin
        "https://api.cerebras.ai:bogus/v1",  # malformed port fails readiness, not requests
    ):
        s = _settings(CEREBRAS_BASE_URL=bad, **base)
        assert any("CEREBRAS_BASE_URL" in p for p in s.weak_secrets()), bad
    for ok_url in (
        "https://api.cerebras.ai/v1",
        "https://API.CEREBRAS.AI/v1",  # hostname compares case-insensitively
        "https://api.cerebras.ai:443/v1",  # explicit default port
    ):
        ok = _settings(CEREBRAS_BASE_URL=ok_url, **base)
        assert ok.weak_secrets() == [], ok_url
    # Development stays free to point at proxies/mocks.
    dev = _settings(CEREBRAS_BASE_URL="http://127.0.0.1:9999/v1")
    assert dev.weak_secrets() == []


def test_weak_secrets_allowed_in_development() -> None:
    # Short test keys + default salt are fine in development.
    assert _settings().weak_secrets() == []


def test_weak_secrets_flagged_in_production() -> None:
    s = _settings(ENVIRONMENT="production")  # short keys + default salt
    problems = s.weak_secrets()
    assert any("STATE_SALT" in p for p in problems)
    assert any("STATE_SIGNING_KEY_CURRENT" in p for p in problems)
    # The shared INBOUND API key is gone; the only key-shaped gate left is the
    # upstream ORIGIN (checked above), never an inbound credential.
    assert not any("DAINTREE_API_KEY" in p for p in problems)


def test_strong_production_secrets_pass() -> None:
    s = _settings(
        ENVIRONMENT="production",
        STATE_SIGNING_KEY_CURRENT="y" * 48,
        STATE_SALT="a-real-random-salt-value",
    )
    assert s.weak_secrets() == []


def test_production_previous_key_must_pair_with_kid() -> None:
    s = _settings(
        ENVIRONMENT="production",
        STATE_SIGNING_KEY_CURRENT="y" * 48,
        STATE_SALT="a-real-random-salt-value",
        STATE_SIGNING_KEY_PREVIOUS="z" * 48,  # no kid → inconsistent
    )
    assert any("PREVIOUS" in p for p in s.weak_secrets())


def test_secret_not_in_repr() -> None:
    s = _settings(CEREBRAS_API_KEY="csk-should-not-appear")
    assert "csk-should-not-appear" not in repr(s)


def test_authenticate_returns_the_caller_key() -> None:
    # Nothing sends one today, but a token that IS present is still carried and
    # still overrides the server's credential — the seam account auth lands in.
    principal = authenticate("Bearer csk-abc123")
    assert principal.has_scope("chat")
    assert principal.api_key is not None
    assert principal.api_key.get_secret_value() == "csk-abc123"


def test_authenticate_accepts_any_wellformed_token() -> None:
    # Structural admission only: the server no longer knows a shared secret, so
    # any header-safe token is accepted here and judged by the upstream instead.
    principal = authenticate("Bearer some-future-subscription-key")
    assert principal.api_key is not None
    assert principal.api_key.get_secret_value() == "some-future-subscription-key"


def test_authenticate_key_not_in_repr() -> None:
    principal = authenticate("Bearer csk-secret-material")
    assert "csk-secret-material" not in repr(principal)


@pytest.mark.parametrize(
    "header",
    ["secret", "Token secret", "Bearer", "Bearer  ", "Bearer bad key", "Bearer k\x7f"],
)
def test_authenticate_rejects_a_malformed_header(header: str) -> None:
    """Present but unusable is still a 401 — deliberately, and unlike absence.

    A caller who sends a broken header MEANT to authenticate. Ignoring it would
    show them a successful sign-in while every turn quietly ran on the server's
    account, which is the one failure mode the open door could otherwise create.
    """
    with pytest.raises(DaintreeError) as exc:
        authenticate(header)
    assert exc.value.status_code == 401
    assert exc.value.code == "invalid_api_key"


def test_authenticate_rejects_an_absurdly_long_token() -> None:
    from daintree_assistant_server.auth import MAX_TOKEN_LENGTH

    at_cap = authenticate(f"Bearer {'k' * MAX_TOKEN_LENGTH}").api_key
    assert at_cap is not None  # at the cap: fine
    with pytest.raises(DaintreeError) as exc:
        authenticate(f"Bearer {'k' * (MAX_TOKEN_LENGTH + 1)}")
    assert exc.value.code == "invalid_api_key"


def test_authenticate_rejects_non_ascii_token() -> None:
    with pytest.raises(DaintreeError) as exc:
        authenticate("Bearer csk-café")
    assert exc.value.code == "invalid_api_key"


def test_authenticate_case_insensitive_scheme() -> None:
    # "bearer" scheme keyword is case-insensitive; the token itself is kept verbatim.
    key = authenticate("bearer MiXeD-case").api_key
    assert key is not None
    assert key.get_secret_value() == "MiXeD-case"


@pytest.mark.parametrize("header", [None, "", "   "])
def test_an_absent_header_is_served_anonymously(header: str | None) -> None:
    """The current credential model, at its one seam.

    The server holds the upstream key, so a request with no bearer token has one
    to spend and is served. This assertion is the thing that must break loudly
    when account authentication lands — it is not an oversight, it is the stage.
    """
    principal = authenticate(header)
    assert principal.id == "anonymous"
    assert principal.api_key is None
    assert principal.has_scope("chat")


# ---- upstream request dumping (local-development-only guard) --------------


def test_dump_dir_unset_by_default() -> None:
    s = _settings()
    assert s.dump_upstream_dir is None
    assert s.local_only_problems() == []


def test_dump_dir_allowed_in_development() -> None:
    s = _settings(DAINTREE_DUMP_UPSTREAM_DIR="/tmp/dumps")
    assert s.dump_upstream_dir == "/tmp/dumps"
    assert s.local_only_problems() == []


def test_dump_dir_fails_readiness_in_production() -> None:
    s = _settings(ENVIRONMENT="production", DAINTREE_DUMP_UPSTREAM_DIR="/tmp/dumps")
    assert any("DAINTREE_DUMP_UPSTREAM_DIR" in p for p in s.local_only_problems())


def test_dump_dir_empty_string_is_not_a_problem_in_production() -> None:
    # An empty value means "unset" — it must not trip the production gate.
    s = _settings(ENVIRONMENT="production", DAINTREE_DUMP_UPSTREAM_DIR="")
    assert s.local_only_problems() == []


def test_max_active_skills_rejects_a_cap_below_two() -> None:
    # Every skill resolves to a domain foundation and the orchestrator admits a
    # specific plus its foundation as one atomic unit, so a budget of 1 fits no unit
    # at all and would silently load NOTHING. The floor makes that unsettable.
    with pytest.raises(ValidationError):
        _settings(MAX_ACTIVE_SKILLS=1)


def test_max_active_skills_accepts_the_floor() -> None:
    assert _settings(MAX_ACTIVE_SKILLS=2).max_active_skills == 2
