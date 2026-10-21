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
    """Readiness: the current catalog and selector artifact validated."""
    if not request.app.state.ready:
        body: dict[str, object] = {"status": "not_ready"}
        error = request.app.state.ready_error
        if error:
            # Startup errors can carry filesystem paths; outside development
            # they stay in the log, which already records them.
            body["error"] = "startup" if request.app.state.settings.is_hardened else error
        return JSONResponse(status_code=503, content=body)
    catalog = request.app.state.container.catalog
    return JSONResponse(
        status_code=200,
        content={
            "status": "ready",
            "catalog_revision": catalog.revision,
            "runbooks": len(catalog.selectable_ids),
        },
    )


@router.get("/version")
async def version(request: Request) -> dict[str, object]:
    """Which build this is. Answers while unready, so a failed deploy can still
    be identified."""
    return {"server_version": __version__, "build_sha": request.app.state.settings.build_sha}
