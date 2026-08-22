"""Request identity — currently open, deliberately and temporarily.

**The `Authorization` header is ignored entirely.** Not parsed, not validated,
not rejected. Every request becomes the same anonymous principal, and the turn is
funded by the server's own upstream credential (``config.SERVING_KEY_ENV``).

That is stronger than "a key is optional", and the difference is the whole point
of this module. A CLI mid-migration may still be sending the OpenRouter key it
used to authenticate with. If this door read that header, the key would be
carried on the `Principal`, threaded to every provider call, and forwarded to
Together — which would reject a credential it never issued, and the caller would
see ``provider_invalid_api_key``: an error blaming their key for a header they
were right to still be sending. Refusing to look at it is what makes an old
client work rather than fail confusingly.

The same reasoning retires the structural check that used to live here. Rejecting
a malformed bearer only made sense while a bearer *meant* something — it warned a
caller that the credential they meant to use was not going to be used. Now no
credential is ever used, so a 401 on a stray header would reject a request the
server can serve perfectly well, for a value it does not read.

**This is a stage, not a destination.** Account authentication and sign-in are
next, and this is the seam they land in: ``authenticate`` is the one place a
request becomes a :class:`Principal`, and every route already depends on it
through ``require_auth``. When accounts arrive, this function starts reading the
header again — validating it against an account store rather than against its own
shape — and the rest of the server does not move. See ``TODO.md``.
"""

from __future__ import annotations

from dataclasses import dataclass

from fastapi import Request


@dataclass(frozen=True)
class Principal:
    """An authenticated caller.

    Carries no credential. The upstream key is the server's own, held on
    ``Settings`` and resolved inside the provider
    (``OpenRouterProvider._resolve_key``) — it never travels on a per-request
    object, because there is no per-request credential to travel.

    ``ProviderChatRequest.api_key`` still exists and still overrides the
    server's default, but nothing on the serving path sets it: benchmarks and
    live tests construct providers directly and name the credential they mean to
    spend. That is the field an account-mapped credential will occupy, and the
    reason it is kept live and tested rather than deleted.
    """

    id: str
    scopes: frozenset[str]

    def has_scope(self, scope: str) -> bool:
        return scope in self.scopes


CALLER_SCOPES = frozenset({"chat", "tasks", "skills:read", "skills:write"})


def authenticate(authorization: str | None) -> Principal:
    """Return the anonymous principal, whatever the caller sent.

    ``authorization`` is accepted and ignored. The parameter stays in the
    signature because it is what account authentication will read, and because a
    function called ``authenticate`` that took no input would be a confusing
    place to put that back.
    """
    return Principal(id="anonymous", scopes=CALLER_SCOPES)


def require_auth(request: Request) -> Principal:
    """FastAPI dependency: the caller's principal for this request."""
    return authenticate(request.headers.get("authorization"))
