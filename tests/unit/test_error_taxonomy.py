"""The upstream-failure taxonomy.

Every code here answers one question: **whose problem is this, and what do they
do about it?** Before the split, five different answers collapsed into one 502
`upstream_error`, so a tester with an exhausted balance was told their
credentials were rejected — and replaced a perfectly good key.

The CLI branches on `error.code`, never on prose, so these codes and their
retryability are a wire contract. `docs/DAINTREE_API.md` and
`docs/PROVIDER_POLICY.md` section 6 are the documented half.
"""

from __future__ import annotations

import httpx
import pytest
from pydantic import SecretStr

from daintree_assistant_server.errors import DaintreeError
from daintree_assistant_server.providers.openrouter import OpenRouterProvider

_BASE = "https://together.test/v1"
_KEY = "tgp_v1_taxonomy"


def _provider(handler) -> OpenRouterProvider:  # type: ignore[no-untyped-def]
    transport = httpx.MockTransport(handler)
    client = httpx.AsyncClient(transport=transport, timeout=httpx.Timeout(5.0))
    return OpenRouterProvider(client, api_key=SecretStr(_KEY), base_url=_BASE)


def _request():  # type: ignore[no-untyped-def]
    from daintree_assistant_server.providers.base import ProviderChatRequest

    return ProviderChatRequest(model="m", messages=[{"role": "user", "content": "hi"}])


async def _fail_with(status: int, message: str = "boom") -> DaintreeError:
    def handler(request: httpx.Request) -> httpx.Response:
        # The FLAT shape Cerebras actually returns — no {"error": {...}} wrapper.
        # The nested shape is covered separately (an intermediary could still
        # produce it), but the default here is what production sees.
        return httpx.Response(
            status, json={"message": message, "type": "invalid_request_error"}
        )

    provider = _provider(handler)
    try:
        with pytest.raises(DaintreeError) as exc:
            await provider.complete(_request())
        return exc.value
    finally:
        await provider.aclose()


# ---------------------------------------------------------------------------
# The full status -> code table.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("status", "expected_status", "code", "retryable"),
    [
        # The one row where the outward status deliberately differs from the
        # upstream one. The key is the SERVER's, so forwarding a 401 told a
        # caller to re-authenticate over a credential they do not hold.
        (401, 503, "provider_invalid_api_key", False),
        (402, 402, "provider_insufficient_credits", False),
        (403, 403, "provider_key_forbidden", False),
        # 404 is a HOST fault again, and the reason is the executor fallback.
        # It was `upstream_request_rejected` while nothing sent a routing policy
        # — a 404 could then only mean a wrong model id, our bug. The fallback
        # adapter sends `provider: {ignore, require_parameters}`, whose filters
        # CAN empty the eligible pool, so "no endpoint matches" is a real 404
        # again. Both readings of a 404 here (emptied pool, model deployment
        # down) are facts about a host rather than about our body — the body was
        # never parsed to produce one — so it is retryable, and, crucially,
        # `providers/failover.py` will now try the second host on it. Under the
        # old mapping the error most likely to mean "this host is down" was the
        # one guaranteed NOT to fail over.
        (404, 503, "upstream_unavailable", True),
        (408, 504, "upstream_timeout", True),
        (429, 429, "upstream_rate_limited", True),
        (500, 503, "upstream_unavailable", True),
        (502, 503, "upstream_unavailable", True),
        (400, 502, "upstream_request_rejected", False),
        (422, 502, "upstream_request_rejected", False),
    ],
)
async def test_status_maps_to_a_distinct_actionable_code(
    status: int, expected_status: int, code: str, retryable: bool
) -> None:
    err = await _fail_with(status)
    assert err.status_code == expected_status
    assert err.code == code
    assert err.retryable is retryable


@pytest.mark.asyncio
async def test_every_mapped_code_is_distinct() -> None:
    """The whole point of the split. If two upstream conditions produce the same
    code, the CLI cannot give different advice for them.

    404 is deliberately absent from this list: it shares ``upstream_unavailable``
    with 500, and correctly so — both mean the host could not serve this, and the
    advice for both is the same (retry, and let the fallback try the other host).
    Distinctness is only valuable where two conditions need DIFFERENT advice.
    """
    codes = set()
    for status in (401, 402, 403, 429, 500, 400):
        codes.add((await _fail_with(status)).code)
    assert len(codes) == 6


# ---------------------------------------------------------------------------
# Deterministic failures must not be retried.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize("status", [401, 402, 403, 400])
async def test_deterministic_failures_are_not_retryable(status: int) -> None:
    """Replaying a revoked key, an empty balance, a guardrail denial or a request
    the upstream rejected reaches the same answer, slower — and on the streaming
    path it burns the CLI's retry budget after `meta` has already committed a
    200.

    404 LEFT this list when the executor fallback landed: a missing route is a
    fact about a host, not a deterministic verdict on our body."""
    assert (await _fail_with(status)).retryable is False


@pytest.mark.asyncio
@pytest.mark.parametrize("status", [429, 500, 503, 408])
async def test_transient_failures_stay_retryable(status: int) -> None:
    err = await _fail_with(status, message="upstream had a moment")
    assert err.retryable is True


# ---------------------------------------------------------------------------
# 5xx is an outage, full stop.
#
# There is no longer a second reading to disambiguate. Against a router, "nothing
# matched your preferences" arrived as EITHER a 404 or a 503 and the message was
# the only thing separating a policy dead-end from a real outage — an entire
# marker-matching layer existed for that one ambiguity. A single-host provider
# has no pool to empty, so every 5xx is simply the upstream being unwell, and the
# ambiguity (with the code that resolved it) is gone rather than defanged.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "message",
    [
        "Service temporarily unavailable",
        # Prose that USED to be classified as a permanent routing dead-end. It
        # must now stay a retryable outage: this is the regression that would
        # silently kill a retry which would have succeeded.
        "No endpoints found matching your data policy.",
        "No allowed providers are available for the selected model",
    ],
)
async def test_every_503_is_a_retryable_outage(message: str) -> None:
    err = await _fail_with(503, message)
    assert err.code == "upstream_unavailable"
    assert err.retryable is True


@pytest.mark.asyncio
async def test_a_nested_error_body_is_still_read() -> None:
    """The other error shape.

    Cerebras returns a flat body, but an intermediary (a proxy, a mock, a future
    gateway) may still wrap it the OpenAI way. Reading only one shape would throw
    the entire diagnostic away — silently, since the status still maps fine.
    """

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(400, json={"error": {"message": "temperature too high"}})

    provider = _provider(handler)
    try:
        with pytest.raises(DaintreeError) as exc:
            await provider.complete(_request())
    finally:
        await provider.aclose()
    assert "temperature too high" in exc.value.message


@pytest.mark.asyncio
async def test_a_flat_error_body_reason_reaches_the_client() -> None:
    """The shape production actually sees, asserted on the message that escapes.

    A 400 here is almost always a bug in what this server sent, and the
    upstream's own words name the offending field. Losing them turns a
    one-line fix into a bisect.
    """
    err = await _fail_with(400, "max_tokens: property is unsupported")
    assert "max_tokens: property is unsupported" in err.message


# ---------------------------------------------------------------------------
# Nothing leaks through the account-failure branch.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize("status", [401, 402, 403])
async def test_account_failures_echo_no_upstream_prose_or_credential(
    status: int,
) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            status,
            json={"error": {"message": f"account detail for key {_KEY}, org acme-corp"}},
        )

    provider = _provider(handler)
    with pytest.raises(DaintreeError) as exc:
        await provider.complete(_request())
    message = exc.value.message
    assert _KEY not in message
    assert "acme-corp" not in message
    assert "account detail" not in message
    await provider.aclose()


@pytest.mark.asyncio
async def test_a_malformed_request_does_surface_the_upstream_reason() -> None:
    """The opposite trade from the account branch: this is our bug, and the
    provider's own complaint is the entire diagnostic."""
    err = await _fail_with(400, "'items' must be a schema object, got list.")
    assert "'items' must be a schema object" in err.message


# ---------------------------------------------------------------------------
# Our door vs the provider's rejection.
# ---------------------------------------------------------------------------


def test_our_door_and_the_provider_are_different_codes_and_now_different_statuses() -> None:
    """Two "a credential was rejected" answers about two different credentials.

    They used to share a 401 and be told apart only by `code` — which is the
    contract, and which held. But the provider one describes `OPENAI_API_KEY`,
    the server's own, and a 401 is the status that tells a caller to
    authenticate: it was pointing at a credential the caller does not hold and
    could not replace. It was also the only 401 on this surface deliberately
    carrying no `WWW-Authenticate`, and a 401 that must not challenge is a sign
    it was never a 401.

    Now they differ by status too, so the two are distinguishable even by a
    consumer doing the thing this envelope tells it not to do.
    """
    from daintree_assistant_server.errors import (
        invalid_api_key,
        provider_invalid_api_key,
    )

    ours, theirs = invalid_api_key(), provider_invalid_api_key()
    assert ours.status_code == 401
    assert theirs.status_code == 503
    assert ours.code == "invalid_api_key"
    assert theirs.code == "provider_invalid_api_key"
    # Ours tells the client to fix its header; a challenge on the provider's
    # rejection would wrongly invite re-sending the same credential — and a 5xx
    # has no business carrying one at all.
    assert "WWW-Authenticate" in ours.headers
    assert "WWW-Authenticate" not in theirs.headers


# ---------------------------------------------------------------------------
# In-band SSE errors use the SAME taxonomy.
#
# Most turns are streamed, so this is the path that matters most. Before it
# shared `_map_status`, the streaming path had its own two-outcome world — rate
# limited, or a generic `upstream_error` — which recreated the exact bug the
# taxonomy exists to fix: an invalid key, a spent balance and a routing dead-end
# all arrived as one retryable-looking code, and the CLI replayed each of them.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("err", "code", "retryable"),
    [
        ({"code": 401, "message": "no"}, "provider_invalid_api_key", False),
        ({"code": 402, "message": "no"}, "provider_insufficient_credits", False),
        ({"code": 403, "message": "no"}, "provider_key_forbidden", False),
        ({"code": 404, "message": "no"}, "upstream_unavailable", True),
        ({"code": 429, "message": "slow down"}, "upstream_rate_limited", True),
        ({"code": 500, "message": "boom"}, "upstream_unavailable", True),
        ({"code": 400, "message": "bad body"}, "upstream_request_rejected", False),
        # String codes appear in the wild too.
        ({"code": "402", "message": "no"}, "provider_insufficient_credits", False),
    ],
)
def test_inline_stream_errors_map_like_http_errors(
    err: dict, code: str, retryable: bool
) -> None:
    from daintree_assistant_server.providers.openrouter import map_stream_error

    mapped = map_stream_error(err)
    assert mapped.code == code
    assert mapped.retryable is retryable


def test_an_inline_503_stays_an_outage_whatever_it_says() -> None:
    """The in-band path must reach the same verdict as the HTTP one.

    It shares ``_map_status``, so this is really a guard against someone
    reintroducing prose-matching on only one of the two paths — which is how the
    streaming and non-streaming taxonomies drifted apart the first time.
    """
    from daintree_assistant_server.providers.openrouter import map_stream_error

    mapped = map_stream_error(
        {"code": 503, "message": "No endpoints found matching your data policy."}
    )
    assert mapped.code == "upstream_unavailable"
    assert mapped.retryable is True


# A symbolic in-band auth code (`{"code": "wrong_api_key"}`, no HTTP status) used
# to be pinned here. That was a CEREBRAS wire behaviour and it is gone with the
# adapter: Together's in-band errors carry numeric codes, which `_stream_error_status`
# already reads, and asserting a prose branch this adapter does not implement would
# have been a test of a fiction. The hazard the case named — a non-retryable auth
# failure degrading into a retryable generic error, so the CLI replays a doomed
# request — is still covered by the numeric cases above.


def test_an_inline_quota_message_is_not_treated_as_a_rate_limit() -> None:
    """The specific regression this guards.

    "quota" used to be a rate-limit marker, so an exhausted balance arriving
    in-band became a retryable 429 — the turn replayed a doomed request and told
    the user to wait rather than to add credits.
    """
    from daintree_assistant_server.providers.openrouter import map_stream_error

    mapped = map_stream_error({"message": "insufficient credits for this request"})
    assert mapped.code == "provider_insufficient_credits"
    assert mapped.retryable is False


def test_an_inline_rate_limit_without_a_status_is_still_recognised() -> None:
    from daintree_assistant_server.providers.openrouter import map_stream_error

    mapped = map_stream_error({"message": "Rate limit exceeded, too many requests"})
    assert mapped.code == "upstream_rate_limited"
    assert mapped.retryable is True


def test_an_unrecognised_inline_error_stays_a_generic_upstream_error() -> None:
    from daintree_assistant_server.providers.openrouter import map_stream_error

    assert map_stream_error({"message": "something odd"}).code == "upstream_error"


@pytest.mark.parametrize("code", [True, False, 99, 600, "4o2", "", None, {"x": 1}])
def test_a_nonsense_inline_code_falls_back_to_prose(code: object) -> None:
    """A bool is an int in Python — `code: true` must not read as HTTP 1."""
    from daintree_assistant_server.providers.openrouter import _stream_error_status

    assert _stream_error_status({"code": code}) is None


# ---------------------------------------------------------------------------
# The credential never escapes, whatever the upstream echoes back.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_an_echoed_credential_is_scrubbed_from_the_diagnostic() -> None:
    """The one way the server's key could leak.

    A 400's upstream reason is carried through into the client-facing error
    envelope, because it names the offending field and is the whole diagnostic.
    That path is only safe because the credential is scrubbed out of it first.
    """
    err = await _fail_with(400, f"bad request from {_KEY}")
    assert _KEY not in err.message
    assert "[REDACTED]" in err.message


@pytest.mark.asyncio
async def test_a_doubly_encoded_echo_drops_the_diagnostic_wholesale() -> None:
    """A literal scrub cannot catch every encoding, so it is backed by a check.

    Here one decode pass still hides the key from an exact match. Losing a rare
    error message is the right trade against leaking a key, so the detail is
    dropped rather than published.
    """
    from daintree_assistant_server.providers.openrouter import _CREDENTIAL_ECHOED

    escaped = "".join(f"\\u{ord(c):04x}" for c in _KEY)
    err = await _fail_with(400, f"rejected: {escaped}")
    assert _KEY not in err.message
    assert _CREDENTIAL_ECHOED in err.message
