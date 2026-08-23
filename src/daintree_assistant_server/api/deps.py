"""Shared API dependencies and helpers."""

from __future__ import annotations

from fastapi import Request

from .. import PROTOCOL_MAX, PROTOCOL_MIN
from ..errors import DaintreeError, unsupported_protocol


def check_protocol_header(request: Request) -> None:
    """Reject an unsupported required protocol declared via header.

    Wired as a ``Depends`` rather than called inside the handler body, because
    FastAPI resolves dependencies BEFORE it validates the request body. A client
    one protocol ahead is likely to be sending a body shape this build cannot
    parse, and from inside the handler this check would never be reached — the
    caller would get a 400 blaming their fields instead of the 426 that tells them
    what is actually wrong. The header is a pre-parse declaration and has to be
    honoured as one.
    """
    header = request.headers.get("x-daintree-protocol")
    if header is None:
        return
    try:
        version = int(header)
    except ValueError as exc:
        raise unsupported_protocol(f"Invalid X-Daintree-Protocol header: {header!r}") from exc
    _check_version(version)


def check_protocol_value(version: int | None) -> None:
    """Reject an unsupported protocol version declared in the request body.

    Every caller passes a value pydantic already parsed as ``int`` (or ``None``,
    which ``/runbooks/resolve`` allows), so the old string-coercion branch was
    unreachable. Omitted stays permitted — only a present, out-of-range value fails.
    """
    if version is None:
        return
    _check_version(version)


def _check_version(version: int) -> None:
    if not PROTOCOL_MIN <= version <= PROTOCOL_MAX:
        raise unsupported_protocol(
            f"Daintree protocol version {version} is not supported "
            f"(supported: {PROTOCOL_MIN}..{PROTOCOL_MAX})."
        )


def request_id(request: Request) -> str:
    """The ONE correlation id for this request.

    The request-context middleware resolves it once — adopting a safe inbound
    ``X-Request-Id`` or minting one — stores it on ``request.state``, binds it to
    every structured log line, and sets it as the response header. Route code must
    read it through this helper rather than deriving an id of its own: a route that
    echoes a different value gives the tester an id that matches nothing in the
    logs, which is worse than no id at all — they quote it, and the operator's
    search comes back empty. ``/tasks`` did exactly that until it was fixed.

    **Raises rather than minting a replacement.** A minted-here id would go into
    the response header while the logs kept the middleware's, which is precisely
    the divergence this helper exists to prevent — the failure would look like a
    working request and only bite during a support conversation weeks later. Every
    real request traverses the middleware, so an absent id is a wiring bug (a route
    mounted outside the stack), and a 500 naming it is the correct outcome.
    """
    existing = getattr(request.state, "request_id", None)
    if isinstance(existing, str) and existing:
        return existing
    raise RuntimeError(
        "No request_id on request.state — this route is mounted outside "
        "RequestContextMiddleware, so its responses cannot be correlated with the logs."
    )


def require_ready(request: Request) -> None:
    if not getattr(request.app.state, "ready", False):
        raise DaintreeError(
            status_code=503,
            message="Service is not ready.",
            type="api_error",
            code="service_unavailable",
            headers={"Retry-After": "5"},
        )
