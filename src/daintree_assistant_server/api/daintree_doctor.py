"""The doctor endpoint — one call that answers "why isn't this working?".

An internal tester who hits a problem has three suspects and no way to tell them
apart: their build, this deployment's configuration, or the upstream we spend
against. (Their KEY is no longer one of them — the server holds the upstream
credential now — which is exactly why the provider section below reports on OUR
key rather than asking about theirs.) Each is diagnosable, but only by asking
three different endpoints and knowing which questions to ask. This asks all of
them and returns one bundle a tester can paste into a support channel.

Two design points that are not obvious:

**It does not require readiness.** Every other authenticated endpoint depends on
``require_ready`` and 503s when the container did not build. That is exactly the
state in which a tester most needs a diagnosis, and a doctor that goes down with
the patient is useless. So this endpoint answers on a degraded server and reports
``ready: false`` with a safe reason, degrading each section it cannot fill rather
than failing the whole call.

**It is safe to paste in public.** The bundle is assembled from an explicit
allowlist of non-sensitive facts, never by serializing settings. It carries no
prompt text, no runbook bodies, no tool schemas, no conversation content, and
nothing derived from the serving key — not even a prefix or a length. What it
does carry about the key is a verdict (``valid``/``usable``) obtained by asking
the provider, which costs no tokens.

The readiness *reason* follows ``/readyz``'s rule for the same reason it exists
there: startup failure text names environment variables and filesystem paths, so
in a hardened environment it collapses to a stable, uninformative word. Unlike
``/readyz`` this endpoint is authenticated, but a doctor bundle is *made to be
copied elsewhere*, which is the same exposure by a slower route.
"""

from __future__ import annotations

import time
from typing import Any

from fastapi import APIRouter, Depends, Request

from .. import PROTOCOL_MAX, PROTOCOL_MIN, __version__
from ..auth import Principal, require_auth
from ..config import (
    EXECUTOR_MODEL,
    EXECUTOR_PROVIDER,
    INTERACTIVE_THINKING_ENABLED,
    PUBLIC_MODEL_ID,
)
from ..errors import DaintreeError
from ..observability import get_logger
from ..prompts.loader import MAIN_PROMPT_VERSION
from .daintree_auth import REASON_OK, REASON_REJECTED, _usability

router = APIRouter()
logger = get_logger("daintree.doctor")


@router.post("/v1/daintree/doctor")
async def doctor(
    request: Request,
    principal: Principal = Depends(require_auth),
) -> dict[str, Any]:
    """Assemble a safe, copyable diagnostic bundle.

    Deliberately NOT gated on ``require_ready`` — see the module docstring.
    Always 200 when the request authenticated: every finding, including "the
    server is not ready" and "the deployment's upstream key was rejected", is a
    successful diagnosis.
    Modelling those as HTTP errors would force the caller to distinguish a
    verdict from a failure to reach us out of status codes that mean both.
    """
    settings = getattr(request.app.state, "settings", None)
    container = getattr(request.app.state, "container", None)
    ready = bool(getattr(request.app.state, "ready", False))
    warnings: list[str] = []

    bundle: dict[str, Any] = {
        "request_id": getattr(request.state, "request_id", None),
        "server_version": __version__,
        "build_sha": getattr(settings, "build_sha", None),
        "protocol": {"min": PROTOCOL_MIN, "max": PROTOCOL_MAX},
        "ready": ready,
        "prompt_version": MAIN_PROMPT_VERSION,
    }

    if not ready:
        error = getattr(request.app.state, "ready_error", None)
        hardened = settings is not None and settings.is_hardened
        bundle["ready_error"] = ("configuration" if hardened else error) if error else None
        warnings.append("server_not_ready")

    bundle["provider"] = await _provider_section(container, principal, warnings)
    bundle["catalog"] = _catalog_section(container, warnings)
    bundle["limits"] = _limits_section(settings)
    bundle["warnings"] = warnings
    return bundle


async def _provider_section(
    container: Any, principal: Principal, warnings: list[str]
) -> dict[str, Any]:
    """Provider identity and a live verdict on the credential this turn would use.

    ``verify_key`` is a tokenless model-listing call: no generation, no spend. The
    distinction between "rejected" and "we could not ask" is preserved — a network
    failure reports ``key_valid: null``, never ``false``, because the key is the
    SERVER's and a tester has no way to act on a verdict about it — reporting
    ``false`` on an unreachable provider would send someone hunting a credential
    problem that is not theirs and was not there, when the honest answer is that
    we could not ask.

    ``upstream_model`` is named here and nowhere public: the respond and task
    surfaces both say ``daintree-assistant``, and this is the operator's view. No
    key, key prefix, or base URL appears — a base URL is the one field of the
    three that could carry userinfo.
    """
    section: dict[str, Any] = {
        "name": EXECUTOR_PROVIDER,
        "model": PUBLIC_MODEL_ID,
        "upstream_model": EXECUTOR_MODEL,
        "thinking_enabled": INTERACTIVE_THINKING_ENABLED,
        # The server holds the upstream credential and funds every turn from it.
        # Stated here because it is the fact a tester most often needs confirmed,
        # and it inverted when callers stopped supplying keys.
        "server_holds_credential": True,
        "key_valid": None,
        "key_usable": None,
        "reason": None,
        "verify_ms": None,
    }
    if container is None:
        warnings.append("provider_unavailable")
        return section

    # A doctor must survive what it diagnoses, and "the container is half-built"
    # is squarely in that class: readiness can leave a container behind whose
    # provider never finished, and a 500 here would replace the diagnosis with
    # the symptom.
    provider = getattr(container, "provider", None)
    if provider is None:
        warnings.append("provider_unavailable")
        return section

    started = time.perf_counter()
    try:
        record = await provider.verify_key()
    except DaintreeError as exc:
        section["verify_ms"] = round((time.perf_counter() - started) * 1000)
        if exc.code in ("invalid_api_key", "provider_invalid_api_key"):
            section["key_valid"] = False
            section["key_usable"] = False
            section["reason"] = REASON_REJECTED
            warnings.append("provider_key_rejected")
        else:
            # Unknown, not bad. `key_valid` stays None.
            section["reason"] = exc.code
            warnings.append("provider_unreachable")
        return section
    except Exception:  # noqa: BLE001 — a doctor must survive what it diagnoses
        logger.warning("doctor_verify_failed", exc_info=True)
        section["verify_ms"] = round((time.perf_counter() - started) * 1000)
        warnings.append("provider_unreachable")
        return section

    section["verify_ms"] = round((time.perf_counter() - started) * 1000)
    section["key_valid"] = True
    # Reuse the sign-in endpoint's budget arithmetic rather than restating it, so
    # the two can never disagree about what "usable" means. Together reports no
    # budget figures, so `_usability` sees an empty record and answers "not known
    # to be exhausted" — the honest verdict, and exactly the asymmetry that
    # helper documents.
    budget: dict[str, object] = {}
    if isinstance(record, dict):
        for field in ("limit", "usage", "limit_remaining"):
            value = record.get(field)
            if isinstance(value, (int, float)) and not isinstance(value, bool):
                budget[field] = value
    usable, reason = _usability(budget)
    section["key_usable"] = usable
    section["reason"] = reason
    if reason != REASON_OK:
        warnings.append("provider_key_unusable")
    return section


def _catalog_section(container: Any, warnings: list[str]) -> dict[str, Any]:
    """Catalog identity — the revision hash, and counts. Never a body or a title.

    The revision is the useful field: it is what a tester and this server compare
    to establish they are talking about the same catalog at all.
    """
    if container is None:
        warnings.append("catalog_unavailable")
        return {"revision": None, "runbooks": 0, "foundations": 0}
    try:
        # `container.catalog` reads through the CatalogManager, which the
        # runbook-learning harness can reload underneath us. A doctor must survive
        # whatever it is diagnosing, so even this is not assumed to work.
        catalog = container.catalog
        return {
            "revision": catalog.revision,
            "runbooks": len(catalog),
            "foundations": sum(1 for runbook in catalog.runbooks if runbook.meta.foundation),
        }
    except Exception:  # noqa: BLE001 — a broken catalog is a finding, not a 500
        logger.warning("doctor_catalog_failed", exc_info=True)
        warnings.append("catalog_unavailable")
        return {"revision": None, "runbooks": 0, "foundations": 0}


def _limits_section(settings: Any) -> dict[str, Any]:
    """The bounds a tester can actually hit, by name.

    An explicit allowlist, not a dump of settings: this bundle is made to be
    pasted somewhere, and serializing the config object would ship whatever
    field someone adds next — including the ones that must never leave.
    """
    if settings is None:
        return {}
    try:
        return {
            "max_active_runbooks": settings.max_active_runbooks,
            "max_request_bytes": settings.max_request_bytes,
            "main_timeout_seconds": settings.main_timeout_seconds,
            "selector_timeout_seconds": settings.selector_timeout_seconds,
            "utility_timeout_seconds": settings.utility_timeout_seconds,
        }
    except Exception:  # noqa: BLE001 — a partially-built Settings must not 500
        logger.warning("doctor_limits_failed", exc_info=True)
        return {}
