"""Settings + live-provider bootstrap, shared by every suite that talks to a model.

Reads the repo-root ``.env`` (the same file production uses) unless an explicit
path is given, so a live run uses the real ``OPENAI_API_KEY``. Deterministic
suites call ``build_settings`` too (they need caps, model ids, and the state
keys) but never construct a provider.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import httpx
from pydantic import SecretStr

from daintree_assistant_server.config import EXECUTOR_KEY_ENV, EXECUTOR_REASONING_EFFORT, Settings
from daintree_assistant_server.providers.base import ChatProvider, ProviderChatRequest
from daintree_assistant_server.providers.openai import OpenAIProvider

#: The adapter production builds. Benchmarks MUST build the same one: a suite
#: that measured wire bytes or latency through a different adapter is reporting
#: a number about nothing, and the failure is silent because every adapter here
#: answers the same calls.
_SERVING_PROVIDER_CLASS = OpenAIProvider

ROOT = Path(__file__).resolve().parents[2]

# CI has no ``.env`` and no state-key secrets, but the deterministic suites that go
# through ``build_container`` (microperf, session_trace) need a signing key or
# ``StateCodec`` raises "current signing key must be non-empty" and the whole
# dashboard step fails. The suites that hardcode their own bench key (prompt_cost,
# safety) were passing for exactly this reason. Token SHAPE is what those suites
# measure, never token secrecy, so a fixed literal is correct here — it also keeps
# runs reproducible. Never a fallback in production config: ``Settings`` still
# defaults this to empty, and a missing key still fails ``/readyz``.
_BENCH_STATE_SIGNING_KEY = "benchmark-state-signing-key-not-a-secret"


class EnvError(Exception):
    """A benchmark environment problem (missing key, unreadable .env)."""


def build_settings(
    env_file: str | None = None, **overrides: Any
) -> Settings:
    """Load immutable settings from ``.env`` (repo root by default).

    Falls back to a non-secret bench signing key when the environment supplies
    none, so a fresh clone or CI runner can run the deterministic suites without
    a ``.env``. A real key from the environment or an explicit override wins.
    """
    if env_file is None:
        default = ROOT / ".env"
        env_file = str(default) if default.is_file() else None
    settings = Settings(_env_file=env_file, **overrides)
    if not settings.state_signing_key_current.get_secret_value():
        settings = Settings(
            _env_file=env_file,
            **{"STATE_SIGNING_KEY_CURRENT": _BENCH_STATE_SIGNING_KEY, **overrides},
        )
    return settings


# Service entry points (orchestrator.complete, task_runner.run, selector.select)
# take an optional per-call caller key. The serving path passes None and lets the
# provider spend the server's own; fake-provider suites pass this fixed sentinel
# (never sent anywhere) and live suites pass the real key via ``caller_api_key``,
# so a bench always names the credential it means to spend rather than inheriting
# one.
BENCH_CALLER_KEY = SecretStr("benchmark-caller-key-not-a-secret")


def caller_api_key(settings: Settings) -> SecretStr:
    """The per-call credential for service-level benchmark calls.

    The real SERVING credential when the environment has one (live suites), else
    the bench sentinel (deterministic suites, whose fake providers never build a
    header from it).
    """
    if settings.serving_api_key.get_secret_value():
        return settings.serving_api_key
    return BENCH_CALLER_KEY


def require_api_key(settings: Settings) -> SecretStr:
    """Validate the key is present and return it still wrapped.

    Returned as a SecretStr so the plaintext is never carried around by a caller;
    the provider unwraps it only to build a header or to scrub an echoed key out of
    upstream error text.
    """
    if not settings.serving_api_key.get_secret_value():
        raise EnvError(
            f"{EXECUTOR_KEY_ENV} is not set — export it or point --env-file at a .env "
            "that has it (live suites make real calls)."
        )
    return settings.serving_api_key


def build_provider_body(
    request: ProviderChatRequest, settings: Settings, *, stream: bool = False
) -> dict[str, Any]:
    """The EXACT upstream body the SERVING adapter would POST — built through the
    real provider (no network, no key needed) so the deterministic cache metric is
    measured over production's wire bytes, not a hand-rebuilt dict."""
    provider = _SERVING_PROVIDER_CLASS(
        None,  # type: ignore[arg-type]  # _build_body never touches the client
        api_key=SecretStr(""),  # no network: _build_body never builds a header
        base_url=settings.serving_base_url,
        # Passed EXPLICITLY, exactly as `main._build_provider` does. The adapter's
        # own default happens to match today, so omitting it would measure the
        # right bytes by coincidence and go on doing so until the two diverged —
        # at which point the benchmark would silently be pricing a body production
        # does not send.
        reasoning_effort=EXECUTOR_REASONING_EFFORT,
    )
    return provider._build_body(request, stream=stream)


def make_upstream_provider(
    settings: Settings, *, timeout: float | None = None
) -> ChatProvider:
    """A real SERVING provider over a fresh httpx client — identical wiring to
    the production ``Container``/existing benches (never a bespoke client)."""
    key = require_api_key(settings)
    t = timeout or settings.utility_timeout_seconds
    client = httpx.AsyncClient(
        timeout=httpx.Timeout(t, connect=settings.upstream_connect_timeout_seconds),
        limits=httpx.Limits(max_connections=100, max_keepalive_connections=20),
    )
    return _SERVING_PROVIDER_CLASS(
        client,
        api_key=key,
        base_url=settings.serving_base_url,
        reasoning_effort=EXECUTOR_REASONING_EFFORT,
        default_timeout=t,
    )
