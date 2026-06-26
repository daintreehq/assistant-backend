"""The named utility-task endpoint (section 11)."""

from __future__ import annotations

from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse

from ..auth import Principal, require_auth
from ..contracts.tasks import TaskRequest
from ..services.validation import reject_forbidden_prompt_keys
from .deps import require_ready

router = APIRouter()


@router.post("/v1/daintree/tasks")
async def run_task(
    body: TaskRequest,
    request: Request,
    _principal: Principal = Depends(require_auth),
    _ready: None = Depends(require_ready),
) -> JSONResponse:
    reject_forbidden_prompt_keys(body.input, param="input")
    container = request.app.state.container
    result = await container.task_runner.run(body)
    headers = {"X-Request-Id": body.request_id} if body.request_id else {}
    return JSONResponse(content=result.model_dump(), headers=headers)
