"""Integration tests for POST /v1/daintree/auth/verify.

This endpoint exists to close a specific gap: our own authentication is structural, so
a well-formed string no provider ever issued passes it and every other endpoint answers
200. Without an upstream probe, a wrong key is discovered on the caller's first real
turn. The tests below pin the three outcomes a sign-in flow has to tell apart —
accepted, definitely rejected, and could-not-check.
"""

from __future__ import annotations

from daintree_assistant_server.errors import invalid_api_key, upstream_error, upstream_timeout


def test_verify_accepts_a_good_key(client, auth, fake_provider) -> None:
    fake_provider.verify_key_result = {"label": "my-key", "limit_remaining": 12.5, "is_free_tier": False}

    r = client.post("/v1/daintree/auth/verify", headers=auth)

    assert r.status_code == 200
    body = r.json()
    assert body["valid"] is True
    # The label is how a user confirms they pasted the key they MEANT to, not merely
    # one that happens to work.
    assert body["label"] == "my-key"
    assert body["limit_remaining"] == 12.5
    assert body["is_free_tier"] is False


def test_verify_probes_the_server_credential_whatever_the_caller_sent(
    client, auth, fake_provider
) -> None:
    """The probe asks about the key this request would actually spend.

    That is always the SERVER's now, and the caller's header is not consulted —
    so a client still holding a stale OpenRouter key gets a truthful answer about
    the backend instead of a verdict on a credential nobody is going to use.
    """
    fake_provider.verify_key_result = {"models": ["gemma-4-31b"]}

    r = client.post("/v1/daintree/auth/verify", headers=auth)

    assert r.status_code == 200
    assert len(fake_provider.verify_key_calls) == 1
    # Called with no argument: the provider falls back to its configured key.
    assert fake_provider.verify_key_calls[0] is None


def test_verify_reports_a_rejected_key_as_a_200_verdict(client, auth, fake_provider) -> None:
    """A rejection is a successful ANSWER, not a failed request.

    Returning our own 401 here would be wrong twice over: this request authenticated
    fine at our door, and a 401 conventionally tells a client to re-send credentials —
    which is exactly the useless retry loop this endpoint exists to prevent.
    """
    fake_provider.verify_key_error = invalid_api_key("The provider rejected this API key.")

    r = client.post("/v1/daintree/auth/verify", headers=auth)

    assert r.status_code == 200
    body = r.json()
    assert body["valid"] is False
    assert body["usable"] is False
    assert body["reason"] == "provider_rejected"
    assert "rejected" in body["detail"].lower()


def test_verify_reports_the_new_provider_rejection_code_too(
    client, auth, fake_provider
) -> None:
    """The provider adapter now raises `provider_invalid_api_key` rather than
    reusing our own door's `invalid_api_key`. This endpoint must recognise both,
    or a rejected key would propagate as a 401 instead of a 200 verdict."""
    from daintree_assistant_server.errors import provider_invalid_api_key

    fake_provider.verify_key_error = provider_invalid_api_key()

    r = client.post("/v1/daintree/auth/verify", headers=auth)

    assert r.status_code == 200
    assert r.json()["reason"] == "provider_rejected"


def test_verify_propagates_an_unreachable_provider_as_502(client, auth, fake_provider) -> None:
    """"Could not check" must NEVER be reported as "invalid".

    Collapsing the two would send a user hunting for a credential problem they do not
    have, every time the provider has a blip.
    """
    fake_provider.verify_key_error = upstream_error("Could not reach the provider to verify the key.")

    r = client.post("/v1/daintree/auth/verify", headers=auth)

    assert r.status_code == 502
    assert r.json()["error"]["code"] == "upstream_error"


def test_verify_without_a_bearer_probes_the_server_credential(client, fake_provider) -> None:
    """The endpoint outlived the question it was written for.

    It used to answer "is the key you pasted valid?". Callers hold no key now, so
    with no bearer it probes the SERVER's — which answers the question that
    actually matters today: can this backend serve me at all?
    """
    fake_provider.verify_key_result = {"models": ["gemma-4-31b"]}

    r = client.post("/v1/daintree/auth/verify")

    assert r.status_code == 200
    assert r.json()["valid"] is True


def test_verify_never_echoes_the_server_key(client, app, fake_provider) -> None:
    """The response is shown to a user and may be logged; it must not carry the key.

    The label is set to the CREDENTIAL ITSELF, which is the realistic leak: some
    providers default a key's label to the key (or a prefix of it). A benign
    placeholder label would let a completely unsafe passthrough pass this test.

    The stake went UP when the credential moved: this is the one key that funds
    all of Daintree, not one tester's account.

    Read through ``serving_api_key`` rather than a vendor-named field on purpose.
    Naming one host here made the test agree with a redaction path that named the
    same host, so both could be wrong together — and they were: with the serving
    host moved, the redaction was still scrubbing the credential of a provider it
    no longer talks to, and this test still passed.
    """
    key = app.state.settings.serving_api_key.get_secret_value()
    fake_provider.verify_key_result = {"label": key}

    r = client.post("/v1/daintree/auth/verify")

    assert key not in r.text
    assert "label" not in r.json()


def test_verify_drops_a_label_that_is_a_prefix_of_the_server_key(
    client, app, fake_provider
) -> None:
    """A truncated key is still key material and must not be echoed."""
    key = app.state.settings.serving_api_key.get_secret_value()
    fake_provider.verify_key_result = {"label": key[: len(key) // 2]}

    r = client.post("/v1/daintree/auth/verify")

    assert "label" not in r.json()


def test_verify_sanitizes_and_bounds_a_hostile_label(client, auth, fake_provider) -> None:
    """The label reaches a terminal, so control characters and unbounded length are out."""
    fake_provider.verify_key_result = {"label": "\x1b[31mred\x07" + "x" * 500}

    body = client.post("/v1/daintree/auth/verify", headers=auth).json()

    assert "\x1b" not in body["label"]
    assert "\x07" not in body["label"]
    assert len(body["label"]) <= 120


def test_verify_reports_a_timeout_as_504(client, auth, fake_provider) -> None:
    """A timed-out probe is "could not check", distinct from both valid and invalid."""
    fake_provider.verify_key_error = upstream_timeout("Verifying the key timed out.")

    r = client.post("/v1/daintree/auth/verify", headers=auth)

    assert r.status_code == 504
    assert r.json()["error"]["code"] == "upstream_timeout"


def test_verify_reports_an_exhausted_key_as_valid_but_not_usable(
    client, auth, fake_provider
) -> None:
    """The distinction the `usable` field exists for.

    A recognised key with a spent balance is genuinely `valid` — and completely
    unable to run a turn. A client reading only `valid` shows a successful
    sign-in, then fails on the first real request with what looks like an
    unrelated error.
    """
    fake_provider.verify_key_result = {"label": "spent", "limit_remaining": 0}

    body = client.post("/v1/daintree/auth/verify", headers=auth).json()

    assert body["valid"] is True
    assert body["usable"] is False
    assert body["reason"] == "credits_exhausted"
    assert body["limit_remaining"] == 0
    assert "credit" in body["detail"].lower()


def test_verify_reports_a_funded_key_as_usable(client, auth, fake_provider) -> None:
    fake_provider.verify_key_result = {"label": "good", "limit_remaining": 12.5}

    body = client.post("/v1/daintree/auth/verify", headers=auth).json()

    assert body["valid"] is True
    assert body["usable"] is True
    assert body["reason"] == "ok"


def test_verify_treats_an_unlimited_key_as_usable(client, auth, fake_provider) -> None:
    """A pay-as-you-go or unlimited key reports no `limit_remaining` at all.

    Guessing "unusable" from a missing field would block a sign-in that would
    have worked — the worse of the two failure modes, since the first real turn
    was never going to fail.
    """
    fake_provider.verify_key_result = {"label": "unlimited"}

    body = client.post("/v1/daintree/auth/verify", headers=auth).json()

    assert body["valid"] is True
    assert body["usable"] is True
    assert body["reason"] == "ok"


def test_verify_reports_a_negative_balance_as_unusable(client, auth, fake_provider) -> None:
    fake_provider.verify_key_result = {"limit_remaining": -3.0}

    body = client.post("/v1/daintree/auth/verify", headers=auth).json()

    assert body["usable"] is False
    assert body["reason"] == "credits_exhausted"


def test_verify_does_not_publish_the_upstream_model_inventory(
    client, fake_provider
) -> None:
    """The probe reads a model listing; the response must not relay it.

    It made sense once: under caller-supplied keys the list described what YOUR
    key could reach. The key is the server's now and the serving model is
    hardcoded, so no caller chooses from it — and `auth.py` ignores the
    Authorization header, which made this an unauthenticated route publishing the
    deployment's whole upstream inventory to anyone who asked. The CLI's response
    struct has never had a field for it, so nothing loses anything.
    """
    fake_provider.verify_key_result = {
        "models": ["zai-org/GLM-5.3-Flash", "some/other-model"]
    }

    r = client.post("/v1/daintree/auth/verify")

    assert r.status_code == 200
    body = r.json()
    assert "models" not in body
    # Not merely absent under that key — absent from the payload entirely.
    assert "other-model" not in r.text
    # The verdict, which is the whole answer this endpoint owes, still arrives.
    assert body["valid"] is True
    assert body["usable"] is True
    assert body["reason"] == "ok"
