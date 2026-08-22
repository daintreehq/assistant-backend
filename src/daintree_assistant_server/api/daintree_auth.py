"""Credential verification — "can this backend actually run a turn for me?".

Originally this answered "is the key you just pasted valid?", because the caller
supplied the credential that funded their turns and our own auth was structural
(see ``auth.py``) — a well-formed string no provider ever issued passed the door
and only failed minutes later, mid-task.

The credential moved to the server, so the question moved with it. What this
endpoint verifies now is **whichever key this request would actually spend**: the
caller's if they sent one, the server's ``CEREBRAS_API_KEY`` otherwise — which is
every request today. Same probe, same verdict shape, and still the only honest
answer available before a real turn: it costs no tokens (a model listing, not a
generation).

It is deliberately kept rather than deleted while the door is open. Account
authentication is being built next, and this is where a caller learns their
credential works — a question that returns, not one that went away.
"""

from __future__ import annotations

import math

from fastapi import APIRouter, Depends, Request

from ..auth import Principal, require_auth
from ..errors import DaintreeError
from .deps import require_ready

router = APIRouter()


@router.post("/v1/daintree/auth/verify")
async def verify(
    request: Request,
    principal: Principal = Depends(require_auth),
    _ready: None = Depends(require_ready),
) -> dict[str, object]:
    """Verify the credential this request would spend, against the upstream provider.

    Always 200 when we managed to GET an answer: "this key is invalid" is a successful
    result for this question, not a request failure, and modelling it as an HTTP error
    would force the CLI to distinguish "rejected" from "could not ask" out of status
    codes that also mean other things. The two outcomes are therefore explicit fields:

    - ``valid: true``  — the provider recognised the key.
    - ``valid: false`` — the provider rejected it; ``detail`` says so.

    A failure to reach the provider at all propagates as the usual upstream envelope
    (503 ``upstream_unavailable``, 502 ``upstream_protocol_error`` for an unreadable
    key record, or 504 ``upstream_timeout`` when the probe timed out), because then
    we genuinely do not know and must not imply a verdict.
    """
    container = request.app.state.container
    provider = container.provider
    settings = request.app.state.settings

    try:
        record = await provider.verify_key()
    except DaintreeError as exc:
        if exc.code in ("invalid_api_key", "provider_invalid_api_key"):
            # A definite rejection from the provider. Report it as a 200 verdict, not
            # as our own 401 — our door already accepted this request, and reusing the
            # 401 code here would tell the CLI to re-send the same header.
            return {
                "valid": False,
                "usable": False,
                "reason": REASON_REJECTED,
                "detail": "The provider rejected this API key.",
            }
        raise

    # Echo only non-sensitive, decision-useful fields. `label` helps a user confirm they
    # pasted the key they meant to; the limit fields let the CLI warn about an exhausted
    # key, which reads as a broken sign-in otherwise. Never echo the key itself.
    #
    # Cerebras returns none of them — its probe answers with a model listing — so
    # in practice only `models` is populated today. The label/budget branches stay
    # because they cost nothing to keep and are exactly what an account-mapped
    # credential will want to report; `_usability` below is already written to
    # treat their absence as "not known to be exhausted" rather than a failure.
    out: dict[str, object] = {"valid": True}
    if isinstance(record, dict):
        models = record.get("models")
        if isinstance(models, list):
            out["models"] = [m for m in models if isinstance(m, str)]
        # The credential ACTUALLY probed, not a vendor-named field: `verify_key`
        # runs against the serving adapter, so redacting against a host we are not
        # talking to would let the live key through in a label that echoed it.
        label = _safe_label(record.get("label"), settings.serving_api_key.get_secret_value())
        if label:
            out["label"] = label
        for field in ("limit", "usage", "limit_remaining"):
            value = record.get(field)
            if isinstance(value, (int, float)) and not isinstance(value, bool):
                out[field] = value
        if isinstance(record.get("is_free_tier"), bool):
            out["is_free_tier"] = record["is_free_tier"]

    # `valid` and `usable` are different questions, and collapsing them is the
    # bug this split exists to fix. A recognised key whose balance is spent is
    # `valid: true` — and completely unable to run a turn. A client that reads
    # only `valid` shows a successful sign-in, then fails on the first real
    # request with what looks like an unrelated error. So the exhausted case gets
    # its own verdict and its own reason code, and the CLI branches on `reason`
    # rather than reimplementing the arithmetic.
    usable, reason = _usability(out)
    out["usable"] = usable
    out["reason"] = reason
    out["detail"] = _REASON_DETAIL[reason]
    return out


# Stable machine-readable outcomes. The CLI maps these to user-facing copy and to
# whether sign-in may complete; they must not be reworded casually.
REASON_OK = "ok"
REASON_REJECTED = "provider_rejected"
REASON_EXHAUSTED = "credits_exhausted"

_REASON_DETAIL = {
    REASON_OK: "The provider accepted this API key.",
    REASON_REJECTED: "The provider rejected this API key.",
    REASON_EXHAUSTED: (
        "The provider accepted this API key, but the account has no remaining "
        "credit. Add credits or use a different key."
    ),
}


def _usability(out: dict[str, object]) -> tuple[bool, str]:
    """Decide `usable` from the provider's own budget figures.

    Read this as **"not known to be exhausted"**, not "proven to work". Only a
    provider-reported, finite, zero-or-negative remaining balance is treated as
    exhausted. Everything else — a missing field, a non-numeric value, a tiny
    positive balance — reports usable.

    The asymmetry is deliberate and is the whole design. Blocking a sign-in that
    would have worked is the worse failure: an unlimited or pay-as-you-go key
    legitimately reports no `limit_remaining` at all, and a tester turned away at
    the door has no way to discover the check was wrong. Letting a doomed key
    through merely defers the failure to the first turn — where the error
    taxonomy now names it precisely (`provider_insufficient_credits`).
    """
    remaining = out.get("limit_remaining")
    if isinstance(remaining, bool) or not isinstance(remaining, (int, float)):
        return True, REASON_OK
    if isinstance(remaining, float) and not math.isfinite(remaining):
        # NaN/inf from a misbehaving provider. NaN compares false against
        # everything, so it would silently read as "funded" — but it is really
        # "unknown", which is the same verdict here and worth being explicit about.
        return True, REASON_OK
    # Compared against the int 0 directly rather than via float(): an arbitrarily
    # large upstream integer raises OverflowError on conversion, and a crash here
    # would turn a successful verification into a 500.
    if remaining <= 0:
        return False, REASON_EXHAUSTED
    return True, REASON_OK


# A provider label is upstream-controlled text that the CLI renders into a terminal.
# Bound it, strip control characters, and — critically — drop it entirely if it contains
# the bearer. Some providers default a key's label to the key itself or a prefix of it,
# which would turn this "safe metadata" field into the exact leak the endpoint is
# careful to avoid everywhere else.
_MAX_LABEL_LENGTH = 120


def _safe_label(value: object, secret: str) -> str | None:
    if not isinstance(value, str):
        return None
    cleaned = "".join(ch for ch in value if ch.isprintable()).strip()
    if not cleaned:
        return None
    if secret and (secret in cleaned or cleaned in secret):
        return None
    return cleaned[:_MAX_LABEL_LENGTH]
