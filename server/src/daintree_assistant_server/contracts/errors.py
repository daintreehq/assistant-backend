"""The OpenAI-style error envelope wire model.

Every JSON error returned by the service uses this shape::

    {
      "error": {
        "message": "Human-readable message",
        "type": "invalid_request_error",
        "param": "messages[0].role",
        "code": "system_messages_not_allowed"
      }
    }
"""

from __future__ import annotations

from pydantic import BaseModel

# Error ``type`` values (OpenAI taxonomy).
TYPE_INVALID_REQUEST = "invalid_request_error"
TYPE_AUTHENTICATION = "authentication_error"
TYPE_RATE_LIMIT = "rate_limit_error"
TYPE_API = "api_error"


class ErrorDetail(BaseModel):
    """The inner error object."""

    message: str
    type: str
    param: str | None = None
    code: str | None = None


class ErrorEnvelope(BaseModel):
    """The top-level error wrapper."""

    error: ErrorDetail
