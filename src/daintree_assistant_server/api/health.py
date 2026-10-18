"""Health, readiness, and version endpoints."""

from __future__ import annotations

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from .. import __version__

router = APIRouter()


@router.get("/healthz")
@router.get("/health", include_in_schema=False)
async def healthz() -> dict[str, str]:
    """Liveness: the event loop is serving.

    Served on two paths because Cloud Run's frontend reserves ``/healthz`` and
    answers it with its own 404 before a request reaches this process. Cloud
    Run's own probe connects to the container directly, so the canonical name
    still works for it; ``/health`` is the alias outside monitoring can reach.
    """
    return {"status": "ok"}


@router.get("/readyz")
async def readyz(request: Request) -> JSONResponse:
    """Readiness: both catalogs loaded and the selector artifact validated.

    ``catalog_revision`` is the control catalog's, which a build sending
    ``Daintree-Host-Control: 1`` is served; ``inbox_catalog_revision`` is what a
    v0.44 client is served (``runbooks/catalog.py``, ``load_catalogs``)."""
    if not request.app.state.ready:
        body: dict[str, object] = {"status": "not_ready"}
        error = request.app.state.ready_error
        if error:
            # Startup errors can carry filesystem paths; outside development
            # they stay in the log, which already records them.
            body["error"] = "startup" if request.app.state.settings.is_hardened else error
        return JSONResponse(status_code=503, content=body)
    catalogs = request.app.state.container.catalogs
    return JSONResponse(
        status_code=200,
        content={
            "status": "ready",
            "catalog_revision": catalogs.control.revision,
            "runbooks": len(catalogs.control.selectable_ids),
            "inbox_catalog_revision": catalogs.inbox.revision,
            "inbox_runbooks": len(catalogs.inbox.selectable_ids),
        },
    )


@router.get("/version")
async def version(request: Request) -> dict[str, object]:
    """Which build this is. Answers while unready, so a failed deploy can still
    be identified."""
    return {"server_version": __version__, "build_sha": request.app.state.settings.build_sha}
