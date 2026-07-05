"""Configuration and authentication tests."""

from __future__ import annotations

import pytest

from daintree_assistant_server.auth import (
    DEVELOPMENT_PRINCIPAL,
    authenticate,
)
from daintree_assistant_server.config import Settings
from daintree_assistant_server.errors import DaintreeError


def _settings(**overrides: object) -> Settings:
    base = {
        "DAINTREE_API_KEY": "secret",
        "DEEPSEEK_API_KEY": "dk",
        "STATE_SIGNING_KEY_CURRENT": "sk",
    }
    base.update(overrides)
    return Settings(**base)  # type: ignore[arg-type]


def test_missing_secrets_reports_each() -> None:
    s = Settings(DAINTREE_API_KEY="", DEEPSEEK_API_KEY="", STATE_SIGNING_KEY_CURRENT="")
    assert set(s.missing_secrets()) == {
        "DEEPSEEK_API_KEY",
        "DAINTREE_API_KEY",
        "STATE_SIGNING_KEY_CURRENT",
    }


def test_missing_secrets_empty_when_present() -> None:
    assert _settings().missing_secrets() == []


def test_weak_secrets_allowed_in_development() -> None:
    # Short test keys + default salt are fine in development.
    assert _settings().weak_secrets() == []


def test_weak_secrets_flagged_in_production() -> None:
    s = _settings(ENVIRONMENT="production")  # short keys + default salt
    problems = s.weak_secrets()
    assert any("STATE_SALT" in p for p in problems)
    assert any("DAINTREE_API_KEY" in p for p in problems)
    assert any("STATE_SIGNING_KEY_CURRENT" in p for p in problems)


def test_strong_production_secrets_pass() -> None:
    s = _settings(
        ENVIRONMENT="production",
        DAINTREE_API_KEY="x" * 24,
        STATE_SIGNING_KEY_CURRENT="y" * 48,
        STATE_SALT="a-real-random-salt-value",
    )
    assert s.weak_secrets() == []


def test_production_previous_key_must_pair_with_kid() -> None:
    s = _settings(
        ENVIRONMENT="production",
        DAINTREE_API_KEY="x" * 24,
        STATE_SIGNING_KEY_CURRENT="y" * 48,
        STATE_SALT="a-real-random-salt-value",
        STATE_SIGNING_KEY_PREVIOUS="z" * 48,  # no kid → inconsistent
    )
    assert any("PREVIOUS" in p for p in s.weak_secrets())


def test_secret_not_in_repr() -> None:
    s = _settings()
    assert "secret" not in repr(s)
    assert "dk" not in repr(s)


def test_authenticate_valid() -> None:
    principal = authenticate("Bearer secret", _settings())
    assert principal is DEVELOPMENT_PRINCIPAL
    assert principal.has_scope("chat")


@pytest.mark.parametrize(
    "header",
    [None, "", "secret", "Token secret", "Bearer", "Bearer wrong", "Bearer SECRET"],
)
def test_authenticate_rejects(header: str | None) -> None:
    with pytest.raises(DaintreeError) as exc:
        authenticate(header, _settings())
    assert exc.value.status_code == 401
    assert exc.value.code == "invalid_api_key"


def test_authenticate_case_insensitive_scheme() -> None:
    # "bearer" scheme keyword is case-insensitive; the token itself is not.
    assert authenticate("bearer secret", _settings()) is DEVELOPMENT_PRINCIPAL


def test_authenticate_dev_no_key_allows_unauthenticated() -> None:
    # No key configured in development → unauthenticated access (any/no header).
    s = Settings(
        ENVIRONMENT="development", DAINTREE_API_KEY="", DEEPSEEK_API_KEY="dk",
        STATE_SIGNING_KEY_CURRENT="sk",
    )
    assert authenticate(None, s) is DEVELOPMENT_PRINCIPAL
    assert authenticate("Bearer anything", s) is DEVELOPMENT_PRINCIPAL


def test_authenticate_prod_no_key_rejects() -> None:
    # Production never allows unauthenticated access, even with no key configured.
    s = Settings(
        ENVIRONMENT="production", DAINTREE_API_KEY="", DEEPSEEK_API_KEY="dk",
        STATE_SIGNING_KEY_CURRENT="sk",
    )
    with pytest.raises(DaintreeError) as exc:
        authenticate(None, s)
    assert exc.value.code == "invalid_api_key"


# ---- skill learning (local-development-only guard) ----------------------


def test_skill_learning_off_by_default() -> None:
    s = _settings()
    assert s.skill_learning_mode == "off"
    assert s.skill_learning_enabled is False
    assert s.local_only_problems() == []


@pytest.mark.parametrize("mode", ["propose", "apply"])
def test_skill_learning_enabled_ok_in_development(mode: str) -> None:
    # The GPT-5.5 builder/improver need an OpenAI key; with it present, dev learning
    # is ready. OPENAI_API_KEY is passed explicitly so the check is hermetic even when
    # the developer's own environment has the key set.
    s = _settings(SKILL_LEARNING_MODE=mode, OPENAI_API_KEY="ok")
    assert s.skill_learning_enabled is True
    assert s.local_only_problems() == []


@pytest.mark.parametrize("mode", ["propose", "apply"])
def test_skill_learning_enabled_needs_openai_key(mode: str) -> None:
    # Enabled without the OpenAI key its builder/improver require fails readiness,
    # rather than silently failing open the first time learning runs. Explicit empty
    # key overrides any ambient OPENAI_API_KEY in the developer's environment.
    s = _settings(SKILL_LEARNING_MODE=mode, OPENAI_API_KEY="")
    assert any("OPENAI_API_KEY" in p for p in s.local_only_problems())


@pytest.mark.parametrize("mode", ["propose", "apply"])
def test_skill_learning_fails_readiness_in_production(mode: str) -> None:
    s = _settings(ENVIRONMENT="production", SKILL_LEARNING_MODE=mode)
    assert any("SKILL_LEARNING_MODE" in p for p in s.local_only_problems())


def test_skill_learning_off_ok_in_production() -> None:
    s = _settings(ENVIRONMENT="production")
    assert s.skill_learning_enabled is False
    assert s.local_only_problems() == []
