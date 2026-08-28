"""The account-authentication surface: OAuth discovery, and the legacy key probe.

Two endpoints that answer two different questions, and the split between them is
the whole reason this module is worth reading.

``GET /v1/daintree/auth/config`` is **public OAuth discovery** — the non-secret
manifest a client reads to learn how to obtain a credential for THIS deployment.
It has to be public and it has to be unauthenticated, because requiring a token
to learn how to get a token is a bootstrap no client can satisfy. The CLI has
already reached the same conclusion from the other side: its token source keeps a
closed allowlist of paths that bypass credential lookup entirely and are always sent
with no Authorization header, and this path sits on it beside the liveness probes
(`internal/backend/tokensource.go`, `publicPaths`). That is narrower than "the paths
it may call unauthenticated" — a signed-out client sends every other route with no
header too — and the difference is the point: these are the ones it must be able to
reach BEFORE it holds anything, so acquiring a credential can never be a
precondition for learning how to acquire one.

``POST /v1/daintree/auth/verify`` is the older question, and it is about the
SERVER's own upstream credential rather than the caller's — see its docstring.
It stays until the CLI stops calling it.

Originally this answered "is the key you just pasted valid?", because the caller
supplied the credential that funded their turns and our own auth was structural
(see ``auth.py``) — a well-formed string no provider ever issued passed the door
and only failed minutes later, mid-task.

The credential moved to the server, so the question moved with it. What this
endpoint verifies now is **the key this request would actually spend** — the
server's ``BASETEN_API_KEY``, which funds every turn. Same probe, same verdict
shape, and still the only honest answer available before a real turn: it costs
no tokens (a model listing, not a generation).

It is deliberately kept rather than deleted, and account authentication landing
did not make it redundant — it made the two questions in this module visibly
different. The endpoint above answers "how do I sign in here?" about the CALLER's
credential; this one answers "can this deployment reach its provider at all?"
about the SERVER's. A signed-in caller can still be served by a deployment whose
``BASETEN_API_KEY`` is expired, and no amount of identity verification would say
so. The CLI's ``doctor`` is what asks.
"""

from __future__ import annotations

import hashlib
import json
import math

from fastapi import APIRouter, Depends, Request, Response

from ..auth import Principal, require_auth
from ..config import OAUTH_REDIRECT_URI, Settings
from ..errors import DaintreeError
from .deps import require_ready

router = APIRouter()

#: The OAuth scopes the Assistant client requests. Hardcoded rather than
#: configured because they are not a deployment fact: Supabase supports the
#: standard set and no application-defined scopes, so there is nothing to vary
#: and nothing a per-environment value could express. Backend permissions will
#: come from the allowlisted client id instead — never from these. The CLI holds
#: a closed allowlist of its own and refuses a manifest naming anything outside
#: it, so widening this is a two-repo change.
_OAUTH_SCOPES = ("openid", "email")

#: The manifest shape version, and **the one number in this repo that is a real
#: version rather than a label.**
#:
#: CLAUDE.md is emphatic that nothing here carries a version, and it is right
#: about what it covers: prompts, runbooks, task identifiers and prompt-family
#: labels are all "simply the current version", because they are internal
#: artifacts a lockstep client never negotiates over. It carves out exactly this
#: category — `protocol_version`, `X-Daintree-Protocol`, the signed-state
#: `TOKEN_VERSION` — as "real wire metadata", and a discovery manifest is that.
#:
#: It is also not optional. The CLI's decoder (`internal/auth/discovery.go`,
#: `SupportedManifestVersion`) checks this FIRST and refuses the whole manifest on
#: a mismatch, so a body without it decodes as version 0 and every sign-in fails
#: with "this backend describes auth configuration version 0". Omitting it is not
#: a smaller contract; it is a broken one.
_AUTH_CONFIG_VERSION = 1


def _auth_config_body(settings: Settings) -> dict[str, object]:
    """The manifest, or an honest statement that there is no account auth here.

    ``version``, ``environment``, ``configured`` and ``required`` are always
    present, so a client reads them unconditionally and only then decides whether
    the endpoints exist. The alternative — a 404 or a 503 when
    OAuth is unconfigured — would make "this deployment has no accounts", which is
    the CURRENT and correct state of production, indistinguishable from "this
    deployment is broken".

    ``configured`` and ``required`` are two questions, not one. A deployment
    mid-rollout will publish a complete manifest while still serving anonymous
    requests: a client that collapsed the two would either refuse to sign in when
    it could, or demand a sign-in that is not yet being enforced.

    Both are derived rather than reported. ``required`` needs a publishable
    manifest as well as the mode: telling a client a credential is mandatory while
    withholding the only description of how to obtain one is a dead end, and the
    honest answer when we cannot say how to sign in is that we are not demanding
    it.

    ``configured`` is false unless the values are complete AND valid — this
    endpoint deliberately answers while the service is unready, so readiness
    cannot be its filter.

    ``version`` is checked FIRST by the client and gates everything after it, so
    it is present in both shapes — including the unconfigured one, where there is
    nothing else to describe. A client must be able to tell "a manifest I do not
    understand" from "a deployment with no accounts", and those are different
    answers with different advice.
    """
    configured = settings.auth_is_configured
    body: dict[str, object] = {
        "version": _AUTH_CONFIG_VERSION,
        "environment": settings.environment,
        "configured": configured,
        "required": configured and settings.auth_mode == "enforce",
    }
    if not configured:
        return body
    body.update(
        {
            "issuer": settings.supabase_auth_issuer,
            "authorization_endpoint": settings.supabase_oauth_authorize_url,
            "token_endpoint": settings.supabase_oauth_token_url,
            "jwks_uri": settings.supabase_jwks_url,
            "client_id": settings.supabase_oauth_client_id,
            "redirect_uri": OAUTH_REDIRECT_URI,
            "scopes": list(_OAUTH_SCOPES),
            "account_url": settings.daintree_account_url,
            "subscribe_url": settings.daintree_subscribe_url,
            "session_policy": {
                "access_token_seconds": settings.auth_access_token_seconds,
                "session_max_age_seconds": settings.auth_session_max_age_seconds,
            },
        }
    )
    return body


@router.get("/v1/daintree/auth/config")
async def auth_config(request: Request) -> Response:
    """Public, non-secret OAuth discovery for this deployment.

    **Deliberately not gated on readiness or on the container.** It is built from
    settings alone, and the moment someone most needs to know how to sign in is
    the moment the rest of the service is unhappy. ``require_ready`` would turn a
    bad upstream key into "you cannot log in either", which is both false and the
    least useful possible answer. What it does NOT skip is validation of the
    values themselves: ``auth_is_configured`` consults
    ``Settings.auth_manifest_problems()``, so the same defects that fail readiness
    also withhold the manifest.

    The response is serialized ONCE and returned as those exact bytes, with the
    ETag taken over them. An ETag computed from a re-serialization of the same
    object is a strong validator for bytes nobody ever sent — key order and
    unicode escaping are both free to differ — and the whole contract of a strong
    validator is that it changes whenever the representation does.

    ``private`` rather than ``public`` on the cache, and 60 seconds rather than
    300. A shared cache is the wrong place for this in two ways: the response
    carries a per-request ``X-Request-Id``, which a shared copy would replay to
    unrelated callers and make correlation ids meaningless, and a cached
    ``configured: false`` would keep clients from signing in for the whole TTL
    after an enforcement cutover. The client that asked is the only one that
    benefits from caching it anyway.
    """
    settings: Settings = request.app.state.settings
    payload = json.dumps(
        _auth_config_body(settings), separators=(",", ":"), ensure_ascii=False
    ).encode("utf-8")
    etag = '"' + hashlib.sha256(payload).hexdigest()[:32] + '"'
    headers = {"ETag": etag, "Cache-Control": "private, max-age=60"}
    if request.headers.get("if-none-match") == etag:
        return Response(status_code=304, headers=headers)
    return Response(content=payload, media_type="application/json", headers=headers)


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
    settings = request.app.state.settings
    provider = container.provider

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
                "detail": _REASON_DETAIL[REASON_REJECTED],
                "server_holds_credential": True,
            }
        raise

    # Echo only non-sensitive, decision-useful fields. `label` is the provider's own
    # name for the credential, which tells an operator WHICH server key answered — the
    # probed key is `settings.serving_api_key`, so nobody is confirming their own
    # typing here. The limit fields let the CLI warn about an exhausted key, which
    # otherwise surfaces as an unrelated failure on the first real turn. Never echo the
    # key itself.
    #
    # Baseten returns none of them — its probe answers with a model listing. The
    # label/budget branches stay because they cost nothing to keep and are exactly
    # what an account-mapped credential will want to report; `_usability` below is
    # already written to treat their absence as "not known to be exhausted" rather
    # than a failure.
    #
    # WHAT IS DELIBERATELY NOT ECHOED: the model listing itself. It was the one
    # field Baseten actually populates, and it was answering a question nobody
    # asks any more. Under caller-supplied keys the list described what YOUR key
    # could reach, so it was yours to see; the key is the server's now, the serving
    # model is hardcoded, and no caller chooses from that list — the CLI's own
    # response struct has never had a field for it. What remained was an
    # route publishing the deployment's full upstream model inventory to anyone
    # who asked — and under the shipped `open` posture that is genuinely anyone,
    # since the header is not read at all — for no consumer at all. A verdict is
    # the whole answer this endpoint owes.
    out: dict[str, object] = {"valid": True}
    if isinstance(record, dict):
        # The credential ACTUALLY probed, read through `serving_api_key` rather
        # than a vendor-named field: redacting against a host we are not talking
        # to would let the live key through in a label that echoed it. That is
        # not hypothetical — it is the shape of a bug this file has already had.
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
    # WHOSE key was probed, said on the wire rather than only in prose.
    #
    # Every verdict this route returns is about `BASETEN_API_KEY` — the
    # deployment's own credential — and nothing in the response used to say so.
    # A reader with no other context sees `valid: false` in reply to a request
    # carrying their bearer, and the available inference is the wrong one. The
    # field mirrors `/doctor`'s, deliberately under the same name: the two
    # routes answer the same question about the same key, and a second spelling
    # would invite a client to believe they are different questions. Constant
    # by construction — there is no caller-supplied key to probe, and
    # `ProviderChatRequest.api_key` stays an unused seam rather than a source —
    # so it is an assertion about the credential model, not a computed value.
    out["server_holds_credential"] = True
    return out


# Stable machine-readable outcomes. The CLI maps these to user-facing copy and to
# whether sign-in may complete; they must not be reworded casually.
REASON_OK = "ok"
REASON_REJECTED = "provider_rejected"
REASON_EXHAUSTED = "credits_exhausted"

_REASON_DETAIL = {
    REASON_OK: "The provider accepted this deployment's API key.",
    REASON_REJECTED: (
        "The provider rejected this deployment's API key. This is a server "
        "configuration problem; the caller holds no key to replace."
    ),
    REASON_EXHAUSTED: (
        "The provider accepted this deployment's API key, but the account "
        "funding it has no remaining credit. Add credits to that provider "
        "account; the caller holds no key to replace."
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
