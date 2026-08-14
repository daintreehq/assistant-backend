"""The Daintree capabilities endpoint (section 13)."""

from __future__ import annotations

from fastapi import APIRouter, Depends, Request

from .. import PROTOCOL_MAX, PROTOCOL_MIN, __version__
from ..auth import Principal, require_auth
from ..config import INTERACTIVE_THINKING_ENABLED, PUBLIC_MODEL_ID
from ..providers.policy import (
    DEFAULT_POLICY,
    MAX_ENDPOINT_LIST,
    EndpointSort,
    PrivacyMode,
)
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
            # The main call's thinking posture is hardcoded OFF (a product decision,
            # like the model ids — see INTERACTIVE_THINKING_ENABLED in config.py), so
            # responses do not carry reasoning_content and the stream emits no
            # status:{phase:"thinking"} event. Read from the constant rather than
            # restated as a literal: this field is what the CLI gates its thinking UI
            # on, and a hand-maintained copy would drift the moment the posture moves.
            "thinking_enabled": INTERACTIVE_THINKING_ENABLED,
            "multimodal_images": {
                "data_urls": True,
                "remote_urls": settings.allow_remote_image_urls,
            },
            "system_messages_accepted": False,
            "max_active_skills": settings.max_active_skills,
            "metadata_transport": "body+stream_meta_event",
        },
        # The active endpoint-routing posture, and what a caller may change about
        # it. Surfaced because privacy is part of the tester's trust decision and
        # must be visible BEFORE the first turn — not discovered from a doc. The
        # description string is served from PrivacyMode itself so a client cannot
        # invent its own wording (and cannot say "does not store").
        "routing": {
            "privacy_mode": DEFAULT_POLICY.privacy.value,
            "privacy_description": DEFAULT_POLICY.privacy.description,
            "sort": DEFAULT_POLICY.sort.value,
            "client_selectable": {
                "field": "routing",
                "privacy": [mode.value for mode in PrivacyMode],
                "sort": [axis.value for axis in EndpointSort],
                "endpoint_lists": ["only", "ignore"],
                "max_endpoints": MAX_ENDPOINT_LIST,
            },
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
                "workflow_plan",
                "workflow_reconcile",
                "workflow_resume_digest",
            ],
            "max_workflow_digests": settings.cap_workflow_digests,
            "max_workflow_state_bytes": settings.cap_workflow_state_bytes,
        },
        "limits": {
            "request_bytes": settings.max_request_bytes,
            "tools": settings.max_tools,
        },
    }
