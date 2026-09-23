"""The Daintree ACCOUNT error family — who the caller is, and what their plan allows.

Which of these a deployment can RAISE depends on its posture: the identity codes
under ``AUTH_MODE=enforce``, the subscription and dependency codes under
``ENTITLEMENT_MODE=enforce``, and none of them under the shipped defaults. Two are
unreachable for a different reason — ``auth_permission_denied`` needs a second
OAuth client with a narrower grant, and ``usage_limit_reached`` needs numeric plan
allowances nobody has decided on.

They are pinned regardless because the taxonomy has a
CONSUMER — the CLI recognises all thirteen codes in
``internal/backend/account.go`` (with a typed remedy for each of the six
identity ones) — so a code reworded or restatused here silently
stops matching a client that is already written, and nothing would fail until the
day the producer landed.

The properties asserted are the ones a client's behaviour actually turns on: the
code string, the status, whether replaying can help, and whether the response
tells a client to go and get a credential.
"""

from __future__ import annotations

import re
from collections.abc import Callable
from pathlib import Path

import pytest

from daintree_assistant_server.contracts.errors import (
    TYPE_API,
    TYPE_AUTHENTICATION,
    TYPE_BILLING,
    TYPE_RATE_LIMIT,
)
from daintree_assistant_server.errors import (
    DaintreeError,
    account_rate_limited,
    auth_client_not_allowed,
    auth_dependency_unavailable,
    auth_permission_denied,
    auth_required,
    auth_session_revoked,
    auth_token_expired,
    auth_token_invalid,
    entitlement_unavailable,
    invalid_api_key,
    provider_insufficient_credits,
    provider_invalid_api_key,
    provider_key_forbidden,
    subscription_inactive,
    subscription_required,
    usage_accounting_unavailable,
    usage_limit_reached,
)

#: (factory, code, status, type, retryable). Written out rather than derived, so
#: ADDING a factory forces a decision about each of these rather than inheriting
#: whatever a loop happened to do.
ACCOUNT_ERRORS: list[tuple[Callable[[], DaintreeError], str, int, str, bool]] = [
    (auth_required, "auth_required", 401, TYPE_AUTHENTICATION, False),
    (auth_token_invalid, "auth_token_invalid", 401, TYPE_AUTHENTICATION, False),
    (auth_token_expired, "auth_token_expired", 401, TYPE_AUTHENTICATION, False),
    (auth_session_revoked, "auth_session_revoked", 401, TYPE_AUTHENTICATION, False),
    (auth_client_not_allowed, "auth_client_not_allowed", 403, TYPE_AUTHENTICATION, False),
    (auth_permission_denied, "auth_permission_denied", 403, TYPE_AUTHENTICATION, False),
    (subscription_required, "subscription_required", 402, TYPE_BILLING, False),
    (subscription_inactive, "subscription_inactive", 402, TYPE_BILLING, False),
    (usage_limit_reached, "usage_limit_reached", 429, TYPE_RATE_LIMIT, False),
    (account_rate_limited, "account_rate_limited", 429, TYPE_RATE_LIMIT, True),
    (auth_dependency_unavailable, "auth_dependency_unavailable", 503, TYPE_API, True),
    (entitlement_unavailable, "entitlement_unavailable", 503, TYPE_API, True),
    (usage_accounting_unavailable, "usage_accounting_unavailable", 503, TYPE_API, True),
]

_IDS = [code for _, code, _, _, _ in ACCOUNT_ERRORS]


@pytest.mark.parametrize(
    ("factory", "code", "status", "type_", "retryable"), ACCOUNT_ERRORS, ids=_IDS
)
def test_each_account_error_keeps_its_wire_contract(
    factory: Callable[[], DaintreeError], code: str, status: int, type_: str, retryable: bool
) -> None:
    err = factory()
    assert isinstance(err, DaintreeError)
    assert err.code == code
    assert err.status_code == status
    assert err.type == type_
    assert err.retryable is retryable


def test_every_account_code_is_distinct() -> None:
    """The whole point of thirteen codes rather than four statuses. Two of these
    sharing a code would make two different remedies indistinguishable — which is
    exactly what the two 403s would be if they collapsed, since they share a
    remedy and are told apart only by the code."""
    codes = [code for _, code, _, _, _ in ACCOUNT_ERRORS]
    assert len(set(codes)) == len(codes)


# ---------------------------------------------------------------------------
# Retryability, in both directions.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "factory",
    [auth_required, auth_token_invalid, auth_token_expired, auth_session_revoked,
     auth_client_not_allowed, auth_permission_denied, subscription_required,
     subscription_inactive, usage_limit_reached],
    ids=lambda f: f.__name__,
)
def test_a_verdict_about_the_caller_is_never_retryable(
    factory: Callable[[], DaintreeError],
) -> None:
    """Every one of these holds identically on replay. Two are actively expensive
    to get wrong: `usage_limit_reached` would spend the whole backoff budget
    re-deriving the same refusal, and the two 403s would refresh
    forever, minting a token wrong in exactly the same way each time.

    `auth_token_expired` is the subtle one. It IS fixable by a refresh — but the
    single refresh-and-replay belongs to the client's auth ladder, which knows
    whether anything has been shown to the user. A transport retry underneath it
    would multiply one deliberate replay into a loop against a credential nothing
    has re-minted.
    """
    assert factory().retryable is False


@pytest.mark.parametrize(
    "factory",
    [auth_dependency_unavailable, entitlement_unavailable, usage_accounting_unavailable],
    ids=lambda f: f.__name__,
)
def test_we_could_not_check_is_always_retryable(
    factory: Callable[[], DaintreeError],
) -> None:
    """The three 503s mean "we could not check", never "the answer is no".
    Presenting an outage as a verdict destroys a good session or tells a paying
    customer to buy again, so they must be replayed rather than believed."""
    assert factory().retryable is True


@pytest.mark.parametrize(
    "factory",
    [auth_dependency_unavailable, entitlement_unavailable, usage_accounting_unavailable],
    ids=lambda f: f.__name__,
)
def test_no_dependency_503_invents_a_retry_after(
    factory: Callable[[], DaintreeError],
) -> None:
    """A client treats a server-supplied `Retry-After` as AUTHORITATIVE and drops
    its own jittered backoff for it. A hardcoded value would therefore pin every
    client to one cadence for the length of an outage — synchronising the herd
    against the dependency that is already struggling, and capping the backoff
    exactly when it most needs to grow.

    The parameter exists for a caller that has a real recovery estimate.
    """
    assert "Retry-After" not in factory().headers


def test_a_dependency_503_forwards_a_real_recovery_estimate() -> None:
    assert auth_dependency_unavailable(retry_after="30").headers["Retry-After"] == "30"
    assert entitlement_unavailable(retry_after="30").headers["Retry-After"] == "30"
    assert usage_accounting_unavailable(retry_after="30").headers["Retry-After"] == "30"


def test_account_rate_limited_carries_an_upstream_retry_after() -> None:
    assert account_rate_limited(retry_after="30").headers["Retry-After"] == "30"
    assert "Retry-After" not in account_rate_limited().headers


# ---------------------------------------------------------------------------
# The WWW-Authenticate invariant, and the two errors it deliberately excludes.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "factory",
    [auth_required, auth_token_invalid, auth_token_expired, auth_session_revoked],
    ids=lambda f: f.__name__,
)
def test_every_account_401_challenges(factory: Callable[[], DaintreeError]) -> None:
    err = factory()
    assert err.status_code == 401
    assert err.headers.get("WWW-Authenticate")


@pytest.mark.parametrize(
    ("factory", "challenge"),
    [
        # No credential was presented, so there is no token to describe as
        # invalid — RFC 6750's `error` parameter would be a claim about something
        # that never arrived.
        (auth_required, "Bearer"),
        (auth_token_invalid, 'Bearer error="invalid_token"'),
        (auth_token_expired, 'Bearer error="invalid_token"'),
        (auth_session_revoked, 'Bearer error="invalid_token"'),
    ],
    ids=["auth_required", "auth_token_invalid", "auth_token_expired", "auth_session_revoked"],
)
def test_the_challenge_is_exactly_what_it_should_be(
    factory: Callable[[], DaintreeError], challenge: str
) -> None:
    """Asserted verbatim rather than by prefix: `startswith("Bearer")` accepts
    "Bearerish garbage", and a bare `Bearer` on an expired token would silently
    lose the one hint that tells a client a refresh is worth trying."""
    assert factory().headers["WWW-Authenticate"] == challenge


@pytest.mark.parametrize(
    "factory", [auth_client_not_allowed, auth_permission_denied], ids=lambda f: f.__name__
)
def test_the_403s_do_not_challenge(factory: Callable[[], DaintreeError]) -> None:
    """Both 403s carry a valid, current credential — what is wrong is the pairing of
    client and deployment, or the authority that credential holds. RFC 7235 reserves
    the challenge for 401, and offering one here would invite the refresh loop that
    cannot terminate.
    """
    err = factory()
    assert err.status_code == 403
    assert "WWW-Authenticate" not in err.headers


@pytest.mark.parametrize(
    "factory",
    [provider_invalid_api_key, provider_insufficient_credits, provider_key_forbidden],
    ids=lambda f: f.__name__,
)
def test_a_provider_credential_failure_never_challenges(
    factory: Callable[[], DaintreeError],
) -> None:
    """None of these may challenge, and none of them is a 401 any more.

    The provider-account codes describe the credential THIS SERVER sends
    upstream — an operator problem no caller can fix. A `WWW-Authenticate` on one
    would tell a client to refresh its Daintree login because the deployment's
    Together key is broken.

    `provider_invalid_api_key` used to be the sharp case: a 401 that deliberately
    carried no challenge, which is the one thing a 401 is for. That contradiction
    is what moved it to 503. The assertion is kept and widened rather than
    retired with it — the reason a challenge is wrong here is the ownership of
    the key, not the status, so it would still be wrong if some later change
    brought a 4xx back to this family.
    """
    error = factory()
    assert "WWW-Authenticate" not in error.headers
    assert error.status_code != 401, (
        "a provider-account failure is never our door refusing a caller credential"
    )


def test_our_door_and_the_provider_are_never_the_same_code() -> None:
    """Both families answer "a credential was rejected", and they name completely
    different credentials. A client that could not tell them apart would send a
    user round a sign-in loop for a key the user does not hold."""
    ours = {code for _, code, _, _, _ in ACCOUNT_ERRORS} | {invalid_api_key().code}
    theirs = {
        provider_invalid_api_key().code,
        provider_insufficient_credits().code,
        provider_key_forbidden().code,
    }
    assert not ours & theirs


# ---------------------------------------------------------------------------
# What reaches the caller.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("factory", [f for f, *_ in ACCOUNT_ERRORS], ids=_IDS)
def test_no_message_narrates_which_check_failed(
    factory: Callable[[], DaintreeError],
) -> None:
    """An unauthenticated caller must not be able to use error text to learn what
    this deployment accepts."""
    message = factory().message.lower()
    for leak in ("issuer", "audience", "signature", "jwks", "kid", "claim", "supabase"):
        assert leak not in message, (message, leak)


@pytest.mark.parametrize(
    "factory",
    [auth_required, auth_token_invalid, auth_token_expired, auth_session_revoked],
    ids=lambda f: f.__name__,
)
def test_an_identity_message_cannot_be_overridden_by_a_caller(
    factory: Callable[[], DaintreeError],
) -> None:
    """The scan above only covers the DEFAULTS, so on its own it is theatre: a
    factory that accepted a message would eventually be called with
    `str(jwt_exception)` and leak the issuer, the key id or the failing claim with
    the scan still green.

    These four take no message at all. The guard is the signature, not the text —
    which is why this asserts the signature.
    """
    with pytest.raises(TypeError):
        factory("anything")  # type: ignore[call-arg]


@pytest.mark.parametrize("factory", [f for f, *_ in ACCOUNT_ERRORS], ids=_IDS)
def test_the_response_carries_the_status_code_and_headers(
    factory: Callable[[], DaintreeError],
) -> None:
    """Through `to_response()`, which is what a client actually receives —
    `envelope()` alone is a projection that cannot show a wrong status or a
    dropped challenge header."""
    import json

    err = factory()
    response = err.to_response()
    assert response.status_code == err.status_code
    for name, value in err.headers.items():
        assert response.headers[name] == value
    detail = json.loads(bytes(response.body))["error"]
    assert detail["code"] == err.code
    assert detail["type"] == err.type
    assert detail["message"] == err.message


# ---------------------------------------------------------------------------
# Cross-repo: the CLI is the authority, and it got here first.
# ---------------------------------------------------------------------------

REPO_ROOT = Path(__file__).resolve().parents[2]
CLI_ACCOUNT_GO = REPO_ROOT / "cli" / "internal" / "backend" / "account.go"


#: `Code<Name> = "<code>" // <status>`, anchored to the start of a declaration
#: line so a commented-out leftover cannot satisfy the gate. The status comment is
#: the CLI's own record of the HTTP status and is REQUIRED: without it the
#: comparison below would silently skip, which is the failure mode a drift gate
#: can least afford. Digits are allowed in a code so a future `auth_token_v2`
#: cannot slip past the character class unseen.
_CLI_CODE_DECL = re.compile(
    r'^\s*(Code\w+)\s*=\s*"([a-z0-9_]+)"\s*//\s*(?:(\d{3})\b|(.*))$', re.MULTILINE
)

#: Declared locally by the CLI and never sent by any backend: it means that
#: process could not produce a credential, so no request was made at all.
_CLI_LOCAL_CODES = frozenset({"auth_credential_unavailable"})


def test_the_cli_and_the_backend_agree_on_every_account_code() -> None:
    """The two halves of one wire contract, checked against each other.

    This is the gate that matters most in this file, and it is the one that
    cannot be written from inside this repo alone: the CLI defined these codes
    first, and a rename here is invisible until a user hits the path months later.
    The other tests pin what we decided; this one pins that the other side still
    agrees.

    SKIPPED rather than failed when `cli/` is not populated, matching every other
    gate that reads the CLI source. **The pin is currently BEHIND** — it predates
    `account.go` — so this skips today and starts enforcing the moment the
    submodule moves, which is exactly when drift becomes possible. `TODO.md`
    carries the bump. Verified against the sibling checkout by hand when written:
    all thirteen codes and statuses agreed.
    """
    if not CLI_ACCOUNT_GO.exists():
        pytest.skip(
            "cli/internal/backend/account.go is absent — the submodule is not "
            "populated, or its pin predates the CLI's account taxonomy "
            "(git submodule update --init cli, then move the pin; see TODO.md)"
        )
    source = CLI_ACCOUNT_GO.read_text(encoding="utf-8")

    declared: dict[str, int] = {}
    unparsed: list[str] = []
    for match in _CLI_CODE_DECL.finditer(source):
        name, code, status, trailing = match.groups()
        if code in _CLI_LOCAL_CODES:
            continue
        if status is None:
            # A declaration whose comment stopped naming a status. Collected
            # rather than skipped: silently dropping it is precisely how a
            # restatused code would sail through.
            unparsed.append(f"{name} = {code!r} (comment {trailing.strip()!r} names no status)")
            continue
        declared[code] = int(status)

    assert not unparsed, (
        f"could not read an HTTP status for {unparsed} in account.go — the gate "
        "compares statuses, so an unreadable one is a hole, not a pass"
    )

    ours = {code: status for _, code, status, _, _ in ACCOUNT_ERRORS}

    missing_here = sorted(set(declared) - set(ours))
    assert not missing_here, (
        f"the CLI handles {missing_here} but this server can never emit them — "
        "either add the factory or the CLI is carrying dead branches"
    )
    missing_there = sorted(set(ours) - set(declared))
    assert not missing_there, (
        f"this server can emit {missing_there} and the CLI does not recognise them, "
        "so they would fall through to its generic handling with no remedy"
    )
    # Both directions above are subset checks, so an empty parse would satisfy
    # each of them. Pin the count too.
    assert len(declared) == len(ACCOUNT_ERRORS), (
        f"parsed {len(declared)} account codes out of account.go but this server "
        f"defines {len(ACCOUNT_ERRORS)} — the declaration syntax has probably moved"
    )
    mismatched = {
        code: (status, ours[code]) for code, status in declared.items() if ours[code] != status
    }
    assert not mismatched, (
        f"HTTP status disagreements (cli, ours): {mismatched} — a client branching "
        "on status would take the wrong path for each"
    )


def test_the_provider_key_failure_is_an_operator_fault_on_the_wire() -> None:
    """503, not the upstream's 401.

    A status is a statement to the CALLER about the CALLER's request. This one
    describes `OPENAI_API_KEY`, which the caller neither holds nor can replace,
    so forwarding the provider's 401 pointed a user at a credential that was
    never in question. 503 says what is actually true: this deployment cannot
    currently serve. It sits beside `provider_not_configured` — the same operator
    fault at two moments, never set and no longer accepted.
    """
    error = provider_invalid_api_key()
    assert error.status_code == 503
    assert error.type == TYPE_API
    assert error.code == "provider_invalid_api_key"


def test_the_operator_5xx_is_still_not_retryable() -> None:
    """The status moved and the retry posture did not, which is the whole reason
    this envelope says to branch on `code`.

    Every other 5xx in the taxonomy is retryable, so a client keying on the
    status class alone would now replay a request that cannot succeed until an
    operator rotates a key — burning its backoff budget to re-derive the same
    refusal. The CLI is safe because `deterministicUpstreamCodes` is consulted
    before any 5xx arm; this asserts the property that makes that necessary.
    """
    assert provider_invalid_api_key().retryable is False


def test_a_subscription_failure_is_billing_rather_than_authentication() -> None:
    """Authentication SUCCEEDED in both cases, and the type used to deny it.

    The codes always said the credential was good. A second consumer reading
    `authentication_error` could reasonably clear that credential and send the
    user back through sign-in — losing a working login on the way to a checkout
    page, which is the exact failure the split codes exist to prevent. The CLI
    branches on `code` and was never exposed to this; the type is now consistent
    for everything else.
    """
    for factory in (subscription_required, subscription_inactive):
        error = factory()
        assert error.type == TYPE_BILLING, error.code
        assert error.status_code == 402


def test_the_deployments_own_upstream_balance_is_not_the_callers_billing() -> None:
    """`provider_insufficient_credits` deliberately stays out of `billing_error`.

    It is a 402 like the subscription codes and it is not the same kind of fact:
    that type names the READER's billing state, and this one is the DEPLOYMENT's
    upstream balance. A consumer rendering it as billing would open the caller's
    own portal over an account they do not own. Pinned so the next person tidying
    the type column has to read the reason before making all three agree.
    """
    error = provider_insufficient_credits()
    assert error.type != TYPE_BILLING
    assert error.type == TYPE_AUTHENTICATION
