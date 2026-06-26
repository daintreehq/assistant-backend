"""The main OpenAI-compatible Chat Completions endpoint."""

from __future__ import annotations

from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse, StreamingResponse

from ..auth import Principal, require_auth
from ..contracts.openai import ChatCompletionRequest
from ..services.validation import reject_forbidden_prompt_keys, validate_chat_request
from .deps import check_protocol_header, check_protocol_value, require_ready

router = APIRouter()


@router.post("/v1/chat/completions", response_model=None)
async def chat_completions(
    body: ChatCompletionRequest,
    request: Request,
    _principal: Principal = Depends(require_auth),
    _ready: None = Depends(require_ready),
) -> JSONResponse | StreamingResponse:
    settings = request.app.state.settings
    container = request.app.state.container

    check_protocol_header(request)
    if body.daintree:
        check_protocol_value(body.daintree.get("protocol_version"))
        reject_forbidden_prompt_keys(body.daintree, param="daintree")

    upstream_model = validate_chat_request(body, settings)

    outcome = await container.orchestrator.complete(
        body,
        upstream_model=upstream_model,
        request_id=request.headers.get("x-request-id"),
    )

    if outcome.streaming:
        assert outcome.stream is not None
        return StreamingResponse(
            outcome.stream,
            media_type="text/event-stream",
            headers={**outcome.headers, "Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
        )
    return JSONResponse(content=outcome.body, headers=outcome.headers)
