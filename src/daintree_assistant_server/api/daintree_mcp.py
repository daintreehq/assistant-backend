"""The runbook-search MCP endpoint — Streamable HTTP, stateless, POST only.

The protocol lives in ``services/runbook_mcp.py``; this module is the transport:
content negotiation, the Origin check, and turning a reply into a response.

Gated exactly like ``/runbooks/resolve`` (``runbooks:read``, readiness) and for
the same reason it is outside the BILLING gate: a search is the local classifier
and spends nothing. It is inside the identity gate, and so inside the
private-staging subject gate, because both dependencies run before the body is
read — a refused caller never reaches the classifier.
"""

from __future__ import annotations

import json
import time
from urllib.parse import urlsplit

from fastapi import APIRouter, Depends, Request, Response
from fastapi.responses import JSONResponse

from ..auth import PERMISSION_RUNBOOKS_READ, Principal, require_permission
from ..observability import get_logger
from ..runbooks.local.selector import RunbookSelector
from ..services.runbook_mcp import (
    INVALID_REQUEST,
    PARSE_ERROR,
    McpReply,
    RequestHeaders,
    error_reply,
    handle_message,
)
from .deps import require_ready

router = APIRouter()
logger = get_logger("daintree.mcp")


@router.post("/v1/daintree/mcp")
async def mcp_post(
    request: Request,
    _principal: Principal = Depends(require_permission(PERMISSION_RUNBOOKS_READ)),
    _ready: None = Depends(require_ready),
) -> Response:
    started = time.perf_counter()

    # DNS-rebinding defence the transport spec requires. A browser page on
    # another origin sends its own Origin; a non-browser agent sends none, and
    # nothing is lost by requiring the two to agree when one is present.
    origin = request.headers.get("origin")
    if origin is not None and urlsplit(origin).netloc != request.headers.get("host"):
        return _send(error_reply(None, INVALID_REQUEST, "Origin not allowed.", status=403))

    content_type = request.headers.get("content-type", "")
    if content_type.split(";", 1)[0].strip().lower() != "application/json":
        return _send(
            error_reply(None, INVALID_REQUEST, "Content-Type must be application/json.", status=415)
        )

    accept = request.headers.get("accept")
    if accept is not None and not _accepts_json(accept):
        return _send(
            error_reply(None, INVALID_REQUEST, "Accept must allow application/json.", status=406)
        )

    try:
        message = json.loads(await request.body())
    except (ValueError, UnicodeDecodeError):
        return _send(error_reply(None, PARSE_ERROR, "Parse error.", status=400))

    container = request.app.state.container
    selector: RunbookSelector = container.selector

    reply = await handle_message(
        message,
        headers=RequestHeaders(
            protocol_version=_header(request, "mcp-protocol-version"),
            method=_header(request, "mcp-method"),
            name=_header(request, "mcp-name"),
        ),
        # One snapshot for rendering; the selector ranks against its own, and a
        # hit the two disagree on (a reload mid-request) is dropped, not guessed.
        catalog=container.catalog_manager.catalog,
        search=lambda query, limit: selector.search(query, limit=limit),
    )
    # Metadata only. The query is user content and stays out of the log plane,
    # like every other request body here.
    logger.info(
        "mcp_request",
        rpc_method=reply.method,
        status=reply.status,
        results=reply.results,
        duration_ms=round((time.perf_counter() - started) * 1000, 1),
    )
    return _send(reply)


@router.api_route("/v1/daintree/mcp", methods=["GET", "DELETE"])
async def mcp_unsupported_method() -> Response:
    """No server-initiated stream to open (GET) and no session to end (DELETE).

    Both are what the transport spec prescribes for a stateless, JSON-only
    server: 405 with ``Allow: POST``, so a client stops trying rather than
    retrying something that will never exist.
    """
    return Response(status_code=405, headers={"Allow": "POST"})


def _send(reply: McpReply) -> Response:
    if reply.body is None:
        return Response(status_code=reply.status)
    return JSONResponse(content=reply.body, status_code=reply.status)


def _header(request: Request, name: str) -> str | None:
    value = request.headers.get(name)
    return value.strip() if value is not None else None


def _accepts_json(accept: str) -> bool:
    types = {part.split(";", 1)[0].strip().lower() for part in accept.split(",")}
    return bool(types & {"application/json", "application/*", "*/*"})
