"""The canonical fixtures, through the real app.

**This is the test that would have caught the defect.** The backend and the CLI
each tested their own hand-written fixture, so each proved its own repository
consistent with itself and neither proved the pair compatible. Everything here
drives ``tests/fixtures/contract/`` — the same files the CLI copies — through the
real routes, the real entitlement client and the real paid-access gate.

It is deliberately not a second copy of ``test_account_endpoint.py`` or
``test_entitlement_guard.py``. Those own the route's behaviour, `observe`'s
promise, and the streaming refusal, and they assert those harder than a fixture
sweep would — so anything here that merely restated them has been cut. What is
left answers one question they cannot: **does a CANONICAL website body produce
the CANONICAL account answer, and the canonical refusal, all the way through?**

*Timestamps are rebased, and that is the one liberty taken with the canonical
bodies.* Each entitlement fixture carries an age measured from the manifest's
reference instant; here that age is re-applied to wall-clock time — minus a five
second cushion — which is what lets the real freshness code run with nothing
patched. The cushion means an age is approximately, not exactly, what the fixture
declares, so **this file proves no boundary**. `granted_cache_at_the_bound.json`
is exercised here as an ordinary old-but-valid cached grant; the fact that it sits
exactly ON the bound is asserted in ``test_entitlement_client.py``, where the
clock is injected. A wall clock is the wrong instrument for that question, and a
suite that pretended otherwise would be flaky rather than strict.
"""

from __future__ import annotations

import json
import re
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

import httpx
import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric import rsa
from fastapi.testclient import TestClient
from structlog.testing import capture_logs

from daintree_assistant_server.config import Settings
from daintree_assistant_server.main import create_app
from daintree_assistant_server.services.entitlement import (
    ENTITLEMENT_CONTRACT_VERSION,
    EntitlementService,
)
from daintree_assistant_server.services.supabase_auth import (
    JwksCache,
    SupabaseTokenVerifier,
)

ISSUER = "https://abcdefghij.supabase.co/auth/v1"
CLIENT_ID = "daintree-assistant"
ENTITLEMENT_URL = "https://staging.daintree.org/api/assistant/entitlement"

_CONTRACT = Path(__file__).resolve().parents[1] / "fixtures" / "contract"
_MANIFEST: dict[str, Any] = json.loads((_CONTRACT / "manifest.json").read_text("utf-8"))

#: The fixtures describe ONE subject, and the tokens minted here must claim the
#: same one — the entitlement client compares the website's `subject` against the
#: verified principal, so a mismatch would refuse every body for the wrong reason.
SUBJECT = UUID(_MANIFEST["subject"])
SUBJECT_HASH = _MANIFEST["subject_hash"]
EMAIL = "person@example.com"

_REFERENCE = datetime.fromisoformat(_MANIFEST["reference_instant"].replace("Z", "+00:00"))

#: Enough to absorb the wall-clock drift between building a body and parsing it,
#: and far too small to rescue a fixture that is an hour or two days over.
_CUSHION = timedelta(seconds=5)

_OAUTH: dict[str, Any] = {
    "SUPABASE_AUTH_ISSUER": ISSUER,
    "SUPABASE_JWKS_URL": f"{ISSUER}/.well-known/jwks.json",
    "SUPABASE_OAUTH_AUTHORIZE_URL": f"{ISSUER}/oauth/authorize",
    "SUPABASE_OAUTH_TOKEN_URL": f"{ISSUER}/oauth/token",
    "SUPABASE_OAUTH_CLIENT_ID": CLIENT_ID,
    "DAINTREE_ACCOUNT_URL": "https://staging.daintree.org/account",
    "DAINTREE_SUBSCRIBE_URL": "https://staging.daintree.org/subscribe",
}


# --- the fixtures ----------------------------------------------------------


#: The contract's timestamp spelling, and the gate on whether a fixture is
#: rebased at all. Deliberately the same strict shape the entitlement client
#: requires rather than whatever `fromisoformat` will swallow: that function
#: accepts ISO week dates and naive strings, so parsing-then-restamping would
#: REPAIR a deliberately malformed fixture into a well-formed one and quietly
#: delete the case it was written to test.
_RFC3339 = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(\.\d+)?([Zz]|[+-]\d{2}:\d{2})$")


def entitlement_fixture(name: str) -> dict[str, Any]:
    """A canonical website body, with its age re-applied to the current clock.

    Only ``checked_at`` moves, and it moves by the offset the fixture declares
    LESS the cushion — so `granted_cache_stale.json` is still about eighteen
    hours old and `invalid_cached_grant_expired.json` is still about two days
    old. Approximately, not exactly: see the module docstring on why no boundary
    is proved here.

    A fixture whose timestamp is not contract-shaped — the naive one — is
    returned untouched, because that string IS the thing under test.
    """
    body = json.loads((_CONTRACT / name).read_text("utf-8"))
    stamped = body.get("checked_at")
    if not isinstance(stamped, str) or not _RFC3339.match(stamped):
        return body
    original = datetime.fromisoformat(stamped.replace("Z", "+00:00"))
    age = _REFERENCE - original
    body["checked_at"] = (
        (datetime.now(UTC) - age + _CUSHION).isoformat().replace("+00:00", "Z")
    )
    return body


def _rows(**where: Any) -> list[Any]:
    return [
        pytest.param(row["file"], id=Path(row["file"]).stem)
        for row in _MANIFEST["entitlement"]
        if all(row.get(k) == v for k, v in where.items())
    ]


def _granting() -> list[Any]:
    return _rows(outcome="granted")


def _refusing() -> list[Any]:
    return [
        pytest.param(row["file"], row["outcome"], id=Path(row["file"]).stem)
        for row in _MANIFEST["entitlement"]
        if row["outcome"] in ("subscription_required", "subscription_inactive")
    ]


def _unusable() -> list[Any]:
    return _rows(outcome="entitlement_unavailable")


def _anything_enforce_would_stop() -> list[Any]:
    """Every body a binding mode refuses, by NAME only.

    Deliberately a separate helper rather than `_refusing() + _unusable()`: those
    two carry different arities, and pytest validates parameter counts at
    collection, so concatenating them is a collection error rather than a test
    failure.
    """
    return [
        pytest.param(row["file"], id=Path(row["file"]).stem)
        for row in _MANIFEST["entitlement"]
        if row["outcome"] != "granted"
    ]


# --- the app ---------------------------------------------------------------


@pytest.fixture
def signing_key() -> tuple[Any, dict[str, Any]]:
    private = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    jwk = json.loads(jwt.algorithms.RSAAlgorithm.to_jwk(private.public_key()))
    jwk.update({"kid": "k1", "alg": "RS256", "use": "sig"})
    return private, jwk


def _token(private: Any, **overrides: Any) -> str:
    now = datetime.now(tz=UTC)
    claims: dict[str, Any] = {
        "iss": ISSUER,
        "aud": "authenticated",
        "sub": str(SUBJECT),
        "session_id": str(uuid4()),
        "role": "authenticated",
        "is_anonymous": False,
        "client_id": CLIENT_ID,
        "email": EMAIL,
        "aal": "aal1",
        "iat": int(now.timestamp()),
        "exp": int((now + timedelta(hours=1)).timestamp()),
    }
    claims.update(overrides)
    return jwt.encode(claims, private, algorithm="RS256", headers={"kid": "k1"})


class _Website:
    """Serves one canonical body, and counts what was actually asked of it."""

    def __init__(self, body: dict[str, Any] | None) -> None:
        self._body = body
        self.calls = 0

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.calls += 1
        return httpx.Response(200, json=self._body, request=request)


@pytest.fixture
def make_client(app: Any, fake_provider: Any) -> Any:
    def build(
        jwk: dict[str, Any],
        website: _Website | None = None,
        *,
        entitlement_mode: str = "off",
        auth_mode: str = "enforce",
        configured: bool = True,
    ) -> TestClient:
        settings = Settings(
            OPENAI_API_KEY="tgp_v1_test-key",
            STATE_SIGNING_KEY_CURRENT="test-signing-key",
            STATE_SIGNING_KID="k1",
            STATE_SALT="test-salt",
            RETRY_BACKOFF_BASE_SECONDS=0.0,
            AUTH_MODE=auth_mode,
            ENTITLEMENT_MODE=entitlement_mode,
            **_OAUTH,
            **(
                {"DAINTREE_ENTITLEMENT_URL": ENTITLEMENT_URL}
                if website and configured
                else {}
            ),
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
        if website is not None and configured:
            application.state.entitlement_service = EntitlementService(
                httpx.AsyncClient(transport=httpx.MockTransport(website.handler)),
                url=ENTITLEMENT_URL,
            )
        application.state.container = app.state.container
        application.state.ready = True
        return TestClient(application, raise_server_exceptions=False)

    return build


def _account(client: TestClient, private: Any) -> httpx.Response:
    return client.get(
        "/v1/daintree/account",
        headers={
            "Authorization": f"Bearer {_token(private)}",
            "X-Daintree-Protocol": "3",
        },
    )


def _respond_body() -> dict[str, Any]:
    return {
        "protocol_version": 3,
        "session": {"id": "s", "turn_id": "t"},
        "startup": {},
        "input": {"messages": [{"role": "user", "content": "hello"}]},
    }


def _task_body() -> dict[str, Any]:
    return {"task": "terminal_summarize", "input": {"output": "some output"}}


def _respond(client: TestClient, private: Any) -> httpx.Response:
    return client.post(
        "/v1/daintree/respond",
        json=_respond_body(),
        headers={"Authorization": f"Bearer {_token(private)}"},
    )




def _instant(stamp: str) -> datetime:
    return datetime.fromisoformat(stamp.replace("Z", "+00:00"))


def _reason_for(name: str) -> str:
    row = next(r for r in _MANIFEST["entitlement"] if r["file"] == name)
    return str(row["reason"])


# --- the manifest itself ----------------------------------------------------


def test_the_sweeps_partition_every_entitlement_fixture() -> None:
    """The guard against a sweep that silently selects nothing.

    Every test below is parametrized off `outcome`, so a misspelled value —
    `entitlement_unavailble` — drops its row out of all three sweeps. Empty
    parameter sets are pytest SKIPS, not failures, so the row would vanish with
    the suite still green. This asserts the three sets are non-empty, disjoint,
    and cover the manifest exactly.
    """
    granting = {p.values[0] for p in _granting()}
    refusing = {p.values[0] for p in _refusing()}
    unusable = {p.values[0] for p in _unusable()}
    assert granting and refusing and unusable
    assert not (granting & refusing) and not (granting & unusable)
    assert not (refusing & unusable)
    assert granting | refusing | unusable == {r["file"] for r in _MANIFEST["entitlement"]}
    # And every outcome is one the code can actually produce.
    assert {r["outcome"] for r in _MANIFEST["entitlement"]} == {
        "granted",
        "subscription_required",
        "subscription_inactive",
        "entitlement_unavailable",
    }


def test_the_manifest_declares_the_contract_version_the_client_understands() -> None:
    assert _MANIFEST["entitlement_contract_version"] == ENTITLEMENT_CONTRACT_VERSION


def test_every_fixture_names_the_subject_these_tokens_claim() -> None:
    """The client compares the website's `subject` against the verified
    principal, so a fixture naming anyone else would be refused for a reason that
    has nothing to do with what it was written to test."""
    for row in _MANIFEST["entitlement"]:
        body = json.loads((_CONTRACT / row["file"]).read_text("utf-8"))
        assert body["subject"] == str(SUBJECT)


def test_the_rebasing_preserves_every_field_but_the_timestamp() -> None:
    """The rebase is the one liberty this file takes, so it is bounded
    explicitly: nothing else may differ."""
    name = "entitlement/granted_cache_stale.json"
    original = json.loads((_CONTRACT / name).read_text("utf-8"))
    rebased = entitlement_fixture(name)
    assert rebased["checked_at"] != original["checked_at"]
    del rebased["checked_at"], original["checked_at"]
    assert rebased == original


def test_the_rebasing_leaves_a_malformed_timestamp_alone() -> None:
    """Restamping it would repair the fixture and delete the case.

    `datetime.fromisoformat` parses the naive string happily, so a helper that
    parsed before checking the shape would hand the parser a well-formed
    timestamp and the naive-timestamp row would start passing for no reason.
    """
    name = "entitlement/invalid_naive_timestamp.json"
    assert entitlement_fixture(name) == json.loads((_CONTRACT / name).read_text("utf-8"))


@pytest.mark.parametrize("name", _unusable())
def test_the_rebasing_never_rescues_an_invalid_fixture(name: str) -> None:
    """A cushion large enough to move a fixture across its bound would silently
    delete half this suite — every `checked_at_stale` case would start passing.

    Asserted for EVERY invalid fixture rather than one, because the cushion is a
    single number and the next fixture added is the one nobody re-checks.
    """
    rebased = entitlement_fixture(name)
    stamp = rebased["checked_at"]
    if not isinstance(stamp, str) or not _RFC3339.match(stamp):
        return  # the malformed-timestamp row; its age is not the point
    age = datetime.now(UTC) - _instant(stamp)
    source, stale = rebased["source"], rebased["stale"]
    if _reason_for(name) == "checked_at_stale":
        bound = timedelta(minutes=5) if source == "polar" and not stale else timedelta(
            seconds=86_400 + 300
        )
        assert age > bound


# --- ENTITLEMENT_MODE=off ---------------------------------------------------


def test_off_with_no_url_is_unverified_and_carries_no_timestamp(
    make_client: Any, signing_key: Any
) -> None:
    """The exact body the CLI rejected.

    Compared against the canonical fixture as a WHOLE rather than field by field,
    because "which fields are present" IS the disagreement — a field-by-field
    assertion would pass while the shape stayed wrong. This is the one place a
    behavioural overlap with the sibling suite is worth paying for: it is the
    historical defect, spelled out.
    """
    private, jwk = signing_key
    client = make_client(jwk, _Website(None), configured=False)
    response = _account(client, private)
    assert response.status_code == 200
    assert response.json() == json.loads(
        (_CONTRACT / "account/unverified_with_email.json").read_text("utf-8")
    )


# --- every canonical body, through the account route ------------------------


@pytest.mark.parametrize("name", _granting())
def test_a_canonical_grant_survives_the_whole_boundary(
    make_client: Any, signing_key: Any, name: str
) -> None:
    """Not "a grant came back" — every field the website sent, where the account
    contract puts it.

    Asserting only `access == "granted"` would pass against a route that
    hardcoded a plan, dropped the provider's status text, or invented its own
    timestamp. Those are the mistakes a boundary test exists to catch, so the
    plan, the status, the provenance AND the instant are all compared against
    what the website actually sent.
    """
    private, jwk = signing_key
    sent = entitlement_fixture(name)
    website = _Website(sent)
    body = _account(make_client(jwk, website), private).json()

    assert website.calls == 1
    assert body["access"] == "granted"
    assert body["subject_hash"] == SUBJECT_HASH
    assert body["plan_id"] == sent["plan_id"]
    assert body["subscription_status"] == sent["subscription_status"]
    assert body["entitlement_source"] == sent["source"]
    assert body["entitlement_stale"] == sent["stale"]
    # The INSTANT, not merely the presence of one. A fabricated timestamp is the
    # specific failure the contract forbids, and popping the field — as an earlier
    # draft of this test did — hides exactly it.
    assert _instant(body["checked_at"]) == _instant(sent["checked_at"])


@pytest.mark.parametrize(("name", "outcome"), _refusing())
def test_a_canonical_refusal_is_two_hundred_data_that_keeps_its_detail(
    make_client: Any, signing_key: Any, name: str, outcome: str
) -> None:
    """A known refusal is DATA here, and only becomes a 402 on a paid route.

    Somebody with no plan has to be able to read their own status and find that
    out; gating the endpoint that explains a refusal behind the refusal is a loop
    with no exit. The plan and status are compared too, because the real inactive
    shape carries `pro`/`past_due` and losing that turns "your Pro plan lapsed"
    into "you have no plan".
    """
    private, jwk = signing_key
    sent = entitlement_fixture(name)
    website = _Website(sent)
    response = _account(make_client(jwk, website), private)

    assert website.calls == 1
    assert response.status_code == 200
    body = response.json()
    assert body["access"] == outcome
    assert body.get("plan_id") == sent["plan_id"]
    assert body.get("subscription_status") == sent["subscription_status"]
    assert _instant(body["checked_at"]) == _instant(sent["checked_at"])


@pytest.mark.parametrize("name", _unusable())
def test_a_canonical_unusable_body_is_a_retryable_outage_for_its_declared_reason(
    make_client: Any, signing_key: Any, name: str
) -> None:
    """Every unusable shape, and the ONE thing none of them may become.

    Two assertions beyond the status, both closing a way this could pass without
    proving anything: the website must actually have been ASKED (a route that
    503'd before reading anything would otherwise satisfy this), and the client
    must have refused it for the reason the manifest declares (a fixture broken
    by an unrelated typo would otherwise pass while never reaching its rule).
    """
    private, jwk = signing_key
    website = _Website(entitlement_fixture(name))
    client = make_client(jwk, website)
    with capture_logs() as logs:
        response = _account(client, private)

    assert website.calls == 1
    assert response.status_code == 503
    assert response.json()["error"]["code"] == "entitlement_unavailable"
    reasons = [e.get("reason") for e in logs if e.get("event") == "entitlement_unusable_response"]
    assert reasons == [_reason_for(name)]


def test_a_cached_grant_arrives_as_the_canonical_account_shape(
    make_client: Any, signing_key: Any
) -> None:
    """The website's cached grant becomes the account's cached grant, field for
    field against the canonical account fixture.

    The token here deliberately carries NO email, so the comparison can be whole
    rather than needing the field discarded — a silent `pop("email", None)` would
    let the route drop an email the token supplied and still pass.
    """
    private, jwk = signing_key
    sent = entitlement_fixture("entitlement/granted_cache_stale.json")
    client = make_client(jwk, _Website(sent))
    response = client.get(
        "/v1/daintree/account",
        headers={
            "Authorization": f"Bearer {_token(private, email=None)}",
            "X-Daintree-Protocol": "3",
        },
    )
    body = response.json()
    expected = json.loads(
        (_CONTRACT / "account/granted_stale_cache.json").read_text("utf-8")
    )
    assert "email" not in body
    assert _instant(body.pop("checked_at")) == _instant(sent["checked_at"])
    expected.pop("checked_at")
    assert body == expected


# --- the same bodies, through the paid gate ---------------------------------


@pytest.mark.parametrize("name", _granting())
def test_enforce_serves_a_paid_turn_on_every_canonical_grant(
    make_client: Any, signing_key: Any, fake_provider: Any, name: str
) -> None:
    """The positive control, and it asserts the WORK happened.

    A 200 alone would pass against a gate that refused to run the turn at all, so
    the provider must have been reached — which is also what makes the refusal
    tests below mean something. Every grant is driven, not just the live one: a
    cached grant is exactly the shape a rollout is most likely to get wrong.
    """
    private, jwk = signing_key
    website = _Website(entitlement_fixture(name))
    client = make_client(jwk, website, entitlement_mode="enforce")
    assert _respond(client, private).status_code == 200
    assert website.calls == 1
    assert fake_provider.requests or fake_provider.stream_requests


@pytest.mark.parametrize(("name", "outcome"), _refusing())
def test_enforce_refuses_a_canonical_refusal_with_the_matching_code(
    make_client: Any, signing_key: Any, name: str, outcome: str
) -> None:
    """The two 402s stay distinct all the way to the wire.

    `subscription_required` sends somebody to the subscribe page and
    `subscription_inactive` to their account page; collapsing them would invite a
    paying customer with a lapsed card to buy a second plan.
    """
    private, jwk = signing_key
    website = _Website(entitlement_fixture(name))
    response = _respond(make_client(jwk, website, entitlement_mode="enforce"), private)
    assert website.calls == 1
    assert response.status_code == 402
    assert response.json()["error"]["code"] == outcome


@pytest.mark.parametrize("name", _unusable())
def test_enforce_never_bills_an_unreadable_answer_as_an_unpaid_account(
    make_client: Any, signing_key: Any, fake_provider: Any, name: str
) -> None:
    """**The property this whole suite exists for.**

    Under enforcement, every canonical unusable body — a stale negative, a
    contradiction, an expired grant — reaches the caller as a retryable 503, and
    not one of them presents as "you have not paid".

    Nothing upstream is opened either, across ALL FOUR of the fake's recording
    lists rather than the two obvious ones: the point of gating at a dependency
    is that the refusal lands while there is still nothing to undo, and a
    structured or tool-call request would escape a narrower assertion.
    """
    private, jwk = signing_key
    website = _Website(entitlement_fixture(name))
    client = make_client(jwk, website, entitlement_mode="enforce")
    response = _respond(client, private)

    assert website.calls == 1
    assert response.status_code == 503
    assert response.json()["error"]["code"] == "entitlement_unavailable"
    assert fake_provider.requests == []
    assert fake_provider.stream_requests == []
    assert fake_provider.tool_call_requests == []
    assert fake_provider.selection_requests == []


@pytest.mark.parametrize(
    ("path", "body", "stream"),
    [
        pytest.param("/v1/daintree/respond", _respond_body(), False, id="respond"),
        pytest.param("/v1/daintree/respond", _respond_body(), True, id="respond_streaming"),
        pytest.param("/v1/daintree/tasks", _task_body(), False, id="tasks"),
    ],
)
def test_a_stale_negative_is_a_retryable_outage_on_every_paid_route(
    make_client: Any,
    signing_key: Any,
    fake_provider: Any,
    path: str,
    body: dict[str, Any],
    stream: bool,
) -> None:
    """The cached-denial row across the routes the gate actually protects.

    This is the shape that was ACCEPTED before the freshness table: the website
    reporting what it believed before it lost Polar, honoured as a settled fact
    about somebody's money. Driven over all three because the gate is composed
    per route, and the streaming path is where a refusal could still open a
    stream and then apologise — the caller gets a status code instead.
    """
    private, jwk = signing_key
    website = _Website(entitlement_fixture("entitlement/invalid_cached_denial.json"))
    client = make_client(jwk, website, entitlement_mode="enforce")
    payload = dict(body)
    if stream:
        payload["generation"] = {"stream": True}
    response = client.post(
        path, json=payload, headers={"Authorization": f"Bearer {_token(private)}"}
    )

    assert response.status_code == 503
    assert response.json()["error"]["code"] == "entitlement_unavailable"
    assert "text/event-stream" not in response.headers.get("content-type", "")
    assert fake_provider.requests == []
    assert fake_provider.stream_requests == []
