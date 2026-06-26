"""Health, readiness, and version endpoints (unauthenticated)."""

from __future__ import annotations

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from .. import PROTOCOL_MAX, PROTOCOL_MIN, __version__

router = APIRouter()


@router.get("/healthz")
async def healthz() -> dict[str, str]:
    """Liveness: the event loop is serving. Does not call upstream."""
    return {"status": "ok"}


@router.get("/readyz")
async def readyz(request: Request) -> JSONResponse:
    """Readiness: config, secrets, prompts, catalog, provider, and signing keys."""
    ready = bool(getattr(request.app.state, "ready", False))
    body: dict[str, object] = {"status": "ready" if ready else "not_ready"}
    if not ready:
        error = getattr(request.app.state, "ready_error", None)
        if error:
            body["error"] = error
        return JSONResponse(status_code=503, content=body)
    container = request.app.state.container
    body["catalog_revision"] = container.catalog.revision
    body["skills"] = len(container.catalog)
    return JSONResponse(status_code=200, content=body)


@router.get("/version")
async def version(request: Request) -> dict[str, object]:
    settings = request.app.state.settings
    return {
        "server_version": __version__,
        "build_sha": settings.build_sha,
        "protocol": {"min": PROTOCOL_MIN, "max": PROTOCOL_MAX},
    }
