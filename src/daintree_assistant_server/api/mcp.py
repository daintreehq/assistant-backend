"""The runbook-search MCP endpoint — Streamable HTTP, POST only.

The protocol lives in ``runbook_mcp.py``; this module is the transport: content
negotiation, the Origin check, session ids, and turning a reply into a response.

A successful legacy ``initialize`` is answered with an ``Mcp-Session-Id``, and a
request carrying one gets that session's record of what it was already sent
(``mcp_sessions.py``). The id changes what a reply repeats, never whether a
request is served: an unknown id is adopted, and a missing one is no session.

Every request uses the current catalog. Daintree's coarse host state narrows
search and determines whether the hosts foundation accompanies other procedures.
It is read per request and never stored in the delivery session.

Open by design: anyone can add this server to their agent. A search is the local
classifier over public runbooks, so there is no credential to hold and nothing a
caller can spend.
"""

from __future__ import annotations

import json
import time
from urllib.parse import urlsplit

from fastapi import APIRouter, Request, Response
from fastapi.responses import JSONResponse

from ..mcp_sessions import SessionMemory
from ..observability import get_logger
from ..runbook_mcp import (
    INTERNAL_ERROR,
    INVALID_REQUEST,
    PARSE_ERROR,
    McpReply,
    RequestHeaders,
    error_reply,
    handle_message,
)
from ..runbooks.catalog import view_for
from ..runbooks.host_state import HOSTS_HEADER, parse_host_state

router = APIRouter()
logger = get_logger("daintree.mcp")


@router.post("/v1/daintree/mcp")
async def mcp_post(request: Request) -> Response:
    started = time.perf_counter()

    if not request.app.state.ready:
        return _send(
            error_reply(None, INTERNAL_ERROR, "Runbook search is starting up.", status=503)
        )

    # DNS-rebinding defence the transport spec requires. A browser page on
    # another origin sends its own Origin; a non-browser agent sends none, and
    # nothing is lost by requiring the two to agree when one is present.
    origin = request.headers.get("origin")
    if origin is not None and _origin_netloc(origin) != request.headers.get("host"):
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
    except (ValueError, UnicodeDecodeError, RecursionError):
        # RecursionError: a few KiB of `[[[[…` nests past the parser's limit,
        # well inside the body cap.
        return _send(error_reply(None, PARSE_ERROR, "Parse error.", status=400))

    container = request.app.state.container
    sessions: SessionMemory = container.sessions
    values = request.headers.getlist(HOSTS_HEADER)
    host_state = parse_host_state(values[0]) if len(values) == 1 else None
    view = view_for(container.catalog, host_state)
    selector = container.selector

    reply = await handle_message(
        message,
        headers=RequestHeaders(
            protocol_version=_header(request, "mcp-protocol-version"),
            method=_header(request, "mcp-method"),
            name=_header(request, "mcp-name"),
        ),
        catalog=view.catalog,
        search=lambda query, limit: selector.search(query, limit=limit, exclude=view.exclude),
        delivered=(record := sessions.delivered(_header(request, "mcp-session-id"))),
        attach=view.attach,
    )
    session_id: str | None = None
    if reply.method == "initialize" and reply.body is not None and "result" in reply.body:
        # Every handshake starts a session afresh, so a client that reconnects is
        # sent its supporting runbooks again.
        session_id = sessions.open()
    # Metadata only. The query is user content and stays out of the log plane.
    logger.info(
        "mcp_request",
        rpc_method=reply.method,
        status=reply.status,
        results=reply.results,
        hosts=host_state.hosts if host_state is not None else None,
        duration_ms=round((time.perf_counter() - started) * 1000, 1),
    )
    # Rendered first: only a reply that serialized marks its runbooks as sent.
    response = _send(reply)
    if record is not None:
        record.update(reply.sent_in_full)
    if session_id is not None:
        response.headers["Mcp-Session-Id"] = session_id
    return response


@router.api_route("/v1/daintree/mcp", methods=["GET", "DELETE"])
async def mcp_unsupported_method() -> Response:
    """No server-initiated stream to open (GET), and no session for a client to end (DELETE).

    405 with ``Allow: POST`` is what the transport spec prescribes for both: a
    JSON-only server offers no stream, and one that does not let clients terminate
    a session answers DELETE this way. Sessions are never terminated at all (no id
    is ever refused); only their delivery records age out of ``SessionMemory``,
    which costs nothing but a resend.
    """
    return Response(status_code=405, headers={"Allow": "POST"})


def _send(reply: McpReply) -> Response:
    if reply.body is None:
        return Response(status_code=reply.status)
    return JSONResponse(content=reply.body, status_code=reply.status)


def _origin_netloc(origin: str) -> str | None:
    """The Origin's host[:port], or None when it does not parse (``https://[``)."""
    try:
        return urlsplit(origin).netloc
    except ValueError:
        return None


def _header(request: Request, name: str) -> str | None:
    value = request.headers.get(name)
    return value.strip() if value is not None else None


def _accepts_json(accept: str) -> bool:
    types = {part.split(";", 1)[0].strip().lower() for part in accept.split(",")}
    return bool(types & {"application/json", "application/*", "*/*"})
