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

# Error ``type`` values (OpenAI taxonomy, plus one).
TYPE_INVALID_REQUEST = "invalid_request_error"
TYPE_AUTHENTICATION = "authentication_error"
TYPE_RATE_LIMIT = "rate_limit_error"
TYPE_API = "api_error"

#: Authentication SUCCEEDED and the account is not entitled to the work.
#:
#: Not part of the OpenAI taxonomy this envelope otherwise borrows, which has no
#: billing bucket at all — and borrowing a taxonomy is not a reason to describe a
#: paying customer's expired card as an authentication failure. Before this
#: existed, ``subscription_required`` and ``subscription_inactive`` were typed
#: ``authentication_error``, which contradicts their own codes: the caller
#: proved who they were, and a second consumer reading the type could
#: reasonably take it as licence to clear the credential and send them back
#: through sign-in — the exact failure the split codes exist to prevent. The
#: type now agrees with the code instead of arguing with it.
TYPE_BILLING = "billing_error"


class ErrorDetail(BaseModel):
    """The inner error object."""

    message: str
    type: str
    param: str | None = None
    code: str | None = None


class ErrorEnvelope(BaseModel):
    """The top-level error wrapper."""

    error: ErrorDetail
