"""The entitlement guard on the routes that spend money.

Four properties, in descending order of how expensive they are to get wrong:

* **an outage never reads as "you have not paid"** — a paying customer told to buy
  a second plan during a billing outage is the failure the whole account taxonomy
  exists to prevent;
* **nothing reaches the provider on a refusal** — the point of gating at the
  dependency rather than inside the handler is that the refusal happens while
  there is still nothing to undo;
* **observe changes nothing a caller can see** — that is the mode's entire promise,
  and it is what makes the measurement safe to run against real traffic;
* **the verdict is resolved exactly once per paid request.**
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID

import httpx
import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric import rsa
from fastapi.testclient import TestClient
from structlog.testing import capture_logs

from daintree_assistant_server.auth import subject_hash
from daintree_assistant_server.config import Settings
from daintree_assistant_server.main import create_app
from daintree_assistant_server.services.entitlement import EntitlementService
from daintree_assistant_server.services.supabase_auth import (
    JwksCache,
    SupabaseTokenVerifier,
)

ISSUER = "https://abcdefghij.supabase.co/auth/v1"
CLIENT_ID = "daintree-assistant"
ENTITLEMENT_URL = "https://staging.daintree.org/api/assistant/entitlement"
SUBJECT = UUID("8f14e45f-ea8b-4c1d-9b2a-0000feedface")

_OAUTH: dict[str, Any] = {
    "SUPABASE_AUTH_ISSUER": ISSUER,
    "SUPABASE_JWKS_URL": f"{ISSUER}/.well-known/jwks.json",
    "SUPABASE_OAUTH_AUTHORIZE_URL": f"{ISSUER}/oauth/authorize",
    "SUPABASE_OAUTH_TOKEN_URL": f"{ISSUER}/oauth/token",
    "SUPABASE_OAUTH_CLIENT_ID": CLIENT_ID,
}


@pytest.fixture
def signing_key() -> tuple[Any, dict[str, Any]]:
    private = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    jwk = json.loads(jwt.algorithms.RSAAlgorithm.to_jwk(private.public_key()))
    jwk.update({"kid": "k1", "alg": "RS256", "use": "sig"})
    return private, jwk


EMAIL = "person@example.com"
AUTH_SESSION = UUID("11111111-2222-3333-4444-555555555555")


def _token(private: Any, subject: UUID = SUBJECT) -> str:
    now = datetime.now(tz=UTC)
    return jwt.encode(
        {
            "iss": ISSUER,
            "aud": "authenticated",
            "sub": str(subject),
            "email": EMAIL,
            "session_id": str(AUTH_SESSION),
            "role": "authenticated",
            "is_anonymous": False,
            "client_id": CLIENT_ID,
            "aal": "aal1",
            "iat": int(now.timestamp()),
            "exp": int((now + timedelta(hours=1)).timestamp()),
        },
        private,
        algorithm="RS256",
        headers={"kid": "k1"},
    )


def _entitlement_body(**overrides: Any) -> dict[str, Any]:
    body: dict[str, Any] = {
        "version": 1,
        "subject": str(SUBJECT),
        "access": "granted",
        "plan_id": "standard",
        "subscription_status": "active",
        "source": "polar",
        "stale": False,
        "checked_at": datetime.now(UTC).isoformat().replace("+00:00", "Z"),
    }
    body.update(overrides)
    return body


class _Website:
    """Counts what the guard actually asked, not just what it concluded."""

    def __init__(self, response: httpx.Response | Exception) -> None:
        self._response = response
        self.calls = 0

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.calls += 1
        if isinstance(self._response, Exception):
            raise self._response
        return httpx.Response(
            status_code=self._response.status_code,
            headers=self._response.headers,
            content=self._response.content,
            request=request,
        )


@pytest.fixture
def make_client(app: Any, fake_provider: Any) -> Any:
    def build(
        entitlement_mode: str,
        jwk: dict[str, Any],
        website: _Website | None = None,
        auth_mode: str = "enforce",
    ) -> tuple[TestClient, _Website | None]:
        settings = Settings(
            TOGETHER_API_KEY="tgp_v1_test-key",
            STATE_SIGNING_KEY_CURRENT="test-signing-key",
            STATE_SIGNING_KID="k1",
            STATE_SALT="test-salt",
            RETRY_BACKOFF_BASE_SECONDS=0.0,
            AUTH_MODE=auth_mode,
            ENTITLEMENT_MODE=entitlement_mode,
            **_OAUTH,
            **({"DAINTREE_ENTITLEMENT_URL": ENTITLEMENT_URL} if website else {}),
        )
        application = create_app(
            settings=settings,
            provider=fake_provider,
            selector=app.state.injected_selector,
        )
        jwks_client = httpx.AsyncClient(
            transport=httpx.MockTransport(
                lambda _r: httpx.Response(200, json={"keys": [jwk]})
            )
        )
        application.state.token_verifier = SupabaseTokenVerifier(
            issuer=ISSUER,
            allowed_client_ids=frozenset({CLIENT_ID}),
            jwks=JwksCache(url=_OAUTH["SUPABASE_JWKS_URL"], client=jwks_client),
        )
        application.state.jwks_client = jwks_client
        if website is not None:
            application.state.entitlement_service = EntitlementService(
                httpx.AsyncClient(transport=httpx.MockTransport(website.handler)),
                url=ENTITLEMENT_URL,
            )
        application.state.container = app.state.container
        application.state.ready = True
        return TestClient(application, raise_server_exceptions=False), website

    return build


def _respond_body() -> dict[str, Any]:
    return {
        "protocol_version": 3,
        "session": {"id": "s", "turn_id": "t"},
        "startup": {},
        "input": {"messages": [{"role": "user", "content": "hello"}]},
    }


def _task_body() -> dict[str, Any]:
    return {"task": "terminal_summarize", "input": {"output": "some output"}}


#: What legitimately differs between two otherwise identical requests, and the
#: only things allowed to.
#:
#: ``request_id``/``id`` are minted per request. ``timings`` is the measurement
#: block, and excluding it is the honest statement of what observe mode actually
#: promises: the STATUS, BODY and HEADERS are what `off` would have returned, and
#: the latency is not — an uncached lookup is a bounded network call on the turn's
#: first-byte path. `docs/DAINTREE_API.md` says exactly that rather than the
#: stronger "a caller cannot tell", which this comparison would disprove.
_PER_REQUEST = ("request_id", "id", "timings")


def _stable(body: dict[str, Any]) -> dict[str, Any]:
    return {k: v for k, v in body.items() if k not in _PER_REQUEST}


def _post(client: TestClient, path: str, private: Any, body: dict[str, Any]) -> Any:
    return client.post(
        path, json=body, headers={"Authorization": f"Bearer {_token(private)}"}
    )


# --- off: today's behaviour, unchanged ---------------------------------------


def test_off_asks_nothing_and_serves_the_turn(
    make_client: Any, signing_key: Any, fake_provider: Any
) -> None:
    private, jwk = signing_key
    website = _Website(httpx.Response(200, json=_entitlement_body()))
    client, _ = make_client("off", jwk, website)
    assert _post(client, "/v1/daintree/respond", private, _respond_body()).status_code == 200
    # Not merely "it worked" — the point of `off` is that a turn's first byte pays
    # nothing for a question nobody is asking.
    assert website.calls == 0
    assert fake_provider.requests


# --- enforce ------------------------------------------------------------------


@pytest.mark.parametrize("path,body", [
    ("/v1/daintree/respond", _respond_body()),
    ("/v1/daintree/tasks", _task_body()),
])
def test_enforce_serves_a_granted_account(
    make_client: Any, signing_key: Any, path: str, body: dict[str, Any]
) -> None:
    private, jwk = signing_key
    website = _Website(httpx.Response(200, json=_entitlement_body()))
    client, _ = make_client("enforce", jwk, website)
    assert _post(client, path, private, body).status_code == 200
    assert website.calls == 1


@pytest.mark.parametrize("path,body", [
    ("/v1/daintree/respond", _respond_body()),
    ("/v1/daintree/tasks", _task_body()),
])
@pytest.mark.parametrize("access,code", [
    ("subscription_required", "subscription_required"),
    ("subscription_inactive", "subscription_inactive"),
])
def test_enforce_refuses_before_the_provider_is_ever_called(
    make_client: Any,
    signing_key: Any,
    fake_provider: Any,
    path: str,
    body: dict[str, Any],
    access: str,
    code: str,
) -> None:
    """The reason the gate is a dependency rather than a check inside the handler.

    A refusal has to land while there is still nothing to undo — no provider call
    in flight, and no SSE stream already open with a `meta` event on it promising
    an answer.
    """
    private, jwk = signing_key
    website = _Website(
        httpx.Response(200, json=_entitlement_body(access=access, plan_id=None))
    )
    client, _ = make_client("enforce", jwk, website)
    response = _post(client, path, private, body)
    assert response.status_code == 402
    assert response.json()["error"]["code"] == code
    # Nothing was spent.
    assert fake_provider.requests == []
    assert fake_provider.stream_requests == []


def test_the_two_refusals_stay_distinct(make_client: Any, signing_key: Any) -> None:
    """They share a status and differ in remedy: a first purchase versus the
    billing portal. Offering a fresh checkout to somebody whose card merely
    bounced is how a person ends up paying for two subscriptions."""
    private, jwk = signing_key
    inactive = _Website(
        httpx.Response(
            200, json=_entitlement_body(access="subscription_inactive", plan_id=None)
        )
    )
    client, _ = make_client("enforce", jwk, inactive)
    body = _post(client, "/v1/daintree/respond", private, _respond_body()).json()
    assert body["error"]["code"] == "subscription_inactive"
    assert "portal" in body["error"]["message"] or "currently" in body["error"]["message"]


@pytest.mark.parametrize("outage", [
    pytest.param(httpx.Response(503), id="website_down"),
    pytest.param(httpx.Response(200, content=b"not json"), id="unreadable"),
    pytest.param(httpx.ConnectError("refused"), id="unreachable"),
    pytest.param(httpx.ConnectTimeout("slow"), id="timeout"),
])
def test_an_outage_is_a_retryable_503_and_never_a_refusal(
    make_client: Any, signing_key: Any, fake_provider: Any, outage: Any
) -> None:
    """The single most expensive mistake this design exists to prevent."""
    private, jwk = signing_key
    client, _ = make_client("enforce", jwk, _Website(outage))
    response = _post(client, "/v1/daintree/respond", private, _respond_body())
    assert response.status_code == 503
    code = response.json()["error"]["code"]
    assert code == "entitlement_unavailable"
    assert code not in ("subscription_required", "subscription_inactive")
    assert fake_provider.requests == []


def test_a_revoked_session_is_reported_as_itself(
    make_client: Any, signing_key: Any, fake_provider: Any
) -> None:
    private, jwk = signing_key
    client, _ = make_client("enforce", jwk, _Website(httpx.Response(401)))
    response = _post(client, "/v1/daintree/respond", private, _respond_body())
    assert response.status_code == 401
    assert response.json()["error"]["code"] == "auth_session_revoked"
    assert fake_provider.requests == []


def test_enforce_without_a_client_fails_closed(
    make_client: Any, signing_key: Any, fake_provider: Any
) -> None:
    """Readiness refuses this configuration, so reaching it is a wiring failure —
    and the safe direction to fail is the one that does not spend money on a turn
    nobody checked."""
    private, jwk = signing_key
    website = _Website(httpx.Response(200, json=_entitlement_body()))
    client, _ = make_client("enforce", jwk, website)
    client.app.state.entitlement_service = None
    response = _post(client, "/v1/daintree/respond", private, _respond_body())
    assert response.status_code == 503
    assert response.json()["error"]["code"] == "entitlement_unavailable"
    assert fake_provider.requests == []


@pytest.mark.parametrize("stream", [False, True])
def test_exactly_one_resolution_per_paid_request(
    make_client: Any, signing_key: Any, stream: bool
) -> None:
    """A hand-rolled call in the streaming and non-streaming paths would keep
    losing this; a dependency resolves once by construction.

    Counted on ``EntitlementService.resolve`` rather than on the transport,
    deliberately: the client caches a granted verdict for 60 seconds, so a SECOND
    resolution inside one request would be a cache hit and a transport count would
    still read 1. That is precisely the regression this test is named for.
    """
    private, jwk = signing_key
    website = _Website(httpx.Response(200, json=_entitlement_body()))
    client, _ = make_client("enforce", jwk, website)
    service = client.app.state.entitlement_service
    calls: list[str] = []
    inner = service.resolve

    async def counting(**kwargs: Any) -> Any:
        calls.append(kwargs["request_id"])
        return await inner(**kwargs)

    service.resolve = counting  # type: ignore[method-assign]

    body = _respond_body()
    body["generation"] = {"stream": stream}
    if stream:
        with client.stream(
            "POST",
            "/v1/daintree/respond",
            json=body,
            headers={"Authorization": f"Bearer {_token(private)}"},
        ) as response:
            assert response.status_code == 200
            list(response.iter_lines())
    else:
        assert _post(client, "/v1/daintree/respond", private, body).status_code == 200
    assert len(calls) == 1


def test_a_streaming_refusal_is_an_http_error_not_a_broken_stream(
    make_client: Any, signing_key: Any, fake_provider: Any
) -> None:
    """Refused before the response begins, so the caller gets a status code rather
    than a stream that opens and then apologises."""
    private, jwk = signing_key
    website = _Website(
        httpx.Response(
            200, json=_entitlement_body(access="subscription_required", plan_id=None)
        )
    )
    client, _ = make_client("enforce", jwk, website)
    body = _respond_body()
    body["generation"] = {"stream": True}
    response = client.post(
        "/v1/daintree/respond",
        json=body,
        headers={"Authorization": f"Bearer {_token(private)}"},
    )
    assert response.status_code == 402
    assert "text/event-stream" not in response.headers.get("content-type", "")
    # And nothing upstream was opened. A 402 alone proves only that the SSE
    # response was never committed — the expensive mistake would be a refusal that
    # still paid for a stream nobody read.
    assert fake_provider.stream_requests == []
    assert fake_provider.requests == []


# --- observe: changes nothing a caller can see --------------------------------


@pytest.mark.parametrize("website_response", [
    pytest.param(
        httpx.Response(200, json=_entitlement_body(access="subscription_required", plan_id=None)),
        id="would_refuse",
    ),
    pytest.param(httpx.Response(503), id="outage"),
    pytest.param(httpx.Response(401), id="revoked"),
    pytest.param(httpx.ConnectError("refused"), id="unreachable"),
])
def test_observe_serves_every_request_enforce_would_have_refused(
    make_client: Any, signing_key: Any, website_response: Any
) -> None:
    """The mode's entire promise, and what makes it safe against real traffic.

    A refusal escaping here would turn a measurement into an outage — including
    the failure paths, which is where a mode like this usually leaks.
    """
    private, jwk = signing_key
    client, _ = make_client("observe", jwk, _Website(website_response))
    response = _post(client, "/v1/daintree/respond", private, _respond_body())
    assert response.status_code == 200


@pytest.mark.parametrize("path,body", [
    ("/v1/daintree/respond", _respond_body()),
    ("/v1/daintree/tasks", _task_body()),
])
def test_observe_returns_exactly_what_off_would_have_returned(
    make_client: Any, signing_key: Any, path: str, body: dict[str, Any]
) -> None:
    """Status 200 is not the claim. The claim is that nothing observable differs.

    Compared field by field against the same request under ``off``, with the
    request id excluded because it is minted per request and is meant to differ.
    A mode that returned a bare 200 without doing the work would pass an
    assertion on the status alone; this one would not.
    """
    private, jwk = signing_key
    refusing = _entitlement_body(access="subscription_required", plan_id=None)

    off_client, _ = make_client("off", jwk, _Website(httpx.Response(200, json=refusing)))
    off = _post(off_client, path, private, body)

    observe_client, _ = make_client(
        "observe", jwk, _Website(httpx.Response(200, json=refusing))
    )
    observed = _post(observe_client, path, private, body)

    assert observed.status_code == off.status_code
    assert _stable(observed.json()) == _stable(off.json())
    volatile = {"x-request-id", "date", "content-length", "server"}
    assert {
        k.lower(): v for k, v in observed.headers.items() if k.lower() not in volatile
    } == {k.lower(): v for k, v in off.headers.items() if k.lower() not in volatile}


@pytest.mark.parametrize("path,body", [
    ("/v1/daintree/respond", _respond_body()),
    ("/v1/daintree/tasks", _task_body()),
])
def test_observe_still_does_the_work_it_is_measuring(
    make_client: Any, signing_key: Any, fake_provider: Any, path: str, body: dict[str, Any]
) -> None:
    """The turn must actually run. A mode that short-circuited to a 200 would be
    indistinguishable from `off` on the wire and useless as a measurement."""
    private, jwk = signing_key
    website = _Website(
        httpx.Response(
            200, json=_entitlement_body(access="subscription_required", plan_id=None)
        )
    )
    client, _ = make_client("observe", jwk, website)
    before = len(fake_provider.requests) + len(fake_provider.stream_requests)
    assert _post(client, path, private, body).status_code == 200
    assert len(fake_provider.requests) + len(fake_provider.stream_requests) > before
    assert website.calls == 1


@pytest.mark.parametrize("mode", ["observe", "enforce"])
def test_an_unexpected_failure_in_the_gate_is_not_a_five_hundred(
    make_client: Any, signing_key: Any, mode: str
) -> None:
    """A bug in the gate must not become an internal error.

    In observe it would break the mode's promise in the loudest possible way; in
    enforce it would be the wrong SHAPE as well as the wrong blame — the CLI
    retries `entitlement_unavailable` and does not retry an internal error, so a
    transient bug would end a turn a retry would have completed.
    """
    private, jwk = signing_key
    website = _Website(httpx.Response(200, json=_entitlement_body()))
    client, _ = make_client(mode, jwk, website)

    async def exploding(**_kwargs: Any) -> Any:
        raise RuntimeError("something the client never normalised")

    client.app.state.entitlement_service.resolve = exploding  # type: ignore[method-assign]
    response = _post(client, "/v1/daintree/respond", private, _respond_body())
    if mode == "observe":
        assert response.status_code == 200
    else:
        assert response.status_code == 503
        assert response.json()["error"]["code"] == "entitlement_unavailable"


@pytest.mark.parametrize("path,body", [
    ("/v1/daintree/respond", _respond_body()),
    ("/v1/daintree/tasks", _task_body()),
])
def test_off_serves_both_spend_routes_without_asking(
    make_client: Any, signing_key: Any, path: str, body: dict[str, Any]
) -> None:
    private, jwk = signing_key
    website = _Website(httpx.Response(200, json=_entitlement_body()))
    client, _ = make_client("off", jwk, website)
    assert _post(client, path, private, body).status_code == 200
    assert website.calls == 0


def test_observe_records_the_verdict_it_did_not_act_on(
    make_client: Any, signing_key: Any
) -> None:
    """Producing the evidence is the only reason the mode costs anything."""
    private, jwk = signing_key
    website = _Website(
        httpx.Response(
            200, json=_entitlement_body(access="subscription_required", plan_id=None)
        )
    )
    client, _ = make_client("observe", jwk, website)
    with capture_logs() as logs:
        _post(client, "/v1/daintree/respond", private, _respond_body())
    observed = [line for line in logs if line["event"] == "entitlement_observed"]
    assert observed
    assert observed[0]["would_refuse"] is True
    assert observed[0]["access"] == "subscription_required"


def test_observe_still_asks(make_client: Any, signing_key: Any) -> None:
    """Not free: a paid request costs one lookup against the website, so latency
    moves even though no verdict does."""
    private, jwk = signing_key
    website = _Website(httpx.Response(200, json=_entitlement_body()))
    client, _ = make_client("observe", jwk, website)
    _post(client, "/v1/daintree/respond", private, _respond_body())
    assert website.calls == 1


# --- what is deliberately NOT gated -------------------------------------------


@pytest.mark.parametrize("path", [
    "/health",
    "/readyz",
    "/version",
    "/v1/daintree/auth/config",
])
def test_public_metadata_routes_are_never_gated(
    make_client: Any, signing_key: Any, path: str
) -> None:
    _, jwk = signing_key
    website = _Website(httpx.Response(200, json=_entitlement_body()))
    client, _ = make_client("enforce", jwk, website)
    assert client.get(path).status_code == 200
    assert website.calls == 0


def test_the_runbook_catalog_and_resolve_are_not_gated(
    make_client: Any, signing_key: Any, fake_provider: Any
) -> None:
    """Runbook selection is a LOCAL ONNX classifier, so this route reaches no
    provider and spends nothing — which is why it carries no entitlement gate.

    Asserted rather than assumed: the brief asks for the decision to be documented
    with a test, and "does this route cost money?" is exactly the question that
    stops being obvious once a selector is swapped.
    """
    private, jwk = signing_key
    website = _Website(httpx.Response(200, json=_entitlement_body()))
    client, _ = make_client("enforce", jwk, website)

    assert client.get(
        "/v1/daintree/runbooks",
        headers={"Authorization": f"Bearer {_token(private)}"},
    ).status_code == 200
    resolved = client.post(
        "/v1/daintree/runbooks/resolve",
        json={
            "protocol_version": 3,
            "session_id": "s",
            "action": "find",
            "query": "explore this project",
        },
        headers={"Authorization": f"Bearer {_token(private)}"},
    )
    assert resolved.status_code == 200

    # No billed completion, and therefore nothing an entitlement lookup would be
    # protecting.
    assert fake_provider.requests == []
    assert fake_provider.stream_requests == []
    # And no lookup was made either, which is the gate's own absence.
    assert website.calls == 0
    # `fake_provider.selection_requests` is deliberately NOT asserted here: the
    # suite injects `ScriptedSelector`, a seam that records its scripted decisions
    # against the fake provider so tests can drive them. Production selection is
    # the in-process ONNX classifier and reaches no provider at all — the entry
    # above is test bookkeeping, not a call anybody is billed for.


def test_the_account_endpoint_is_not_behind_the_gate_it_explains(
    make_client: Any, signing_key: Any
) -> None:
    """Somebody with no plan has to be able to read their own account and find out.

    Gating the explanation behind the refusal is a loop with no exit — so a
    verdict that refuses /respond must still return 200 here.
    """
    private, jwk = signing_key
    website = _Website(
        httpx.Response(
            200, json=_entitlement_body(access="subscription_required", plan_id=None)
        )
    )
    client, _ = make_client("enforce", jwk, website)
    assert _post(client, "/v1/daintree/respond", private, _respond_body()).status_code == 402
    account = client.get(
        "/v1/daintree/account",
        headers={"Authorization": f"Bearer {_token(private)}"},
    )
    assert account.status_code == 200
    assert account.json()["access"] == "subscription_required"


def test_observe_without_a_client_still_serves_the_turn(
    make_client: Any, signing_key: Any
) -> None:
    """The unresolvable case, in the mode that must never refuse.

    Enforce fails closed here (there is nothing to check with, and spending money
    unchecked is the wrong direction); observe must do the opposite, because a
    wiring gap in a measurement is still not the caller's problem.
    """
    private, jwk = signing_key
    website = _Website(httpx.Response(200, json=_entitlement_body()))
    client, _ = make_client("observe", jwk, website)
    client.app.state.entitlement_service = None
    with capture_logs() as logs:
        response = _post(client, "/v1/daintree/respond", private, _respond_body())
    assert response.status_code == 200
    assert any(
        line.get("event") == "entitlement_observed" and line.get("result") == "unresolvable"
        for line in logs
    )


def test_a_request_with_no_readable_credential_cannot_resolve(
    make_client: Any, signing_key: Any, fake_provider: Any
) -> None:
    """Two Authorization headers are refused at the door, so the guard never gets
    a credential to forward and nothing reaches the website."""
    private, jwk = signing_key
    website = _Website(httpx.Response(200, json=_entitlement_body()))
    client, _ = make_client("enforce", jwk, website)
    token = _token(private)
    response = client.post(
        "/v1/daintree/respond",
        json=_respond_body(),
        headers=[
            ("Authorization", f"Bearer {token}"),
            ("Authorization", f"Bearer {token}"),
        ],
    )
    assert response.status_code == 401
    assert website.calls == 0
    assert fake_provider.requests == []


def test_production_runbook_selection_cannot_reach_a_provider() -> None:
    """Pins the reason ``/runbooks/resolve`` carries no entitlement gate.

    The test above runs against the injected ``ScriptedSelector``, so on its own
    it says nothing about production wiring — a future change swapping the default
    for a billed selector would leave it green while making an ungated route cost
    money.

    This asserts the structural fact instead: ``build_container`` constructs
    ``LocalSelector`` when nothing is injected, and ``LocalSelector`` has no
    provider parameter to call one with. Checked by signature rather than by
    running it, so the ONNX artifacts are not needed.
    """
    import inspect

    from daintree_assistant_server.main import build_container
    from daintree_assistant_server.runbooks.local.selector import LocalSelector

    parameters = set(inspect.signature(LocalSelector.__init__).parameters)
    assert not {"provider", "chat_provider", "api_key"} & parameters, (
        f"LocalSelector gained a provider-shaped parameter ({parameters}); runbook "
        "resolution may now cost money and /v1/daintree/runbooks/resolve is ungated"
    )
    source = inspect.getsource(build_container)
    assert "selector = LocalSelector(" in source, (
        "build_container no longer defaults to LocalSelector; re-check whether "
        "/v1/daintree/runbooks/resolve still spends nothing"
    )


# --- attribution reaches the accounting ---------------------------------------


@pytest.mark.parametrize("stream", [False, True])
def test_a_turn_is_attributed_to_the_account_that_paid_for_it(
    make_client: Any, signing_key: Any, stream: bool
) -> None:
    """The P1 the brief names: the real principal used to stop at the route.

    Without this the server cannot say who spent what, which is the prerequisite
    for every later billing question — and it has to hold on BOTH response paths,
    because they log their usage from different places.
    """
    private, jwk = signing_key
    website = _Website(httpx.Response(200, json=_entitlement_body(plan_id="pro")))
    client, _ = make_client("enforce", jwk, website)
    body = _respond_body()
    body["generation"] = {"stream": stream}

    with capture_logs() as logs:
        if stream:
            with client.stream(
                "POST",
                "/v1/daintree/respond",
                json=body,
                headers={"Authorization": f"Bearer {_token(private)}"},
            ) as response:
                assert response.status_code == 200
                list(response.iter_lines())
        else:
            assert _post(client, "/v1/daintree/respond", private, body).status_code == 200

    usage = [line for line in logs if line["event"] == "turn_usage"]
    assert len(usage) == 1, "a turn must produce exactly one accounting record"
    assert usage[0]["account_subject_hash"] == subject_hash(str(SUBJECT))
    assert usage[0]["account_plan_id"] == "pro"
    assert usage[0]["account_client_id"] == CLIENT_ID
    assert usage[0]["account_entitlement_source"] == "polar"
    # The attribution and the COST have to be on the same record, or the
    # correlation this whole change exists for cannot be made.
    assert "turn_cost_usd" in usage[0]
    assert "turn_cost_complete" in usage[0]
    assert usage[0]["request_id"]


def test_a_task_is_attributed_too(make_client: Any, signing_key: Any) -> None:
    """A task spends the server's key like any turn, so it earns the same line."""
    private, jwk = signing_key
    website = _Website(httpx.Response(200, json=_entitlement_body()))
    client, _ = make_client("enforce", jwk, website)
    with capture_logs() as logs:
        assert _post(client, "/v1/daintree/tasks", private, _task_body()).status_code == 200
    runs = [line for line in logs if line["event"] == "task_run"]
    assert len(runs) == 1
    assert runs[0]["account_subject_hash"] == subject_hash(str(SUBJECT))
    assert runs[0]["account_plan_id"] == "standard"
    # Same rule as a turn: the account and what it spent, on one record. Without
    # these the task half of "correlate spend with an account" does not exist.
    assert "cost_usd" in runs[0]
    assert "cost_complete" in runs[0]
    assert runs[0]["prompt_tokens"] >= 0


def test_the_raw_identity_never_reaches_the_accounting_log(
    make_client: Any, signing_key: Any
) -> None:
    """The whole reason the attribution is pseudonymous.

    These records are the ones that survive the request and get queried months
    later; a raw user id here is a raw user id in a log pipeline forever.
    """
    private, jwk = signing_key
    token = _token(private)
    website = _Website(httpx.Response(200, json=_entitlement_body()))
    client, _ = make_client("enforce", jwk, website)
    with capture_logs() as logs:
        client.post(
            "/v1/daintree/respond",
            json=_respond_body(),
            headers={"Authorization": f"Bearer {token}"},
        )
    rendered = json.dumps(logs)
    # Every canary the brief names, each with a fixed value so its absence is a
    # real assertion rather than an accident of a random uuid.
    assert str(SUBJECT) not in rendered, "raw user uuid"
    assert str(AUTH_SESSION) not in rendered, "oauth session uuid"
    assert EMAIL not in rendered, "email"
    assert token not in rendered, "bearer"
    # The pseudonymous form IS there — this is attribution, not silence.
    assert subject_hash(str(SUBJECT)) in rendered


def test_an_unattributed_turn_carries_no_account_fields(
    make_client: Any, signing_key: Any
) -> None:
    """Under entitlement `off` there is still an authenticated caller, so the turn
    IS attributed — but to no plan, because none was looked up.

    ``None`` here says "not asked" rather than "no plan", which are different
    claims and only one of them is true.
    """
    private, jwk = signing_key
    client, _ = make_client("off", jwk, _Website(httpx.Response(200, json=_entitlement_body())))
    with capture_logs() as logs:
        _post(client, "/v1/daintree/respond", private, _respond_body())
    usage = [line for line in logs if line["event"] == "turn_usage"]
    assert usage[0]["account_subject_hash"] == subject_hash(str(SUBJECT))
    assert usage[0]["account_plan_id"] is None
    assert usage[0]["account_entitlement_source"] is None


def test_the_open_door_attributes_nothing(make_client: Any, signing_key: Any) -> None:
    """Under ``open`` there is no caller to attribute to, and none is invented.

    A context of empty strings would put five null fields on every turn's cost
    line while identifying nobody. Absent says "not attributed" without
    pretending otherwise.
    """
    _, jwk = signing_key
    client, _ = make_client("off", jwk, None, auth_mode="open")
    with capture_logs() as logs:
        response = client.post("/v1/daintree/respond", json=_respond_body())
    assert response.status_code == 200
    usage = [line for line in logs if line["event"] == "turn_usage"]
    assert usage
    assert "account_subject_hash" not in usage[0]
    assert "account_plan_id" not in usage[0]


def test_two_accounts_are_attributed_separately(
    make_client: Any, signing_key: Any
) -> None:
    """Guards against attribution that is global, cached, or left over.

    Every other test in this file uses one subject, so a context stashed
    somewhere process-wide — or reused from the previous request — would pass all
    of them and misattribute every real customer's spend to whoever went first.
    """
    private, jwk = signing_key
    other = UUID("aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee")

    def handler(request: httpx.Request) -> httpx.Response:
        # Answer for whichever subject the caller's own token names, which is what
        # the client's subject check requires anyway.
        token = request.headers["authorization"].removeprefix("Bearer ")
        claims = jwt.decode(token, options={"verify_signature": False})
        plan = "pro" if claims["sub"] == str(other) else "standard"
        return httpx.Response(
            200, json=_entitlement_body(subject=claims["sub"], plan_id=plan)
        )

    website = _Website(httpx.Response(200, json=_entitlement_body()))
    client, _ = make_client("enforce", jwk, website)
    client.app.state.entitlement_service = EntitlementService(
        httpx.AsyncClient(transport=httpx.MockTransport(handler)), url=ENTITLEMENT_URL
    )

    with capture_logs() as logs:
        for subject in (SUBJECT, other):
            response = client.post(
                "/v1/daintree/respond",
                json=_respond_body(),
                headers={"Authorization": f"Bearer {_token(private, subject)}"},
            )
            assert response.status_code == 200

    usage = [line for line in logs if line["event"] == "turn_usage"]
    assert len(usage) == 2
    assert usage[0]["account_subject_hash"] == subject_hash(str(SUBJECT))
    assert usage[0]["account_plan_id"] == "standard"
    assert usage[1]["account_subject_hash"] == subject_hash(str(other))
    assert usage[1]["account_plan_id"] == "pro"
    assert usage[0]["account_subject_hash"] != usage[1]["account_subject_hash"]
