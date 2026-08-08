"""Settings + live-provider bootstrap, shared by every suite that talks to a model.

Reads the repo-root ``.env`` (the same file production uses) unless an explicit
path is given, so a live run uses the real ``OPENROUTER_API_KEY``. Deterministic
suites call ``build_settings`` too (they need caps, model ids, and the state
keys) but never construct a provider.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import httpx

from daintree_assistant_server.config import Settings
from daintree_assistant_server.providers.base import ProviderChatRequest
from daintree_assistant_server.providers.openrouter import OpenRouterProvider

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


def require_api_key(settings: Settings) -> str:
    key = settings.openrouter_api_key.get_secret_value()
    if not key:
        raise EnvError(
            "OPENROUTER_API_KEY is not set — export it or point --env-file at a .env "
            "that has it (live suites make real, paid calls)."
        )
    return key


def build_provider_body(
    request: ProviderChatRequest, settings: Settings, *, stream: bool = False
) -> dict[str, Any]:
    """The EXACT upstream body ``OpenRouterProvider`` would POST — built through the
    real provider (no network, no key needed) so the deterministic cache metric is
    measured over production's wire bytes, not a hand-rebuilt dict."""
    provider = OpenRouterProvider(
        None,  # type: ignore[arg-type]  # _build_body never touches the client
        api_key="",
        base_url=settings.openrouter_base_url,
    )
    return provider._build_body(request, stream=stream)


def make_upstream_provider(
    settings: Settings, *, timeout: float | None = None
) -> OpenRouterProvider:
    """A real OpenRouter provider over a fresh httpx client — identical wiring to
    the production ``Container``/existing benches (never a bespoke client)."""
    key = require_api_key(settings)
    t = timeout or settings.utility_timeout_seconds
    client = httpx.AsyncClient(
        timeout=httpx.Timeout(t, connect=settings.upstream_connect_timeout_seconds),
        limits=httpx.Limits(max_connections=100, max_keepalive_connections=20),
    )
    return OpenRouterProvider(
        client,
        api_key=key,
        base_url=settings.openrouter_base_url,
        default_timeout=t,
    )
