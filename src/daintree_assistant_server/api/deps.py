"""Shared API dependencies and helpers."""

from __future__ import annotations

from fastapi import Request

from .. import PROTOCOL_MAX, PROTOCOL_MIN
from ..errors import DaintreeError, unsupported_protocol


def check_protocol_header(request: Request) -> None:
    """Reject an unsupported required protocol declared via header."""
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
    which ``/skills/resolve`` allows), so the old string-coercion branch was
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


def require_ready(request: Request) -> None:
    if not getattr(request.app.state, "ready", False):
        raise DaintreeError(
            status_code=503,
            message="Service is not ready.",
            type="api_error",
            code="service_unavailable",
            headers={"Retry-After": "5"},
        )
