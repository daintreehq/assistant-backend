"""The entitlement client: every response variant, and what it must never do.

Two properties this file exists to hold, above all the field-by-field checks:

* **uncertainty is never a verdict.** No timeout, no 5xx, no malformed body and no
  contradiction may resolve to ``subscription_required``. A paying customer told
  to buy a second plan during a billing outage is the failure the whole account
  taxonomy was built to prevent;
* **the bearer never becomes state.** Not a cache key, not a log field, not an
  exception string.
"""

from __future__ import annotations

import asyncio
import gzip
import json
import time
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from email.utils import format_datetime
from typing import Any
from uuid import UUID, uuid4

import httpx
import pytest
from structlog.testing import capture_logs

from daintree_assistant_server.errors import DaintreeError
from daintree_assistant_server.services.entitlement import (
    ENTITLEMENT_CACHE_MAX_ENTRIES,
    ENTITLEMENT_CONNECT_TIMEOUT_SECONDS,
    ENTITLEMENT_DENIED_TTL_SECONDS,
    ENTITLEMENT_GRANTED_TTL_SECONDS,
    ENTITLEMENT_RESPONSE_MAX_BYTES,
    ENTITLEMENT_TIMEOUT_SECONDS,
    Entitlement,
    EntitlementContractError,
    EntitlementService,
    _CacheEntry,
    _parse,
)

_URL = "https://staging.daintree.org/api/assistant/entitlement"
_TOKEN = "eyJhbGciOiJFUzI1NiJ9.super-secret-bearer.signature"
_SUBJECT = UUID("8f14e45f-ea8b-4c1d-9b2a-0000feedface")


def _body(**overrides: Any) -> dict[str, Any]:
    body: dict[str, Any] = {
        "version": 1,
        "subject": str(_SUBJECT),
        "access": "granted",
        "plan_id": "standard",
        "subscription_status": "active",
        "source": "polar",
        "stale": False,
        "checked_at": datetime.now(UTC).isoformat().replace("+00:00", "Z"),
    }
    body.update(overrides)
    return {k: v for k, v in body.items() if v is not _OMIT}


_OMIT = object()


class _Recorder:
    """A fake transport that records what it was actually asked to send."""

    def __init__(self, *responses: httpx.Response | Exception) -> None:
        self._responses = list(responses)
        self.requests: list[httpx.Request] = []

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        # The LAST response repeats, but only after every earlier one has been
        # handed out in order. A test that expected N requests and made N+1 sees
        # the last response again rather than an error — which is why every
        # multi-response test below also asserts the exact request count.
        item = self._responses.pop(0) if len(self._responses) > 1 else self._responses[0]
        if isinstance(item, Exception):
            raise item
        return httpx.Response(
            status_code=item.status_code,
            headers=item.headers,
            content=item.content,
            request=request,
        )


def _service(*responses: httpx.Response | Exception) -> tuple[EntitlementService, _Recorder]:
    """A service over a fake transport.

    ``follow_redirects=True`` on the CLIENT is deliberate and is what gives the
    redirect test its teeth: httpx's own default is False, so a test client built
    with the default would keep passing if the call site stopped asking. Here the
    client would happily follow one, and only `_fetch`'s explicit
    ``follow_redirects=False`` prevents it.
    """
    recorder = _Recorder(*responses)
    client = httpx.AsyncClient(
        transport=httpx.MockTransport(recorder.handler), follow_redirects=True
    )
    return EntitlementService(client, url=_URL), recorder


def _json_response(payload: Any, status: int = 200, **headers: str) -> httpx.Response:
    return httpx.Response(
        status_code=status,
        content=json.dumps(payload).encode("utf-8"),
        headers={"content-type": "application/json", **headers},
    )


async def _resolve(service: EntitlementService, **overrides: Any) -> Any:
    kwargs: dict[str, Any] = {
        "token": _TOKEN,
        "user_id": _SUBJECT,
        "oauth_client_id": "daintree-assistant",
        "request_id": "req-1",
        "subject_hash": "0123456789abcdef",
    }
    kwargs.update(overrides)
    return await service.resolve(**kwargs)


# --- the happy path and what reaches the wire ------------------------------


@pytest.mark.asyncio
async def test_a_granted_answer_is_parsed() -> None:
    service, _ = _service(_json_response(_body()))
    verdict = await _resolve(service)
    assert verdict.granted is True
    assert verdict.plan_id == "standard"
    assert verdict.source == "polar"
    assert verdict.stale is False


@pytest.mark.asyncio
async def test_the_request_carries_the_bearer_and_nothing_else() -> None:
    service, recorder = _service(_json_response(_body()))
    await _resolve(service)
    (request,) = recorder.requests
    assert request.method == "GET"
    assert str(request.url) == _URL
    assert request.headers["authorization"] == f"Bearer {_TOKEN}"
    assert request.headers["accept"] == "application/json"
    assert request.content == b""


@pytest.mark.asyncio
@pytest.mark.parametrize("access", ["subscription_required", "subscription_inactive"])
async def test_a_known_refusal_is_a_verdict_not_an_error(access: str) -> None:
    service, _ = _service(_json_response(_body(access=access, plan_id=None)))
    verdict = await _resolve(service)
    assert verdict.granted is False
    assert verdict.access == access


@pytest.mark.asyncio
async def test_an_inactive_subscription_may_still_name_its_plan() -> None:
    """The shape the website actually sends for a lapsed subscription.

    `plan_id` is only REQUIRED alongside `granted`; it is not forbidden beside a
    refusal, and the website fills it in because a cancelled Pro subscription is
    still a Pro subscription. Pinned because "granted requires a plan" is easy to
    over-read as "only granted may have one", which would 503 an ordinary answer.
    """
    service, _ = _service(
        _json_response(_body(access="subscription_inactive", plan_id="pro"))
    )
    verdict = await _resolve(service)
    assert verdict.granted is False
    assert verdict.access == "subscription_inactive"
    assert verdict.plan_id == "pro"


# --- status mapping --------------------------------------------------------


@pytest.mark.asyncio
async def test_a_401_means_the_session_is_gone() -> None:
    """The one thing local JWKS verification cannot establish.

    The website performs a fresh Supabase user check, so its 401 against a token
    whose signature still verifies is a revoked session — and the CLI already maps
    that code to "delete the local credential".
    """
    service, _ = _service(httpx.Response(401))
    with pytest.raises(DaintreeError) as exc:
        await _resolve(service)
    assert exc.value.code == "auth_session_revoked"
    assert exc.value.status_code == 401


@pytest.mark.asyncio
async def test_a_403_means_the_oauth_client_is_not_accepted() -> None:
    service, _ = _service(httpx.Response(403))
    with pytest.raises(DaintreeError) as exc:
        await _resolve(service)
    assert exc.value.code == "auth_client_not_allowed"


@pytest.mark.asyncio
async def test_a_403_naming_permission_denied_is_configuration_drift_not_a_verdict() -> None:
    """The one thing that answer can no longer mean is something about the caller.

    This server holds its own copy of the private-staging subject allowlist and
    `auth.py` refuses a non-member before the lookup is ever opened, so a subject
    that reached the website was already approved here. The website enforcing the
    same contract and disagreeing therefore says the two deployed configurations
    have drifted — an operator's problem, and one the caller could do nothing about
    if we passed the non-retryable 403 through.
    """
    service, _ = _service(
        _json_response({"error": {"code": "auth_permission_denied"}}, status=403)
    )
    with pytest.raises(DaintreeError) as exc:
        await _resolve(service)
    assert exc.value.code == "entitlement_unavailable"
    assert exc.value.status_code == 503
    assert exc.value.retryable is True
    # The credential survives: nothing here challenges it or asks for a new one.
    assert "WWW-Authenticate" not in exc.value.headers


@pytest.mark.asyncio
async def test_gate_drift_is_logged_as_itself_with_nothing_from_the_body() -> None:
    """An operator has to be able to alert on the divergence specifically.

    It gets its own event rather than joining the ordinary dependency noise —
    nothing retries its way out of two configurations disagreeing. And it carries
    a NAME for what happened, never a byte the website chose.
    """
    hostile = "subject 8f14e45f is not on the staging allowlist"
    service, _ = _service(
        _json_response(
            {"error": {"code": "auth_permission_denied", "message": hostile}},
            status=403,
        )
    )
    with capture_logs() as logs, pytest.raises(DaintreeError):
        await _resolve(service)
    (record,) = [line for line in logs if line["event"] == "entitlement_gate_drift"]
    assert record["reason"] == "permission_denied_403"
    assert record["request_id"] == "req-1"
    assert hostile not in json.dumps(logs)


@pytest.mark.asyncio
async def test_a_429_naming_the_account_is_rate_limiting_and_passes_a_retry_after() -> None:
    """The website deliberately throttling one account, passed through as itself.

    This is the only producer of `account_rate_limited` in the server, and its
    `Retry-After` is scoped to one account — so there is no herd to synchronise by
    honouring it.
    """
    service, _ = _service(
        _json_response(
            {"error": {"code": "account_rate_limited"}},
            status=429,
            **{"retry-after": "30"},
        )
    )
    with pytest.raises(DaintreeError) as exc:
        await _resolve(service)
    assert exc.value.code == "account_rate_limited"
    assert exc.value.headers["Retry-After"] == "30"


@pytest.mark.asyncio
@pytest.mark.parametrize("value", ["999999", "-1", "Wed, 21 Oct 2015 07:28:00 GMT", "soon"])
async def test_an_implausible_retry_after_is_dropped_rather_than_clamped(value: str) -> None:
    """The CLI treats a server-supplied value as authoritative.

    A number we do not believe is not evidence of a better one, so the client's
    own jittered backoff is left in charge.
    """
    service, _ = _service(
        _json_response(
            {"error": {"code": "account_rate_limited"}},
            status=429,
            **{"retry-after": value},
        )
    )
    with pytest.raises(DaintreeError) as exc:
        await _resolve(service)
    assert exc.value.code == "account_rate_limited"
    assert "Retry-After" not in exc.value.headers


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "response",
    [
        httpx.Response(429, headers={"retry-after": "30"}),
        httpx.Response(429, content=b"<html>Too Many Requests</html>"),
        _json_response({"error": {"code": "rate_limited"}}, status=429),
        _json_response({"message": "slow down"}, status=429),
    ],
    ids=["bare", "html", "foreign-code", "no-envelope"],
)
async def test_an_opaque_429_is_an_edge_throttle_and_not_the_caller_s_fault(
    response: httpx.Response,
) -> None:
    """An edge limit counts requests by SOURCE ADDRESS, and ours is shared.

    A CDN or pre-function firewall throttle never reaches the website's own code,
    so it answers with its own opaque body. This server runs on Cloud Run, so the
    address being counted is one egress IP shared by every caller on the instance —
    reporting it as `account_rate_limited` would tell an innocent user they are
    sending too many requests. Its `Retry-After` is dropped for the mirror-image
    reason: it applies to the whole instance, so honouring it would wake every
    caller behind this server at the same second.
    """
    service, _ = _service(response)
    with pytest.raises(DaintreeError) as exc:
        await _resolve(service)
    assert exc.value.code == "entitlement_unavailable"
    assert exc.value.status_code == 503
    assert exc.value.retryable is True
    assert "Retry-After" not in exc.value.headers
    assert "WWW-Authenticate" not in exc.value.headers


@pytest.mark.asyncio
async def test_an_edge_throttle_is_logged_without_the_edge_s_body() -> None:
    hostile = "your IP 203.0.113.7 exceeded the limit"
    service, _ = _service(httpx.Response(429, content=hostile.encode("utf-8")))
    with capture_logs() as logs, pytest.raises(DaintreeError):
        await _resolve(service)
    assert any(line.get("reason") == "edge_throttle_429" for line in logs), logs
    assert hostile not in json.dumps(logs)


@pytest.mark.asyncio
@pytest.mark.parametrize("status", [400, 402, 404, 500, 502, 503, 504, 418])
async def test_any_other_status_is_unavailable_never_a_refusal(status: int) -> None:
    """Including 402.

    Only a well-formed 200 body may produce a subscription verdict. A bare 402
    from the website is a status we did not agree on, and reading it as "not
    subscribed" would let a misconfigured route refuse a paying customer.
    """
    service, _ = _service(httpx.Response(status))
    with pytest.raises(DaintreeError) as exc:
        await _resolve(service)
    assert exc.value.code == "entitlement_unavailable"
    assert exc.value.retryable is True


@pytest.mark.asyncio
@pytest.mark.parametrize("status", [301, 302, 303, 307, 308])
async def test_a_redirect_is_refused_rather_than_followed(status: int) -> None:
    """The next hop would receive the caller's account credential.

    The fake client is built with ``follow_redirects=True``, so it WOULD follow
    one — the only thing stopping it is `_fetch`'s explicit refusal. The assertion
    is therefore that nothing was ever sent to the redirect target, not merely
    that an error came back.
    """
    service, recorder = _service(
        httpx.Response(status, headers={"location": "https://elsewhere.example/e"})
    )
    with pytest.raises(DaintreeError) as exc:
        await _resolve(service)
    assert exc.value.code == "entitlement_unavailable"
    assert [str(r.url) for r in recorder.requests] == [_URL]
    assert not any("elsewhere.example" in str(r.url) for r in recorder.requests)


@pytest.mark.asyncio
async def test_a_timeout_is_unavailable() -> None:
    service, _ = _service(httpx.ConnectTimeout("timed out"))
    with pytest.raises(DaintreeError) as exc:
        await _resolve(service)
    assert exc.value.code == "entitlement_unavailable"
    # Retryable and a 503, asserted rather than assumed: this is the exact path a
    # billing outage takes, and the one it must never take instead is a 402.
    assert exc.value.status_code == 503
    assert exc.value.retryable is True


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("fraction_of_budget", "accepted"),
    [
        pytest.param(0.65, True, id="inside_the_budget"),
        pytest.param(1.4, False, id="past_the_budget"),
    ],
)
async def test_an_answer_is_judged_against_the_whole_budget(
    fraction_of_budget: float, accepted: bool
) -> None:
    """The behaviour, at 1/200th scale so it runs in milliseconds.

    Scaled rather than mocked because `asyncio.timeout` owns the loop clock:
    the service takes an injectable `timeout=`, so a real deadline of 50ms with a
    transport that really waits gives a genuine cancellation. 0.65 of the budget
    is the shape that matters — it is where the website's outage fallback lands,
    and it is what the previous 5-second bound cut off.
    """
    budget = 0.05

    async def _slow(request: httpx.Request) -> httpx.Response:
        await asyncio.sleep(budget * fraction_of_budget)
        return _json_response(_body(source="cache", stale=True))

    client = httpx.AsyncClient(transport=httpx.MockTransport(_slow))
    service = EntitlementService(client, url=_URL, timeout=budget)
    if accepted:
        assert (await _resolve(service)).granted is True
    else:
        with pytest.raises(DaintreeError) as exc:
            await _resolve(service)
        assert exc.value.code == "entitlement_unavailable"
        assert exc.value.status_code == 503
        assert exc.value.retryable is True


@pytest.mark.asyncio
async def test_the_deadline_the_service_enforces_is_the_one_it_advertises() -> None:
    """The scaled test above proves the MECHANISM; this proves the NUMBER.

    Between them they close the gap a constant-only assertion leaves: a service
    that enforced five seconds while still exporting ten would satisfy either one
    alone. Here the default-constructed service is driven with no `timeout=`
    override, and the deadline it actually applies is read off the request it
    built.
    """
    service, recorder = _service(_json_response(_body()))
    await _resolve(service)
    (request,) = recorder.requests
    applied = request.extensions["timeout"]
    assert applied["connect"] == ENTITLEMENT_CONNECT_TIMEOUT_SECONDS
    for phase in ("read", "write", "pool"):
        assert applied[phase] == ENTITLEMENT_TIMEOUT_SECONDS


@pytest.mark.asyncio
async def test_a_host_that_never_answers_is_cut_off_by_our_own_deadline() -> None:
    """Not the transport's deadline — ours.

    The mock transport has no timeout of its own, so nothing but
    `asyncio.timeout` in `resolve` can end this. The bound asserted is tight
    against the injected deadline rather than merely "eventually", which is what
    distinguishes the wall-clock cut-off from a coincidence.
    """
    async def _never(request: httpx.Request) -> httpx.Response:
        await asyncio.sleep(30)
        raise AssertionError("unreachable")  # pragma: no cover

    client = httpx.AsyncClient(transport=httpx.MockTransport(_never))
    service = EntitlementService(client, url=_URL, timeout=0.05)
    started = time.monotonic()
    with pytest.raises(DaintreeError) as exc:
        await _resolve(service)
    assert exc.value.code == "entitlement_unavailable"
    assert time.monotonic() - started < 1.0


def test_the_budget_is_the_agreed_cross_service_number() -> None:
    """Ten seconds is coordinated with the website's 8-second hard total, and two
    seconds of connect sits inside it. Both pinned as literals rather than
    compared to themselves."""
    assert ENTITLEMENT_TIMEOUT_SECONDS == 10.0
    assert ENTITLEMENT_CONNECT_TIMEOUT_SECONDS == 2.0
    assert ENTITLEMENT_CONNECT_TIMEOUT_SECONDS < ENTITLEMENT_TIMEOUT_SECONDS


# --- body validation -------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "overrides",
    [
        pytest.param({"version": 2}, id="unknown_version"),
        pytest.param({"version": "1"}, id="version_wrong_type"),
        pytest.param({"subject": "11111111-2222-3333-4444-555555555555"}, id="subject_mismatch"),
        pytest.param({"subject": 12345}, id="subject_wrong_type"),
        pytest.param({"subject": "not-a-uuid"}, id="subject_not_a_uuid"),
        pytest.param({"access": "maybe"}, id="access_outside_the_enum"),
        pytest.param({"plan_id": "enterprise"}, id="plan_outside_the_enum"),
        pytest.param({"access": "granted", "plan_id": None}, id="granted_without_a_plan"),
        pytest.param(
            {"source": "cache", "stale": True, "access": "granted", "plan_id": None},
            id="cached_grant_without_a_plan",
        ),
        pytest.param({"source": "guess"}, id="source_outside_the_enum"),
        pytest.param({"stale": "yes"}, id="stale_wrong_type"),
        pytest.param({"stale": True, "source": "polar"}, id="stale_without_cache"),
        pytest.param({"source": "cache", "stale": False}, id="cache_without_stale"),
        pytest.param(
            {
                "source": "cache",
                "stale": True,
                "access": "subscription_required",
                "plan_id": None,
            },
            id="cached_denial",
        ),
        pytest.param(
            {
                "source": "cache",
                "stale": True,
                "access": "subscription_inactive",
                "plan_id": None,
            },
            id="cached_inactive",
        ),
        pytest.param({"subscription_status": 7}, id="status_wrong_type"),
        pytest.param({"subscription_status": "x" * 200}, id="status_oversized"),
        pytest.param({"checked_at": "2026-08-25T12:00:00"}, id="checked_at_naive"),
        pytest.param({"checked_at": "not a time"}, id="checked_at_unparseable"),
    ],
)
async def test_an_unusable_body_is_unavailable_never_a_refusal(
    overrides: dict[str, Any],
) -> None:
    """The body is built INSIDE the test, not in the parametrize list.

    `_body` stamps `checked_at` with `datetime.now`, and a list comprehension in a
    decorator runs at COLLECTION time. While the age limit was a day that gap was
    invisible; now that a live answer expires in five minutes, a slow suite could
    make one of these rows pass because the timestamp went stale rather than
    because the rule under test fired — the row would be green with its own rule
    deleted.
    """
    service, _ = _service(_json_response(_body(**overrides)))
    with pytest.raises(DaintreeError) as exc:
        await _resolve(service)
    assert exc.value.code == "entitlement_unavailable"
    # Retryable, and specifically NOT either subscription refusal: an unusable
    # body is uncertainty, and uncertainty must never be billed as "you have not
    # paid".
    assert exc.value.status_code == 503
    assert exc.value.retryable is True


@pytest.mark.asyncio
async def test_a_lone_surrogate_in_the_status_is_survived_not_crashed_on() -> None:
    """`json.loads` accepts `"\ud800"`, and a bare `.encode("utf-8")` on it raises
    `UnicodeEncodeError` — which is not an `EntitlementContractError`, so it would
    escape `_parse`'s contract, lose the operator-facing reason, and land in the
    broad handler as an anonymous failure. The byte count goes through
    `text_utils.utf8_len`, which exists for exactly this.

    The value is then dropped rather than refused, and that is the right outcome:
    it is one unprintable character in a diagnostic field, so the sanitizer
    removes it and the VERDICT — the part that decides whether somebody may work
    — is unaffected. Refusing the whole body would convert a cosmetic upstream
    defect into a paid turn that cannot run.
    """
    service, _ = _service(_json_response(_body(subscription_status="\ud800")))
    verdict = await _resolve(service)
    assert verdict.granted is True
    assert verdict.subscription_status is None


@pytest.mark.asyncio
async def test_a_status_of_many_surrogates_is_still_bounded() -> None:
    """The size check must still BIND on a value it cannot encode strictly."""
    service, _ = _service(_json_response(_body(subscription_status="\ud800" * 100)))
    with pytest.raises(DaintreeError) as exc:
        await _resolve(service)
    assert exc.value.code == "entitlement_unavailable"


@pytest.mark.asyncio
async def test_a_non_object_body_is_unavailable() -> None:
    service, _ = _service(_json_response([1, 2, 3]))
    with pytest.raises(DaintreeError) as exc:
        await _resolve(service)
    assert exc.value.code == "entitlement_unavailable"


@pytest.mark.asyncio
async def test_invalid_json_is_unavailable() -> None:
    service, _ = _service(
        httpx.Response(200, content=b"<html>not json</html>", headers={"content-type": "text/html"})
    )
    with pytest.raises(DaintreeError) as exc:
        await _resolve(service)
    assert exc.value.code == "entitlement_unavailable"


@pytest.mark.asyncio
async def test_a_checked_at_in_the_future_is_refused() -> None:
    future = (datetime.now(UTC) + timedelta(hours=1)).isoformat()
    service, _ = _service(_json_response(_body(checked_at=future)))
    with pytest.raises(DaintreeError) as exc:
        await _resolve(service)
    assert exc.value.code == "entitlement_unavailable"


@pytest.mark.asyncio
async def test_a_body_past_the_cap_is_refused_even_though_it_would_have_granted() -> None:
    """VALID oversized JSON, not junk.

    An oversized body that is also unparseable proves nothing: remove the cap and
    the JSON decoder produces the same 503. This one would grant access if it were
    read, so only the cap can be what refuses it.
    """
    payload = _body(padding="x" * ENTITLEMENT_RESPONSE_MAX_BYTES)
    service, _ = _service(_json_response(payload))
    with pytest.raises(DaintreeError) as exc:
        await _resolve(service)
    assert exc.value.code == "entitlement_unavailable"


@pytest.mark.asyncio
async def test_a_body_that_lies_about_its_length_is_still_capped() -> None:
    """The declared length is a hint, not the bound.

    A chunked reply has none at all, and a hostile one can understate it, so the
    bytes are counted as they arrive.
    """
    payload = json.dumps(_body(padding="x" * ENTITLEMENT_RESPONSE_MAX_BYTES)).encode()
    service, _ = _service(
        httpx.Response(
            200,
            content=payload,
            headers={"content-type": "application/json", "content-length": "12"},
        )
    )
    with pytest.raises(DaintreeError) as exc:
        await _resolve(service)
    assert exc.value.code == "entitlement_unavailable"


@pytest.mark.asyncio
async def test_a_body_just_under_the_cap_is_still_read() -> None:
    """The cap is a ceiling, not a fence somewhere below one."""
    padding = "x" * (ENTITLEMENT_RESPONSE_MAX_BYTES - len(json.dumps(_body())) - 20)
    service, _ = _service(_json_response(_body(padding=padding)))
    assert (await _resolve(service)).granted is True


@pytest.mark.asyncio
async def test_the_request_refuses_compression() -> None:
    """`aiter_bytes` yields DECODED bytes, so a compressed reply expands first.

    A few KiB of gzip becomes hundreds of megabytes before anything can measure
    it. Asking for identity is what makes the wire bytes and the counted bytes the
    same thing.
    """
    service, recorder = _service(_json_response(_body()))
    await _resolve(service)
    assert recorder.requests[0].headers["accept-encoding"] == "identity"


@pytest.mark.asyncio
async def test_a_compressed_reply_is_refused_rather_than_decoded() -> None:
    """We asked for identity; a peer that compressed anyway is not cooperating.

    Its decoded size cannot be bounded before allocating it, so the answer is
    refused rather than expanded — the cap has to hold against a hostile peer, not
    only a polite one.
    """
    # Its OWN transport, not the shared recorder: that one rebuilds each response
    # from `.content`, which DECODES it — so the body would arrive as plain JSON
    # under a gzip header and the refusal would come from a confused decoder
    # rather than from the check under test. Here the wire bytes are genuinely
    # compressed and would decode perfectly well, so only the header check can be
    # what refuses them.
    compressed = gzip.compress(json.dumps(_body()).encode())

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            content=compressed,
            headers={"content-type": "application/json", "content-encoding": "gzip"},
        )

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    service = EntitlementService(client, url=_URL)
    with capture_logs() as logs, pytest.raises(DaintreeError) as exc:
        await _resolve(service)
    assert exc.value.code == "entitlement_unavailable"
    # The reason, not just the outcome: this must be the encoding refusal.
    assert any(
        line.get("reason") == "unexpected_content_encoding" for line in logs
    ), logs


@pytest.mark.asyncio
async def test_unknown_fields_are_ignored_for_forward_compatibility() -> None:
    """The website must be able to add a field without a lockstep release."""
    service, _ = _service(_json_response(_body(seats=4, tomorrows_field="whatever")))
    verdict = await _resolve(service)
    assert verdict.granted is True


# --- the cache -------------------------------------------------------------


@pytest.mark.asyncio
async def test_a_granted_answer_is_reused_within_its_ttl() -> None:
    service, recorder = _service(_json_response(_body()))
    await _resolve(service)
    await _resolve(service)
    assert len(recorder.requests) == 1


@pytest.mark.asyncio
async def test_the_cache_is_keyed_by_subject_and_oauth_client() -> None:
    """The same person through two OAuth clients is two grants.

    And — the point of keying on verified claims rather than credential text — two
    tokens for one session share an entry, so a leaked key is never a lookup
    handle.
    """
    service, recorder = _service(_json_response(_body()))
    await _resolve(service)
    await _resolve(service, token="a-completely-different-token")
    assert len(recorder.requests) == 1
    await _resolve(service, oauth_client_id="some-other-client")
    assert len(recorder.requests) == 2


@pytest.mark.asyncio
async def test_a_different_subject_gets_its_own_entry() -> None:
    other = uuid4()
    service, recorder = _service(
        _json_response(_body()), _json_response(_body(subject=str(other)))
    )
    await _resolve(service)
    await _resolve(service, user_id=other)
    assert len(recorder.requests) == 2


@pytest.mark.asyncio
async def test_an_expired_entry_is_refetched(monkeypatch: pytest.MonkeyPatch) -> None:
    clock = {"now": 1000.0}
    monkeypatch.setattr(
        "daintree_assistant_server.services.entitlement.time.monotonic",
        lambda: clock["now"],
    )
    service, recorder = _service(_json_response(_body()))
    await _resolve(service)
    clock["now"] += ENTITLEMENT_GRANTED_TTL_SECONDS + 1
    await _resolve(service)
    assert len(recorder.requests) == 2


@pytest.mark.asyncio
async def test_a_refusal_is_cached_for_a_shorter_window(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Somebody who has just paid should not wait out a cache to start working."""
    clock = {"now": 1000.0}
    monkeypatch.setattr(
        "daintree_assistant_server.services.entitlement.time.monotonic",
        lambda: clock["now"],
    )
    assert ENTITLEMENT_DENIED_TTL_SECONDS < ENTITLEMENT_GRANTED_TTL_SECONDS
    service, recorder = _service(
        _json_response(_body(access="subscription_required", plan_id=None))
    )
    await _resolve(service)
    clock["now"] += ENTITLEMENT_DENIED_TTL_SECONDS + 1
    await _resolve(service)
    assert len(recorder.requests) == 2


@pytest.mark.asyncio
@pytest.mark.parametrize("status", [401, 403, 429, 500])
async def test_nothing_uncertain_or_refused_is_ever_cached(status: int) -> None:
    service, recorder = _service(httpx.Response(status))
    for _ in range(3):
        with pytest.raises(DaintreeError):
            await _resolve(service)
    assert len(recorder.requests) == 3


@pytest.mark.asyncio
async def test_a_warm_grant_is_the_revocation_window_and_it_is_bounded(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Stated rather than wished away: a revocation is not seen while a grant is warm.

    One instance can keep authorizing for at most the positive TTL, because the
    cache is consulted before the network. That is the whole cost of caching at
    all, and the reason the TTL is a minute rather than an hour — the bound has to
    be short enough to be an acceptable answer on its own.
    """
    clock = {"now": 1000.0}
    monkeypatch.setattr(
        "daintree_assistant_server.services.entitlement.time.monotonic",
        lambda: clock["now"],
    )
    service, recorder = _service(_json_response(_body()), httpx.Response(401))
    await _resolve(service)

    # Inside the window: the revocation is invisible, and no request is made.
    clock["now"] += ENTITLEMENT_GRANTED_TTL_SECONDS - 1
    assert (await _resolve(service)).granted is True
    assert len(recorder.requests) == 1

    # Past it: the refusal arrives, and it takes the entry with it.
    clock["now"] += 2
    with pytest.raises(DaintreeError) as exc:
        await _resolve(service)
    assert exc.value.code == "auth_session_revoked"
    assert service._cache == {}
    assert len(recorder.requests) == 2


@pytest.mark.asyncio
@pytest.mark.parametrize("status", [401, 403, 429, 500])
async def test_a_rejection_purges_the_subject_s_entry(status: int) -> None:
    """So the window closes at the first refusal rather than at expiry.

    Without the purge, a refusal followed by a retry inside the TTL would be
    served the stale grant the refusal had just contradicted.
    """
    service, _ = _service(httpx.Response(status))
    # An entry past its TTL, seeded directly: present, so the purge has something
    # to remove, and expired, so the refusal is actually reached. An entry that
    # merely expires is NOT the same as one that is removed — a later refetch
    # inside a fresh TTL would otherwise find the contradicted grant still there.
    service._cache[(_SUBJECT, "daintree-assistant")] = _CacheEntry(
        verdict=Entitlement(
            access="granted",
            plan_id="standard",
            subscription_status="active",
            source="polar",
            stale=False,
            checked_at=datetime.now(UTC),
        ),
        expires_at=time.monotonic() - 1,
    )
    with pytest.raises(DaintreeError):
        await _resolve(service)
    assert service._cache == {}


@pytest.mark.asyncio
async def test_invalidate_drops_the_entry() -> None:
    service, recorder = _service(_json_response(_body()))
    await _resolve(service)
    service.invalidate(user_id=_SUBJECT, oauth_client_id="daintree-assistant")
    await _resolve(service)
    assert len(recorder.requests) == 2


# --- the bearer never becomes state ----------------------------------------


@pytest.mark.asyncio
async def test_the_success_log_carries_only_the_agreed_fields() -> None:
    """Captured through structlog, which is what this module actually logs to.

    `caplog` would pass on httpx's own stdlib request line without ever seeing
    `entitlement_resolved`, so the assertion has to be on the event dict.
    """
    service, _ = _service(_json_response(_body()))
    with capture_logs() as logs:
        await _resolve(service)
    (record,) = [line for line in logs if line["event"] == "entitlement_resolved"]
    assert set(record) == {
        "event",
        "log_level",
        "request_id",
        "subject_hash",
        "cached",
        "duration_ms",
        "access",
        "plan_id",
        "entitlement_source",
        "entitlement_stale",
    }
    # The one free-form field in the contract is carried on the verdict and
    # deliberately NOT logged.
    assert "subscription_status" not in record


@pytest.mark.asyncio
async def test_a_status_field_echoing_the_bearer_never_reaches_a_log() -> None:
    """The adversarial case for the one upstream-controlled string in the body.

    `subscription_status` is free-form text the website chooses. If it ever echoed
    the credential — some providers default a label to the key itself — logging it
    would put the bearer in the log line beside the account it belongs to.
    """
    hostile = _TOKEN[:60]
    service, _ = _service(_json_response(_body(subscription_status=hostile)))
    with capture_logs() as logs:
        verdict = await _resolve(service)
    assert hostile not in json.dumps(logs)
    # And DROPPED from the verdict, so it cannot be returned to the client either.
    # It is the one free-form field in the contract and it is rendered in a
    # terminal, so it is the route by which an upstream mistake would put a
    # credential fragment back on somebody's screen. Dropped rather than refused:
    # nothing branches on it, and turning a cosmetic upstream bug into a billing
    # outage would be the worse trade.
    assert verdict.subscription_status is None
    assert verdict.granted is True


@pytest.mark.asyncio
async def test_a_status_field_echoing_the_raw_subject_is_dropped() -> None:
    """Same hazard, different secret: this response is pasted into issues."""
    service, _ = _service(_json_response(_body(subscription_status=str(_SUBJECT))))
    verdict = await _resolve(service)
    assert verdict.subscription_status is None


@pytest.mark.asyncio
async def test_an_ordinary_status_survives() -> None:
    """The guard must not eat the field it is protecting."""
    service, _ = _service(_json_response(_body(subscription_status="past_due")))
    assert (await _resolve(service)).subscription_status == "past_due"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "response",
    [
        pytest.param(httpx.Response(401), id="revoked"),
        pytest.param(httpx.Response(500), id="upstream_error"),
        pytest.param(httpx.Response(200, content=b"{"), id="invalid_json"),
        pytest.param(httpx.ConnectError("refused"), id="transport_error"),
        pytest.param(httpx.ConnectTimeout("slow"), id="timeout"),
    ],
)
async def test_the_bearer_never_reaches_a_failure_path(
    response: httpx.Response | Exception,
) -> None:
    """Every route out of a failure, walked to the end.

    The exception GRAPH is the one that nearly leaked: an `httpx.RequestError`
    carries `.request`, whose headers hold the bearer, so `raise ... from exc`
    would have made the credential reachable from anything that renders a
    traceback. The chain is broken deliberately; this walks causes, contexts and
    attached requests to prove it.
    """
    service, _ = _service(response)
    with capture_logs() as logs, pytest.raises(DaintreeError) as exc:
        await _resolve(service)

    assert _TOKEN not in json.dumps(logs)
    assert _TOKEN not in str(exc.value)
    assert _TOKEN not in repr(exc.value.headers)

    seen: list[BaseException] = []
    node: BaseException | None = exc.value
    while node is not None and node not in seen:
        seen.append(node)
        assert _TOKEN not in repr(node)
        request = getattr(node, "request", None)
        assert request is None, f"{type(node).__name__} still carries a request"
        node = node.__cause__ or node.__context__


@pytest.mark.asyncio
async def test_the_verdict_has_nowhere_to_put_a_subject_or_a_credential() -> None:
    service, _ = _service(_json_response(_body()))
    verdict = await _resolve(service)
    rendered = repr(verdict) + repr(verdict.log_fields())
    assert str(_SUBJECT) not in rendered
    assert _TOKEN not in rendered


@pytest.mark.asyncio
async def test_the_cache_key_is_the_verified_subject_not_the_token() -> None:
    service, _ = _service(_json_response(_body()))
    await _resolve(service)
    ((user_id, client_id),) = service._cache.keys()
    assert user_id == _SUBJECT
    assert client_id == "daintree-assistant"

# --- ambiguous and non-conforming JSON -------------------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "raw",
    [
        pytest.param(
            b'{"version":1,"subject":"8f14e45f-ea8b-4c1d-9b2a-0000feedface",'
            b'"access":"granted","access":"subscription_required","plan_id":"standard",'
            b'"subscription_status":"active","source":"polar","stale":false,'
            b'"checked_at":"2026-08-25T12:00:00Z"}',
            id="two_conflicting_access_fields",
        ),
        pytest.param(
            b'{"version":1,"version":2,"subject":"8f14e45f-ea8b-4c1d-9b2a-0000feedface",'
            b'"access":"granted","plan_id":"standard","subscription_status":"active",'
            b'"source":"polar","stale":false,"checked_at":"2026-08-25T12:00:00Z"}',
            id="two_versions",
        ),
        pytest.param(
            b'{"version":1,"subject":"8f14e45f-ea8b-4c1d-9b2a-0000feedface",'
            b'"access":"granted","plan_id":"standard","subscription_status":"active",'
            b'"source":"polar","stale":false,"checked_at":"2026-08-25T12:00:00Z",'
            b'"seats":NaN}',
            id="non_json_constant_in_an_ignored_field",
        ),
    ],
)
async def test_an_ambiguous_body_is_refused_rather_than_resolved(raw: bytes) -> None:
    """Python keeps the LAST duplicate name, which would turn "the dependency sent
    two different answers" into a confident verdict about somebody's money.

    There is no safe way to guess which of two contradictory answers was meant, so
    the whole body is refused.
    """
    service, _ = _service(
        httpx.Response(200, content=raw, headers={"content-type": "application/json"})
    )
    with pytest.raises(DaintreeError) as exc:
        await _resolve(service)
    assert exc.value.code == "entitlement_unavailable"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "missing",
    ["version", "subject", "access", "plan_id", "source", "stale", "checked_at"],
)
async def test_a_missing_field_is_not_the_same_as_an_explicit_null(missing: str) -> None:
    """A dependency that answered and one that did not finish the sentence are
    different claims, and ``.get()`` alone cannot tell them apart."""
    payload = _body()
    payload.pop(missing)
    service, _ = _service(_json_response(payload))
    with pytest.raises(DaintreeError) as exc:
        await _resolve(service)
    assert exc.value.code == "entitlement_unavailable"


@pytest.mark.asyncio
async def test_a_malformed_denial_shaped_body_is_still_unavailable() -> None:
    """The direction that matters most.

    Every other malformed case starts from a granted payload, where refusing is
    the cautious answer anyway. This one is shaped like a REFUSAL — so a parser
    that shrugged and accepted it would deny a paying customer.
    """
    service, _ = _service(
        _json_response(_body(access="subscription_required", plan_id=None, version=9))
    )
    with pytest.raises(DaintreeError) as exc:
        await _resolve(service)
    assert exc.value.code == "entitlement_unavailable"
    assert exc.value.code != "subscription_required"


@pytest.mark.asyncio
async def test_deeply_nested_json_is_unavailable_rather_than_a_server_error() -> None:
    """`RecursionError` is not an `httpx.HTTPError`; the narrow catch missed it and
    it would have surfaced on a paid request as a 500."""
    raw = b'{"a":' * 20000 + b"1" + b"}" * 20000
    service, _ = _service(
        httpx.Response(200, content=raw, headers={"content-type": "application/json"})
    )
    with pytest.raises(DaintreeError) as exc:
        await _resolve(service)
    assert exc.value.code == "entitlement_unavailable"


@pytest.mark.asyncio
async def test_a_stream_failure_is_unavailable_rather_than_a_server_error() -> None:
    """`httpx.StreamError` inherits `RuntimeError`, not `HTTPError`."""
    service, _ = _service(httpx.StreamError("stream went away"))
    with pytest.raises(DaintreeError) as exc:
        await _resolve(service)
    assert exc.value.code == "entitlement_unavailable"


@pytest.mark.asyncio
async def test_a_closed_client_is_unavailable_rather_than_a_server_error() -> None:
    service, _ = _service(_json_response(_body()))
    await service.aclose()
    with pytest.raises(DaintreeError) as exc:
        await _resolve(service)
    assert exc.value.code == "entitlement_unavailable"


@pytest.mark.asyncio
async def test_cancellation_is_never_converted_into_a_verdict() -> None:
    """A disconnected client must keep unwinding.

    `CancelledError` is a `BaseException` in 3.12, so the broad `except Exception`
    does not catch it — and it must not, or an abandoned request would be recorded
    as a billing outage.
    """

    def cancel(request: httpx.Request) -> httpx.Response:
        raise asyncio.CancelledError

    client = httpx.AsyncClient(transport=httpx.MockTransport(cancel))
    service = EntitlementService(client, url=_URL)
    with pytest.raises(asyncio.CancelledError):
        await _resolve(service)


@pytest.mark.asyncio
async def test_a_slow_drip_response_is_bounded_by_wall_clock() -> None:
    """httpx's read timeout is the gap BETWEEN chunks, not a total budget.

    A host sending one byte every fraction of the read timeout stays inside it
    forever, so the deadline has to be on elapsed time.
    """

    async def drip() -> AsyncIterator[bytes]:
        for _ in range(1000):
            await asyncio.sleep(0.01)
            yield b" "

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=drip(), headers={"content-type": "application/json"})

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    service = EntitlementService(client, url=_URL, timeout=0.1)
    with pytest.raises(DaintreeError) as exc:
        await _resolve(service)
    assert exc.value.code == "entitlement_unavailable"


# --- an HTTP-date Retry-After ----------------------------------------------


@pytest.mark.asyncio
async def test_an_http_date_retry_after_is_converted() -> None:
    """The website may send this form; dropping it would discard the only real
    recovery estimate available.

    Carried on the ACCOUNT throttle, which is the only 429 whose delay is passed
    on at all — an opaque edge throttle's delay is about the whole instance and is
    dropped with the status.
    """
    when = format_datetime(datetime.now(UTC) + timedelta(seconds=45), usegmt=True)
    service, _ = _service(
        _json_response(
            {"error": {"code": "account_rate_limited"}},
            status=429,
            **{"retry-after": when},
        )
    )
    with pytest.raises(DaintreeError) as exc:
        await _resolve(service)
    assert 30 <= int(exc.value.headers["Retry-After"]) <= 50


@pytest.mark.asyncio
async def test_an_http_date_far_in_the_future_is_dropped() -> None:
    when = format_datetime(datetime.now(UTC) + timedelta(days=2), usegmt=True)
    service, _ = _service(
        _json_response(
            {"error": {"code": "account_rate_limited"}},
            status=429,
            **{"retry-after": when},
        )
    )
    with pytest.raises(DaintreeError) as exc:
        await _resolve(service)
    assert exc.value.code == "account_rate_limited"
    assert "Retry-After" not in exc.value.headers


# --- TTL contract, boundaries and bounding ----------------------------------


def test_the_ttl_contract_is_pinned_to_its_literals() -> None:
    """Derived assertions elsewhere would stay green if 60s became an hour.

    These numbers ARE the revocation window, so they are pinned as values rather
    than only as relationships.
    """
    assert ENTITLEMENT_GRANTED_TTL_SECONDS == 60.0
    assert ENTITLEMENT_DENIED_TTL_SECONDS == 15.0


@pytest.mark.asyncio
async def test_a_denial_is_reused_just_before_it_expires(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The negative-cache test above passes if denials are never cached at all."""
    clock = {"now": 1000.0}
    monkeypatch.setattr(
        "daintree_assistant_server.services.entitlement.time.monotonic",
        lambda: clock["now"],
    )
    service, recorder = _service(
        _json_response(_body(access="subscription_required", plan_id=None))
    )
    await _resolve(service)
    clock["now"] += ENTITLEMENT_DENIED_TTL_SECONDS - 0.5
    assert (await _resolve(service)).access == "subscription_required"
    assert len(recorder.requests) == 1


@pytest.mark.asyncio
async def test_an_entry_exactly_at_its_expiry_is_refetched(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The comparison is strict (`expires_at > now`), so equality must refetch."""
    clock = {"now": 1000.0}
    monkeypatch.setattr(
        "daintree_assistant_server.services.entitlement.time.monotonic",
        lambda: clock["now"],
    )
    service, recorder = _service(_json_response(_body()))
    await _resolve(service)
    clock["now"] += ENTITLEMENT_GRANTED_TTL_SECONDS
    await _resolve(service)
    assert len(recorder.requests) == 2


@pytest.mark.asyncio
async def test_the_cache_cannot_grow_without_bound() -> None:
    """Expiry is not a bound: an entry is examined only when its own key returns,
    so many distinct callers would leave dead entries for the process's life."""
    service, _ = _service(_json_response(_body()))
    # Seeded directly: the point is the eviction rule, not the fetch path.
    for index in range(ENTITLEMENT_CACHE_MAX_ENTRIES + 50):
        service._store(
            (UUID(int=index), "daintree-assistant"),
            _CacheEntry(
                verdict=Entitlement(
                    access="granted",
                    plan_id="standard",
                    subscription_status=None,
                    source="polar",
                    stale=False,
                    checked_at=datetime.now(UTC),
                ),
                expires_at=time.monotonic() + ENTITLEMENT_GRANTED_TTL_SECONDS,
            ),
        )
    assert len(service._cache) <= ENTITLEMENT_CACHE_MAX_ENTRIES


@pytest.mark.asyncio
async def test_a_concurrent_refusal_is_not_overwritten_by_an_older_grant() -> None:
    """Two requests for one account can miss together — this server runs at
    concurrency 8.

    If the second is refused and purges while the first is still in flight, the
    first must not then insert a fresh 60-second grant. That would fund turns on a
    session that had just been revoked, which is the exact failure the purge rule
    exists to prevent.
    """
    released = asyncio.Event()
    calls: list[int] = []

    async def slow_grant() -> AsyncIterator[bytes]:
        await released.wait()
        yield json.dumps(_body()).encode()

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(1)
        if len(calls) == 1:
            return httpx.Response(
                200, content=slow_grant(), headers={"content-type": "application/json"}
            )
        return httpx.Response(401)

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    service = EntitlementService(client, url=_URL)

    first = asyncio.create_task(_resolve(service))
    await asyncio.sleep(0)  # let the first call reach the transport
    with pytest.raises(DaintreeError) as exc:
        await _resolve(service)
    assert exc.value.code == "auth_session_revoked"

    released.set()
    await first  # the older grant still answers ITS caller...
    assert service._cache == {}  # ...but never resurrects the purged entry


# --- the smaller refusal paths ---------------------------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "value",
    [
        pytest.param("1" * 10_000, id="past_the_integer_conversion_limit"),
        pytest.param("", id="empty"),
        pytest.param("  ", id="whitespace"),
    ],
)
async def test_a_retry_after_we_cannot_read_is_dropped(value: str) -> None:
    """`isdigit()` is true for digit strings past the interpreter's conversion
    limit, where `int()` raises rather than answers.

    (Unicode digits — "٣٠" — satisfy `isdigit()` too, and are not testable here:
    an HTTP header cannot carry them, so that branch is unreachable over the
    wire and the guard is belt-and-braces rather than a live path.)

    Carried on the account throttle, the only 429 whose delay is read at all.
    """
    service, _ = _service(
        _json_response(
            {"error": {"code": "account_rate_limited"}},
            status=429,
            **{"retry-after": value},
        )
    )
    with pytest.raises(DaintreeError) as exc:
        await _resolve(service)
    assert exc.value.code == "account_rate_limited"
    assert "Retry-After" not in exc.value.headers


@pytest.mark.asyncio
async def test_an_account_throttle_without_a_retry_after_is_still_a_throttle() -> None:
    """The header is optional, and its absence says nothing about the verdict —
    the client's own backoff takes over."""
    service, _ = _service(
        _json_response({"error": {"code": "account_rate_limited"}}, status=429)
    )
    with pytest.raises(DaintreeError) as exc:
        await _resolve(service)
    assert exc.value.code == "account_rate_limited"
    assert "Retry-After" not in exc.value.headers


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "payload",
    [
        pytest.param({"error": "not an object"}, id="error_is_not_an_object"),
        pytest.param({"error": {"code": 7}}, id="code_is_not_a_string"),
        pytest.param(["not", "an", "object"], id="body_is_not_an_object"),
        pytest.param({}, id="no_error_key"),
    ],
)
async def test_a_403_without_a_readable_code_falls_back_to_the_broader_message(
    payload: Any,
) -> None:
    """The caller gets a correct answer either way and only loses the narrower of
    two messages."""
    service, _ = _service(_json_response(payload, status=403))
    with pytest.raises(DaintreeError) as exc:
        await _resolve(service)
    assert exc.value.code == "auth_client_not_allowed"


@pytest.mark.asyncio
async def test_a_403_with_an_unreadable_body_still_answers() -> None:
    service, _ = _service(httpx.Response(403, content=b"<html>nope</html>"))
    with pytest.raises(DaintreeError) as exc:
        await _resolve(service)
    assert exc.value.code == "auth_client_not_allowed"


@pytest.mark.asyncio
async def test_a_checked_at_from_days_ago_is_refused() -> None:
    """`stale: true` describes a cache hit at the website, not a licence to hand
    back a billing answer from last week."""
    old = (datetime.now(UTC) - timedelta(days=3)).isoformat()
    service, _ = _service(_json_response(_body(checked_at=old, source="cache", stale=True)))
    with pytest.raises(DaintreeError) as exc:
        await _resolve(service)
    assert exc.value.code == "entitlement_unavailable"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "value",
    [
        pytest.param("2026-W35-2T12:00:00Z", id="iso_week_date"),
        pytest.param("20260825T120000Z", id="basic_format"),
        pytest.param("2026-08-25", id="date_only"),
    ],
)
async def test_a_timestamp_outside_rfc_3339_is_refused(value: str) -> None:
    """`fromisoformat` accepts far more than the contract defines, and parsing
    something is not the same as the dependency sending what it agreed to."""
    service, _ = _service(_json_response(_body(checked_at=value)))
    with pytest.raises(DaintreeError) as exc:
        await _resolve(service)
    assert exc.value.code == "entitlement_unavailable"


@pytest.mark.asyncio
async def test_a_stale_cached_answer_is_accepted_when_it_is_recent() -> None:
    """`source: cache` with `stale: true` is a legal, useful answer — the website
    telling us its own lookup was not fresh. It must not be refused."""
    service, _ = _service(_json_response(_body(source="cache", stale=True)))
    verdict = await _resolve(service)
    assert verdict.granted is True
    assert verdict.stale is True
    assert verdict.source == "cache"


@pytest.mark.asyncio
async def test_the_pro_plan_is_accepted() -> None:
    service, _ = _service(_json_response(_body(plan_id="pro")))
    assert (await _resolve(service)).plan_id == "pro"


def test_eviction_prefers_entries_that_have_already_expired() -> None:
    """Losing a dead entry costs nothing; losing a live one costs a lookup."""
    service, _ = _service(_json_response(_body()))
    verdict = Entitlement(
        access="granted",
        plan_id="standard",
        subscription_status=None,
        source="polar",
        stale=False,
        checked_at=datetime.now(UTC),
    )
    live = (UUID(int=0), "daintree-assistant")
    service._cache[live] = _CacheEntry(verdict=verdict, expires_at=time.monotonic() + 300)
    for index in range(1, ENTITLEMENT_CACHE_MAX_ENTRIES + 1):
        service._cache[(UUID(int=index), "daintree-assistant")] = _CacheEntry(
            verdict=verdict, expires_at=time.monotonic() - 1
        )
    service._store(
        (UUID(int=99_999), "daintree-assistant"),
        _CacheEntry(verdict=verdict, expires_at=time.monotonic() + 300),
    )
    assert live in service._cache
    assert len(service._cache) <= ENTITLEMENT_CACHE_MAX_ENTRIES


def test_the_url_is_readable_for_diagnostics() -> None:
    """Read at call time from the service rather than captured elsewhere, so a
    caller reporting where a lookup went is reporting where it actually went."""
    service, _ = _service(_json_response(_body()))
    assert service.url == _URL


@pytest.mark.asyncio
async def test_a_verdict_with_no_status_passes_the_echo_guard_untouched() -> None:
    """The guard has an early exit for the ordinary case; it must not rewrite a
    verdict that has nothing to redact."""
    service, _ = _service(_json_response(_body(subscription_status=None)))
    verdict = await _resolve(service)
    assert verdict.subscription_status is None
    assert verdict.granted is True


# --- the freshness table ---------------------------------------------------
#
# `(source, stale, access)` is read as ONE closed combination, and these tests
# drive `_parse` DIRECTLY with an injected clock. Two reasons, both deliberate:
# a boundary that has to be reached by sleeping is a boundary nothing really
# tests, and `resolve()` maps every internal fault to the same public 503 — so a
# test that only asserts `entitlement_unavailable` cannot tell the rule firing
# from an accidental `KeyError` in the same function.


def _parse_body(*, now: datetime | None = None, **overrides: Any) -> Entitlement:
    """`_parse` on a body, at a chosen instant.

    `checked_at` defaults RELATIVE to the injected clock rather than to wall time,
    so a test that is not about freshness does not accidentally depend on it —
    `_body`'s own default is `datetime.now`, which is minutes or hours away from a
    frozen `now` and would refuse every body here for the wrong reason.
    """
    at = now or datetime.now(UTC)
    overrides.setdefault("checked_at", _stamp(at, timedelta(seconds=1)))
    return _parse(
        json.dumps(_body(**overrides)).encode("utf-8"),
        expected_subject=_SUBJECT,
        now=at,
    )


def _stamp(now: datetime, age: timedelta) -> str:
    return (now - age).isoformat().replace("+00:00", "Z")


_NOW = datetime(2026, 8, 26, 12, 0, 0, tzinfo=UTC)


@pytest.mark.parametrize(
    ("source", "stale", "access", "plan_id", "reason"),
    [
        # `polar` + stale — all three accesses.
        pytest.param("polar", True, "granted", "standard", "stale_without_cache", id="polar_stale"),
        pytest.param(
            "polar", True, "subscription_required", None, "stale_without_cache",
            id="polar_stale_required",
        ),
        pytest.param(
            "polar", True, "subscription_inactive", None, "stale_without_cache",
            id="polar_stale_inactive",
        ),
        # `cache` + fresh — all three accesses.
        pytest.param("cache", False, "granted", "standard", "cache_without_stale", id="cache_fresh"),
        pytest.param(
            "cache", False, "subscription_required", None, "cache_without_stale",
            id="cache_fresh_required",
        ),
        pytest.param(
            "cache", False, "subscription_inactive", None, "cache_without_stale",
            id="cache_fresh_inactive",
        ),
        # `cache` + stale — the two refusing accesses (granted is the valid cell).
        pytest.param(
            "cache", True, "subscription_required", None, "cached_denial", id="cached_required"
        ),
        pytest.param(
            "cache", True, "subscription_inactive", None, "cached_denial", id="cached_inactive"
        ),
    ],
)
def test_every_invalid_combination_names_itself(
    source: str, stale: bool, access: str, plan_id: str | None, reason: str
) -> None:
    """The CLASS and the reason, not the public code.

    `resolve()` collapses a contract error and an unexpected `KeyError` into the
    same 503, so asserting the status proves the rule exists only by coincidence.
    The reason is what reaches the log line an operator reads.
    """
    with pytest.raises(EntitlementContractError) as exc:
        _parse_body(
            now=_NOW,
            source=source,
            stale=stale,
            access=access,
            plan_id=plan_id,
            checked_at=_stamp(_NOW, timedelta(seconds=1)),
        )
    assert str(exc.value) == reason


def test_a_cached_grant_is_the_one_cache_combination_that_survives() -> None:
    """The website's outage fallback, and the reason the table is not simply
    "refuse anything cached"."""
    verdict = _parse_body(
        now=_NOW,
        source="cache",
        stale=True,
        checked_at=_stamp(_NOW, timedelta(hours=6)),
    )
    assert verdict.granted is True
    assert verdict.source == "cache"
    assert verdict.stale is True


@pytest.mark.asyncio
async def test_a_cached_denial_is_never_held_in_the_cache() -> None:
    """"Nothing uncertain is cached" has to cover a malformed 200, not just a 5xx.

    A cached denial is refused as unusable, so there is no verdict to keep — and
    keeping one would extend the misreading for its whole TTL. Three calls, three
    requests, empty cache.
    """
    body = _body(
        source="cache",
        stale=True,
        access="subscription_required",
        plan_id=None,
        checked_at=_stamp(datetime.now(UTC), timedelta(minutes=1)),
    )
    service, recorder = _service(_json_response(body))
    for _ in range(3):
        with pytest.raises(DaintreeError) as exc:
            await _resolve(service)
        assert exc.value.code == "entitlement_unavailable"
    assert len(recorder.requests) == 3
    assert service._cache == {}


@pytest.mark.asyncio
async def test_a_cached_denial_purges_a_grant_it_contradicts() -> None:
    """An unusable answer still INVALIDATES.

    The held grant is not evidence any more — the website has started saying
    something we cannot read, and continuing to serve the old verdict from memory
    would let a stale positive outlive the moment we stopped understanding the
    dependency.
    """
    body = _body(
        source="cache",
        stale=True,
        access="subscription_inactive",
        plan_id=None,
        checked_at=_stamp(datetime.now(UTC), timedelta(minutes=1)),
    )
    service, _ = _service(_json_response(body))
    key = service._key(user_id=_SUBJECT, oauth_client_id="daintree-assistant")
    service._cache[key] = _CacheEntry(
        verdict=Entitlement(
            access="granted",
            plan_id="pro",
            subscription_status="active",
            source="polar",
            stale=False,
            checked_at=datetime.now(UTC),
        ),
        # Already expired, so the lookup runs rather than being served from here.
        expires_at=time.monotonic() - 1,
    )
    with pytest.raises(DaintreeError):
        await _resolve(service)
    assert key not in service._cache


# --- the two age bounds ----------------------------------------------------


@pytest.mark.parametrize(
    "age",
    [
        pytest.param(timedelta(seconds=0), id="just_now"),
        pytest.param(timedelta(minutes=4, seconds=59), id="inside"),
        pytest.param(timedelta(minutes=5), id="exactly_at_the_bound"),
    ],
)
def test_a_live_answer_within_five_minutes_is_accepted(age: timedelta) -> None:
    """Inclusive at the bound: the contract says "no more than five minutes"."""
    assert _parse_body(now=_NOW, checked_at=_stamp(_NOW, age)).granted is True


@pytest.mark.parametrize(
    "age",
    [
        pytest.param(timedelta(minutes=5, microseconds=1), id="one_microsecond_over"),
        pytest.param(timedelta(minutes=5, seconds=1), id="just_outside"),
        pytest.param(timedelta(hours=20), id="a_working_day_old"),
    ],
)
def test_a_live_answer_older_than_five_minutes_is_refused(age: timedelta) -> None:
    """`source: polar` claims the website JUST asked the provider. Past five
    minutes that sentence is not true, and the answer is a cache that did not say
    so — twenty hours old was accepted under the single day-long limit this table
    replaced.
    """
    with pytest.raises(EntitlementContractError) as exc:
        _parse_body(now=_NOW, checked_at=_stamp(_NOW, age))
    assert str(exc.value) == "checked_at_stale"


@pytest.mark.parametrize(
    "age",
    [
        pytest.param(timedelta(hours=23, minutes=59), id="inside"),
        pytest.param(timedelta(seconds=86_400), id="the_websites_hard_maximum"),
        pytest.param(timedelta(seconds=86_400 + 299), id="inside_the_margin"),
        pytest.param(timedelta(seconds=86_400 + 300), id="exactly_at_the_margin"),
    ],
)
def test_a_cached_grant_within_a_day_plus_the_margin_is_accepted(age: timedelta) -> None:
    """The website's stale-positive maximum is 86,400 seconds exactly, so 86,400
    must be INSIDE the bound here or its published default races the boundary in
    transit — which is the defect this margin was agreed to close.
    """
    verdict = _parse_body(
        now=_NOW, source="cache", stale=True, checked_at=_stamp(_NOW, age)
    )
    assert verdict.granted is True


@pytest.mark.parametrize(
    "age",
    [
        pytest.param(timedelta(seconds=86_400 + 300, microseconds=1), id="one_microsecond_over"),
        pytest.param(timedelta(seconds=86_400 + 301), id="a_second_over"),
        pytest.param(timedelta(days=3), id="last_week"),
    ],
)
def test_a_cached_grant_past_the_margin_is_refused(age: timedelta) -> None:
    with pytest.raises(EntitlementContractError) as exc:
        _parse_body(now=_NOW, source="cache", stale=True, checked_at=_stamp(_NOW, age))
    assert str(exc.value) == "checked_at_stale"


def test_the_two_bounds_are_genuinely_different_limits() -> None:
    """A day-old answer is fine as a declared cache hit and refused as a live
    check. One undifferentiated age limit could not express that, which is why
    the constants are named for what they mean."""
    day_old = _stamp(_NOW, timedelta(hours=23))
    assert _parse_body(
        now=_NOW, source="cache", stale=True, checked_at=day_old
    ).granted is True
    with pytest.raises(EntitlementContractError):
        _parse_body(now=_NOW, source="polar", stale=False, checked_at=day_old)


@pytest.mark.parametrize(
    ("ahead", "accepted"),
    [
        pytest.param(timedelta(minutes=4, seconds=59), True, id="inside"),
        pytest.param(timedelta(minutes=5), True, id="exactly_at_the_bound"),
        pytest.param(timedelta(minutes=5, microseconds=1), False, id="one_microsecond_over"),
        pytest.param(timedelta(minutes=6), False, id="a_minute_over"),
    ],
)
def test_the_future_skew_bound_is_inclusive_at_five_minutes(
    ahead: timedelta, accepted: bool
) -> None:
    """Unchanged by the table, and pinned at the boundary here so an edit to the
    age bounds cannot quietly widen or tighten the other end.

    Without the exact and one-microsecond cells, flipping the implementation's
    `>` to `>=`, or widening the allowance to five and a half minutes, passes.
    """
    stamp = (_NOW + ahead).isoformat().replace("+00:00", "Z")
    if accepted:
        assert _parse_body(now=_NOW, checked_at=stamp).granted is True
    else:
        with pytest.raises(EntitlementContractError) as exc:
            _parse_body(now=_NOW, checked_at=stamp)
        assert str(exc.value) == "checked_at_future"


# --- the status byte cap ---------------------------------------------------


def test_the_status_cap_counts_bytes_not_characters() -> None:
    """The locked number is 64 BYTES, and the sanitizer already CROPS in bytes.

    While the refusal counted characters the two disagreed, and the disagreement
    had a direction: a 64-character multibyte status is 192 bytes, passed the
    check, and was then silently shortened to 64 bytes — repairing an
    over-contract body instead of refusing it, which is the one thing every other
    rule in `_parse` declines to do.
    """
    multibyte = "あ" * 64
    assert len(multibyte) == 64
    assert len(multibyte.encode("utf-8")) == 192
    with pytest.raises(EntitlementContractError) as exc:
        _parse_body(now=_NOW, subscription_status=multibyte)
    assert str(exc.value) == "subscription_status"


@pytest.mark.parametrize(
    ("status", "accepted"),
    [
        pytest.param("a" * 63, True, id="inside"),
        pytest.param("a" * 64, True, id="exactly_at_the_cap"),
        pytest.param("a" * 65, False, id="one_byte_over"),
        pytest.param("あ" * 21, True, id="multibyte_inside_63_bytes"),
        pytest.param("あ" * 22, False, id="multibyte_one_char_over_at_66_bytes"),
    ],
)
def test_the_status_cap_is_inclusive_at_sixty_four_bytes(status: str, accepted: bool) -> None:
    if accepted:
        verdict = _parse_body(now=_NOW, subscription_status=status)
        assert verdict.subscription_status == status
        # Whatever survives is <= 64 bytes, so it is also <= 64 CHARACTERS — which
        # is what `AccountResponse.subscription_status` bounds. The two layers
        # cannot disagree about a value that got this far.
        assert len(verdict.subscription_status or "") <= 64
    else:
        with pytest.raises(EntitlementContractError):
            _parse_body(now=_NOW, subscription_status=status)


