"""The native Daintree generation endpoint: ``POST /v1/daintree/respond``.

This is the Daintree-owned protocol (not OpenAI-compatible). It returns a native
JSON response, or — when ``generation.stream`` is true — a named-event SSE stream
(``meta`` / ``delta`` / ``done`` / ``error``).
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse, StreamingResponse

from ..auth import Principal, require_auth
from ..contracts.daintree_api import RespondRequest
from ..services.validation import validate_respond_request
from .deps import check_protocol_header, check_protocol_value, require_ready

router = APIRouter()


@router.post("/v1/daintree/respond", response_model=None)
async def respond(
    body: RespondRequest,
    request: Request,
    principal: Principal = Depends(require_auth),
    _ready: None = Depends(require_ready),
) -> JSONResponse | StreamingResponse:
    settings = request.app.state.settings
    container = request.app.state.container

    check_protocol_header(request)
    check_protocol_value(body.protocol_version)

    upstream_model = validate_respond_request(body, settings)

    outcome = await container.orchestrator.complete(
        body,
        upstream_model=upstream_model,
        # The caller's own key funds every upstream call this turn makes.
        api_key=principal.api_key,
        # The middleware-resolved id, so logs, X-Request-Id, and SSE meta all agree.
        request_id=getattr(request.state, "request_id", None),
    )

    if outcome.streaming:
        assert outcome.stream is not None
        return StreamingResponse(
            outcome.stream,
            media_type="text/event-stream",
            headers={**outcome.headers, "Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
        )
    return JSONResponse(content=outcome.body, headers=outcome.headers)
