"""Shared API dependencies and helpers."""

from __future__ import annotations

from fastapi import Request

from .. import PROTOCOL_MAX, PROTOCOL_MIN
from ..errors import DaintreeError, invalid_request, unsupported_protocol


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


def check_protocol_value(version: object) -> None:
    """Reject an unsupported (or malformed) protocol declared in the request body."""
    if version is None:
        return
    if isinstance(version, int) and not isinstance(version, bool):
        _check_version(version)
        return
    try:
        parsed = int(str(version).strip())
    except (TypeError, ValueError) as exc:
        raise invalid_request(
            f"Invalid protocol_version: {version!r}.",
            param="daintree.protocol_version",
            code="invalid_protocol_version",
        ) from exc
    _check_version(parsed)


def _check_version(version: int) -> None:
    if not PROTOCOL_MIN <= version <= PROTOCOL_MAX:
        raise unsupported_protocol(
            f"Daintree protocol version {version} is not supported "
            f"(supported: {PROTOCOL_MIN}..{PROTOCOL_MAX})."
        )


def require_daintree_fields(raw: dict[str, object]) -> None:
    """Enforce the minimum Daintree-aware contract on a present extension.

    A generic OpenAI client omits ``daintree`` entirely and stays in stateless
    compatibility mode (this is never called for it). But once a client *opts
    in* by sending a ``daintree`` object, it must identify the session and turn
    so a signed state token is never issued against an empty/unspecified
    session and the selector cadence has a real turn id to key on.

    ``instruction_revision`` and ``round`` remain optional (they default to 0),
    so the CLI is not forced to send a round counter on every request.
    """
    if raw.get("protocol_version") is None:
        raise invalid_request(
            "daintree.protocol_version is required when the daintree extension is present.",
            param="daintree.protocol_version",
            code="protocol_version_required",
        )
    check_protocol_value(raw.get("protocol_version"))
    for field in ("session_id", "turn_id"):
        value = raw.get(field)
        if not isinstance(value, str) or not value.strip():
            raise invalid_request(
                f"daintree.{field} is required and must be a non-empty string when the "
                "daintree extension is present.",
                param=f"daintree.{field}",
                code=f"{field}_required",
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
