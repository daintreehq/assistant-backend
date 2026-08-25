"""``ENTITLEMENT_MODE`` and the URL it depends on.

The rule the whole file exists to hold: a bad entitlement configuration fails
readiness. It is never quietly demoted to ``off``, because an operator who asked
for enforcement and got an open till has no way to find out.
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from daintree_assistant_server.config import (
    EXECUTOR_KEY_ENV,
    FAST_RESPONSE_KEY_ENV,
    Settings,
)

_STAGING_URL = "https://staging.daintree.org/api/assistant/entitlement"

_ENFORCED_AUTH: dict[str, object] = {
    "AUTH_MODE": "enforce",
    "SUPABASE_AUTH_ISSUER": "https://proj.supabase.co/auth/v1",
    "SUPABASE_JWKS_URL": "https://proj.supabase.co/auth/v1/.well-known/jwks.json",
    "SUPABASE_OAUTH_AUTHORIZE_URL": "https://proj.supabase.co/auth/v1/oauth/authorize",
    "SUPABASE_OAUTH_TOKEN_URL": "https://proj.supabase.co/auth/v1/oauth/token",
    "SUPABASE_OAUTH_CLIENT_ID": "daintree-assistant",
}


def _settings(**overrides: object) -> Settings:
    base: dict[str, object] = {
        "STATE_SIGNING_KEY_CURRENT": "sk",
        EXECUTOR_KEY_ENV: "sk-test",
        FAST_RESPONSE_KEY_ENV: "csk-test",
    }
    base.update(overrides)
    return Settings(**base)  # type: ignore[arg-type]


def test_the_shipped_default_is_off_and_asks_nothing() -> None:
    settings = _settings()
    assert settings.entitlement_mode == "off"
    assert settings.daintree_entitlement_url == ""
    assert settings.entitlement_problems() == []
    assert settings.entitlement_is_configured is False


def test_an_unknown_mode_is_rejected_by_the_type() -> None:
    with pytest.raises(ValidationError):
        _settings(ENTITLEMENT_MODE="on")


@pytest.mark.parametrize("mode", ["observe", "enforce"])
def test_a_binding_mode_requires_enforced_identity(mode: str) -> None:
    problems = _settings(
        ENTITLEMENT_MODE=mode, DAINTREE_ENTITLEMENT_URL=_STAGING_URL
    ).entitlement_problems()
    assert any("requires AUTH_MODE=enforce" in p for p in problems)


@pytest.mark.parametrize("mode", ["observe", "enforce"])
def test_a_binding_mode_requires_a_url(mode: str) -> None:
    problems = _settings(ENTITLEMENT_MODE=mode, **_ENFORCED_AUTH).entitlement_problems()
    assert any("requires DAINTREE_ENTITLEMENT_URL" in p for p in problems)


@pytest.mark.parametrize("mode", ["observe", "enforce"])
def test_a_complete_binding_configuration_passes(mode: str) -> None:
    settings = _settings(
        ENTITLEMENT_MODE=mode, DAINTREE_ENTITLEMENT_URL=_STAGING_URL, **_ENFORCED_AUTH
    )
    assert settings.entitlement_problems() == []
    assert settings.entitlement_is_configured is True


def test_a_bad_configuration_fails_readiness_rather_than_downgrading() -> None:
    """The single most important property here.

    ``local_only_problems`` is what readiness consults, so an entitlement problem
    has to reach it — otherwise the revision starts, reports itself healthy, and
    charges nobody.
    """
    settings = _settings(ENTITLEMENT_MODE="enforce", ENVIRONMENT="staging")
    assert settings.entitlement_mode == "enforce"  # NOT silently reset to "off"
    problems = settings.local_only_problems()
    assert any("ENTITLEMENT_MODE=enforce" in p for p in problems)


# --- the URL itself, validated in every mode -------------------------------


def test_the_url_is_validated_even_while_the_mode_is_off() -> None:
    """A malformed value is not dormant under ``off``.

    The account endpoint uses it the moment somebody configures a mode, and the
    rollout is the worst time to discover it.
    """
    problems = _settings(DAINTREE_ENTITLEMENT_URL="https://evil.example.com/e").entitlement_problems()
    assert any("must point at a Daintree site" in p for p in problems)


def test_a_query_string_is_refused() -> None:
    problems = _settings(
        DAINTREE_ENTITLEMENT_URL=f"{_STAGING_URL}?user=someone-else"
    ).entitlement_problems()
    assert any("must not carry a query string" in p for p in problems)


def test_a_fragment_is_refused() -> None:
    problems = _settings(
        DAINTREE_ENTITLEMENT_URL=f"{_STAGING_URL}#frag"
    ).entitlement_problems()
    assert any("fragment" in p for p in problems)


def test_embedded_credentials_are_refused() -> None:
    problems = _settings(
        DAINTREE_ENTITLEMENT_URL="https://user:pass@staging.daintree.org/api/assistant/entitlement"
    ).entitlement_problems()
    assert any("must not embed credentials" in p for p in problems)


def test_plain_http_is_refused_outside_loopback() -> None:
    problems = _settings(
        DAINTREE_ENTITLEMENT_URL="http://staging.daintree.org/api/assistant/entitlement"
    ).entitlement_problems()
    assert any("https" in p for p in problems)


def test_a_loopback_url_is_developable() -> None:
    settings = _settings(
        DAINTREE_ENTITLEMENT_URL="http://127.0.0.1:5173/api/assistant/entitlement"
    )
    assert settings.entitlement_problems() == []
    assert settings.entitlement_is_configured is True


def test_the_loopback_carve_out_is_withdrawn_in_a_hardened_environment() -> None:
    problems = _settings(
        ENVIRONMENT="production",
        DAINTREE_ENTITLEMENT_URL="http://127.0.0.1:5173/api/assistant/entitlement",
    ).entitlement_problems()
    assert problems


def test_configured_is_independent_of_the_mode() -> None:
    """``/v1/daintree/account`` resolves whenever the URL is usable.

    A mode is what makes a verdict BINDING, not what makes it obtainable — which
    is what lets the contract be exercised against the real website before it can
    refuse anybody's turn.
    """
    settings = _settings(ENTITLEMENT_MODE="off", DAINTREE_ENTITLEMENT_URL=_STAGING_URL)
    assert settings.entitlement_is_configured is True


# --- the full mode matrix ---------------------------------------------------


@pytest.mark.parametrize("auth_mode", ["open", "observe"])
@pytest.mark.parametrize("entitlement_mode", ["observe", "enforce"])
def test_no_unenforced_identity_mode_may_carry_a_binding_entitlement_mode(
    auth_mode: str, entitlement_mode: str
) -> None:
    """``observe`` is the one worth stating separately.

    It verifies a present token and then deliberately returns the ANONYMOUS
    principal, so it looks like identity is on while there is still no subject to
    ask the website about — the combination most likely to be configured by
    someone who believes they have staged this correctly.
    """
    settings = _settings(
        AUTH_MODE=auth_mode,
        ENTITLEMENT_MODE=entitlement_mode,
        DAINTREE_ENTITLEMENT_URL=_STAGING_URL,
    )
    assert any("requires AUTH_MODE=enforce" in p for p in settings.entitlement_problems())


@pytest.mark.parametrize("auth_mode", ["open", "observe", "enforce"])
def test_entitlement_off_is_compatible_with_every_identity_mode(auth_mode: str) -> None:
    """The two rollouts are staged independently; `off` constrains neither."""
    overrides = {**_ENFORCED_AUTH, "AUTH_MODE": auth_mode, "ENTITLEMENT_MODE": "off"}
    assert _settings(**overrides).entitlement_problems() == []


def test_a_url_ending_in_a_bare_question_mark_is_still_refused() -> None:
    """`urlsplit` reports an empty query for this, so a component test passes it.

    It carries no data — which is why it should be corrected in configuration
    rather than tolerated in a value that is compared and forwarded.
    """
    problems = _settings(
        DAINTREE_ENTITLEMENT_URL=f"{_STAGING_URL}?"
    ).entitlement_problems()
    assert any("query string or fragment" in p for p in problems)
