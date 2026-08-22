"""Configuration and authentication tests."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from daintree_assistant_server.auth import authenticate
from daintree_assistant_server.config import (
    _SERVING_KEY_ENV,
    _SERVING_ORIGIN,
    _SERVING_URL_ENV,
    SERVING_HOST,
    SERVING_KEY_ENV,
    SERVING_ORIGIN,
    SERVING_URL_ENV,
    Settings,
)


def _settings(**overrides: object) -> Settings:
    base = {"STATE_SIGNING_KEY_CURRENT": "sk", SERVING_KEY_ENV: "sk-test"}
    base.update(overrides)
    return Settings(**base)  # type: ignore[arg-type]


def test_missing_secrets_reports_both_server_owned_secrets() -> None:
    # Both are the SERVER's own material, which is the whole shape of the current
    # credential model: Daintree holds the upstream key and callers hold none.
    # The SERVING key must fail READINESS — without it nothing can be answered,
    # and discovering that on the first turn surfaces as an upstream 401 that
    # reads like the caller's fault. Named off SERVING_HOST rather than a vendor,
    # so switching hosts moves the gate instead of stranding this assertion.
    s = Settings(**{SERVING_KEY_ENV: "", "STATE_SIGNING_KEY_CURRENT": ""})
    assert s.missing_secrets() == ["STATE_SIGNING_KEY_CURRENT", SERVING_KEY_ENV]


def test_missing_upstream_key_alone_still_fails_readiness() -> None:
    s = Settings(**{SERVING_KEY_ENV: "", "STATE_SIGNING_KEY_CURRENT": "sk"})
    assert s.missing_secrets() == [SERVING_KEY_ENV]


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

    s = _settings(**{SERVING_KEY_ENV: "sk-server-owned"})
    provider = _build_provider(s)
    assert provider._resolve_key(None).get_secret_value() == "sk-server-owned"  # noqa: SLF001


def test_the_serving_host_table_is_pinned_to_literals() -> None:
    """The independent oracle behind every SERVING_*-parameterised assertion.

    Those tests read the same constants the code reads, which is what lets them
    survive a host switch — and also means a mutation of the TABLE moves the test
    with it. Both such mutations happen to be caught elsewhere today
    (a misspelled key name by the provider-credential test, a wrong origin by the
    strong-secrets test), but only incidentally: neither of those tests is about
    this table, and either could stop covering it for reasons of its own.

    So the literals live here, once, for BOTH hosts — asserting the whole table
    rather than the active row, so switching SERVING_HOST does not strand it. A
    wrong origin is the one that matters: this is the host the server's own
    upstream credential is sent to on every request.
    """
    assert _SERVING_KEY_ENV == {
        "together": "TOGETHER_API_KEY",
        "cerebras": "CEREBRAS_API_KEY",
    }
    assert _SERVING_URL_ENV == {
        "together": "TOGETHER_BASE_URL",
        "cerebras": "CEREBRAS_BASE_URL",
    }
    assert _SERVING_ORIGIN == {
        "together": "api.together.xyz",
        "cerebras": "api.cerebras.ai",
    }
    # And the resolved names really are the active row, not a stale copy.
    assert _SERVING_KEY_ENV[SERVING_HOST] == SERVING_KEY_ENV
    assert _SERVING_URL_ENV[SERVING_HOST] == SERVING_URL_ENV
    assert _SERVING_ORIGIN[SERVING_HOST] == SERVING_ORIGIN


def test_production_rejects_a_valid_https_origin_that_is_not_the_serving_host() -> None:
    """Hardening must pin THE origin, not merely require https.

    A check that accepted any well-formed https URL would pass every case in the
    test above it — every rejection there is malformed in some other way (plain
    http, userinfo, odd port, lookalike suffix). This one is clean, valid, and
    simply somewhere else, which is what a misdirected key actually looks like.
    """
    s = _settings(
        **{SERVING_URL_ENV: "https://api.openai.com/v1"},
        ENVIRONMENT="production",
        STATE_SIGNING_KEY_CURRENT="y" * 48,
        STATE_SALT="a-real-random-salt-value",
    )
    assert any(SERVING_URL_ENV in p for p in s.weak_secrets())


def test_production_requires_the_real_serving_origin() -> None:
    # THE SERVER's key rides every request to this origin, so a hardened
    # deployment must not be able to point it anywhere else (or downgrade to
    # plain http). The stake went UP with the credential move: a misdirected
    # request now leaks the one key that funds all of Daintree, not one caller's.
    base = {
        "ENVIRONMENT": "production",
        "STATE_SIGNING_KEY_CURRENT": "y" * 48,
        "STATE_SALT": "a-real-random-salt-value",
    }
    host = SERVING_ORIGIN
    for bad in (
        f"http://{host}/v1",
        f"https://{host}.evil.example/v1",
        f"https://{host}@evil.example/v1",
        f"https://user@{host}/v1",  # userinfo never belongs here
        f"https://{host}:444/v1",  # non-default port = different origin
        f"https://{host}:bogus/v1",  # malformed port fails readiness, not requests
    ):
        s = _settings(**{SERVING_URL_ENV: bad}, **base)
        assert any(SERVING_URL_ENV in p for p in s.weak_secrets()), bad
    for ok_url in (
        f"https://{host}/v1",
        f"https://{host.upper()}/v1",  # hostname compares case-insensitively
        f"https://{host}:443/v1",  # explicit default port
    ):
        ok = _settings(**{SERVING_URL_ENV: ok_url}, **base)
        assert ok.weak_secrets() == [], ok_url
    # Development stays free to point at proxies/mocks.
    dev = _settings(**{SERVING_URL_ENV: "http://127.0.0.1:9999/v1"})
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
    s = _settings(**{SERVING_KEY_ENV: "sk-should-not-appear"})
    assert "sk-should-not-appear" not in repr(s)


@pytest.mark.parametrize(
    "header",
    [
        None,
        "",
        "   ",
        # A CLI mid-migration, still sending the OpenRouter key it used to
        # authenticate with. This is the case the ignore rule exists for: read it,
        # and it would be forwarded to Cerebras and rejected as the CALLER's bad
        # key — an error blaming them for a header they were right to still send.
        "Bearer sk-or-v1-a-stale-openrouter-key",
        # Shapes that used to be our own 401. A malformed bearer only meant
        # something while a bearer meant something; rejecting one now would
        # refuse a request the server can serve, over a value it does not read.
        "Bearer",
        "Bearer  ",
        "Bearer bad key",
        "Bearer k\x7f",
        "Basic abc",
        "Token secret",
        "not-a-scheme-at-all",
    ],
)
def test_every_authorization_header_is_ignored(header: str | None) -> None:
    """The header is not parsed, not validated, and never rejected.

    This is stronger than "a key is optional", and the difference is the whole
    point: the server holds the upstream credential, so there is no per-request
    credential for a bearer to supply and nothing for its shape to be wrong
    about.
    """
    principal = authenticate(header)
    assert principal.id == "anonymous"
    assert principal.has_scope("chat")


def test_the_principal_carries_no_credential_at_all() -> None:
    """Not `None` — absent.

    A `SecretStr | None` field that is structurally always None is a trap: the
    next reader threads it through, sees no type error, and believes a caller key
    is reaching the provider. The field comes back with account authentication,
    deliberately.
    """
    assert not hasattr(authenticate("Bearer anything"), "api_key")


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


# ---- skill overlay directory (local-development-only guard) ---------------


def test_skills_overlay_dir_unset_by_default() -> None:
    s = _settings()
    assert s.skills_overlay_dir is None
    assert s.local_only_problems() == []


def test_skills_overlay_dir_allowed_in_development() -> None:
    s = _settings(DAINTREE_SKILLS_OVERLAY_DIR="/tmp/skills")
    assert s.skills_overlay_dir == "/tmp/skills"
    assert s.local_only_problems() == []


def test_skills_overlay_dir_fails_readiness_in_production() -> None:
    s = _settings(ENVIRONMENT="production", DAINTREE_SKILLS_OVERLAY_DIR="/tmp/skills")
    assert any("DAINTREE_SKILLS_OVERLAY_DIR" in p for p in s.local_only_problems())


def test_skills_overlay_dir_fails_readiness_in_staging() -> None:
    # Staging is hardened too: it is the rehearsal for production, so a catalog
    # it serves must be the one its image shipped.
    s = _settings(ENVIRONMENT="staging", DAINTREE_SKILLS_OVERLAY_DIR="/tmp/skills")
    assert any("DAINTREE_SKILLS_OVERLAY_DIR" in p for p in s.local_only_problems())


def test_skills_overlay_dir_empty_string_is_not_a_problem_in_production() -> None:
    # An empty value means "unset" — it must not trip the production gate.
    s = _settings(ENVIRONMENT="production", DAINTREE_SKILLS_OVERLAY_DIR="")
    assert s.local_only_problems() == []


def test_both_local_only_features_are_reported_together() -> None:
    # The gate returns a LIST, and a deploy with two development flags left on
    # should learn about both from one readiness failure rather than one per fix.
    s = _settings(
        ENVIRONMENT="production",
        DAINTREE_DUMP_UPSTREAM_DIR="/tmp/dumps",
        DAINTREE_SKILLS_OVERLAY_DIR="/tmp/skills",
    )
    problems = s.local_only_problems()
    assert len(problems) == 2
    assert any("DAINTREE_DUMP_UPSTREAM_DIR" in p for p in problems)
    assert any("DAINTREE_SKILLS_OVERLAY_DIR" in p for p in problems)


def test_max_active_skills_rejects_a_cap_below_two() -> None:
    # Every skill resolves to a domain foundation and the orchestrator admits a
    # specific plus its foundation as one atomic unit, so a budget of 1 fits no unit
    # at all and would silently load NOTHING. The floor makes that unsettable.
    with pytest.raises(ValidationError):
        _settings(MAX_ACTIVE_SKILLS=1)


def test_max_active_skills_accepts_the_floor() -> None:
    assert _settings(MAX_ACTIVE_SKILLS=2).max_active_skills == 2


# ---------------------------------------------------------------------------
# Tool-projection rollout controls.
# ---------------------------------------------------------------------------


def test_tool_projection_defaults_to_shadow() -> None:
    """A deployment that configures nothing keeps behaving as it did.

    `shadow` is the safe default because it is the CURRENT behaviour: measure,
    send everything. A default of `enforced` would make this feature ship itself
    on the next deploy, which is the opposite of a staged rollout.
    """
    settings = _settings()
    assert settings.tool_projection_mode == "shadow"
    # 100 rather than 0 so that `enforced` means enforced. A mode that silently
    # enforces nothing is a worse trap than one needing a second variable set.
    assert settings.tool_projection_rollout_percent == 100


@pytest.mark.parametrize("mode", ["disabled", "shadow", "enforced"])
def test_every_documented_mode_is_accepted(mode: str) -> None:
    assert _settings(TOOL_PROJECTION_MODE=mode).tool_projection_mode == mode


def test_an_unknown_mode_is_rejected_rather_than_defaulted() -> None:
    """A typo must fail readiness, not silently pick a posture.

    Falling back to a default here would let `TOOL_PROJECTION_MODE=enfoced`
    read as shadow — an operator believing enforcement is on while it is not,
    or (worse, on the way back) believing it is off while it is not.
    """
    with pytest.raises(ValidationError):
        _settings(TOOL_PROJECTION_MODE="enfoced")


@pytest.mark.parametrize("percent", [0, 1, 50, 100])
def test_the_rollout_percentage_spans_its_whole_range(percent: int) -> None:
    assert _settings(TOOL_PROJECTION_ROLLOUT_PERCENT=percent).tool_projection_rollout_percent == (
        percent
    )


@pytest.mark.parametrize("percent", [-1, 101])
def test_a_percentage_outside_zero_to_one_hundred_is_rejected(percent: int) -> None:
    with pytest.raises(ValidationError):
        _settings(TOOL_PROJECTION_ROLLOUT_PERCENT=percent)


def test_the_rollout_controls_do_not_gate_readiness() -> None:
    """They are operational switches, not secrets or dev-only escape hatches.

    Neither belongs in the readiness gates, which exist for things that make the
    server unable or unfit to ANSWER — a missing upstream key, a malformed skill.
    A misconfigured rollout changes how many tools a turn is offered, which the
    fail-open paths already bound; it cannot stop the turn being served, and a
    server refused readiness over it would be down for a reason it could have
    simply logged.
    """
    settings = _settings(TOOL_PROJECTION_MODE="enforced", TOOL_PROJECTION_ROLLOUT_PERCENT=100)
    assert settings.missing_secrets() == []
    assert settings.local_only_problems() == []
