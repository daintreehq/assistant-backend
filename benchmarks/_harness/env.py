"""Settings + live-provider bootstrap, shared by every suite that talks to a model.

Reads the repo-root ``.env`` (the same file production uses) unless an explicit
path is given, so a live run uses the real ``DEEPSEEK_API_KEY``. Deterministic
suites call ``build_settings`` too (they need caps, model ids, and the state
keys) but never construct a provider.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import httpx

from daintree_assistant_server.config import Settings
from daintree_assistant_server.providers.base import ProviderChatRequest
from daintree_assistant_server.providers.deepseek import DeepSeekProvider

ROOT = Path(__file__).resolve().parents[2]


class EnvError(Exception):
    """A benchmark environment problem (missing key, unreadable .env)."""


def build_settings(
    env_file: str | None = None, **overrides: Any
) -> Settings:
    """Load immutable settings from ``.env`` (repo root by default)."""
    if env_file is None:
        default = ROOT / ".env"
        env_file = str(default) if default.is_file() else None
    return Settings(_env_file=env_file, **overrides)


def require_deepseek_key(settings: Settings) -> str:
    key = settings.deepseek_api_key.get_secret_value()
    if not key:
        raise EnvError(
            "DEEPSEEK_API_KEY is not set — export it or point --env-file at a .env "
            "that has it (live suites make real, paid calls)."
        )
    return key


def has_openai_key(settings: Settings) -> bool:
    getter = getattr(settings, "openai_api_key", None)
    if getter is None:
        return False
    try:
        return bool(getter.get_secret_value())
    except AttributeError:
        return bool(getter)


def build_provider_body(
    request: ProviderChatRequest, settings: Settings, *, stream: bool = False
) -> dict[str, Any]:
    """The EXACT upstream body ``DeepSeekProvider`` would POST — built through the
    real provider (no network, no key needed) so the deterministic cache metric is
    measured over production's wire bytes, not a hand-rebuilt dict."""
    provider = DeepSeekProvider(
        None,  # type: ignore[arg-type]  # _build_body never touches the client
        api_key="",
        base_url=settings.deepseek_base_url,
        send_prompt_cache_key=settings.deepseek_send_prompt_cache_key,
    )
    return provider._build_body(request, stream=stream)


def make_deepseek_provider(
    settings: Settings, *, timeout: float | None = None
) -> DeepSeekProvider:
    """A real DeepSeek provider over a fresh httpx client — identical wiring to
    the production ``Container``/existing benches (never a bespoke client)."""
    key = require_deepseek_key(settings)
    t = timeout or settings.utility_timeout_seconds
    client = httpx.AsyncClient(
        timeout=httpx.Timeout(t, connect=settings.upstream_connect_timeout_seconds),
        limits=httpx.Limits(max_connections=100, max_keepalive_connections=20),
    )
    return DeepSeekProvider(
        client,
        api_key=key,
        base_url=settings.deepseek_base_url,
        default_timeout=t,
        send_prompt_cache_key=settings.deepseek_send_prompt_cache_key,
    )
