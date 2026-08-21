"""Manual skill catalog and resolve endpoints (section 12)."""

from __future__ import annotations

from typing import Literal

from fastapi import APIRouter, Depends, Request, Response
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field

from ..auth import Principal, require_auth
from ..errors import invalid_request
from .deps import check_protocol_header, check_protocol_value, request_id, require_ready

router = APIRouter()


@router.get("/v1/daintree/skills")
async def list_skills(
    request: Request,
    response: Response,
    _principal: Principal = Depends(require_auth),
    _ready: None = Depends(require_ready),
) -> Response:
    container = request.app.state.container
    revision = container.catalog.revision
    etag = f'"{revision}"'

    if request.headers.get("if-none-match") == etag:
        return Response(status_code=304, headers={"ETag": etag})

    body = {
        "object": "list",
        "catalog_revision": revision,
        "data": container.catalog.public_list(),
    }
    return JSONResponse(content=body, headers={"ETag": etag})


class ResolveRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    protocol_version: int | None = None
    session_id: str | None = Field(default=None, max_length=256)
    turn_id: str | None = Field(default=None, max_length=256)
    instruction_revision: int = Field(default=0, ge=0)
    state: str | None = Field(default=None, max_length=16384)
    action: Literal["find", "load", "clear"]
    query: str | None = Field(default=None, max_length=8192)
    skill_ids: list[str] | None = None


@router.post("/v1/daintree/skills/resolve")
async def resolve_skills(
    body: ResolveRequest,
    request: Request,
    principal: Principal = Depends(require_auth),
    _ready: None = Depends(require_ready),
    _protocol: None = Depends(check_protocol_header),
) -> JSONResponse:
    check_protocol_value(body.protocol_version)

    if body.action == "find" and not (body.query or "").strip():
        raise invalid_request("A 'find' action requires a non-empty query.", param="query")
    if body.action == "load" and not body.skill_ids:
        raise invalid_request("A 'load' action requires skill_ids.", param="skill_ids")

    container = request.app.state.container
    result = await container.orchestrator.resolve(
        action=body.action,
        session_id=body.session_id,
        state_token=body.state,
        turn_id=body.turn_id,
        instruction_revision=body.instruction_revision,
        query=body.query,
        skill_ids=body.skill_ids,
        # The middleware-resolved id, so the body and X-Request-Id always agree.
        # Reading the raw header instead mints a SECOND id when the client sends none.
        request_id=request_id(request),
    )
    return JSONResponse(content=result)
