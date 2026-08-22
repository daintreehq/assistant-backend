"""Settings + live-provider bootstrap, shared by every suite that talks to a model.

Reads the repo-root ``.env`` (the same file production uses) unless an explicit
path is given, so a live run uses the real ``CEREBRAS_API_KEY``. Deterministic
suites call ``build_settings`` too (they need caps, model ids, and the state
keys) but never construct a provider.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import httpx
from pydantic import SecretStr

from daintree_assistant_server.config import Settings
from daintree_assistant_server.modes import DEFAULT_MODE, MODE_SPECS, ModeSpec, OperationMode
from daintree_assistant_server.providers.base import ChatProvider, ProviderChatRequest
from daintree_assistant_server.providers.cerebras import CerebrasProvider
from daintree_assistant_server.providers.together import TogetherProvider

#: Which adapter class serves a provider id. Benchmarks MUST build the same one
#: production does: a suite that measured wire bytes or latency through a host
#: the server does not use is reporting a number about nothing, and the failure
#: is silent because both adapters answer the same calls.
_ADAPTERS = {"together": TogetherProvider, "cerebras": CerebrasProvider}


def bench_mode(mode: str | OperationMode | None = None) -> ModeSpec:
    """The route a benchmark measures.

    Defaults to Standard rather than "whatever is configured", because a
    benchmark's job is to produce a comparable number: a suite whose route
    followed a mutable setting would report two incomparable series under one
    name. A suite measuring Pro passes it explicitly (``--mode pro``), and its
    artifact should stamp the mode it ran.
    """
    if mode is None:
        return MODE_SPECS[DEFAULT_MODE]
    return MODE_SPECS[OperationMode(mode)]

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


def caller_api_key(settings: Settings, spec: ModeSpec | None = None) -> SecretStr:
    spec = spec or bench_mode()
    """The per-call credential for service-level benchmark calls.

    The real credential for the measured MODE when the environment has one (live
    suites), else the bench sentinel (deterministic suites, whose fake providers
    never build a header from it). Resolved from the mode rather than named as a
    vendor: a benchmark holding the wrong host's key does not fail, it silently
    measures a model that route does not serve.
    """
    key = settings.provider_api_key(spec.provider_id)
    if key.get_secret_value():
        return key
    return BENCH_CALLER_KEY


def require_api_key(settings: Settings, spec: ModeSpec | None = None) -> SecretStr:
    """Validate the measured mode's key is present and return it still wrapped.

    Returned as a SecretStr so the plaintext is never carried around by a caller;
    the provider unwraps it only to build a header or to scrub an echoed key out of
    upstream error text.
    """
    spec = spec or bench_mode()
    key = settings.provider_api_key(spec.provider_id)
    if not key.get_secret_value():
        raise EnvError(
            f"{spec.key_env} is not set — export it or point --env-file at a .env "
            f"that has it (live suites make real calls; {spec.mode.value} mode "
            f"routes to {spec.provider_id})."
        )
    return key


def build_provider_body(
    request: ProviderChatRequest,
    settings: Settings,
    *,
    stream: bool = False,
    spec: ModeSpec | None = None,
) -> dict[str, Any]:
    """The EXACT upstream body the measured mode's adapter would POST — built
    through the real provider (no network, no key needed) so the deterministic
    cache metric is measured over production's wire bytes, not a hand-rebuilt
    dict. The two adapters do NOT produce the same body, so the mode matters
    here: Cerebras sends `max_completion_tokens` and no `reasoning` object."""
    spec = spec or bench_mode()
    provider = _ADAPTERS[spec.provider_id](
        None,  # type: ignore[arg-type]  # _build_body never touches the client
        api_key=SecretStr(""),  # no network: _build_body never builds a header
        base_url=settings.provider_base_url(spec.provider_id),
    )
    return provider._build_body(request, stream=stream)


def make_upstream_provider(
    settings: Settings, *, timeout: float | None = None, spec: ModeSpec | None = None
) -> ChatProvider:
    """A real provider for the measured mode over a fresh httpx client —
    identical wiring to the production ``Container``/existing benches (never a
    bespoke client)."""
    spec = spec or bench_mode()
    key = require_api_key(settings, spec)
    t = timeout or settings.utility_timeout_seconds
    client = httpx.AsyncClient(
        timeout=httpx.Timeout(t, connect=settings.upstream_connect_timeout_seconds),
        limits=httpx.Limits(max_connections=100, max_keepalive_connections=20),
    )
    return _ADAPTERS[spec.provider_id](
        client,
        api_key=key,
        base_url=settings.provider_base_url(spec.provider_id),
        default_timeout=t,
    )
