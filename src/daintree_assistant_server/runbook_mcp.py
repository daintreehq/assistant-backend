"""The runbook-search MCP server: JSON-RPC dispatch, transport-agnostic.

One tool, ``search_runbooks``, which runs the runbook selector's two-stage
search over a query and returns the top runbooks as JSON — bodies included, since
the body IS the runbook. Nothing else: no model call, no session, no state.
The HTTP half (``api/mcp.py``) owns headers and status codes; this
module owns what a JSON-RPC message means.

**Stateless, and serving both eras of the protocol from one endpoint.** MCP
revision 2026-07-28 removed the ``initialize`` handshake and sessions: every
request carries its protocol version (the ``MCP-Protocol-Version`` header and
``params._meta["io.modelcontextprotocol/protocolVersion"]``) and discovery is
``server/discover``. The 2025 revisions still open with ``initialize``, and
several hosted clients still speak them. Because this server holds NO session
state in either era, supporting both costs one extra method each way: a legacy
``initialize`` is answered and forgotten, a ``tools/call`` is answered the same
whether or not one preceded it, and no ``Mcp-Session-Id`` is ever issued. That
is also what makes it safe on Cloud Run, where consecutive requests land on
different instances.

**Every response is plain ``application/json``**, never SSE. The spec lets a
server choose per request, and a search that answers in ~10ms has no progress to
stream. Consequently there is no GET stream to resume and no session to DELETE.
"""

from __future__ import annotations

import json
import re
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any

from . import __version__
from .runbooks.catalog import Catalog
from .runbooks.local.selector import RunbookSearchHit

#: Newest first. The first entry is what an ``initialize`` naming a version we do
#: not know is answered with (the spec's rule: reply with one you DO support and
#: let the client decide), and what ``server/discover`` leads with.
SUPPORTED_PROTOCOL_VERSIONS: tuple[str, ...] = (
    "2026-07-28",
    "2025-11-25",
    "2025-06-18",
    "2025-03-26",
)

#: The revisions that open with ``initialize``. 2026-07-28 does not, so a legacy
#: handshake is answered with the newest of THESE rather than of all.
_HANDSHAKE_VERSIONS: tuple[str, ...] = SUPPORTED_PROTOCOL_VERSIONS[1:]

#: What the transport spec says to assume when a request carries no
#: ``MCP-Protocol-Version`` header at all: the revision before the header existed.
DEFAULT_PROTOCOL_VERSION = "2025-03-26"

_META_VERSION_KEY = "io.modelcontextprotocol/protocolVersion"
_VERSION_SHAPE = re.compile(r"\d{4}-\d{2}-\d{2}")

TOOL_NAME = "search_runbooks"
DEFAULT_MAX_RESULTS = 5
#: Bounded by the ballot, not by taste: the ranking is the head's view of the
#: cosine shortlist (32 candidates today), so a larger number would be a promise
#: the search cannot keep once the catalog outgrows the shortlist.
MAX_RESULTS_CAP = 20
#: The encoder reads 256 tokens and ``fit_query`` shrinks deliberately to that;
#: this is only a ceiling on what is worth parsing.
MAX_QUERY_CHARS = 8192

SERVER_INFO = {"name": "daintree-runbooks", "title": "Daintree Runbooks", "version": __version__}

INSTRUCTIONS = (
    "Daintree runbooks are step-by-step procedures for work in Daintree and its "
    "agent workspace. Call search_runbooks with a plain description of the task at "
    "hand; each result carries the runbook's full procedure under `instructions`. "
    "Runbooks name Daintree tools in their steps — follow a runbook only where you "
    "hold the tools it uses."
)

TOOL_DEFINITION: dict[str, Any] = {
    "name": TOOL_NAME,
    "title": "Search Daintree runbooks",
    "description": (
        "Find the Daintree runbooks that fit a task. Describe the task the way a user "
        "would ask for it (\"rename my terminals so I can tell the agents apart\"), "
        "not as keywords. Returns up to max_results runbooks, best first, each with "
        "its full step-by-step procedure in `instructions`. `score` is the ranker's "
        "confidence (0-1) and `selected` is true for the runbooks Daintree's own "
        "automatic selection would load for this request — prefer those, and treat "
        "an empty `selected` set as \"no runbook really fits\"."
    ),
    "inputSchema": {
        "type": "object",
        "properties": {
            "query": {
                "type": "string",
                "minLength": 1,
                "maxLength": MAX_QUERY_CHARS,
                "description": "The task, in natural language.",
            },
            "max_results": {
                "type": "integer",
                "minimum": 1,
                "maximum": MAX_RESULTS_CAP,
                "default": DEFAULT_MAX_RESULTS,
                "description": "The most runbooks to return.",
            },
        },
        "required": ["query"],
        "additionalProperties": False,
    },
    "outputSchema": {
        "type": "object",
        "properties": {
            "catalog_revision": {"type": "string"},
            "runbooks": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "id": {"type": "string"},
                        "title": {"type": "string"},
                        "summary": {"type": "string"},
                        "when_to_use": {"type": "string"},
                        "foundation": {"type": "boolean"},
                        "required_tools": {"type": "array", "items": {"type": "string"}},
                        "score": {"type": "number"},
                        "selected": {"type": "boolean"},
                        "instructions": {"type": "string"},
                    },
                    "required": [
                        "id",
                        "title",
                        "summary",
                        "when_to_use",
                        "foundation",
                        "required_tools",
                        "score",
                        "selected",
                        "instructions",
                    ],
                },
            },
        },
        "required": ["catalog_revision", "runbooks"],
    },
    "annotations": {
        "readOnlyHint": True,
        "destructiveHint": False,
        "idempotentHint": True,
        "openWorldHint": False,
    },
}

_CAPABILITIES: dict[str, Any] = {"tools": {"listChanged": False}}

#: Caching hints 2026-07-28 requires on `tools/list` and `server/discover`
#: (`CacheableResult`). The tool list and capabilities change only with a
#: deploy and are identical for every caller, so they are safe to share.
_CACHE_HINTS: dict[str, Any] = {"ttlMs": 300_000, "cacheScope": "public"}

# JSON-RPC 2.0 codes, plus the two MCP 2026-07-28 added for the HTTP transport.
PARSE_ERROR = -32700
INVALID_REQUEST = -32600
METHOD_NOT_FOUND = -32601
INVALID_PARAMS = -32602
INTERNAL_ERROR = -32603
HEADER_MISMATCH = -32020
UNSUPPORTED_PROTOCOL_VERSION = -32022

SearchFn = Callable[[str, int], Awaitable[list[RunbookSearchHit] | None]]


@dataclass(frozen=True)
class McpReply:
    """What the transport should send: a status and a body, or 202 and nothing."""

    status: int
    body: dict[str, Any] | None
    #: For the log line only — never echoed to the client.
    method: str | None = None
    results: int | None = None


@dataclass(frozen=True)
class RequestHeaders:
    """The MCP headers, as the transport read them. ``None`` means absent."""

    protocol_version: str | None = None
    method: str | None = None
    name: str | None = None


def error_reply(
    request_id: Any, code: int, message: str, *, status: int = 200, data: Any = None
) -> McpReply:
    error: dict[str, Any] = {"code": code, "message": message}
    if data is not None:
        error["data"] = data
    return McpReply(status=status, body={"jsonrpc": "2.0", "id": request_id, "error": error})


def _result(
    request_id: Any, result: dict[str, Any], *, method: str, results: int | None = None
) -> McpReply:
    # `resultType` is required on every 2026-07-28 result; clients of the
    # earlier revisions ignore a field they do not know.
    return McpReply(
        status=200,
        body={
            "jsonrpc": "2.0",
            "id": request_id,
            "result": {"resultType": "complete", **result},
        },
        method=method,
        results=results,
    )


def _unsupported(request_id: Any, requested: str) -> McpReply:
    data: dict[str, Any] = {"supported": list(SUPPORTED_PROTOCOL_VERSIONS)}
    # Echoed only when it has the shape of a revision date. The header is a
    # caller-controlled string, and the likeliest malformed one is a credential
    # pasted into the wrong header.
    if _VERSION_SHAPE.fullmatch(requested):
        data["requested"] = requested
    return error_reply(
        request_id,
        UNSUPPORTED_PROTOCOL_VERSION,
        "Unsupported protocol version.",
        status=400,
        data=data,
    )


def _valid_id(value: Any) -> bool:
    # bool is an int subclass; JSON `true` is not a request id.
    return isinstance(value, str) or (isinstance(value, int) and not isinstance(value, bool))


async def handle_message(
    message: Any,
    *,
    headers: RequestHeaders,
    catalog: Catalog,
    search: SearchFn,
) -> McpReply:
    """Answer ONE JSON-RPC message.

    Batches are refused rather than unrolled: 2025-06-18 removed them from the
    protocol, and no client of the revisions this serves may send one.
    """
    if isinstance(message, list):
        return error_reply(None, INVALID_REQUEST, "JSON-RPC batches are not supported.", status=400)
    if not isinstance(message, dict) or message.get("jsonrpc") != "2.0":
        return error_reply(None, INVALID_REQUEST, "Not a JSON-RPC 2.0 message.", status=400)

    method = message.get("method")
    has_id = "id" in message
    if not isinstance(method, str):
        # A client's RESPONSE to a server request. This server never sends one,
        # but the transport says a response is accepted with 202, not refused.
        if has_id and ("result" in message or "error" in message):
            return McpReply(status=202, body=None)
        return error_reply(None, INVALID_REQUEST, "Missing 'method'.", status=400)

    if not has_id:
        # A notification (`notifications/initialized`, `notifications/cancelled`,
        # ...). Nothing here has state for one to change; the transport answer is
        # 202 with no body, and a JSON-RPC body would be a protocol violation.
        return McpReply(status=202, body=None, method=method)

    request_id = message["id"]
    if not _valid_id(request_id):
        return error_reply(None, INVALID_REQUEST, "'id' must be a string or integer.", status=400)

    params = message.get("params", {})
    if params is None:
        params = {}
    if not isinstance(params, dict):
        return error_reply(request_id, INVALID_PARAMS, "'params' must be an object.")

    # Header-to-body agreement (2026-07-28). Checked only where the client sent
    # the header: a 2025 client never does, and demanding it would refuse every
    # one of them.
    if headers.method is not None and headers.method != method:
        return error_reply(
            request_id, HEADER_MISMATCH, "Mcp-Method header does not match the body.", status=400
        )

    if (
        headers.protocol_version is not None
        and headers.protocol_version not in SUPPORTED_PROTOCOL_VERSIONS
    ):
        return _unsupported(request_id, headers.protocol_version)

    meta = params.get("_meta")
    meta_version = meta.get(_META_VERSION_KEY) if isinstance(meta, dict) else None
    if meta_version is not None:
        if not isinstance(meta_version, str):
            return error_reply(request_id, INVALID_PARAMS, "Protocol version must be a string.")
        if headers.protocol_version is not None and headers.protocol_version != meta_version:
            return error_reply(
                request_id,
                HEADER_MISMATCH,
                "MCP-Protocol-Version header does not match params._meta.",
                status=400,
            )
        if meta_version not in SUPPORTED_PROTOCOL_VERSIONS:
            return _unsupported(request_id, meta_version)

    if method == "initialize":
        requested = params.get("protocolVersion")
        version = requested if requested in _HANDSHAKE_VERSIONS else _HANDSHAKE_VERSIONS[0]
        return _result(
            request_id,
            {
                "protocolVersion": version,
                "capabilities": _CAPABILITIES,
                "serverInfo": SERVER_INFO,
                "instructions": INSTRUCTIONS,
            },
            method=method,
        )
    if method == "server/discover":
        return _result(
            request_id,
            {
                "supportedVersions": list(SUPPORTED_PROTOCOL_VERSIONS),
                "capabilities": _CAPABILITIES,
                "serverInfo": SERVER_INFO,
                "instructions": INSTRUCTIONS,
                "_meta": {"io.modelcontextprotocol/serverInfo": SERVER_INFO},
                **_CACHE_HINTS,
            },
            method=method,
        )
    if method == "ping":
        return _result(request_id, {}, method=method)
    if method == "tools/list":
        return _result(
            request_id, {"tools": [TOOL_DEFINITION], **_CACHE_HINTS}, method=method
        )
    if method == "tools/call":
        return await _call_tool(request_id, params, headers, catalog, search)
    return error_reply(request_id, METHOD_NOT_FOUND, f"Method not found: {method}")


async def _call_tool(
    request_id: Any,
    params: dict[str, Any],
    headers: RequestHeaders,
    catalog: Catalog,
    search: SearchFn,
) -> McpReply:
    name = params.get("name")
    if headers.name is not None and headers.name != name:
        return error_reply(
            request_id, HEADER_MISMATCH, "Mcp-Name header does not match the body.", status=400
        )
    if name != TOOL_NAME:
        # A protocol error, not a tool error: there is no tool to have failed.
        return error_reply(request_id, INVALID_PARAMS, f"Unknown tool: {name}")

    arguments = params.get("arguments", {})
    if arguments is None:
        arguments = {}
    if not isinstance(arguments, dict):
        return _tool_error(request_id, "`arguments` must be an object.")

    # Argument problems are TOOL errors (isError), not JSON-RPC errors: the spec
    # routes them back to the model, which can read the message and correct the
    # call — a protocol error would be swallowed by the client instead.
    unknown = sorted(set(arguments) - {"query", "max_results"})
    if unknown:
        return _tool_error(request_id, f"Unknown argument(s): {', '.join(unknown)}.")
    query = arguments.get("query")
    if not isinstance(query, str) or not query.strip():
        return _tool_error(request_id, "`query` must be a non-empty string.")
    if len(query) > MAX_QUERY_CHARS:
        return _tool_error(request_id, f"`query` is longer than {MAX_QUERY_CHARS} characters.")
    max_results = arguments.get("max_results", DEFAULT_MAX_RESULTS)
    if (
        not isinstance(max_results, int)
        or isinstance(max_results, bool)
        or not 1 <= max_results <= MAX_RESULTS_CAP
    ):
        return _tool_error(
            request_id, f"`max_results` must be an integer from 1 to {MAX_RESULTS_CAP}."
        )

    hits = await search(query.strip(), max_results)
    if hits is None:
        return _tool_error(
            request_id, "Runbook search is temporarily unavailable. Try again shortly."
        )

    runbooks: list[dict[str, Any]] = []
    for hit in hits:
        runbook = catalog.get(hit.runbook_id)
        if runbook is None:
            # The search ranks the same catalog this renders, so this is a guard
            # against a seam (a scripted selector) naming an id it does not hold.
            continue
        runbooks.append(
            {
                "id": runbook.id,
                "title": runbook.title,
                "summary": runbook.summary,
                "when_to_use": runbook.when_to_use,
                "foundation": runbook.meta.foundation,
                "required_tools": list(runbook.meta.required_tools),
                "score": round(hit.score, 4),
                "selected": hit.selected,
                "instructions": runbook.body,
            }
        )
    structured = {"catalog_revision": catalog.revision, "runbooks": runbooks}
    return _result(
        request_id,
        {
            # The text block is the same JSON: clients that predate
            # `structuredContent` read this one, and the spec asks for both.
            "content": [
                {"type": "text", "text": json.dumps(structured, ensure_ascii=False)}
            ],
            "structuredContent": structured,
            "isError": False,
        },
        method="tools/call",
        results=len(runbooks),
    )


def _tool_error(request_id: Any, message: str) -> McpReply:
    return McpReply(
        status=200,
        body={
            "jsonrpc": "2.0",
            "id": request_id,
            "result": {
                "resultType": "complete",
                "content": [{"type": "text", "text": message}],
                "isError": True,
            },
        },
        method="tools/call",
    )
