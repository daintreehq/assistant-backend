"""Health, readiness, and version endpoints (unauthenticated)."""

from __future__ import annotations

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from .. import PROTOCOL_MAX, PROTOCOL_MIN, __version__

router = APIRouter()


@router.get("/healthz")
@router.get("/health", include_in_schema=False)
async def healthz() -> dict[str, str]:
    """Liveness: the event loop is serving. Does not call upstream.

    Served on two paths because ``/healthz`` is not reachable from outside a
    Cloud Run deployment: Google's frontend reserves that exact path and answers
    it with its own 404 before the request ever reaches this process. It still
    works for Cloud Run's own liveness probe, which connects to the container
    directly and bypasses the frontend, so the canonical name is kept. ``/health``
    is the alias external uptime monitoring can actually reach.

    For an external readiness check prefer ``/readyz``, which is strictly
    stronger: this endpoint only proves the event loop is turning, while
    ``/readyz`` also proves config, secrets, prompts, and the catalog loaded.
    """
    return {"status": "ok"}


@router.get("/readyz")
async def readyz(request: Request) -> JSONResponse:
    """Readiness: config, secrets, prompts, catalog, provider, and signing keys."""
    ready = bool(getattr(request.app.state, "ready", False))
    body: dict[str, object] = {"status": "ready" if ready else "not_ready"}
    if not ready:
        # /readyz is UNAUTHENTICATED. The startup-failure text names weak/missing
        # secrets by variable and can carry filesystem paths and Pydantic detail, so
        # outside development it stays in the log (main.py already records it) and the
        # probe gets a stable, uninformative reason.
        settings = getattr(request.app.state, "settings", None)
        error = getattr(request.app.state, "ready_error", None)
        if error:
            hardened = settings is not None and settings.is_hardened
            body["error"] = "configuration" if hardened else error
        return JSONResponse(status_code=503, content=body)
    container = request.app.state.container
    body["catalog_revision"] = container.catalog.revision
    body["runbooks"] = len(container.catalog)
    return JSONResponse(status_code=200, content=body)


@router.get("/version")
async def version(request: Request) -> dict[str, object]:
    """What exactly this revision is: the build, the protocol range, the posture.

    ``posture`` reports the two rollout axes — ``AUTH_MODE`` and
    ``ENTITLEMENT_MODE`` — and it is here rather than on the auth manifest for a
    reason worth stating, because the manifest is the endpoint it looks like it
    belongs on. ``/v1/daintree/auth/config`` is a CLIENT contract about how to
    sign in; it answers "must I hold a credential" with ``required``, which is
    ``configured and auth_mode == "enforce"`` and therefore cannot tell ``open``
    from ``observe`` — the two are indistinguishable to a caller by design, since
    ``observe`` changes nothing anybody can see. That is the right answer for a
    client and the wrong one for the operator who just deployed a revision and
    needs to know which mode it actually came up in. ``/version`` is already the
    "what exactly is this" endpoint, the release smoke already fetches it, and an
    OPERATOR fact costs nothing extra when it rides the request that was going to
    be made anyway.

    Nothing here is a secret and nothing here is derived from a caller. A mode
    name is the same three words the deploy substitutions carry, published so the
    pre-promotion smoke can assert the revision matches what was requested rather
    than inferring it from a door it cannot see behind. ``ENTITLEMENT_MODE`` was
    previously readable from no endpoint at all, so a billing rollout could only
    be confirmed by reading the Cloud Run control plane.

    ``subject_gate`` is the third axis and the one that needs its bounds stated,
    because it is the only one derived from a SECRET. Exactly three facts are
    published: whether the gate decides anything, how many subjects it admits, and
    the operator-chosen label for that membership. No entry, no prefix or suffix
    of one, no hash of the set, and no answer to "is this subject in it" — a
    public unauthenticated endpoint that could be asked about membership would be
    a membership oracle, which is worse than publishing the list, because it
    cannot be noticed. The count and the label are what an operator needs to
    confirm that the revision in front of them holds the list they just published,
    and neither narrows who is on it.

    The label is published only once it has VALIDATED, and an unvalidated one
    reads as absent. This route answers while the service is unready on purpose,
    so "readiness would have refused that label" is not a reason it is safe to
    echo — the deployment whose label is wrong is precisely the one being read
    here, and a digest typed into the wrong variable would otherwise be published
    by the mistake that made it unsafe.

    Additive on the wire: the CLI decodes ``/version`` through ``doJSON`` with no
    ``DisallowUnknownFields``, so an unknown field is ignored rather than an error.
    """
    settings = request.app.state.settings
    return {
        "server_version": __version__,
        "build_sha": settings.build_sha,
        "protocol": {"min": PROTOCOL_MIN, "max": PROTOCOL_MAX},
        "posture": {
            "auth": settings.auth_mode,
            "entitlement": settings.entitlement_mode,
            "subject_gate": {
                "enabled": settings.staging_subject_gate_enabled,
                "entries": len(settings.allowed_subject_digests()),
                "revision": settings.published_gate_revision,
            },
        },
    }
