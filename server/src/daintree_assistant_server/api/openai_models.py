"""The OpenAI-compatible models listing."""

from __future__ import annotations

from fastapi import APIRouter, Depends, Request

from ..auth import Principal, require_auth
from ..contracts.openai import Model, ModelList

router = APIRouter()


@router.get("/v1/models", response_model=ModelList)
async def list_models(request: Request, _principal: Principal = Depends(require_auth)) -> ModelList:
    settings = request.app.state.settings
    return ModelList(data=[Model(id=settings.public_model_id, owned_by="daintree")])
