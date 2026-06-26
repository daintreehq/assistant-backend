"""Bearer authentication.

Version 1 uses one fixed development key, compared in constant time. The
:class:`Principal` abstraction means downstream code never sees the raw key and
the fixed-key shortcut does not leak into service logic — later migration to
multiple hashed keys only changes this module.
"""

from __future__ import annotations

import hmac
from dataclasses import dataclass

from fastapi import Request

from .config import Settings
from .errors import invalid_api_key


@dataclass(frozen=True)
class Principal:
    """An authenticated caller."""

    id: str
    scopes: frozenset[str]

    def has_scope(self, scope: str) -> bool:
        return scope in self.scopes


DEVELOPMENT_PRINCIPAL = Principal(
    id="development",
    scopes=frozenset({"chat", "tasks", "skills:read", "skills:write"}),
)


def _extract_bearer(authorization: str | None) -> str | None:
    if not authorization:
        return None
    parts = authorization.split(" ", 1)
    if len(parts) != 2 or parts[0].lower() != "bearer":
        return None
    token = parts[1].strip()
    return token or None


def authenticate(authorization: str | None, settings: Settings) -> Principal:
    """Validate the bearer header and return the principal, or raise 401."""
    token = _extract_bearer(authorization)
    expected = settings.daintree_api_key.get_secret_value()
    if not token or not expected:
        raise invalid_api_key()
    # Constant-time comparison; encode to bytes to avoid unicode timing quirks.
    if not hmac.compare_digest(token.encode("utf-8"), expected.encode("utf-8")):
        raise invalid_api_key()
    return DEVELOPMENT_PRINCIPAL


def require_auth(request: Request) -> Principal:
    """FastAPI dependency: authenticate using app settings."""
    settings: Settings = request.app.state.settings
    return authenticate(request.headers.get("authorization"), settings)
