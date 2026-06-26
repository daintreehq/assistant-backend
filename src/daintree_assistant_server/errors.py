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
        # Whether retrying the operation that raised this error may succeed.
        # Only meaningful before any public bytes have been sent.
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


def unsupported_parameter(name: str, message: str | None = None) -> DaintreeError:
    return DaintreeError(
        status_code=400,
        message=message or f"Parameter {name!r} is not supported.",
        type=TYPE_INVALID_REQUEST,
        code="unsupported_parameter",
        param=name,
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


def model_not_found(model: str) -> DaintreeError:
    return DaintreeError(
        status_code=404,
        message=f"Model {model!r} does not exist.",
        type=TYPE_INVALID_REQUEST,
        code="model_not_found",
        param="model",
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


def metadata_too_large() -> DaintreeError:
    return DaintreeError(
        status_code=500,
        message="Daintree response metadata exceeds the header size limit.",
        type=TYPE_API,
        code="daintree_metadata_too_large",
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
    # Log the failure (route + exception class + stack) without request content.
    logger.error(
        "unhandled_exception",
        path=request.url.path,
        method=request.method,
        exc_type=type(exc).__name__,
        exc_info=exc,
    )
    return internal_error().to_response()


def register_exception_handlers(app: Any) -> None:
    """Wire all error handlers onto a FastAPI application."""
    app.add_exception_handler(DaintreeError, daintree_error_handler)
    app.add_exception_handler(RequestValidationError, validation_error_handler)
    app.add_exception_handler(StarletteHTTPException, http_exception_handler)
    app.add_exception_handler(Exception, unhandled_exception_handler)
