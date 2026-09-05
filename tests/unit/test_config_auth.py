"""Configuration and authentication tests."""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest
from pydantic import ValidationError

from daintree_assistant_server.auth import authenticate
from daintree_assistant_server.config import (
    EXECUTOR_KEY_ENV,
    EXECUTOR_ORIGIN,
    EXECUTOR_URL_ENV,
    FAST_RESPONSE_ENABLED,
    FAST_RESPONSE_KEY_ENV,
    Settings,
)


def _settings(**overrides: object) -> Settings:
    base: dict[str, object] = {
        "STATE_SIGNING_KEY_CURRENT": "sk",
        EXECUTOR_KEY_ENV: "sk-test",
        FAST_RESPONSE_KEY_ENV: "csk-test",
    }
    base.update(overrides)
    return Settings(**base)  # type: ignore[arg-type]


def test_missing_secrets_reports_every_server_owned_secret() -> None:
    # BOTH are the SERVER's own material, which is the whole shape of the current
    # credential model: Daintree holds every upstream key and callers hold none.
    # The SERVING key must fail READINESS — without it nothing can be answered,
    # and discovering that on the first turn surfaces as an upstream 401 that
    # reads like the caller's fault.
    #
    # The fast-response key is deliberately NOT here. It was required only while
    # that stage had no off switch, when a missing key meant failing open on every
    # eligible turn. `FAST_RESPONSE_ENABLED` is False, so the stage opens no call
    # and the key funds nothing — see the test below.
    s = Settings(  # type: ignore[arg-type]
        **{EXECUTOR_KEY_ENV: "", FAST_RESPONSE_KEY_ENV: "", "STATE_SIGNING_KEY_CURRENT": ""}
    )
    assert s.missing_secrets() == [
        "STATE_SIGNING_KEY_CURRENT",
        EXECUTOR_KEY_ENV,
    ]


def test_missing_upstream_key_alone_still_fails_readiness() -> None:
    s = Settings(  # type: ignore[arg-type]
        **{
            EXECUTOR_KEY_ENV: "",
            FAST_RESPONSE_KEY_ENV: "csk-test",
            "STATE_SIGNING_KEY_CURRENT": "sk",
        }
    )
    assert s.missing_secrets() == [EXECUTOR_KEY_ENV]


def test_a_deployment_without_the_fast_response_key_is_complete() -> None:
    """The inverse of what this test asserted while the stage was always on.

    It used to fail readiness, because a missing key then meant the stage failed
    open on every eligible turn — indistinguishable from a model that chose not
    to answer, which is the failure nobody notices. `FAST_RESPONSE_ENABLED` is
    False now, so no call is ever opened and the credential funds nothing.
    Demanding it would fail readiness on a deployment that is in fact complete,
    which is the mirror-image mistake.
    """
    assert FAST_RESPONSE_ENABLED is False, (
        "The stage was re-enabled. It now runs on every eligible turn again, so "
        "CEREBRAS_API_KEY must go back into missing_secrets() in this commit — "
        "otherwise the stage fails open silently wherever the key is absent."
    )
    s = Settings(  # type: ignore[arg-type]
        **{
            EXECUTOR_KEY_ENV: "sk-test",
            FAST_RESPONSE_KEY_ENV: "",
            "STATE_SIGNING_KEY_CURRENT": "sk",
        }
    )
    assert s.missing_secrets() == []


def test_missing_secrets_empty_when_present() -> None:
    assert _settings().missing_secrets() == []


def test_no_inbound_api_key_is_ever_required() -> None:
    # There is still no INBOUND key to configure — the door is open by design
    # until account auth lands. A deployment carrying only the server-owned
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

    s = _settings(**{EXECUTOR_KEY_ENV: "sk-server-owned"})
    provider = _build_provider(s)
    assert provider._resolve_key(None).get_secret_value() == "sk-server-owned"  # noqa: SLF001


def test_the_serving_identity_is_pinned_to_literals() -> None:
    """The independent oracle behind every SERVING_*-parameterised assertion.

    Those tests read the same constants the code reads, which is what lets them
    survive a rename — and also means a mutation of the CONSTANT moves the test
    with it. So the literals live here, once. A wrong origin is the one that
    matters: it is where the server's own upstream credential is sent on every
    request.
    """
    from daintree_assistant_server.config import (
        EXECUTOR_CONTEXT_TOKENS,
        EXECUTOR_MODEL,
        EXECUTOR_REASONING_EFFORT,
    )

    assert EXECUTOR_KEY_ENV == "BASETEN_API_KEY"
    assert EXECUTOR_URL_ENV == "BASETEN_BASE_URL"
    assert EXECUTOR_ORIGIN == "inference.baseten.co"
    assert EXECUTOR_MODEL == "zai-org/GLM-5.3-Flash"
    # Pinned INDEPENDENTLY of anything that derives from it. The oversized-turn
    # tests size their payload FROM this constant, so they stay green if it
    # reverts — they would simply be testing a smaller window without saying so.
    # This literal is what notices. Read from the host: Baseten's `/v1/models`
    # publishes `context_length: 1048576` for these weights.
    assert EXECUTOR_CONTEXT_TOKENS == 1_048_576
    # The floor, and the most expensive constant in the file to get wrong: this
    # model has no "off", and an ABSENT `reasoning_effort` means `high` on every
    # turn and every utility task.
    assert EXECUTOR_REASONING_EFFORT == "low"


def test_production_rejects_a_valid_https_origin_that_is_not_the_serving_host() -> None:
    """Hardening must pin THE origin, not merely require https.

    A check that accepted any well-formed https URL would pass every case in the
    test above it — every rejection there is malformed in some other way (plain
    http, userinfo, odd port, lookalike suffix). This one is clean, valid, and
    simply somewhere else, which is what a misdirected key actually looks like.
    """
    s = _settings(
        **{EXECUTOR_URL_ENV: "https://api.openai.com/v1"},
        ENVIRONMENT="production",
        STATE_SIGNING_KEY_CURRENT="y" * 48,
        STATE_SALT="a-real-random-salt-value",
    )
    assert any(EXECUTOR_URL_ENV in p for p in s.weak_secrets())


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
    host, env = EXECUTOR_ORIGIN, EXECUTOR_URL_ENV
    for bad in (
        f"http://{host}/v1",
        f"https://{host}.evil.example/v1",
        f"https://{host}@evil.example/v1",
        f"https://user@{host}/v1",  # userinfo never belongs here
        f"https://{host}:444/v1",  # non-default port = different origin
        f"https://{host}:bogus/v1",  # malformed port fails readiness, not requests
    ):
        s = _settings(**{env: bad}, **base)
        assert any(env in p for p in s.weak_secrets()), bad
    for ok_url in (
        f"https://{host}/v1",
        f"https://{host.upper()}/v1",  # hostname compares case-insensitively
        f"https://{host}:443/v1",  # explicit default port
    ):
        ok = _settings(**{env: ok_url}, **base)
        assert ok.weak_secrets() == [], ok_url
    # Development stays free to point at proxies/mocks.
    dev = _settings(**{env: "http://127.0.0.1:9999/v1"})
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
    s = _settings(**{EXECUTOR_KEY_ENV: "sk-should-not-appear"})
    assert "sk-should-not-appear" not in repr(s)


def _open_request(header: str | None) -> Any:
    """A request object with just enough shape for `authenticate` under `open`."""
    raw = [] if header is None else [(b"authorization", header.encode("iso-8859-1"))]
    settings = Settings(
        STATE_SIGNING_KEY_CURRENT="x" * 32, BASETEN_API_KEY="sk-server-owned"
    )
    return SimpleNamespace(
        app=SimpleNamespace(state=SimpleNamespace(settings=settings, token_verifier=None)),
        scope={"headers": raw},
    )


@pytest.mark.parametrize(
    "header",
    [
        None,
        "",
        "   ",
        # A CLI mid-migration, still sending the OpenRouter key it used to
        # authenticate with. This is the case the ignore rule exists for: read it,
        # and it would be forwarded upstream and rejected as the CALLER's bad
        # key — an error blaming them for a header they were right to still send.
        "Bearer sk-or-v1-a-stale-openrouter-key",
        # Shapes that would be a 401 under enforcement. A malformed bearer only
        # means something while a bearer means something; rejecting one under the
        # open door would refuse a request the server can serve, over a value it
        # does not read.
        "Bearer",
        "Bearer  ",
        "Bearer bad key",
        "Bearer k\x7f",
        "Basic abc",
        "Token secret",
        "not-a-scheme-at-all",
    ],
)
@pytest.mark.asyncio
async def test_every_authorization_header_is_ignored_under_the_open_door(
    header: str | None,
) -> None:
    """Under `AUTH_MODE=open` the header is not parsed, not validated, and never
    rejected.

    This is stronger than "a credential is optional", and the difference is the
    whole point: the server holds the upstream credential, so there is no
    per-request credential for a bearer to supply and nothing for its shape to be
    wrong about.

    The mode is what carries that promise now, rather than the absence of any code
    to break it — which is why these cases are kept rather than deleted. They are
    the assertion that turning the machinery ON did not turn the default posture
    on with it.
    """
    principal = await authenticate(_open_request(header))
    assert not principal.is_authenticated
    assert principal.has_permission("chat")


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


def _enforced_production_settings(**overrides: object) -> Settings:
    return _settings(
        ENVIRONMENT="production",
        AUTH_MODE="enforce",
        SUPABASE_AUTH_ISSUER="https://test-project.supabase.co/auth/v1",
        SUPABASE_JWKS_URL="https://test-project.supabase.co/auth/v1/.well-known/jwks.json",
        SUPABASE_OAUTH_AUTHORIZE_URL="https://test-project.supabase.co/auth/v1/oauth/authorize",
        SUPABASE_OAUTH_TOKEN_URL="https://test-project.supabase.co/auth/v1/oauth/token",
        SUPABASE_OAUTH_CLIENT_ID="test-client",
        **overrides,
    )


def test_dump_dir_empty_string_is_not_a_problem_in_production() -> None:
    # An empty value means "unset" — it must not trip the production gate.
    s = _enforced_production_settings(DAINTREE_DUMP_UPSTREAM_DIR="")
    assert s.local_only_problems() == []


# ---- runbook overlay directory (local-development-only guard) ---------------


def test_runbooks_overlay_dir_unset_by_default() -> None:
    s = _settings()
    assert s.runbooks_overlay_dir is None
    assert s.local_only_problems() == []


def test_runbooks_overlay_dir_allowed_in_development() -> None:
    s = _settings(DAINTREE_RUNBOOKS_OVERLAY_DIR="/tmp/runbooks")
    assert s.runbooks_overlay_dir == "/tmp/runbooks"
    assert s.local_only_problems() == []


def test_runbooks_overlay_dir_fails_readiness_in_production() -> None:
    s = _settings(ENVIRONMENT="production", DAINTREE_RUNBOOKS_OVERLAY_DIR="/tmp/runbooks")
    assert any("DAINTREE_RUNBOOKS_OVERLAY_DIR" in p for p in s.local_only_problems())


def test_runbooks_overlay_dir_fails_readiness_in_staging() -> None:
    # Staging is hardened too: it is the rehearsal for production, so a catalog
    # it serves must be the one its image shipped.
    s = _settings(ENVIRONMENT="staging", DAINTREE_RUNBOOKS_OVERLAY_DIR="/tmp/runbooks")
    assert any("DAINTREE_RUNBOOKS_OVERLAY_DIR" in p for p in s.local_only_problems())


def test_runbooks_overlay_dir_empty_string_is_not_a_problem_in_production() -> None:
    # An empty value means "unset" — it must not trip the production gate.
    s = _enforced_production_settings(DAINTREE_RUNBOOKS_OVERLAY_DIR="")
    assert s.local_only_problems() == []


def test_both_local_only_features_are_reported_together() -> None:
    # The gate returns a LIST, and a deploy with two development flags left on
    # should learn about both from one readiness failure rather than one per fix.
    s = _enforced_production_settings(
        DAINTREE_DUMP_UPSTREAM_DIR="/tmp/dumps",
        DAINTREE_RUNBOOKS_OVERLAY_DIR="/tmp/runbooks",
    )
    problems = s.local_only_problems()
    assert len(problems) == 2
    assert any("DAINTREE_DUMP_UPSTREAM_DIR" in p for p in problems)
    assert any("DAINTREE_RUNBOOKS_OVERLAY_DIR" in p for p in problems)


def test_max_active_runbooks_rejects_a_cap_below_two() -> None:
    # Every runbook resolves to a domain foundation and the orchestrator admits a
    # specific plus its foundation as one atomic unit, so a budget of 1 fits no unit
    # at all and would silently load NOTHING. The floor makes that unsettable.
    with pytest.raises(ValidationError):
        _settings(MAX_ACTIVE_RUNBOOKS=1)


def test_max_active_runbooks_accepts_the_floor() -> None:
    assert _settings(MAX_ACTIVE_RUNBOOKS=2).max_active_runbooks == 2


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
    server unable or unfit to ANSWER — a missing upstream key, a malformed runbook.
    A misconfigured rollout changes how many tools a turn is offered, which the
    fail-open paths already bound; it cannot stop the turn being served, and a
    server refused readiness over it would be down for a reason it could have
    simply logged.
    """
    settings = _settings(TOOL_PROJECTION_MODE="enforced", TOOL_PROJECTION_ROLLOUT_PERCENT=100)
    assert settings.missing_secrets() == []
    assert settings.local_only_problems() == []


# ---- permissions -----------------------------------------------------------


@pytest.mark.asyncio
async def test_the_anonymous_principal_holds_every_permission() -> None:
    """Under the open door there is no identity to narrow authority by, so a
    narrower set would refuse requests this deployment is meant to serve — the
    mode would stop meaning "the header is not read" and start meaning "everyone
    is a restricted user", which is a different and much worse thing to ship by
    accident."""
    from daintree_assistant_server.auth import ANONYMOUS, ASSISTANT_PERMISSIONS

    assert ANONYMOUS.permissions == ASSISTANT_PERMISSIONS
    assert not ANONYMOUS.is_authenticated


@pytest.mark.asyncio
async def test_require_permission_refuses_a_caller_without_it() -> None:
    """403 `auth_permission_denied`, not a 401: the credential is fine and a
    refresh would mint another token with exactly the same authority.

    And not `auth_client_not_allowed` either, though both are 403s a refresh
    cannot fix. That one says the token came from an OAuth client this deployment
    does not accept — saying it about a client that IS accepted sends whoever
    reads the log hunting a configuration error that does not exist.
    """
    from daintree_assistant_server.auth import Principal, require_permission
    from daintree_assistant_server.errors import DaintreeError

    dependency = require_permission("chat")
    request = _open_request(None)
    request.app.state.settings = Settings(
        STATE_SIGNING_KEY_CURRENT="x" * 32, BASETEN_API_KEY="k"
    )

    # Stand in a principal that holds nothing, the way a future second OAuth
    # client with a narrower grant would.
    import daintree_assistant_server.auth as auth_module

    original = auth_module.ANONYMOUS
    auth_module.ANONYMOUS = Principal(permissions=frozenset())
    try:
        with pytest.raises(DaintreeError) as exc:
            await dependency(request)
    finally:
        auth_module.ANONYMOUS = original
    assert exc.value.status_code == 403
    assert exc.value.code == "auth_permission_denied"


def test_runbooks_write_is_not_granted_to_anybody() -> None:
    """The reload route it would gate is local-authoring only and is already
    refused twice over in a hardened deployment. A permission for it would be the
    first step toward shipping the route."""
    from daintree_assistant_server.auth import ASSISTANT_PERMISSIONS

    assert not any(p.endswith(":write") for p in ASSISTANT_PERMISSIONS)


# ---- the verifier's own lifecycle ------------------------------------------


@pytest.mark.asyncio
async def test_no_verifier_is_built_for_the_open_door() -> None:
    """A deployment not doing account authentication should not hold an open
    connection pool for a provider it never calls."""
    from daintree_assistant_server.main import _build_token_verifier

    settings = Settings(STATE_SIGNING_KEY_CURRENT="x" * 32, BASETEN_API_KEY="k")
    verifier, client = await _build_token_verifier(settings)
    assert verifier is None and client is None


@pytest.mark.asyncio
async def test_a_configured_non_open_deployment_builds_one() -> None:
    from daintree_assistant_server.main import _build_token_verifier

    settings = Settings(
        STATE_SIGNING_KEY_CURRENT="x" * 32,
        BASETEN_API_KEY="k",
        AUTH_MODE="observe",
        SUPABASE_AUTH_ISSUER="https://abcdefghij.supabase.co/auth/v1",
        SUPABASE_JWKS_URL="https://abcdefghij.supabase.co/auth/v1/.well-known/jwks.json",
        SUPABASE_OAUTH_AUTHORIZE_URL="https://abcdefghij.supabase.co/auth/v1/oauth/authorize",
        SUPABASE_OAUTH_TOKEN_URL="https://abcdefghij.supabase.co/auth/v1/oauth/token",
        SUPABASE_OAUTH_CLIENT_ID="daintree-assistant",
        DAINTREE_ACCOUNT_URL="https://staging.daintree.org/account",
        DAINTREE_SUBSCRIBE_URL="https://staging.daintree.org/subscribe",
    )
    verifier, client = await _build_token_verifier(settings)
    assert verifier is not None and client is not None
    try:
        # The redirect refusal is set on the client as well as at the call site.
        assert client.follow_redirects is False
    finally:
        await client.aclose()


@pytest.mark.asyncio
async def test_a_failed_verifier_build_does_not_strand_its_connection_pool() -> None:
    """The client is created before the verifier, and nothing else holds a
    reference to it yet — so an exception from the constructor would leave an open
    pool for the life of the process, owned by nobody."""
    import daintree_assistant_server.main as main_module

    settings = Settings(
        STATE_SIGNING_KEY_CURRENT="x" * 32,
        BASETEN_API_KEY="k",
        AUTH_MODE="observe",
        SUPABASE_AUTH_ISSUER="https://abcdefghij.supabase.co/auth/v1",
        SUPABASE_JWKS_URL="https://abcdefghij.supabase.co/auth/v1/.well-known/jwks.json",
        SUPABASE_OAUTH_AUTHORIZE_URL="https://abcdefghij.supabase.co/auth/v1/oauth/authorize",
        SUPABASE_OAUTH_TOKEN_URL="https://abcdefghij.supabase.co/auth/v1/oauth/token",
        SUPABASE_OAUTH_CLIENT_ID="daintree-assistant",
        DAINTREE_ACCOUNT_URL="https://staging.daintree.org/account",
        DAINTREE_SUBSCRIBE_URL="https://staging.daintree.org/subscribe",
    )
    created: list[Any] = []
    real_client = main_module.httpx.AsyncClient

    def _record(*args: Any, **kwargs: Any) -> Any:
        client = real_client(*args, **kwargs)
        created.append(client)
        return client

    def _explode(**kwargs: Any) -> Any:
        raise RuntimeError("constructor failed")

    main_module.httpx.AsyncClient = _record  # type: ignore[misc]
    original_verifier = main_module.SupabaseTokenVerifier
    main_module.SupabaseTokenVerifier = _explode  # type: ignore[misc]
    try:
        with pytest.raises(RuntimeError):
            await main_module._build_token_verifier(settings)
    finally:
        main_module.httpx.AsyncClient = real_client  # type: ignore[misc]
        main_module.SupabaseTokenVerifier = original_verifier  # type: ignore[misc]
    assert created, "no client was created, so this proves nothing"
    assert created[0].is_closed


@pytest.mark.asyncio
async def test_a_failed_verifier_build_does_not_take_liveness_down() -> None:
    """It runs before the `yield`, so an escape would fail the whole lifespan —
    turning a configuration problem into a dead process, which is the opposite of
    what every other startup failure in `main.py` does.

    Degrading to no verifier is safe: under `enforce` every request then answers
    `auth_dependency_unavailable`, which is the honest verdict for a door that
    could not be built.
    """
    from fastapi.testclient import TestClient

    import daintree_assistant_server.main as main_module
    from tests.fakes import FakeProvider  # type: ignore[import-not-found]

    async def _explode(settings: Any) -> Any:
        raise RuntimeError("cannot build a verifier")

    original = main_module._build_token_verifier
    main_module._build_token_verifier = _explode  # type: ignore[assignment]
    try:
        app = main_module.create_app(
            settings=Settings(
                STATE_SIGNING_KEY_CURRENT="x" * 32,
                BASETEN_API_KEY="k",
                RETRY_BACKOFF_BASE_SECONDS=0.0,
            ),
            provider=FakeProvider(),
        )
        with TestClient(app) as client:
            # Liveness is up, which is the whole point.
            assert client.get("/healthz").status_code == 200
            assert app.state.token_verifier is None
    finally:
        main_module._build_token_verifier = original  # type: ignore[assignment]
