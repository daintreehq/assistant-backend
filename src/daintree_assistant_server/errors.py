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


def reserved_message_name(name: str, param: str) -> DaintreeError:
    """A client message wearing a name the SERVER authors messages under.

    The sibling of ``reserved_tool_name``, and reserved for the same reason: the
    backend appends its own rows to a prepared request (the forked-spawn
    manifest and branch assignment), and those rows carry authority the
    conversation must not be able to claim for itself. A user typing the same
    words into ordinary content is harmless; a message arriving already NAMED as
    one of them is a client trying to address machinery it cannot see.
    """
    return DaintreeError(
        status_code=400,
        message=f"Message name {name!r} is reserved by Daintree and may not be supplied.",
        type=TYPE_INVALID_REQUEST,
        code="reserved_message_name",
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
    message: str = "The model provider rejected this API key. Replace or rotate it.",
) -> DaintreeError:
    """Upstream 401 — the provider does not recognise the caller's credential.

    Deliberately 401 with a code distinct from our own ``invalid_api_key``. Both
    mean "fix your key", but ours means the header was missing or malformed at
    Daintree's door, and this one means the key travelled fine and the provider
    refused it. A client that cannot tell them apart cannot write correct copy
    for either.
    """
    return DaintreeError(
        status_code=401,
        message=message,
        type=TYPE_AUTHENTICATION,
        code="provider_invalid_api_key",
    )


def provider_insufficient_credits(
    message: str = (
        "The model provider account backing this API key has insufficient credits. "
        "Add credits or use a different key."
    ),
) -> DaintreeError:
    """Upstream 402 — the key is valid and the account cannot fund the call."""
    return DaintreeError(
        status_code=402,
        message=message,
        type=TYPE_AUTHENTICATION,
        code="provider_insufficient_credits",
    )


def provider_key_forbidden(
    message: str = (
        "The model provider refused this request for this API key. Check the key's "
        "model permissions, spend limits and guardrails."
    ),
) -> DaintreeError:
    """Upstream 403 — recognised, funded, and not permitted to do *this*.

    Covers a key scoped away from the model, a per-key spend cap, and a provider
    moderation/guardrail block. All are settings on the caller's own account.
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


def fork_plan_invalid(detail: str) -> DaintreeError:
    """The model's internal fork plan is not one the backend can act on.

    FAILS CLOSED, deliberately. Every field in a fork plan either names prose or
    selects a mutation target — which worktree, which agent, whether a
    supervisor watches — and the server has no standing to guess any of the
    second kind on the user's behalf. A 502 rather than a 400 because the fault
    is the model's, not the caller's: the request was well-formed and the reply
    was not.
    """
    return DaintreeError(
        status_code=502,
        message=f"The model produced an unusable forked-spawn plan: {detail}.",
        type=TYPE_API,
        code="fork_plan_invalid",
    )


def fork_call_not_alone(call_count: int) -> DaintreeError:
    """The internal fork call arrived alongside other tool calls.

    A reply is ONE assistant decision. Some siblings of a fork call may be
    prerequisites of it and others independent, and there is no way to tell
    which from here — so running half the batch is not a safer outcome than
    running none of it. Exposing the siblings while dropping the fork would be
    the worst of the three: the model would continue believing it had spawned
    four agents that do not exist.
    """
    return DaintreeError(
        status_code=502,
        message=(
            f"The model mixed the internal forked-spawn call with {call_count - 1} other "
            "tool call(s); it must be the only call in the reply."
        ),
        type=TYPE_API,
        code="fork_call_not_alone",
    )


def fork_call_not_offered() -> DaintreeError:
    """The model called the internal fork function on a turn that never offered it.

    Low-probability and not theoretical: a user can quote the name in prose, and
    model output is not a security boundary. What makes it worth failing over is
    that the alternative is RELAYING a reserved name — which the client is
    forbidden from replaying, so the turn after this one would 400 on Daintree's
    own rule, stranding the session rather than one reply.

    Whether to EXPAND a fork call depends on whether the tool was offered.
    Whether to EMIT one never does.
    """
    return DaintreeError(
        status_code=502,
        message=(
            "The model called an internal Daintree function that was not offered on "
            "this request."
        ),
        type=TYPE_API,
        code="fork_call_not_offered",
    )


def fork_target_unavailable() -> DaintreeError:
    """The spawn tool the fork call expands into is not in the prepared request.

    Unreachable through the normal gate, which refuses to offer the fork tool
    without its target present. Reaching it means assembly and rewrite disagree
    about what was offered, and the alternative to failing is fabricating four
    calls the client has no way to execute.
    """
    return DaintreeError(
        status_code=502,
        message="The forked-spawn target tool is not available on this request.",
        type=TYPE_API,
        code="fork_target_unavailable",
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
