"""The doctor endpoint — one call that answers "why isn't this working?".

An internal tester who hits a problem has three suspects and no way to tell them
apart: their key, their build, or ours. Each is diagnosable, but only by asking
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
prompt text, no skill bodies, no tool schemas, no conversation content, and
nothing derived from the caller's key — not even a prefix or a length. What it
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
from ..config import PUBLIC_MODEL_ID
from ..errors import DaintreeError
from ..modes import MODE_SPECS, ModeSpec, OperationMode
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
    server is not ready" and "your key was rejected", is a successful diagnosis.
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

    bundle["modes"] = await _modes_section(container, principal, warnings)
    bundle["catalog"] = _catalog_section(container, warnings)
    bundle["limits"] = _limits_section(settings)
    bundle["warnings"] = warnings
    return bundle


async def _modes_section(
    container: Any, principal: Principal, warnings: list[str]
) -> dict[str, Any]:
    """Both routes: identity, and a live verdict on each credential.

    Both are verified, sequentially, rather than only the default one. The
    question a tester brings here is "can this backend answer for me?", and with
    two product modes that has two answers — an instance whose Pro key was never
    granted looks perfectly healthy right up until the first Pro turn, which is
    precisely the failure this endpoint exists to find before a user does.

    ``verify_key`` is a tokenless model-listing call: no generation, no spend, so
    checking both costs one extra round trip on a rarely-called diagnostic and
    nothing at all in tokens.

    Warnings are namespaced by mode (`provider_key_rejected:pro`) because an
    unqualified warning across two routes cannot say which one is broken — and
    "one of your two providers is unreachable" is not an actionable report.
    """
    section: dict[str, Any] = {}
    for mode, spec in MODE_SPECS.items():
        section[mode.value] = await _one_mode(container, spec, principal, warnings)
    return section


async def _one_mode(
    container: Any, spec: ModeSpec, principal: Principal, warnings: list[str]
) -> dict[str, Any]:
    """One route's identity and credential verdict.

    The distinction between "rejected" and "we could not ask" is preserved — a
    network failure reports ``key_valid: null``, never ``false``, because telling
    a tester the key is bad when we simply could not reach the provider sends
    them to re-paste a credential that was fine.

    ``provider`` and ``upstream_model`` are named here and nowhere public: the
    respond and task surfaces both say ``daintree-assistant`` in either mode, and
    this is the operator's view. No key, key prefix, or base URL appears — a base
    URL is the one field of the three that could carry userinfo.
    """
    mode: OperationMode = spec.mode
    section: dict[str, Any] = {
        "provider": spec.provider_id,
        "upstream_model": spec.upstream_model,
        "public_model": PUBLIC_MODEL_ID,
        "thinking_enabled": spec.thinking.enabled,
        # The server holds BOTH upstream credentials and funds every turn from
        # them. Stated here because it is the fact a tester most often needs
        # confirmed, and it inverted when the backend stopped taking caller keys.
        "server_holds_credential": True,
        "key_valid": None,
        "key_usable": None,
        "reason": None,
        "verify_ms": None,
    }
    # A doctor must survive what it diagnoses, and "the container is half-built"
    # is squarely in that class: readiness can leave a container behind whose
    # registry never finished, and a 500 here would replace the diagnosis with
    # the symptom. Any failure to reach a route is reported as that route being
    # unavailable, which is exactly what it is.
    try:
        provider = container.modes.resolve(mode).provider
    except Exception:  # noqa: BLE001 — see above
        warnings.append(f"provider_unavailable:{mode.value}")
        return section
    if provider is None:
        warnings.append(f"provider_unavailable:{mode.value}")
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
            warnings.append(f"provider_key_rejected:{mode.value}")
        else:
            # Unknown, not bad. `key_valid` stays None.
            section["reason"] = exc.code
            warnings.append(f"provider_unreachable:{mode.value}")
        return section
    except Exception:  # noqa: BLE001 — a doctor must survive what it diagnoses
        logger.warning("doctor_verify_failed", mode=mode.value, exc_info=True)
        section["verify_ms"] = round((time.perf_counter() - started) * 1000)
        warnings.append(f"provider_unreachable:{mode.value}")
        return section

    section["verify_ms"] = round((time.perf_counter() - started) * 1000)
    section["key_valid"] = True
    # Reuse the sign-in endpoint's budget arithmetic rather than restating it, so
    # the two can never disagree about what "usable" means. Neither host reports
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
        warnings.append(f"provider_key_unusable:{mode.value}")
    return section


def _catalog_section(container: Any, warnings: list[str]) -> dict[str, Any]:
    """Catalog identity — the revision hash, and counts. Never a body or a title.

    The revision is the useful field: it is what a tester and this server compare
    to establish they are talking about the same catalog at all.
    """
    if container is None:
        warnings.append("catalog_unavailable")
        return {"revision": None, "skills": 0, "foundations": 0}
    try:
        # `container.catalog` reads through the CatalogManager, which the
        # skill-learning harness can reload underneath us. A doctor must survive
        # whatever it is diagnosing, so even this is not assumed to work.
        catalog = container.catalog
        return {
            "revision": catalog.revision,
            "skills": len(catalog),
            "foundations": sum(1 for skill in catalog.skills if skill.meta.foundation),
        }
    except Exception:  # noqa: BLE001 — a broken catalog is a finding, not a 500
        logger.warning("doctor_catalog_failed", exc_info=True)
        warnings.append("catalog_unavailable")
        return {"revision": None, "skills": 0, "foundations": 0}


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
            "max_active_skills": settings.max_active_skills,
            "max_request_bytes": settings.max_request_bytes,
            "main_timeout_seconds": settings.main_timeout_seconds,
            "selector_timeout_seconds": settings.selector_timeout_seconds,
            "utility_timeout_seconds": settings.utility_timeout_seconds,
        }
    except Exception:  # noqa: BLE001 — a partially-built Settings must not 500
        logger.warning("doctor_limits_failed", exc_info=True)
        return {}
