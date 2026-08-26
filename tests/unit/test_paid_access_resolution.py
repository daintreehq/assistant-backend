"""The entitlement resolver's wiring states, which are not answers about a caller.

`_resolve` returns `None` for every reason this deployment cannot obtain a
verdict — no service configured, an anonymous principal, or no bearer to forward.
None of those is a statement that the account is unentitled, and the distinction
is the whole reason the helper returns an optional rather than raising: a real
refusal arrives as its own typed error, and a wiring state must never be able to
present as one.

The bearer case is defensive rather than reachable today: the identity dependency
parses the same header through the same function, so a principal that reached
here authenticated normally implies a token. "Normally" is doing work in that
sentence — `parse_bearer` refuses an AMBIGUOUS header, so the invariant rests on
two independent reads of the raw scope agreeing — and this module carries a
coverage floor precisely because an untested branch on it is a security hole
rather than a style gap.
"""

from __future__ import annotations

from typing import Any
from uuid import uuid4

import pytest
from starlette.requests import Request

from daintree_assistant_server.api.paid_access import _resolve
from daintree_assistant_server.auth import Principal


def _request(headers: list[tuple[bytes, bytes]]) -> Request:
    """A request carrying nothing but the headers under test.

    `_resolve` reads the RAW scope rather than the parsed header mapping, because
    two `Authorization` headers must not be able to forward a different
    credential than the one identity verified.
    """
    return Request(
        {
            "type": "http",
            "http_version": "1.1",
            "method": "POST",
            "path": "/v1/daintree/respond",
            "raw_path": b"/v1/daintree/respond",
            "query_string": b"",
            "root_path": "",
            "scheme": "https",
            "headers": headers,
            "client": ("127.0.0.1", 1234),
            "server": ("testserver", 443),
        }
    )


def _authenticated() -> Principal:
    return Principal(
        permissions=frozenset({"assistant.respond"}),
        user_id=uuid4(),
        session_id=uuid4(),
        oauth_client_id="daintree-assistant",
    )


class _NeverAsked:
    """An entitlement service that fails the test if it is consulted.

    The property is not merely "returns None" — it is that no lookup is made.
    Forwarding an empty or guessed credential to the website would be the failure
    worth catching, and a service returning a canned verdict would hide it.
    """

    def __init__(self) -> None:
        self.calls = 0

    async def resolve(self, **kwargs: Any) -> Any:
        self.calls += 1
        raise AssertionError("the resolver asked the website with no bearer to send")


@pytest.mark.asyncio
async def test_an_authenticated_principal_with_no_bearer_yields_no_verdict() -> None:
    """Identity said yes and the header cannot be re-read as a credential.

    There is nothing to forward, and the website lookup authenticates with the
    CALLER's own token — so the only honest outcome is "no verdict obtained",
    which the caller treats as a wiring state. Asking with no credential would
    get a 401 back and this server maps a 401 from that endpoint to
    `auth_session_revoked`, so a missing bearer would present as a revoked
    session and tell a signed-in user to sign in again.
    """
    service = _NeverAsked()
    verdict = await _resolve(_request([]), _authenticated(), service, "req-1")
    assert verdict is None
    assert service.calls == 0


@pytest.mark.asyncio
async def test_an_ambiguous_authorization_header_yields_no_verdict() -> None:
    """Two `Authorization` headers is the shape that makes the branch above
    reachable at all.

    `parse_bearer` refuses rather than picking one, because picking one means
    forwarding a credential that may not be the one identity verified. Identity
    reads the same scope through the same function and would refuse too — so this
    is the two reads agreeing, asserted rather than assumed.
    """
    service = _NeverAsked()
    request = _request(
        [(b"authorization", b"Bearer one"), (b"authorization", b"Bearer two")]
    )
    assert await _resolve(request, _authenticated(), service, "req-2") is None
    assert service.calls == 0


@pytest.mark.asyncio
async def test_an_anonymous_principal_is_never_asked_about() -> None:
    """The open door produces a principal with no `user_id`, and there is no
    account to ask a billing question about."""
    service = _NeverAsked()
    anonymous = Principal(permissions=frozenset())
    request = _request([(b"authorization", b"Bearer something")])
    assert await _resolve(request, anonymous, service, "req-3") is None
    assert service.calls == 0


@pytest.mark.asyncio
async def test_no_configured_service_yields_no_verdict() -> None:
    """A deployment with no entitlement URL has nowhere to ask. Distinct from a
    refusal in every way that matters, and returned as the same `None` because
    both mean "this turn was not gated", never "this account may not spend"."""
    request = _request([(b"authorization", b"Bearer something")])
    assert await _resolve(request, _authenticated(), None, "req-4") is None
