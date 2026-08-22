"""Integration tests for POST /v1/daintree/doctor.

Two properties carry this endpoint, and both are easy to lose:

* it answers when the server is NOT ready — that is the outage a tester calls
  about, and a doctor gated on readiness reports nothing exactly then;
* the bundle is safe to paste into a support channel — it is assembled from an
  allowlist, so the test that matters is a negative one, scanning the whole
  serialized body for anything that must never leave.
"""

from __future__ import annotations

import contextlib
import json

import pytest
from fastapi.testclient import TestClient

from daintree_assistant_server.errors import invalid_api_key, upstream_error
from daintree_assistant_server.modes import MODE_SPECS

URL = "/v1/daintree/doctor"


def test_doctor_reports_a_healthy_server(client, auth, fake_provider) -> None:
    fake_provider.verify_key_result = {"label": "my-key", "limit_remaining": 12.5}

    r = client.post(URL, headers=auth)

    assert r.status_code == 200
    body = r.json()
    assert body["ready"] is True
    assert body["modes"]["standard"]["key_valid"] is True
    assert body["modes"]["standard"]["key_usable"] is True
    assert body["modes"]["standard"]["server_holds_credential"] is True
    assert body["catalog"]["revision"]
    assert body["catalog"]["skills"] > 0
    assert body["warnings"] == []
    assert body["request_id"]


def test_doctor_verifies_the_server_credential(client, auth, fake_provider) -> None:
    """Same rule as sign-in: the probe must ask about the credential in play.

    That is the SERVER's, and the caller's header is not consulted — so a
    diagnosis is about the account that will actually run the turn rather than
    about a stale key the client happens to still be sending.
    """
    client.post(URL, headers=auth)

    # One probe PER MODE: both credentials are verified, because an instance
    # whose Pro key was never granted looks healthy on every other check.
    assert len(fake_provider.verify_key_calls) == 2
    # No argument: the provider falls back to its configured credential.
    assert fake_provider.verify_key_calls[0] is None


def test_a_rejected_key_is_a_verdict_not_an_error(client, auth, fake_provider) -> None:
    fake_provider.verify_key_error = invalid_api_key("nope")

    r = client.post(URL, headers=auth)

    assert r.status_code == 200
    body = r.json()
    assert body["modes"]["standard"]["key_valid"] is False
    assert body["modes"]["standard"]["key_usable"] is False
    assert "provider_key_rejected:standard" in body["warnings"]


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
    assert body["modes"]["standard"]["key_valid"] is None
    assert "provider_unreachable:standard" in body["warnings"]
    assert not any(w.startswith("provider_key_rejected") for w in body["warnings"])


def test_an_exhausted_key_is_valid_but_unusable(client, auth, fake_provider) -> None:
    """The split that stops a spent key reading as a broken sign-in."""
    fake_provider.verify_key_result = {"limit_remaining": 0}

    r = client.post(URL, headers=auth)

    body = r.json()
    assert body["modes"]["standard"]["key_valid"] is True
    assert body["modes"]["standard"]["key_usable"] is False
    assert body["modes"]["standard"]["reason"] == "credits_exhausted"
    assert "provider_key_unusable:standard" in body["warnings"]


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
    assert r.json()["modes"]["standard"]["server_holds_credential"] is True


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
    assert body["modes"]["standard"]["public_model"] == "daintree-assistant"


def test_the_bundle_states_who_holds_the_credential(client, auth) -> None:
    """The fact a tester most needs confirmed, and the one that inverted.

    There is no privacy_mode to report any more — Cerebras is a single host with
    no per-request data-handling knob, so the posture is its published policy
    rather than something this server selects (docs/PROVIDER_POLICY.md section 4).
    What the bundle reports instead is who is paying.
    """
    body = client.post(URL, headers=auth).json()
    for mode, spec in MODE_SPECS.items():
        assert body["modes"][mode.value]["provider"] == spec.provider_id
    assert body["modes"]["standard"]["server_holds_credential"] is True
    assert "privacy_mode" not in body["modes"]["standard"]


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
    assert body["modes"]["standard"]["key_valid"] is None
    assert "provider_unavailable:standard" in body["warnings"]
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

        modes = None

        async def aclose(self) -> None:
            """Only so lifespan teardown does not fail the test for us."""

    with TestClient(app) as raw:
        app.state.container = _Exploding()
        r = raw.post(URL, headers=auth)

    assert r.status_code == 200
    assert "catalog_unavailable" in r.json()["warnings"]


# ---------------------------------------------------------------------------
# Per-mode isolation. Everything above uses ONE fake under both modes, so it
# cannot tell "both routes were probed" from "the Standard fake was probed
# twice" — which is the regression that matters, because the mode nobody
# exercises by default is the one a wiring mistake survives in.
# ---------------------------------------------------------------------------


@contextlib.contextmanager
def _split_mode_client(settings, healthy, broken):
    """A client whose two modes hold DIFFERENT providers.

    The swap happens AFTER entering `TestClient`, not before. Entering runs the
    app lifespan, which builds its own container and discards whatever was
    installed beforehand — the same trap `conftest.client` documents for the
    docs-client detachment, and one that would have left this whole section
    passing while testing a single shared fake.
    """
    from conftest import detach_docs_client, scripted_selector
    from fastapi.testclient import TestClient

    from daintree_assistant_server.main import create_app
    from daintree_assistant_server.modes import (
        MODE_SPECS,
        ModeRegistry,
        OperationMode,
        ResolvedMode,
    )

    seam = scripted_selector(healthy, settings)
    application = create_app(settings=settings, provider=healthy, selector=seam)
    with TestClient(application) as client:
        container = application.state.container
        detach_docs_client(container)
        container.modes = ModeRegistry(
            {
                OperationMode.STANDARD: ResolvedMode(
                    MODE_SPECS[OperationMode.STANDARD], healthy
                ),
                OperationMode.PRO: ResolvedMode(MODE_SPECS[OperationMode.PRO], broken),
            }
        )
        yield client


def test_a_broken_pro_key_is_reported_as_pro_and_leaves_standard_healthy(
    settings, fake_provider, auth
) -> None:
    """A per-mode failure must be attributed to that mode and contained there.

    Two things are wrong with an unnamespaced report here: an operator reading
    "provider_key_rejected" cannot tell which tier to fix, and a Standard row
    marked unhealthy because Pro is would send them to re-paste a working key.
    """
    from fakes import FakeProvider

    from daintree_assistant_server.errors import invalid_api_key

    broken = FakeProvider()
    broken.verify_key_error = invalid_api_key("nope")

    with _split_mode_client(settings, fake_provider, broken) as client:
        body = client.post(URL, headers=auth).json()

    assert body["modes"]["pro"]["key_valid"] is False
    assert body["modes"]["standard"]["key_valid"] is True
    assert "provider_key_rejected:pro" in body["warnings"]
    assert "provider_key_rejected:standard" not in body["warnings"]
    # Each fake saw exactly its own probe — this is what "one fake under both
    # modes" could never show.
    assert len(broken.verify_key_calls) == 1
    assert len(fake_provider.verify_key_calls) == 1


def test_a_provider_raising_an_untyped_error_does_not_500(settings, fake_provider, auth) -> None:
    """The `except Exception` arm, which nothing pinned.

    A doctor must survive what it diagnoses; an adapter raising something that is
    not a DaintreeError (a malformed body crashing a parser, say) is exactly the
    situation this endpoint exists for, and it must degrade rather than become
    the outage it is reporting on.
    """
    from fakes import FakeProvider

    class _Exploding(FakeProvider):
        async def verify_key(self, api_key=None):  # type: ignore[no-untyped-def]
            raise RuntimeError("the provider client is in a bad state")

    with _split_mode_client(settings, fake_provider, _Exploding()) as client:
        response = client.post(URL, headers=auth)

    assert response.status_code == 200
    body = response.json()
    assert body["modes"]["pro"]["key_valid"] is None, "unreachable is not the same as rejected"
    assert "provider_unreachable:pro" in body["warnings"]
    assert body["modes"]["standard"]["key_valid"] is True
