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
