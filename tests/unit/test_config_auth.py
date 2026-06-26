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


def test_authenticate_empty_expected_key_rejects() -> None:
    s = Settings(DAINTREE_API_KEY="", DEEPSEEK_API_KEY="dk", STATE_SIGNING_KEY_CURRENT="sk")
    with pytest.raises(DaintreeError):
        authenticate("Bearer ", s)
