"""Manual skill catalog and resolve endpoints (section 12)."""

from __future__ import annotations

from typing import Literal

from fastapi import APIRouter, Depends, Request, Response
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field

from ..auth import Principal, require_auth
from ..errors import DaintreeError, invalid_request
from ..observability import get_logger
from ..skills.loader import SkillLoadError
from .deps import check_protocol_header, check_protocol_value, request_id, require_ready

router = APIRouter()
logger = get_logger("daintree.skills")


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


# Kept on its own router because mounting is the security boundary: `create_app`
# includes this one only when a skill-overlay directory is configured and the
# environment is not hardened, so production never exposes it. A runtime guard
# inside a route production still serves would be a weaker version of the same
# idea — and `require_auth` cannot carry it, since it gates nothing yet.
reload_router = APIRouter()


@reload_router.post("/v1/daintree/skills/reload")
async def reload_skills(
    request: Request,
    _principal: Principal = Depends(require_auth),
    _ready: None = Depends(require_ready),
) -> JSONResponse:
    """Rebuild the catalog from disk so an overlay edit takes effect live.

    Fail-safe by construction: ``CatalogManager.reload`` loads and validates the
    whole new catalog before it rebinds, so a skill saved half-written leaves the
    previous catalog serving and the server ready. The author fixes the file and
    calls again — a malformed skill costs a 409, not a restart.

    Nothing else has to be told. Every consumer reads the catalog through the
    manager, and the local selector re-embeds only what changed on its next
    selection, keyed off the revision.
    """
    manager = request.app.state.container.catalog_manager
    try:
        previous, catalog = await manager.reload()
    except (SkillLoadError, OSError) as exc:
        # The catalog that is still bound, which the load never got far enough
        # to replace — so this names what the caller is still being served.
        logger.warning(
            "skill_reload_failed", error=str(exc), catalog_revision=manager.catalog.revision
        )
        raise DaintreeError(
            status_code=409,
            message=f"Skill catalog reload failed; the previous catalog is still active: {exc}",
            type="api_error",
            code="skill_catalog_reload_failed",
        ) from exc

    changed = catalog.revision != previous
    logger.info(
        "skill_catalog_reloaded",
        previous_catalog_revision=previous,
        catalog_revision=catalog.revision,
        changed=changed,
        skills=len(catalog),
    )
    return JSONResponse(
        content={
            "object": "skill_catalog_reload",
            "previous_catalog_revision": previous,
            "catalog_revision": catalog.revision,
            "changed": changed,
            "skills": len(catalog),
        }
    )
