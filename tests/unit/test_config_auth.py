"""Configuration and authentication tests."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from daintree_assistant_server.auth import authenticate
from daintree_assistant_server.config import Settings
from daintree_assistant_server.errors import DaintreeError


def _settings(**overrides: object) -> Settings:
    base = {"STATE_SIGNING_KEY_CURRENT": "sk"}
    base.update(overrides)
    return Settings(**base)  # type: ignore[arg-type]


def test_missing_secrets_reports_signing_key_only() -> None:
    # No API key is required at startup: every request supplies the caller's own,
    # which is both the auth credential and the upstream credential.
    s = Settings(OPENROUTER_API_KEY="", STATE_SIGNING_KEY_CURRENT="")
    assert s.missing_secrets() == ["STATE_SIGNING_KEY_CURRENT"]


def test_missing_secrets_empty_when_present() -> None:
    assert _settings().missing_secrets() == []


def test_ready_without_any_api_key_env_vars() -> None:
    # A deployment with zero key material beyond the signing key is complete —
    # in development AND in production shape. OPENROUTER_API_KEY is pinned empty
    # so a developer's exported key can't quietly satisfy the premise.
    for env in ("development", "production"):
        s = _settings(
            ENVIRONMENT=env,
            OPENROUTER_API_KEY="",
            STATE_SIGNING_KEY_CURRENT="y" * 48,
            STATE_SALT="a-real-random-salt-value",
        )
        assert not s.openrouter_api_key.get_secret_value()
        assert s.missing_secrets() == []


def test_server_provider_is_built_without_a_default_credential() -> None:
    # THE production wiring guarantee: even with a key in the environment, the
    # serving provider must be keyless — every call carries the caller's key, and
    # a propagation bug fails loudly instead of billing this shared account.
    from daintree_assistant_server.main import _build_provider

    s = _settings(OPENROUTER_API_KEY="must-not-be-used")
    provider = _build_provider(s)
    assert provider._default_api_key is None  # noqa: SLF001


def test_production_requires_the_real_openrouter_origin() -> None:
    # Caller keys ride every request to this origin; a hardened deployment must
    # not be able to point them anywhere else (or downgrade to plain http).
    base = {
        "ENVIRONMENT": "production",
        "STATE_SIGNING_KEY_CURRENT": "y" * 48,
        "STATE_SALT": "a-real-random-salt-value",
    }
    for bad in (
        "http://openrouter.ai/api/v1",
        "https://openrouter.ai.evil.example/api/v1",
        "https://openrouter.ai@evil.example/api/v1",
        "https://user@openrouter.ai/api/v1",  # userinfo never belongs here
        "https://openrouter.ai:444/api/v1",  # non-default port = different origin
        "https://openrouter.ai:bogus/api/v1",  # malformed port fails readiness, not requests
    ):
        s = _settings(OPENROUTER_BASE_URL=bad, **base)
        assert any("OPENROUTER_BASE_URL" in p for p in s.weak_secrets()), bad
    for ok_url in (
        "https://openrouter.ai/api/v1",
        "https://OPENROUTER.AI/api/v1",  # hostname compares case-insensitively
        "https://openrouter.ai:443/api/v1",  # explicit default port
    ):
        ok = _settings(OPENROUTER_BASE_URL=ok_url, **base)
        assert ok.weak_secrets() == [], ok_url
    # Development stays free to point at proxies/mocks.
    dev = _settings(OPENROUTER_BASE_URL="http://127.0.0.1:9999/v1")
    assert dev.weak_secrets() == []


def test_weak_secrets_allowed_in_development() -> None:
    # Short test keys + default salt are fine in development.
    assert _settings().weak_secrets() == []


def test_weak_secrets_flagged_in_production() -> None:
    s = _settings(ENVIRONMENT="production")  # short keys + default salt
    problems = s.weak_secrets()
    assert any("STATE_SALT" in p for p in problems)
    assert any("STATE_SIGNING_KEY_CURRENT" in p for p in problems)
    # The shared inbound API key is gone; nothing key-related is gated here.
    assert not any("API_KEY" in p for p in problems)


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
    s = _settings(OPENROUTER_API_KEY="dk")
    assert "dk" not in repr(s)


def test_authenticate_returns_the_caller_key() -> None:
    principal = authenticate("Bearer sk-or-v1-abc123")
    assert principal.has_scope("chat")
    assert principal.api_key.get_secret_value() == "sk-or-v1-abc123"


def test_authenticate_accepts_any_wellformed_token() -> None:
    # Structural admission only: the server no longer knows a shared secret, so
    # any header-safe token is accepted here and judged by the upstream instead.
    principal = authenticate("Bearer some-future-subscription-key")
    assert principal.api_key.get_secret_value() == "some-future-subscription-key"


def test_authenticate_key_not_in_repr() -> None:
    principal = authenticate("Bearer sk-or-v1-secret-material")
    assert "sk-or-v1-secret-material" not in repr(principal)


@pytest.mark.parametrize(
    "header",
    [None, "", "secret", "Token secret", "Bearer", "Bearer  ", "Bearer bad key", "Bearer k\x7f"],
)
def test_authenticate_rejects_missing_or_malformed(header: str | None) -> None:
    with pytest.raises(DaintreeError) as exc:
        authenticate(header)
    assert exc.value.status_code == 401
    assert exc.value.code == "invalid_api_key"


def test_authenticate_rejects_an_absurdly_long_token() -> None:
    from daintree_assistant_server.auth import MAX_TOKEN_LENGTH

    assert authenticate(f"Bearer {'k' * MAX_TOKEN_LENGTH}").api_key  # at the cap: fine
    with pytest.raises(DaintreeError) as exc:
        authenticate(f"Bearer {'k' * (MAX_TOKEN_LENGTH + 1)}")
    assert exc.value.code == "invalid_api_key"


def test_authenticate_rejects_non_ascii_token() -> None:
    with pytest.raises(DaintreeError) as exc:
        authenticate("Bearer sk-or-v1-café")
    assert exc.value.code == "invalid_api_key"


def test_authenticate_case_insensitive_scheme() -> None:
    # "bearer" scheme keyword is case-insensitive; the token itself is kept verbatim.
    assert authenticate("bearer MiXeD-case").api_key.get_secret_value() == "MiXeD-case"


def test_authenticate_never_allows_unauthenticated() -> None:
    # The old dev shortcut is gone: no environment serves without a caller key
    # (without one there would be no upstream credential to run the turn with).
    with pytest.raises(DaintreeError) as exc:
        authenticate(None)
    assert exc.value.code == "invalid_api_key"


# ---- skill learning (local-development-only guard) ----------------------


def test_skill_learning_off_by_default() -> None:
    s = _settings()
    assert s.skill_learning_mode == "off"
    assert s.skill_learning_enabled is False
    assert s.local_only_problems() == []


@pytest.mark.parametrize("mode", ["propose", "apply"])
def test_skill_learning_enabled_ok_in_development(mode: str) -> None:
    # Learning is a local-dev feature, so an enabled mode is fine outside production
    # and needs no credential beyond the one OpenRouter key.
    s = _settings(SKILL_LEARNING_MODE=mode)
    assert s.skill_learning_enabled is True
    assert s.local_only_problems() == []


@pytest.mark.parametrize("mode", ["propose", "apply"])
def test_skill_learning_enabled_needs_no_extra_credential(mode: str) -> None:
    # The builder/improver reach their stronger model over the SAME OpenRouter key
    # as every other call, so enabling learning must not demand a second credential
    # (it used to require its own OPENAI_API_KEY). Readiness stays clean in dev.
    s = _settings(SKILL_LEARNING_MODE=mode)
    assert s.local_only_problems() == []


@pytest.mark.parametrize("mode", ["propose", "apply"])
def test_skill_learning_fails_readiness_in_production(mode: str) -> None:
    s = _settings(ENVIRONMENT="production", SKILL_LEARNING_MODE=mode)
    assert any("SKILL_LEARNING_MODE" in p for p in s.local_only_problems())


def test_skill_learning_off_ok_in_production() -> None:
    s = _settings(ENVIRONMENT="production")
    assert s.skill_learning_enabled is False
    assert s.local_only_problems() == []


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
