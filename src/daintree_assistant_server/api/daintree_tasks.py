"""The named utility-task endpoint (section 11)."""

from __future__ import annotations

from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse

from ..auth import PERMISSION_TASKS, Principal, require_permission
from ..contracts.tasks import TaskRequest
from ..services.validation import reject_forbidden_prompt_keys
from .deps import check_protocol_header, request_id, require_ready

router = APIRouter()


@router.post("/v1/daintree/tasks")
async def run_task(
    body: TaskRequest,
    request: Request,
    principal: Principal = Depends(require_permission(PERMISSION_TASKS)),
    _ready: None = Depends(require_ready),
    # `_protocol` carries the same rule as /respond and /runbooks/resolve: a task
    # sends the caller's content upstream and returns a typed result the client
    # parses, so an unsupported client must be told 426 rather than handed a body
    # whose shape it may not understand. There is no `protocol_version` body field
    # on a task request — the header the CLI sends on every backend call is the
    # whole declaration.
    _protocol: None = Depends(check_protocol_header),
) -> JSONResponse:
    reject_forbidden_prompt_keys(body.input, param="input")
    container = request.app.state.container
    result = await container.task_runner.run(body)
    # exclude_none, matching /respond: an unreported `usage.cost` must be ABSENT
    # rather than null, or a client testing key presence to distinguish "unknown"
    # from "free" gets a different answer per endpoint.
    #
    # The header is redundant now that the middleware assigns it unconditionally,
    # and is kept as the explicit statement that this route's id is the request's
    # id. Setting anything else here is the bug this endpoint used to ship.
    return JSONResponse(
        content=result.model_dump(exclude_none=True),
        headers={"X-Request-Id": request_id(request)},
    )
