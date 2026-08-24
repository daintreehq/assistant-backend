"""The native Daintree generation endpoint: ``POST /v1/daintree/respond``.

This is the Daintree-owned protocol (not OpenAI-compatible). It returns a native
JSON response, or — when ``generation.stream`` is true — a named-event SSE stream
(``meta`` / ``status`` / ``delta`` / ``compaction`` / ``done`` / ``error``).
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse, StreamingResponse

from ..auth import PERMISSION_CHAT, Principal, require_permission
from ..contracts.daintree_api import RespondRequest
from ..services.validation import validate_respond_request
from .deps import check_protocol_header, check_protocol_value, request_id, require_ready

router = APIRouter()


@router.post("/v1/daintree/respond", response_model=None)
async def respond(
    body: RespondRequest,
    request: Request,
    principal: Principal = Depends(require_permission(PERMISSION_CHAT)),
    _ready: None = Depends(require_ready),
    _protocol: None = Depends(check_protocol_header),
) -> JSONResponse | StreamingResponse:
    """Run one turn.

    **Cancellation.** The streaming path stops promptly when a client goes away:
    GeneratorExit unwinds the body generator, whose ``finally`` closes the
    upstream stream. The non-streaming path has no equivalent signal — ASGI
    offers no disconnect callback for a buffered request, and polling
    ``request.is_disconnected()`` deadlocks against this app's body-buffering
    middleware (the poll delegates to the raw ``receive``, which will not report
    a disconnect until the response completes, so the handler waits for the
    client while the client waits for the handler).

    What bounds an abandoned non-streaming turn instead is the request budget
    (``services/deadlines.py``): it cannot outlive the total deadline, so the
    worst case is wasted work for that bounded window rather than indefinitely.
    """
    settings = request.app.state.settings
    container = request.app.state.container

    check_protocol_value(body.protocol_version)

    validate_respond_request(body, settings)

    outcome = await container.orchestrator.complete(
        body,
        # No per-call credential: the provider spends the SERVER's key. The
        # parameter is still there for benchmarks and live tests, which name the
        # credential they mean to spend; the serving path never does.
        # The middleware-resolved id, so logs, X-Request-Id, and SSE meta all agree.
        request_id=request_id(request),
    )

    if outcome.streaming:
        assert outcome.stream is not None
        return StreamingResponse(
            outcome.stream,
            media_type="text/event-stream",
            headers={**outcome.headers, "Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
        )
    return JSONResponse(content=outcome.body, headers=outcome.headers)
