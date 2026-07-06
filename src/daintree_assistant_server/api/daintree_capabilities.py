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
            "stream_events": ["meta", "status", "delta", "done", "error"],
            # Server's default thinking posture. When true, responses may carry
            # reasoning_content and the stream may emit a status:{phase:"thinking"}
            # event. A per-request forced tool_choice still disables thinking.
            "thinking_enabled": settings.main_thinking_enabled,
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
        # Workflow intelligence: the CLI owns the durable execution graphs; the
        # server contributes the stateless plan/reconcile/resume tasks plus the
        # turn.workflow_state footer rendering. The CLI gates the feature on
        # this block during rollout.
        "workflow_intelligence": {
            "turn_context_field": "workflow_state",
            "tasks": [
                "workflow_plan.v1",
                "workflow_reconcile.v1",
                "workflow_resume_digest.v1",
            ],
            "max_workflow_digests": settings.cap_workflow_digests,
            "max_workflow_state_bytes": settings.cap_workflow_state_bytes,
        },
        "limits": {
            "request_bytes": settings.max_request_bytes,
            "tools": settings.max_tools,
        },
    }
