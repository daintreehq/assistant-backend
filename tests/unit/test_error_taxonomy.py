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
from daintree_assistant_server.providers.openrouter import (
    OpenRouterProvider,
    _looks_unroutable,
)

_BASE = "https://openrouter.test/api/v1"
_KEY = "sk-or-v1-taxonomy"


def _provider(handler) -> OpenRouterProvider:  # type: ignore[no-untyped-def]
    transport = httpx.MockTransport(handler)
    client = httpx.AsyncClient(transport=transport, timeout=httpx.Timeout(5.0))
    return OpenRouterProvider(client, api_key=SecretStr(_KEY), base_url=_BASE)


def _request():  # type: ignore[no-untyped-def]
    from daintree_assistant_server.providers.base import ProviderChatRequest

    return ProviderChatRequest(model="m", messages=[{"role": "user", "content": "hi"}])


async def _fail_with(status: int, message: str = "boom") -> DaintreeError:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(status, json={"error": {"message": message}})

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
        (401, 401, "provider_invalid_api_key", False),
        (402, 402, "provider_insufficient_credits", False),
        (403, 403, "provider_key_forbidden", False),
        (404, 503, "upstream_no_compliant_provider", False),
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
    code, the CLI cannot give different advice for them."""
    codes = set()
    for status in (401, 402, 403, 404, 429, 500, 400):
        codes.add((await _fail_with(status)).code)
    assert len(codes) == 7


# ---------------------------------------------------------------------------
# Deterministic failures must not be retried.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize("status", [401, 402, 403, 404, 400])
async def test_deterministic_failures_are_not_retryable(status: int) -> None:
    """Replaying a revoked key, an empty balance, a guardrail denial or an empty
    routing pool reaches the same answer, slower — and on the streaming path it
    burns the CLI's retry budget after `meta` has already committed a 200."""
    assert (await _fail_with(status)).retryable is False


@pytest.mark.asyncio
@pytest.mark.parametrize("status", [429, 500, 503, 408])
async def test_transient_failures_stay_retryable(status: int) -> None:
    err = await _fail_with(status, message="upstream had a moment")
    assert err.retryable is True


# ---------------------------------------------------------------------------
# 503: routing dead-end vs real outage.
#
# OpenRouter answers "nothing matched your preferences" with EITHER 404 or 503,
# so on the 503 branch the message is the only thing separating a policy dead-end
# from an outage.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "detail",
    [
        "No endpoints found matching your data policy.",
        "No allowed providers are available for the selected model",
        "There is no available model provider that meets your routing requirements.",
        "NO ENDPOINTS FOUND matching your data policy",
    ],
)
def test_no_route_messages_are_recognised(detail: str) -> None:
    assert _looks_unroutable(detail) is True


@pytest.mark.parametrize(
    "detail",
    [
        None,
        "",
        "Service temporarily unavailable",
        "upstream connect error or disconnect/reset before headers",
        "The model is overloaded, please try again",
    ],
)
def test_ordinary_outage_messages_are_not_mistaken_for_a_routing_dead_end(
    detail: str | None,
) -> None:
    """Biased toward the outage reading on purpose.

    Mistaking an outage for a policy problem sends an operator to inspect a
    routing policy that is fine — and, worse, marks a transient failure permanent,
    killing a retry that would have succeeded.
    """
    assert _looks_unroutable(detail) is False


@pytest.mark.asyncio
async def test_503_with_a_no_route_message_is_a_routing_failure() -> None:
    err = await _fail_with(503, "No endpoints found matching your data policy.")
    assert err.code == "upstream_no_compliant_provider"
    assert err.retryable is False


@pytest.mark.asyncio
async def test_503_without_one_is_an_outage_and_stays_retryable() -> None:
    err = await _fail_with(503, "Service temporarily unavailable")
    assert err.code == "upstream_unavailable"
    assert err.retryable is True


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
# Our 401 vs the provider's 401.
# ---------------------------------------------------------------------------


def test_our_door_401_and_the_provider_401_are_different_codes() -> None:
    from daintree_assistant_server.errors import (
        invalid_api_key,
        provider_invalid_api_key,
    )

    ours, theirs = invalid_api_key(), provider_invalid_api_key()
    assert ours.status_code == theirs.status_code == 401
    assert ours.code == "invalid_api_key"
    assert theirs.code == "provider_invalid_api_key"
    # Ours tells the client to fix its header; a WWW-Authenticate challenge on the
    # provider's rejection would wrongly invite re-sending the same credential.
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
        ({"code": 404, "message": "no"}, "upstream_no_compliant_provider", False),
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


def test_an_inline_503_no_route_is_a_routing_failure() -> None:
    from daintree_assistant_server.providers.openrouter import map_stream_error

    mapped = map_stream_error(
        {"code": 503, "message": "No endpoints found matching your data policy."}
    )
    assert mapped.code == "upstream_no_compliant_provider"


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
# The no-route matcher requires a COMBINATION, not one loose phrase.
# ---------------------------------------------------------------------------


def test_no_endpoints_found_alone_is_not_conclusive() -> None:
    """An outage can borrow the same words.

    Classifying this as a policy dead-end would mark a transient failure
    permanent and kill a retry that would have succeeded.
    """
    assert (
        _looks_unroutable("No endpoints found because the service is temporarily down")
        is False
    )


def test_no_endpoints_found_with_a_policy_reason_is_conclusive() -> None:
    assert _looks_unroutable("No endpoints found matching your data policy.") is True


def test_a_withheld_credential_diagnostic_reads_as_an_outage() -> None:
    """When the credential-scrubbing path replaces the detail wholesale, we know
    nothing about the cause — and the safe reading is the retryable one."""
    from daintree_assistant_server.providers.openrouter import _CREDENTIAL_ECHOED

    assert _looks_unroutable(_CREDENTIAL_ECHOED) is False
