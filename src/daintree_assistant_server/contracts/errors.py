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


# --------------------------------------------------------------------------
# Error ``code`` values with a CROSS-MODULE consumer.
#
# Most codes are written once, by the factory that raises them, and read only by
# the caller receiving the envelope — a literal at the raise site is the clearest
# thing there and these constants are deliberately not a registry of all of them.
#
# These four are different. ``providers/failover.py`` classifies a failure by
# comparing ``DaintreeError.code`` against :data:`~..providers.failover.
# REQUEST_FAULT_CODES`, so the spelling in the factory and the spelling in that
# set must agree EXACTLY or the classification silently inverts: a request fault
# read as a host fault is replayed, whole, onto a second host — which for
# ``provider_key_forbidden`` means laundering a moderation refusal. A shared name
# is what makes that agreement a fact rather than a coincidence of typing.
# --------------------------------------------------------------------------

#: A 4xx that is not about length — malformed body, unknown field, bad schema.
CODE_UPSTREAM_REQUEST_REJECTED = "upstream_request_rejected"

#: The assembled prompt does not fit the window. Also the code the upstream's own
#: length rejection is mapped to, so both arrive as one thing to the caller.
CODE_CONTEXT_WINDOW_EXCEEDED = "context_window_exceeded"

#: We stopped the generation ourselves.
CODE_UPSTREAM_RUNAWAY_OUTPUT = "upstream_runaway_output"

#: A 403 — ambiguous between model permissions and a host's moderation refusal.
CODE_PROVIDER_KEY_FORBIDDEN = "provider_key_forbidden"


class ErrorDetail(BaseModel):
    """The inner error object."""

    message: str
    type: str
    param: str | None = None
    code: str | None = None


class ErrorEnvelope(BaseModel):
    """The top-level error wrapper."""

    error: ErrorDetail
