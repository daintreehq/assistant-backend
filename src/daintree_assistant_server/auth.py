"""Bearer authentication.

Every request carries the caller's own API key as the bearer token; that key is
also the upstream credential for the turn's provider calls (for early testers it
is literally their OpenRouter key — later a subscription key the backend maps to
a credential server-side, with no wire change). Authentication is therefore
credential *extraction* plus structural validation, identical in every
environment: there is no shared server secret to compare against and no
unauthenticated development mode. A missing or malformed key 401s here
(``invalid_api_key``); a well-formed key that the upstream then rejects surfaces
as a 502 ``upstream_error`` — two distinct codes so the CLI can tell "fix your
header" from "fix your account".
"""

from __future__ import annotations

from dataclasses import dataclass, field

from fastapi import Request
from pydantic import SecretStr

from .errors import invalid_api_key


@dataclass(frozen=True)
class Principal:
    """An authenticated caller.

    ``api_key`` is the caller's own upstream credential, threaded explicitly to
    every provider call the request makes. It is credential material: repr is
    suppressed and downstream code must never log or echo it.
    """

    id: str
    scopes: frozenset[str]
    api_key: SecretStr = field(repr=False)

    def has_scope(self, scope: str) -> bool:
        return scope in self.scopes


CALLER_SCOPES = frozenset({"chat", "tasks", "skills:read", "skills:write"})

# Generous ceiling on the bearer token. Real keys are well under 200 bytes
# (OpenRouter today, subscription keys later); the cap only stops an absurd
# header from being scanned, copied into a SecretStr, and forwarded upstream.
MAX_TOKEN_LENGTH = 4096


def _extract_bearer(authorization: str | None) -> str | None:
    if not authorization:
        return None
    # Bound the RAW header before any scanning/stripping, so a whitespace-padded
    # monster can't hide a short token behind an expensive normalization pass.
    if len(authorization) > MAX_TOKEN_LENGTH + len("Bearer "):
        return None
    parts = authorization.split(" ", 1)
    if len(parts) != 2 or parts[0].lower() != "bearer":
        return None
    token = parts[1].strip()
    return token or None


def _structurally_valid(token: str) -> bool:
    """Printable non-space ASCII only (0x21–0x7E).

    The same shape ``OpenRouterProvider._resolve_key`` enforces before building
    an Authorization header, applied at our door so garbage 401s here instead of
    surfacing as an upstream 502. Deliberately NO prefix check: the key's issuer
    is not our business (today an OpenRouter key, later a subscription key),
    only that it can ride an HTTP header safely — plus a generous length cap
    (``MAX_TOKEN_LENGTH``) so absurd input is refused rather than forwarded.
    """
    if len(token) > MAX_TOKEN_LENGTH:
        return False
    return all(0x21 <= ord(ch) <= 0x7E for ch in token)


def authenticate(authorization: str | None) -> Principal:
    """Validate the bearer header and return the principal, or raise 401.

    No environment ever allows unauthenticated access: without a caller key
    there is no upstream credential to run the turn with, so the old
    development shortcut is gone rather than gated.
    """
    token = _extract_bearer(authorization)
    if not token or not _structurally_valid(token):
        raise invalid_api_key()
    return Principal(id="caller", scopes=CALLER_SCOPES, api_key=SecretStr(token))


def require_auth(request: Request) -> Principal:
    """FastAPI dependency: authenticate the request's bearer header."""
    return authenticate(request.headers.get("authorization"))
