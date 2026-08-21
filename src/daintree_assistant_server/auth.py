"""Bearer authentication — currently open, deliberately and temporarily.

**The door is open.** The server now holds its own upstream credential
(``CEREBRAS_API_KEY``) and funds every turn from it, so a request no longer needs
to carry a key in order to have one to spend. With nothing to authenticate
against — no shared server secret, no account store yet — requiring a bearer
token would reject callers on a check that could not tell a real credential from
any other well-formed string. So this module accepts a request with no
``Authorization`` header at all.

That is a **stage, not a destination**. Account authentication and sign-in are
being built next, and this is the seam they land in: ``authenticate`` is the one
place a request becomes a :class:`Principal`, and every route already depends on
it through ``require_auth``. When accounts arrive, the anonymous branch below
becomes a rejection and the rest of the server does not move.

Two things are kept alive in the meantime rather than deleted, because they are
what the next step needs:

* :class:`Principal` still carries an ``api_key``, now ``None`` on every real
  request. It is the field an account-mapped credential will occupy, and the
  provider already prefers a request-supplied key over the server's default
  (``CerebrasProvider._resolve_key``), so per-caller credentials need no new
  plumbing — only a value here.
* A bearer token that IS present is still structurally validated and rejected
  when malformed. A caller who sends garbage should hear about it at the door
  rather than have it silently ignored.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from fastapi import Request
from pydantic import SecretStr

from .errors import invalid_api_key


@dataclass(frozen=True)
class Principal:
    """An authenticated caller.

    ``api_key`` is the caller's own upstream credential when they have one, and
    ``None`` otherwise — which, until account auth lands, is every request. When
    it is set it is threaded explicitly to every provider call the request makes
    and overrides the server's own key. It is credential material: repr is
    suppressed and downstream code must never log or echo it.
    """

    id: str
    scopes: frozenset[str]
    api_key: SecretStr | None = field(default=None, repr=False)

    def has_scope(self, scope: str) -> bool:
        return scope in self.scopes


CALLER_SCOPES = frozenset({"chat", "tasks", "skills:read", "skills:write"})

# Generous ceiling on the bearer token. Real keys are well under 200 bytes; the
# cap only stops an absurd header from being scanned and copied into a SecretStr.
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

    The same shape ``CerebrasProvider._resolve_key`` enforces before building an
    Authorization header, applied at our door so garbage 401s here instead of
    surfacing as an upstream 502. Deliberately NO prefix check: the key's issuer
    is not our business, only that it can ride an HTTP header safely — plus a
    generous length cap (``MAX_TOKEN_LENGTH``) so absurd input is refused rather
    than forwarded.
    """
    if len(token) > MAX_TOKEN_LENGTH:
        return False
    return all(0x21 <= ord(ch) <= 0x7E for ch in token)


def authenticate(authorization: str | None) -> Principal:
    """Turn the bearer header into a principal.

    Three outcomes, and the middle one is the current normal case:

    * **A well-formed token** — carried on the principal and used as this
      request's upstream credential, ahead of the server's own.
    * **No header at all** — an anonymous principal with full caller scopes. The
      server's key funds the turn. This is what the CLI sends today.
    * **A header that is present but unusable** (not ``Bearer <token>``, or a
      token with characters no HTTP header may carry) — 401. Silently ignoring it
      would strand a caller who *meant* to authenticate: they would see their key
      apparently accepted while every turn ran on the server's account instead.

    Note the asymmetry between the last two. Absence is a valid choice; a
    malformed presence is a mistake, and saying so is the whole value of still
    checking.
    """
    if authorization is None or not authorization.strip():
        return Principal(id="anonymous", scopes=CALLER_SCOPES, api_key=None)
    token = _extract_bearer(authorization)
    if not token or not _structurally_valid(token):
        raise invalid_api_key()
    return Principal(id="caller", scopes=CALLER_SCOPES, api_key=SecretStr(token))


def require_auth(request: Request) -> Principal:
    """FastAPI dependency: authenticate the request's bearer header."""
    return authenticate(request.headers.get("authorization"))
