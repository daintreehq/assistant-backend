"""How the PRODUCTION entitlement client is built.

The client's own tests drive an injected fake transport, which is what makes them
deterministic — and also what means they say nothing about the object a real
deployment ends up holding. Every property below is a security posture that lives
in the constructor and nowhere else, so nothing but this file would notice it
being dropped.
"""

from __future__ import annotations

import httpx
import pytest

from daintree_assistant_server.config import (
    EXECUTOR_KEY_ENV,
    FAST_RESPONSE_KEY_ENV,
    Settings,
)
from daintree_assistant_server.main import _build_entitlement_service
from daintree_assistant_server.services.entitlement import (
    ENTITLEMENT_CONNECT_TIMEOUT_SECONDS,
    ENTITLEMENT_TIMEOUT_SECONDS,
)

_URL = "https://staging.daintree.org/api/assistant/entitlement"


def _settings(**overrides: object) -> Settings:
    base: dict[str, object] = {
        "STATE_SIGNING_KEY_CURRENT": "sk",
        EXECUTOR_KEY_ENV: "sk-test",
        FAST_RESPONSE_KEY_ENV: "csk-test",
    }
    base.update(overrides)
    return Settings(**base)  # type: ignore[arg-type]


@pytest.mark.asyncio
async def test_no_url_means_no_client_and_that_is_a_normal_deployment() -> None:
    """Every deployment running the identity-only rollout is this one.

    ``None`` is what makes the account endpoint answer ``unverified`` rather than
    fail, so it must not be treated as a degraded state.
    """
    assert await _build_entitlement_service(_settings()) is None


@pytest.mark.asyncio
async def test_a_configured_url_produces_a_client_with_the_agreed_posture() -> None:
    service = await _build_entitlement_service(_settings(DAINTREE_ENTITLEMENT_URL=_URL))
    assert service is not None
    try:
        client = service._client
        assert service.url == _URL
        # A redirect would hand the caller's account credential to the next hop.
        assert client.follow_redirects is False
        # An ambient proxy would route that credential through a third party
        # without changing any configuration a reader would think to check.
        assert client.trust_env is False
        assert client.timeout.connect == ENTITLEMENT_CONNECT_TIMEOUT_SECONDS
        assert client.timeout.read == ENTITLEMENT_TIMEOUT_SECONDS
    finally:
        await service.aclose()


@pytest.mark.asyncio
async def test_the_shared_client_stores_no_cookies() -> None:
    """This client is long-lived and shared by every caller.

    A ``Set-Cookie`` retained while checking one account would be sent alongside
    the NEXT caller's bearer — one person's session state travelling with another
    person's credential.
    """
    service = await _build_entitlement_service(_settings(DAINTREE_ENTITLEMENT_URL=_URL))
    assert service is not None
    try:
        request = httpx.Request("GET", _URL)
        response = httpx.Response(
            200,
            headers={"set-cookie": "sid=secret; Domain=daintree.org; Path=/"},
            request=request,
        )
        service._client.cookies.extract_cookies(response)
        assert dict(service._client.cookies) == {}

        # And nothing is attached to a later request either.
        later = httpx.Request("GET", _URL)
        service._client.cookies.set_cookie_header(later)
        assert "cookie" not in later.headers
    finally:
        await service.aclose()


@pytest.mark.asyncio
async def test_a_url_that_fails_validation_produces_no_client() -> None:
    """``entitlement_is_configured`` gates construction, so an invalid value never
    becomes a live client pointed somewhere it should not be."""
    assert (
        await _build_entitlement_service(
            _settings(DAINTREE_ENTITLEMENT_URL="https://evil.example.com/e")
        )
        is None
    )
