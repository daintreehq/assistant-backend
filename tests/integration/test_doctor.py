"""Integration tests for POST /v1/daintree/doctor.

Two properties carry this endpoint, and both are easy to lose:

* it answers when the server is NOT ready — that is the outage a tester calls
  about, and a doctor gated on readiness reports nothing exactly then;
* the bundle is safe to paste into a support channel — it is assembled from an
  allowlist, so the test that matters is a negative one, scanning the whole
  serialized body for anything that must never leave.
"""

from __future__ import annotations

import json

import pytest
from fastapi.testclient import TestClient

from daintree_assistant_server.errors import invalid_api_key, upstream_error

URL = "/v1/daintree/doctor"


def test_doctor_reports_a_healthy_server(client, auth, fake_provider) -> None:
    fake_provider.verify_key_result = {"label": "my-key", "limit_remaining": 12.5}

    r = client.post(URL, headers=auth)

    assert r.status_code == 200
    body = r.json()
    assert body["ready"] is True
    assert body["provider"]["key_valid"] is True
    assert body["provider"]["key_usable"] is True
    assert body["provider"]["server_holds_credential"] is True
    assert body["catalog"]["revision"]
    assert body["catalog"]["skills"] > 0
    assert body["warnings"] == []
    assert body["request_id"]


def test_doctor_verifies_with_the_callers_own_key(client, auth, fake_provider) -> None:
    """Same rule as sign-in: the probe must ask about the CALLER's credential.

    The server holds none, so verifying with anything else would answer a
    question nobody asked and report a verdict about the wrong account.
    """
    client.post(URL, headers=auth)

    assert len(fake_provider.verify_key_calls) == 1
    forwarded = fake_provider.verify_key_calls[0]
    assert forwarded.get_secret_value() == auth["Authorization"].removeprefix("Bearer ")


def test_a_rejected_key_is_a_verdict_not_an_error(client, auth, fake_provider) -> None:
    fake_provider.verify_key_error = invalid_api_key("nope")

    r = client.post(URL, headers=auth)

    assert r.status_code == 200
    body = r.json()
    assert body["provider"]["key_valid"] is False
    assert body["provider"]["key_usable"] is False
    assert "provider_key_rejected" in body["warnings"]


def test_an_unreachable_provider_does_not_blame_the_key(client, auth, fake_provider) -> None:
    """ "We could not ask" must never render as "your key is bad".

    A tester told their key is invalid re-pastes a credential that was fine and
    concludes the product is broken. `key_valid` stays null; the warning names
    the real suspect.
    """
    fake_provider.verify_key_error = upstream_error("the provider is down")

    r = client.post(URL, headers=auth)

    assert r.status_code == 200
    body = r.json()
    assert body["provider"]["key_valid"] is None
    assert "provider_unreachable" in body["warnings"]
    assert "provider_key_rejected" not in body["warnings"]


def test_an_exhausted_key_is_valid_but_unusable(client, auth, fake_provider) -> None:
    """The split that stops a spent key reading as a broken sign-in."""
    fake_provider.verify_key_result = {"limit_remaining": 0}

    r = client.post(URL, headers=auth)

    body = r.json()
    assert body["provider"]["key_valid"] is True
    assert body["provider"]["key_usable"] is False
    assert body["provider"]["reason"] == "credits_exhausted"
    assert "provider_key_unusable" in body["warnings"]


def test_doctor_answers_on_a_server_that_never_became_ready(app, auth) -> None:
    """THE point of the endpoint.

    Every other authenticated route 503s here. A tester whose server failed to
    start has no other way to learn why, and "the diagnostic endpoint is also
    down" is the least useful possible answer.
    """
    app.state.ready = False
    app.state.ready_error = "STATE_SIGNING_KEY_CURRENT is missing"

    with TestClient(app) as raw:
        # Re-assert after startup: the lifespan sets readiness on the way up.
        app.state.ready = False
        app.state.ready_error = "STATE_SIGNING_KEY_CURRENT is missing"
        r = raw.post(URL, headers=auth)

    assert r.status_code == 200
    body = r.json()
    assert body["ready"] is False
    assert "server_not_ready" in body["warnings"]
    # Development is not hardened, so the real reason survives to the tester.
    assert body["ready_error"] == "STATE_SIGNING_KEY_CURRENT is missing"
    # And the parts that do not depend on readiness are still filled in.
    assert body["server_version"]
    assert body["protocol"]["max"] >= body["protocol"]["min"]


def test_the_startup_reason_is_withheld_in_a_hardened_environment(app, auth, monkeypatch) -> None:
    """Same rule /readyz follows, for a stronger reason.

    Startup failure text names environment variables and filesystem paths. This
    endpoint is authenticated, but a doctor bundle exists to be COPIED
    elsewhere — which is the same exposure by a slower route.
    """
    monkeypatch.setattr(type(app.state.settings), "is_hardened", property(lambda _self: True))
    with TestClient(app) as raw:
        app.state.ready = False
        app.state.ready_error = "STATE_SIGNING_KEY_CURRENT is weak: 'changeme'"
        r = raw.post(URL, headers=auth)

    body = r.json()
    assert body["ready_error"] == "configuration"
    assert "changeme" not in json.dumps(body)


def test_doctor_is_reachable_without_a_bearer(client) -> None:
    """Same open door as every other route — and the doctor is where a tester
    with no credential goes first to find out what is wrong."""
    r = client.post(URL)
    assert r.status_code == 200
    assert r.json()["provider"]["caller_supplied_key"] is False


@pytest.mark.parametrize(
    "forbidden",
    [
        # The caller's credential, in every shape someone might "helpfully" add.
        "test-key",
        "Bearer",
        # Server-owned prompt text and the real upstream model id.
        "deepseek/deepseek-v4-flash-0731",
        "You are Daintree",
        # Signing material.
        "STATE_SIGNING_KEY",
    ],
)
def test_the_bundle_carries_nothing_that_must_not_be_pasted(
    client, auth, fake_provider, forbidden: str
) -> None:
    """A negative test over the WHOLE serialized bundle.

    The allowlist is the real defence; this is what catches the field someone
    adds later without thinking about where this JSON ends up.
    """
    fake_provider.verify_key_result = {"label": "my-key", "limit_remaining": 12.5}
    r = client.post(URL, headers=auth)
    assert forbidden not in json.dumps(r.json())


def test_the_bundle_reports_the_public_model_alias(client, auth) -> None:
    body = client.post(URL, headers=auth).json()
    assert body["provider"]["model"] == "daintree-assistant"


def test_the_bundle_states_who_holds_the_credential(client, auth) -> None:
    """The fact a tester most needs confirmed, and the one that inverted.

    There is no privacy_mode to report any more — Cerebras is a single host with
    no per-request data-handling knob, so the posture is its published policy
    rather than something this server selects (docs/PROVIDER_POLICY.md section 4).
    What the bundle reports instead is who is paying.
    """
    body = client.post(URL, headers=auth).json()
    assert body["provider"]["name"] == "cerebras"
    assert body["provider"]["server_holds_credential"] is True
    assert "privacy_mode" not in body["provider"]


def test_doctor_survives_a_server_with_no_container_at_all(app, auth) -> None:
    """The worst case the endpoint exists for: the container never built.

    Every section degrades to a stated absence rather than a 500. A doctor that
    crashes on a broken server tells the tester nothing except that something
    else is also broken.
    """
    with TestClient(app) as raw:
        app.state.ready = False
        app.state.ready_error = "prompt template failed to load"
        app.state.container = None
        r = raw.post(URL, headers=auth)

    assert r.status_code == 200
    body = r.json()
    assert body["ready"] is False
    assert body["catalog"]["revision"] is None
    assert body["provider"]["key_valid"] is None
    assert "provider_unavailable" in body["warnings"]
    assert "catalog_unavailable" in body["warnings"]
    # Build identity does not depend on the container and must still be there —
    # it is the first thing anyone triaging asks for.
    assert body["server_version"]


def test_a_catalog_that_raises_is_a_finding_not_a_500(app, auth) -> None:
    """A doctor must survive whatever it is diagnosing."""

    class _Exploding:
        @property
        def catalog(self):  # type: ignore[no-untyped-def]
            raise RuntimeError("catalog reload left the manager in a bad state")

        provider = None

        async def aclose(self) -> None:
            """Only so lifespan teardown does not fail the test for us."""

    with TestClient(app) as raw:
        app.state.container = _Exploding()
        r = raw.post(URL, headers=auth)

    assert r.status_code == 200
    assert "catalog_unavailable" in r.json()["warnings"]
