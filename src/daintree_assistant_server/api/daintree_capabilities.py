"""The Daintree capabilities endpoint (section 13)."""

from __future__ import annotations

from fastapi import APIRouter, Depends, Request

from .. import PROTOCOL_MAX, PROTOCOL_MIN, __version__
from ..auth import Principal, require_auth
from ..config import PUBLIC_MODEL_ID
from .deps import require_ready

router = APIRouter()


@router.get("/v1/daintree/capabilities")
async def capabilities(
    request: Request,
    _principal: Principal = Depends(require_auth),
    _ready: None = Depends(require_ready),
) -> dict[str, object]:
    container = request.app.state.container
    settings = request.app.state.settings
    return {
        "server_version": __version__,
        "protocol": {"min": PROTOCOL_MIN, "max": PROTOCOL_MAX},
        "respond": {
            "endpoint": "/v1/daintree/respond",
            "model": PUBLIC_MODEL_ID,
            "streaming": True,
            "stream_events": ["meta", "delta", "done", "error"],
            "multimodal_images": {
                "data_urls": True,
                "remote_urls": settings.allow_remote_image_urls,
            },
            "system_messages_accepted": False,
            "max_active_skills": settings.max_active_skills,
            "metadata_transport": "body+stream_meta_event",
        },
        "skills": {
            "catalog_revision": container.catalog.revision,
            "manual_resolve": True,
        },
        "tasks": container.task_runner.task_ids(),
        "limits": {
            "request_bytes": settings.max_request_bytes,
            "tools": settings.max_tools,
        },
    }
