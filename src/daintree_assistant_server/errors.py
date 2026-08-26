"""Application error type and FastAPI handlers.

A :class:`DaintreeError` carries everything needed to render an OpenAI-style
error envelope with the correct HTTP status. Factory helpers cover the
mappings required by the specification (section 6.9).
"""

from __future__ import annotations

from typing import Any

from fastapi import Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException

from .contracts.errors import (
    TYPE_API,
    TYPE_AUTHENTICATION,
    TYPE_INVALID_REQUEST,
    TYPE_RATE_LIMIT,
    ErrorDetail,
    ErrorEnvelope,
)
from .observability import get_logger

logger = get_logger("daintree.errors")


class DaintreeError(Exception):
    """An error that maps directly to an OpenAI error envelope response."""

    def __init__(
        self,
        *,
        status_code: int,
        message: str,
        type: str = TYPE_INVALID_REQUEST,
        code: str | None = None,
        param: str | None = None,
        headers: dict[str, str] | None = None,
        retryable: bool = False,
    ) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.message = message
        self.type = type
        self.code = code
        self.param = param
        self.headers = headers or {}
        # Whether retrying the operation that raised this error may succeed. The
        # caller is responsible for retrying only while no model output or other
        # replay-unsafe side effect has been exposed.
        self.retryable = retryable

    def envelope(self) -> ErrorEnvelope:
        return ErrorEnvelope(
            error=ErrorDetail(
                message=self.message,
                type=self.type,
                param=self.param,
                code=self.code,
            )
        )

    def to_response(self) -> JSONResponse:
        return JSONResponse(
            status_code=self.status_code,
            content=self.envelope().model_dump(exclude_none=True),
            headers=self.headers,
        )


# --------------------------------------------------------------------------
# Factory helpers (section 6.9 mappings).
# --------------------------------------------------------------------------


def invalid_api_key(message: str = "Invalid or missing API key.") -> DaintreeError:
    return DaintreeError(
        status_code=401,
        message=message,
        type=TYPE_AUTHENTICATION,
        code="invalid_api_key",
        headers={"WWW-Authenticate": "Bearer"},
    )


def unsupported_protocol(message: str) -> DaintreeError:
    return DaintreeError(
        status_code=426,
        message=message,
        type=TYPE_INVALID_REQUEST,
        code="unsupported_daintree_protocol",
    )


def system_messages_not_allowed(param: str) -> DaintreeError:
    return DaintreeError(
        status_code=400,
        message=(
            "Client-supplied system messages are not allowed. Daintree Assistant "
            "system instructions are server-owned."
        ),
        type=TYPE_INVALID_REQUEST,
        code="system_messages_not_allowed",
        param=param,
    )


def reserved_tool_name(name: str, param: str) -> DaintreeError:
    return DaintreeError(
        status_code=400,
        message=f"Tool name {name!r} is reserved by Daintree and may not be supplied.",
        type=TYPE_INVALID_REQUEST,
        code="reserved_tool_name",
        param=param,
    )


def invalid_request(
    message: str, *, param: str | None = None, code: str | None = None
) -> DaintreeError:
    return DaintreeError(
        status_code=400,
        message=message,
        type=TYPE_INVALID_REQUEST,
        code=code or "invalid_request",
        param=param,
    )


def request_too_large(message: str, *, param: str | None = None) -> DaintreeError:
    return DaintreeError(
        status_code=413,
        message=message,
        type=TYPE_INVALID_REQUEST,
        code="request_too_large",
        param=param,
    )


def upstream_rate_limited(
    message: str = "The upstream model provider is rate limiting requests.",
    retry_after: str | None = None,
) -> DaintreeError:
    headers = {"Retry-After": retry_after} if retry_after else None
    return DaintreeError(
        status_code=429,
        message=message,
        type=TYPE_RATE_LIMIT,
        code="upstream_rate_limited",
        headers=headers,
        retryable=True,
    )


def upstream_timeout(message: str = "The upstream model provider timed out.") -> DaintreeError:
    return DaintreeError(
        status_code=504,
        message=message,
        type=TYPE_API,
        code="upstream_timeout",
        retryable=True,
    )


# --------------------------------------------------------------------------
# Daintree ACCOUNT failures — who the caller is, and what their plan allows.
#
# **These are not the provider-account family below, and the distinction is the
# whole reason both exist.** These describe the caller's Daintree login and
# subscription at OUR door; those describe the credential this server sends
# UPSTREAM, which is the operator's problem and which no caller can fix by
# signing in again. Collapsing them is how a broken deployment key ends up
# telling a user their session expired.
#
# The wire contract is not being invented here. The CLI already recognises every
# one of these codes (`internal/backend/account.go`), so a code reworded or
# restatused on this side silently stops matching a consumer that is already
# written. What it does with them varies by group and is worth being exact about:
# the six IDENTITY codes carry a typed `AuthRemedy` each, while the subscription,
# usage and dependency codes carry none — they are classified for retryability and
# for "is this the account's problem", and their handling is the client's to build.
# Three groups, and the grouping is the CLI's:
#
# * **Identity** (401/403) — who you are. Each has a DIFFERENT next step, which
#   is why they are not one code: sign in, refresh once, delete the credential,
#   or report a refusal no credential operation can lift. A single `401
#   unauthorized` would collapse them all into "log in again", and three of them
#   are refresh loops that can never succeed. The two 403s are the exception that
#   proves the rule: they SHARE a next step and are still two codes, because the
#   message differs and telling someone their OAuth client is not accepted when it
#   is sends them hunting a registration problem that does not exist.
# * **Subscription** (402) — authenticated, not entitled. Two codes because the
#   fixes differ: a first purchase versus the billing portal. Suggesting a second
#   checkout to someone whose card merely failed is how people pay twice.
# * **Dependency** (503) — we could not CHECK. Never a verdict. Presenting an
#   outage as "you are not subscribed" or "you are signed out" destroys a
#   perfectly good session over a blip, which is why these three are retryable
#   and the ones above are not. None of them sets a fixed `Retry-After`, and that
#   omission is deliberate: the CLI treats a server-supplied value as
#   AUTHORITATIVE and replaces its own jittered exponential schedule with it, so a
#   hardcoded "5" would pin every client on Earth to the same five-second cadence
#   for the duration of a dependency outage — synchronising the herd against the
#   dependency that is already struggling, and capping the backoff exactly when it
#   most needs to grow. The parameter is there for a caller that has a REAL
#   recovery estimate to pass on.
#
# The identity half of this path IS emitted now. `auth.py` dispatches on
# `AUTH_MODE`, and under `enforce` a request without a verified credential raises
# `auth_required`, `auth_token_invalid`, `auth_token_expired` or
# `auth_client_not_allowed` for real. What a deployment DOES with that is still a
# rollout decision — the shipped default is `open`, which reads no header at all —
# so "reachable" and "reached" are different questions here.
#
# The ENTITLEMENT half is emitted now as well, and by far less machinery than
# that took: Daintree does not hold subscription state, the WEBSITE does. So
# `services/entitlement.py` makes one bounded GET to `DAINTREE_ENTITLEMENT_URL`
# carrying the CALLER's own bearer and caches the verdict in process, and the
# database, the Redis and the Polar outbox an earlier plan wanted turned out to be
# for storing an answer somebody else already owns. `api/paid_access.py` dispatches
# `subscription_required` and `subscription_inactive` from that verdict, and both
# it and `api/daintree_account.py` raise `entitlement_unavailable` for every
# uncertainty — the one translation this taxonomy refuses to make is an outage into
# a verdict.
#
# `auth_session_revoked` is emitted too, from `services/entitlement.py`, but as a
# SIDE EFFECT rather than as a revocation check, and only on the strength of the
# WEBSITE's half of the contract: nothing here reaches back into a minted JWT, and
# what makes the code reachable is that the website's endpoint performs a fresh
# Supabase user check whose 401 maps to this. So `enforce` ALONE still means "a
# genuine, unexpired token" rather than "a live session"; it is the entitlement
# lookup that narrows the window, on the routes that make it.
#
# One is still defined and unreachable: `usage_limit_reached`. Nothing decrements
# a quota, because numeric plan allowances are an undecided product question
# (`TODO.md`) — a turn is ATTRIBUTED, not metered.
#
# All of them are defined here regardless, so the taxonomy, the documentation and
# the CLI's handling agree before anything depends on them. That is not
# speculative: the CLI already maps every one of these codes
# (`cli/internal/backend/account.go`), and
# `tests/unit/test_account_error_taxonomy.py` compares the two lists.
# --------------------------------------------------------------------------

#: A 401 challenge, per RFC 6750. `auth_required` gets the bare form because no
#: credential was presented, so there is no token to describe as invalid; the
#: rest name the reason, which is what makes the header useful rather than
#: ceremonial.
_BEARER_CHALLENGE = "Bearer"
_INVALID_TOKEN_CHALLENGE = 'Bearer error="invalid_token"'


def auth_required() -> DaintreeError:
    """401 — no bearer credential arrived at all.

    Distinct from ``auth_token_invalid`` because the remedy differs: there is
    nothing to refresh, so the only move is a sign-in. Never retried
    automatically — a replay of the same credential-free request is the same
    request.
    """
    return DaintreeError(
        status_code=401,
        message="This endpoint requires a Daintree account credential.",
        type=TYPE_AUTHENTICATION,
        code="auth_required",
        headers={"WWW-Authenticate": _BEARER_CHALLENGE},
    )


def auth_token_invalid() -> DaintreeError:
    """401 — malformed, wrong issuer/audience, or a bad signature.

    **Deliberately one code for all of those**, and deliberately vague on the
    wire. Which specific check failed is diagnostic detail about a credential we
    did not accept, and narrating it to an unauthenticated caller turns the
    endpoint into an oracle for probing what this deployment will take. The
    reason belongs in the server's own logs, where the request id joins it up.
    The message is therefore NOT overridable: a caller that could pass one would
    eventually pass ``str(exception)``, and the leak would arrive by accident
    rather than by decision.

    **One case that looks like it belongs here and does not:** a token whose
    ``client_id`` is outside this deployment's allowlist is
    ``auth_client_not_allowed``, never this. The distinction is load-bearing
    rather than tidy — this code tells a client to REFRESH, and refreshing a
    perfectly valid token from the wrong OAuth client mints another token wrong
    in exactly the same way, forever. Draw the line at the claim: a malformed,
    unverifiable or wrong-``aud`` token is invalid; a verified token from an
    unallowlisted client is not.
    """
    return DaintreeError(
        status_code=401,
        message="The account credential could not be verified.",
        type=TYPE_AUTHENTICATION,
        code="auth_token_invalid",
        headers={"WWW-Authenticate": _INVALID_TOKEN_CHALLENGE},
    )


def auth_token_expired() -> DaintreeError:
    """401 — the token verified but is past its expiry.

    Split from ``auth_token_invalid`` because it is the one identity failure a
    client can fix silently: refresh once and replay. That makes it the only one
    worth telling apart at all, and getting it wrong in either direction is
    expensive — a refresh loop on a genuinely dead credential, or a sign-in
    prompt for a token that needed one round trip.

    ``retryable`` stays False: the single refresh-and-replay belongs to the
    client's auth ladder, which knows whether anything has been shown to the user
    yet. A transport-level retry underneath it would multiply one deliberate
    replay into a loop against a credential nothing has re-minted.
    """
    return DaintreeError(
        status_code=401,
        message="The account credential has expired.",
        type=TYPE_AUTHENTICATION,
        code="auth_token_expired",
        headers={"WWW-Authenticate": _INVALID_TOKEN_CHALLENGE},
    )


def auth_session_revoked() -> DaintreeError:
    """401 — the signature is fine and the session is gone.

    A logout, a grant revocation or a security action ended it, which means the
    refresh token died with it. Refreshing is guaranteed to fail, so the only
    correct response is to delete the local credential — the reason this is not
    ``auth_token_expired``.
    """
    return DaintreeError(
        status_code=401,
        message="The session behind this credential no longer exists.",
        type=TYPE_AUTHENTICATION,
        code="auth_session_revoked",
        headers={"WWW-Authenticate": _INVALID_TOKEN_CHALLENGE},
    )


def auth_client_not_allowed(
    message: str = (
        "This credential was issued to an OAuth client this deployment does not accept."
    ),
) -> DaintreeError:
    """403 — a valid, fresh token from the wrong OAuth client.

    **403 and not 401, and no challenge header.** Both are deliberate. The
    credential is not the problem: it verified, it is current, and re-presenting
    it will keep verifying. What is wrong is the pairing of client and
    deployment, and a 401 with a `WWW-Authenticate` invites exactly the refresh
    loop that cannot terminate — every refresh mints another token that is wrong
    in the same way.

    The reason is that a refresh cannot help, NOT that the header is forbidden
    here: RFC 7235 requires a challenge on a 401 and does not prohibit one on
    other statuses. Getting that rationale right matters, because "the RFC says
    so" is the kind of justification a later edit overturns by reading the RFC.
    """
    return DaintreeError(
        status_code=403,
        message=message,
        type=TYPE_AUTHENTICATION,
        code="auth_client_not_allowed",
    )


def auth_permission_denied(
    message: str = "This credential is not permitted to perform this operation.",
) -> DaintreeError:
    """403 — a credential this deployment accepts, for an operation it may not do.

    Distinct from ``auth_client_not_allowed``, and the distinction is the message
    rather than the remedy. Both are 403s that a refresh cannot fix, so both would
    "work" as one code — but that code says the token came from an OAuth client
    this deployment does not accept, and telling someone that when their client IS
    accepted sends whoever reads the log looking for a configuration error that
    does not exist.

    Unreachable today: every allowlisted client holds the whole permission set, so
    only a second client with a narrower grant can produce this. Defined now
    because the alternative was to let the wrong code be correct-by-accident until
    that client existed.
    """
    return DaintreeError(
        status_code=403,
        message=message,
        type=TYPE_AUTHENTICATION,
        code="auth_permission_denied",
    )


def subscription_required(
    message: str = "This account does not have a plan that includes the Daintree Assistant.",
) -> DaintreeError:
    """402 — authenticated, and no product grants access.

    The login is fine and must be KEPT. A client that treated this as an auth
    failure would sign the user out on the way to the checkout page.
    """
    return DaintreeError(
        status_code=402,
        message=message,
        type=TYPE_AUTHENTICATION,
        code="subscription_required",
    )


def subscription_inactive(
    message: str = "This account's subscription does not currently grant access.",
) -> DaintreeError:
    """402 — a subscription exists and is not currently granting access.

    Payment failed, paused, or ended. Distinct from ``subscription_required``
    because the fix is the billing portal rather than a first purchase, and
    offering a fresh checkout to someone whose card merely bounced is how a
    person ends up paying for two subscriptions.
    """
    return DaintreeError(
        status_code=402,
        message=message,
        type=TYPE_AUTHENTICATION,
        code="subscription_inactive",
    )


def usage_limit_reached(
    message: str = "This account has used its plan's usage allowance for the current period.",
) -> DaintreeError:
    """429 — the plan's hard usage cap is spent.

    Shares its status with ``account_rate_limited`` and is its opposite: that one
    is transient and this one holds until the period rolls over or the plan
    changes. Not retryable, and that is the more expensive of the two mistakes to
    get wrong — replaying an exhausted quota spends the entire backoff budget to
    re-derive the same refusal.
    """
    return DaintreeError(
        status_code=429,
        message=message,
        type=TYPE_RATE_LIMIT,
        code="usage_limit_reached",
    )


def account_rate_limited(
    message: str = "Too many requests from this account.",
    retry_after: str | None = None,
) -> DaintreeError:
    """429 — this account is sending requests too quickly. Genuinely transient."""
    headers = {"Retry-After": retry_after} if retry_after else None
    return DaintreeError(
        status_code=429,
        message=message,
        type=TYPE_RATE_LIMIT,
        code="account_rate_limited",
        headers=headers,
        retryable=True,
    )


def auth_dependency_unavailable(
    message: str = "The account credential could not be checked right now.",
    retry_after: str | None = None,
) -> DaintreeError:
    """503 — we could not establish session validity safely.

    **This is not a logout.** The user is not signed out by it; we simply could
    not check, and converting "we don't know" into "you are signed out" deletes a
    perfectly good credential during an outage. Retryable for that reason.
    """
    return DaintreeError(
        status_code=503,
        message=message,
        type=TYPE_API,
        code="auth_dependency_unavailable",
        headers={"Retry-After": retry_after} if retry_after else None,
        retryable=True,
    )


def entitlement_unavailable(
    message: str = "The billing authority is unavailable and no recent result is usable.",
    retry_after: str | None = None,
) -> DaintreeError:
    """503 — we could not determine entitlement, and will not guess.

    **Must never be presented as "you are not subscribed."** A paying customer
    told to buy again during a billing outage is the single worst outcome this
    whole taxonomy exists to prevent, and it is why an unknown answer gets its
    own code rather than degrading into ``subscription_required``.
    """
    return DaintreeError(
        status_code=503,
        message=message,
        type=TYPE_API,
        code="entitlement_unavailable",
        headers={"Retry-After": retry_after} if retry_after else None,
        retryable=True,
    )


def usage_accounting_unavailable(
    message: str = "Usage could not be recorded durably, so the request was not started.",
    retry_after: str | None = None,
) -> DaintreeError:
    """503 — the durable usage write failed BEFORE any paid work began.

    Fails closed on purpose: work that cannot be accounted for is work billed to
    nobody, so the provider is never invoked rather than run unrecorded. It is
    safe to retry precisely because nothing was spent.

    **"Before" is the entire contract, and the name does not carry it.** The CLI
    retries this code unconditionally, including from a mid-stream error, on the
    strength of that promise. Raising it after the provider has run would make
    every replay a double-spend while the wire still said nothing was spent — the
    one bug in this family that costs real money. A post-work accounting failure
    is a DIFFERENT condition and needs its own code; it must not borrow this one
    because the wording happens to fit. The name cannot be improved to say so:
    the CLI already ships this exact string, so the constraint lives here, in the
    only place a future caller will read.
    """
    return DaintreeError(
        status_code=503,
        message=message,
        type=TYPE_API,
        code="usage_accounting_unavailable",
        headers={"Retry-After": retry_after} if retry_after else None,
        retryable=True,
    )


# --------------------------------------------------------------------------
# Provider account failures.
#
# These four used to collapse into one 502 ``upstream_error``, which meant a
# tester with an exhausted balance was told their credentials were rejected and
# reasonably concluded their key was wrong. They are four different problems with
# four different fixes, and the caller — who owns the key and pays the bill — is
# the only one who can apply any of them.
#
# None is retryable. Replaying a revoked key, an empty balance or a guardrail
# denial reaches the same answer, slower; on the streaming path it also burns the
# CLI's retry budget after `meta` has already committed a 200.
# --------------------------------------------------------------------------


def provider_invalid_api_key(
    message: str = (
        "The model provider rejected this deployment's API key. This is a server "
        "configuration problem; signing in again will not change it."
    ),
) -> DaintreeError:
    """Upstream 401 — the provider does not recognise the credential WE sent.

    The key is the server's own (``TOGETHER_API_KEY``), so this is an operator
    problem and no caller action can fix it. Deliberately 401 with a code
    distinct from our own door's 401s, and deliberately carrying no
    ``WWW-Authenticate``: a challenge here would tell a client to go and refresh
    a Daintree login because the deployment's provider key is broken. The CLI
    relies on that, excluding the provider-account codes from ``IsAuth()`` by
    code rather than by status.

    The 401 status is inherited from the upstream and is arguably the wrong
    outward-facing shape for an operator fault — ``TODO.md`` carries that, since
    changing it is a coordinated change with the CLI's own cost accounting.
    """
    return DaintreeError(
        status_code=401,
        message=message,
        type=TYPE_AUTHENTICATION,
        code="provider_invalid_api_key",
    )


def provider_insufficient_credits(
    message: str = (
        "The model provider account funding this deployment has insufficient credits. "
        "This is a server configuration problem, not an account or billing issue for "
        "the caller."
    ),
) -> DaintreeError:
    """Upstream 402 — the key is valid and the deployment's account cannot fund it.

    An operator problem, like every code in this group. Nothing the caller holds
    or buys affects it.
    """
    return DaintreeError(
        status_code=402,
        message=message,
        type=TYPE_AUTHENTICATION,
        code="provider_insufficient_credits",
    )


def provider_key_forbidden(
    message: str = (
        "The model provider refused this request for this deployment's API key — its "
        "model permissions, spend limits or guardrails. A server configuration problem."
    ),
) -> DaintreeError:
    """Upstream 403 — recognised, funded, and not permitted to do *this*.

    Covers a key scoped away from the model, a per-key spend cap, and a provider
    moderation/guardrail block. All are settings on the DEPLOYMENT's provider
    account — they were the caller's own when callers supplied the key, and that
    has not been true since the credential moved to the server.
    """
    return DaintreeError(
        status_code=403,
        message=message,
        type=TYPE_AUTHENTICATION,
        code="provider_key_forbidden",
    )


# --------------------------------------------------------------------------
# Upstream availability.
# --------------------------------------------------------------------------


def upstream_unavailable(
    message: str = "The upstream model provider is currently unavailable.",
) -> DaintreeError:
    """Provider outage / no capacity. Transient, so retryable."""
    return DaintreeError(
        status_code=503,
        message=message,
        type=TYPE_API,
        code="upstream_unavailable",
        retryable=True,
    )


# --------------------------------------------------------------------------
# Failures that are OUR bug, not the caller's.
# --------------------------------------------------------------------------


def provider_not_configured(provider: str) -> DaintreeError:
    """A named task asked for a provider this deployment did not configure.

    Distinct from :func:`upstream_unavailable`, which shares the 503 but means an
    outage — transient, and retryable. This one is a deployment fact: the
    credential for a SECOND, optional provider was never set, so the provider was
    never constructed (see ``main._build_openrouter_provider``).

    ``retryable=False`` governs OUR retry loop (``providers/retry.py``), which is
    the only consumer of the flag — it is deliberately not serialized, so
    ``ErrorDetail`` carries message/type/param/code and nothing else. What
    reaches a client is the distinct ``code``, which is the whole reason to mint
    one rather than reuse ``upstream_unavailable``: a client that retried this
    503 the way it should retry that one would replay a deterministic failure
    until its budget ran out.

    It is deliberately NOT a readiness failure. The optional provider funds one
    utility task; a deployment without it answers every turn and every other task
    normally, so the honest place to report the gap is the call that actually
    needed it — not /readyz, which would take the whole server down over a
    feature it is not using.

    The provider is NAMED because the fix is to set that provider's key, and an
    operator reading "not configured" with no subject has to go looking.
    """
    return DaintreeError(
        status_code=503,
        message=f"The {provider} provider is not configured on this server.",
        type=TYPE_API,
        code="provider_not_configured",
        retryable=False,
    )


def upstream_request_rejected(
    message: str = "The upstream model provider rejected this server's request.",
) -> DaintreeError:
    """The provider judged the body Daintree generated to be malformed.

    Almost always a server-side bug in what we sent. Permanent, and the useful
    action is a bug report carrying the request id — not anything the caller can
    change about their own account.
    """
    return DaintreeError(
        status_code=502,
        message=message,
        type=TYPE_API,
        code="upstream_request_rejected",
    )


def upstream_context_window_exceeded(
    message: str = "This turn does not fit the model's context window.",
) -> DaintreeError:
    """The provider rejected the request because the prompt was too long.

    Structurally this arrives as an ordinary 4xx — the same branch that produces
    ``upstream_request_rejected`` — and collapsing it there was the bug: "the
    provider rejected this server's request" tells a caller to file a bug report,
    when the fix is theirs and is simply to send less. The two are opposite
    diagnoses reached from the same status code, so only the upstream's own
    reason text can separate them.

    502 -> 413, because the fault is not the provider's and not a Daintree bug: a
    turn genuinely was too large. It shares its code with the check
    ``services/context_budget.py`` performs BEFORE the call, so a caller and an
    operator see one code whether the size was caught here or there, and neither
    has to know which side noticed.

    Deterministic, so not retryable: replaying an oversized prompt produces an
    oversized prompt.

    NO ``param``, unlike the check in ``services/context_budget.py``. That one
    measured the request and knows the transcript is what dominated it; this one
    only has the host's word that the total was too big, and the total includes
    the output reservation. A host rejecting ``inputs + max_new_tokens >
    max_total_tokens`` is describing a turn a smaller ``generation.max_tokens``
    would fix, so blaming ``input.messages`` would point a caller at the wrong
    field with the same confidence as the case where it is right.
    """
    return DaintreeError(
        status_code=413,
        message=message,
        type=TYPE_INVALID_REQUEST,
        code="context_window_exceeded",
    )


def upstream_protocol_error(
    message: str = "The upstream model provider returned a malformed response.",
) -> DaintreeError:
    """A response we could not parse: bad JSON, a broken chunk, a truncated stream.

    Distinct from ``upstream_request_rejected`` because the direction of the
    fault is opposite, and from ``upstream_unavailable`` because the provider did
    answer. Usually a provider/compatibility problem rather than an outage.
    """
    return DaintreeError(
        status_code=502,
        message=message,
        type=TYPE_API,
        code="upstream_protocol_error",
    )


def turn_deadline_exceeded(phase: str, message: str | None = None) -> DaintreeError:
    """Daintree's own end-to-end budget ran out — not the upstream's.

    Distinct from ``upstream_timeout`` on purpose. That one means the provider
    went quiet; this one means the turn as a whole outran the budget this server
    sets, which is a different diagnosis and often a different fix (a smaller
    request, or a Daintree bug). ``phase`` says *where* it ran out: preparation
    means the model was never even reached.

    Retryable, because the usual cause is a transient stall rather than anything
    about the request — but the client is responsible for not replaying a turn
    whose visible content has already committed.
    """
    return DaintreeError(
        status_code=504,
        message=message or f"The turn exceeded its {phase} deadline.",
        type=TYPE_API,
        code="turn_deadline_exceeded",
        retryable=True,
    )


def upstream_runaway_output(
    message: str = "The upstream model produced a runaway response and was stopped.",
) -> DaintreeError:
    """A generation that ran away and was cut off mid-stream by the server.

    Distinct from ``upstream_protocol_error``: the chunks parsed fine and the
    endpoint was healthy by every transport measure. What failed is the CONTENT
    — a degenerating generation that had already produced more visible prose
    than any real answer to a turn ever does, and showed no sign of stopping.

    Terminal on purpose, rather than closing the stream with a ``done``. The
    text already delivered is unusable, and a ``done`` would invite the client to
    commit it to conversation history — where it becomes the prefix that makes
    the NEXT turn degenerate too. An error lets the client discard the round.
    """
    return DaintreeError(
        status_code=502,
        message=message,
        type=TYPE_API,
        code="upstream_runaway_output",
    )


def upstream_error(
    message: str = "The upstream model provider returned an error.",
    *,
    retryable: bool = False,
) -> DaintreeError:
    return DaintreeError(
        status_code=502,
        message=message,
        type=TYPE_API,
        code="upstream_error",
        retryable=retryable,
    )


def internal_error(message: str = "An internal error occurred.") -> DaintreeError:
    return DaintreeError(
        status_code=500,
        message=message,
        type=TYPE_API,
        code="internal_error",
    )


def unknown_task(task: str) -> DaintreeError:
    return DaintreeError(
        status_code=404,
        message=f"Unknown task profile {task!r}.",
        type=TYPE_INVALID_REQUEST,
        code="unknown_task",
        param="task",
    )


# --------------------------------------------------------------------------
# FastAPI exception handlers.
# --------------------------------------------------------------------------


def _validation_param(loc: tuple[Any, ...]) -> str | None:
    """Render a Pydantic error location as an OpenAI-style ``param`` path."""
    if not loc:
        return None
    parts = [p for p in loc if p != "body"]
    if not parts:
        return None
    out = ""
    for part in parts:
        if isinstance(part, int):
            out += f"[{part}]"
        else:
            out += f".{part}" if out else str(part)
    return out


async def daintree_error_handler(_request: Request, exc: DaintreeError) -> JSONResponse:
    return exc.to_response()


async def validation_error_handler(_request: Request, exc: RequestValidationError) -> JSONResponse:
    first = exc.errors()[0] if exc.errors() else {}
    param = _validation_param(tuple(first.get("loc", ())))
    message = first.get("msg", "Invalid request.")
    err = DaintreeError(
        status_code=400,
        message=str(message),
        type=TYPE_INVALID_REQUEST,
        code="invalid_request_error",
        param=param,
    )
    return err.to_response()


async def http_exception_handler(_request: Request, exc: StarletteHTTPException) -> JSONResponse:
    err = DaintreeError(
        status_code=exc.status_code,
        message=str(exc.detail),
        type=TYPE_INVALID_REQUEST if exc.status_code < 500 else TYPE_API,
        code="http_error",
        headers=dict(exc.headers or {}),
    )
    return err.to_response()


async def unhandled_exception_handler(request: Request, exc: Exception) -> JSONResponse:
    """The 500 of last resort — and the response most likely to be reported.

    This handler runs in Starlette's OUTERMOST middleware, above the request-context
    middleware. By the time it is reached that middleware has unwound: its
    contextvars are cleared, so the bound request id is no longer attached to log
    lines automatically, and its ``return response`` never ran, so nothing has set
    the response header. Left alone, the one failure a tester is certain to report
    is the one they can quote no id for, and whose log line an operator cannot find.

    The id survives on ``request.state`` (the ASGI scope outlives the middleware
    frame), so both are recovered here explicitly. It is read defensively rather
    than through ``api.deps.request_id``: this handler must not raise, and a
    request that failed before the middleware ran has no id to recover.
    """
    correlation = getattr(request.state, "request_id", None)
    if not isinstance(correlation, str) or not correlation:
        correlation = None
    # Log the failure (route + exception class + stack) without request content.
    logger.error(
        "unhandled_exception",
        request_id=correlation,
        path=request.url.path,
        method=request.method,
        exc_type=type(exc).__name__,
        exc_info=exc,
    )
    response = internal_error().to_response()
    if correlation:
        response.headers["X-Request-Id"] = correlation
    return response


def register_exception_handlers(app: Any) -> None:
    """Wire all error handlers onto a FastAPI application."""
    app.add_exception_handler(DaintreeError, daintree_error_handler)
    app.add_exception_handler(RequestValidationError, validation_error_handler)
    app.add_exception_handler(StarletteHTTPException, http_exception_handler)
    app.add_exception_handler(Exception, unhandled_exception_handler)
