"""The runbook MCP server: JSON-RPC dispatch, transport-agnostic.

Two read-only tools. ``search_runbooks`` runs the runbook selector's two-stage
search over a task description and returns the top runbooks as JSON: the
selected ones with their bodies, since the body IS the runbook, and the
unselected neighbours by id and title only. ``get_runbook`` returns one
runbook by id, with no ranking involved. Both deliver the instruction
dependencies (domain foundation and declared ``requires``) of each runbook they
deliver in full, once, as ``supporting_runbooks``. Nothing else: no model call
and no workspace action — the runbooks tell a caller which Daintree app actions
to take; this server takes none. The HTTP half (``api/mcp.py``) owns headers,
status codes and session ids; this module owns what a JSON-RPC message means.

**Serving both eras of the protocol from one endpoint, with no state a request
depends on.** MCP revision 2026-07-28 removed the ``initialize`` handshake and
sessions: every request carries its protocol version (the ``MCP-Protocol-Version``
header and ``params._meta["io.modelcontextprotocol/protocolVersion"]``) and
discovery is ``server/discover``. The 2025 revisions still open with
``initialize``, and several hosted clients still speak them. The one thing a 2025
session buys is not resending a supporting runbook the session already received in
full (``mcp_sessions.py``); every answer is otherwise the same whether or not a
handshake preceded it, and an unknown session only means sending everything. That
is what keeps it safe on Cloud Run, where consecutive requests land on different
instances.

**Every response is plain ``application/json``**, never SSE. The spec lets a
server choose per request, and a search that answers in ~10ms has no progress to
stream. Consequently there is no GET stream to resume and no session to DELETE.
"""

from __future__ import annotations

import json
import re
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from . import __version__
from .runbooks.catalog import Catalog
from .runbooks.local.selector import RunbookSearchHit
from .runbooks.models import Runbook

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
GET_TOOL_NAME = "get_runbook"
TOOL_NAMES: tuple[str, ...] = (TOOL_NAME, GET_TOOL_NAME)
#: Three, not more: a selected hit carries its full procedure plus its supporting
#: runbooks, and even a neighbour's name and summary is context the caller pays for.
DEFAULT_MAX_RESULTS = 3
#: Bounded by the ballot, not by taste: the ranking is the head's view of the
#: cosine shortlist (32 candidates today), so a larger number would be a promise
#: the search cannot keep once the catalog outgrows the shortlist.
MAX_RESULTS_CAP = 20
#: The encoder reads 256 tokens and ``fit_query`` shrinks deliberately to that;
#: this is only a ceiling on what is worth parsing.
MAX_QUERY_CHARS = 8192
#: Runbook ids are dotted identifiers; anything longer is not one.
MAX_ID_CHARS = 256

SERVER_INFO = {"name": "daintree-runbooks", "title": "Daintree Runbooks", "version": __version__}

INSTRUCTIONS = (
    "Daintree runbooks are procedures for orchestrating work in the Daintree app: "
    "launching and prompting visible agents, waiting for them through Daintree "
    "notices, worktrees, reviews, issue and PR workflows, and handing project work to "
    "an agent. Call search_runbooks with a "
    "short, self-contained description of the outcome wanted (not a transcript, "
    "terminal output or credentials) and follow a result marked `selected`; call "
    "get_runbook for a runbook you already know by id. Every response carries the "
    "runbooks' prerequisite instructions once, in `supporting_runbooks` — read those "
    "first; one this session was already sent in full comes back as its id and a note. "
    "This server supplies instructions only: the steps call the separate "
    "Daintree app MCP, and a runbook grants no permission. Use the tools and "
    "arguments your Daintree connection actually offers."
)

_STRING_LIST: dict[str, Any] = {"type": "array", "items": {"type": "string"}}

#: The fields every served runbook carries, whichever tool served it.
_DOCUMENT_PROPERTIES: dict[str, Any] = {
    "id": {"type": "string"},
    "title": {"type": "string"},
    "summary": {"type": "string"},
    "when_to_use": {"type": "string"},
    "foundation": {"type": "boolean"},
    "risk": {"type": "string"},
    "required_tools": _STRING_LIST,
    "optional_tools": _STRING_LIST,
    "requires": _STRING_LIST,
    "content_hash": {"type": "string"},
    "instructions": {"type": "string"},
}
_DOCUMENT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": _DOCUMENT_PROPERTIES,
    "required": list(_DOCUMENT_PROPERTIES),
    "additionalProperties": False,
}
#: A selected hit is the runbook to follow, so it carries the whole document.
_SELECTED_HIT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        **_DOCUMENT_PROPERTIES,
        "score": {"type": "number"},
        "selected": {"const": True},
    },
    "required": [*_DOCUMENT_PROPERTIES, "score", "selected"],
    "additionalProperties": False,
}
#: An unselected hit is a neighbour the caller should not follow, so it is named,
#: not delivered: its procedure, and even its summary, would cost the caller
#: context for nothing, and ``get_runbook`` fetches it in the rare case it is wanted.
_NEIGHBOUR_PROPERTIES: dict[str, Any] = {
    "id": {"type": "string"},
    "title": {"type": "string"},
    "score": {"type": "number"},
    "selected": {"const": False},
}
_NEIGHBOUR_HIT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": _NEIGHBOUR_PROPERTIES,
    "required": list(_NEIGHBOUR_PROPERTIES),
    "additionalProperties": False,
}
_HIT_SCHEMA: dict[str, Any] = {"oneOf": [_SELECTED_HIT_SCHEMA, _NEIGHBOUR_HIT_SCHEMA]}

#: A supporting runbook is read because a delivered runbook needs it, not chosen,
#: so its routing text, flags and tool lists would be paid for and never used.
_SUPPORTING_PROPERTIES: dict[str, Any] = {
    "id": {"type": "string"},
    "title": {"type": "string"},
    "content_hash": {"type": "string"},
    "instructions": {"type": "string"},
}
_SUPPORTING_FULL_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": _SUPPORTING_PROPERTIES,
    "required": list(_SUPPORTING_PROPERTIES),
    "additionalProperties": False,
}
#: One this session already received in full: the caller still has it in context.
_SUPPORTING_SENT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {"id": {"type": "string"}, "note": {"type": "string"}},
    "required": ["id", "note"],
    "additionalProperties": False,
}
_SUPPORTING_LIST_SCHEMA: dict[str, Any] = {
    "type": "array",
    "items": {"oneOf": [_SUPPORTING_FULL_SCHEMA, _SUPPORTING_SENT_SCHEMA]},
}
SENT_EARLIER_NOTE = (
    "Sent in full earlier in this session. Follow that copy; if its full text is not "
    "in your own context (a subagent, a compacted or cleared conversation), "
    "get_runbook this id first."
)

_READ_ONLY_ANNOTATIONS: dict[str, Any] = {
    "readOnlyHint": True,
    "destructiveHint": False,
    "idempotentHint": True,
    "openWorldHint": False,
}

TOOL_DEFINITION: dict[str, Any] = {
    "name": TOOL_NAME,
    "title": "Search Daintree runbooks",
    "description": (
        "Find the Daintree runbooks that fit a task. Describe the outcome the way a "
        'user would ask for it ("ask one agent a question and bring back its '
        'answer"), not as keywords, and keep it short: no transcripts, terminal '
        "output or secrets. Keep how the user wants it done, not just the code change: "
        "several agents, a time limit, a running agent, a review loop, a plugin skill, a "
        "slash command or another machine (say remote host, whatever the user calls it) "
        "can decide which procedure fits. Returns up to max_results runbooks, "
        "best first. `selected` is true for the runbooks the classifier judges actually "
        "fit: those carry their "
        "full procedure in `instructions`, so follow them. The rest are neighbours, "
        "named by id and title only (get_runbook fetches one if it is really wanted), and "
        "an empty selected set means no runbook really fits. `score` is the ranker's "
        "confidence (0-1). `supporting_runbooks` holds the prerequisite instructions "
        "(the foundation) of the selected runbooks, once each; one already sent in full "
        "earlier in this session comes back as its id and a `note` instead."
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
                "description": "The most ranked runbooks to return (supporting ones are extra).",
            },
        },
        "required": ["query"],
        "additionalProperties": False,
    },
    "outputSchema": {
        "type": "object",
        "properties": {
            "catalog_revision": {"type": "string"},
            "runbooks": {"type": "array", "items": _HIT_SCHEMA},
            "supporting_runbooks": _SUPPORTING_LIST_SCHEMA,
        },
        "required": ["catalog_revision", "runbooks", "supporting_runbooks"],
        "additionalProperties": False,
    },
    "annotations": _READ_ONLY_ANNOTATIONS,
}

GET_TOOL_DEFINITION: dict[str, Any] = {
    "name": GET_TOOL_NAME,
    "title": "Get a Daintree runbook by id",
    "description": (
        'Return one Daintree runbook by its exact id (e.g. "daintree.foundation"), '
        "with its full procedure in `instructions` and its prerequisite instructions in "
        "`supporting_runbooks` (by id and a note when this session already has them). "
        "Use it for a runbook named by another runbook, a search result or a checkpoint, "
        "or one whose instructions are no longer in your context; compare `content_hash` "
        "with the one you recorded to see whether the procedure changed. An unknown id is "
        "an error — search instead."
    ),
    "inputSchema": {
        "type": "object",
        "properties": {
            "id": {
                "type": "string",
                "minLength": 1,
                "maxLength": MAX_ID_CHARS,
                "description": "The runbook id, exactly as a runbook or search result gave it.",
            },
        },
        "required": ["id"],
        "additionalProperties": False,
    },
    "outputSchema": {
        "type": "object",
        "properties": {
            "catalog_revision": {"type": "string"},
            "runbook": _DOCUMENT_SCHEMA,
            "supporting_runbooks": _SUPPORTING_LIST_SCHEMA,
        },
        "required": ["catalog_revision", "runbook", "supporting_runbooks"],
        "additionalProperties": False,
    },
    "annotations": _READ_ONLY_ANNOTATIONS,
}

TOOL_DEFINITIONS: tuple[dict[str, Any], ...] = (TOOL_DEFINITION, GET_TOOL_DEFINITION)

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
    #: (runbook id, content hash) of everything this reply carries in full, for the
    #: transport to add to the session's record once the reply has been rendered.
    sent_in_full: tuple[tuple[str, str], ...] = ()


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
    request_id: Any,
    result: dict[str, Any],
    *,
    method: str,
    results: int | None = None,
    sent_in_full: tuple[tuple[str, str], ...] = (),
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
        sent_in_full=sent_in_full,
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
    delivered: Mapping[str, str] | None = None,
    attach: Sequence[str] = (),
) -> McpReply:
    """Answer ONE JSON-RPC message.

    ``catalog`` is the caller's own (``catalog.view_for``), and ``attach``
    what it gets with every runbook served in full (``Catalog.supporting_for``).

    ``delivered`` is what the caller's session was already sent in full (runbook
    id to content hash; ``mcp_sessions``), or None for no session. It is only
    read: the reply's ``sent_in_full`` says what to add to it, and the transport
    adds it once the reply is rendered, so a reply that never gets that far cannot
    mark anything as sent.

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
        return _result(request_id, {"tools": list(TOOL_DEFINITIONS), **_CACHE_HINTS}, method=method)
    if method == "tools/call":
        version = headers.protocol_version or meta_version
        if version is not None and version not in _HANDSHAKE_VERSIONS:
            # A sessionless revision: a session header it happens to carry is not
            # a session, and such a caller is always sent everything.
            delivered = None
        return await _call_tool(request_id, params, headers, catalog, search, delivered, attach)
    return error_reply(request_id, METHOD_NOT_FOUND, f"Method not found: {method}")


async def _call_tool(
    request_id: Any,
    params: dict[str, Any],
    headers: RequestHeaders,
    catalog: Catalog,
    search: SearchFn,
    delivered: Mapping[str, str] | None,
    attach: Sequence[str],
) -> McpReply:
    name = params.get("name")
    if headers.name is not None and headers.name != name:
        return error_reply(
            request_id, HEADER_MISMATCH, "Mcp-Name header does not match the body.", status=400
        )
    if name not in TOOL_NAMES:
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
    if name == GET_TOOL_NAME:
        return _get_runbook(request_id, arguments, catalog, delivered, attach)
    return await _search_runbooks(request_id, arguments, catalog, search, delivered, attach)


async def _search_runbooks(
    request_id: Any,
    arguments: dict[str, Any],
    catalog: Catalog,
    search: SearchFn,
    delivered: Mapping[str, str] | None,
    attach: Sequence[str],
) -> McpReply:
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
    selected: list[Runbook] = []
    for hit in hits:
        runbook = catalog.get_public(hit.runbook_id)
        if runbook is None:
            # The search ranks the same catalog this renders, so this is a guard
            # against a seam (a scripted selector) naming an id it does not hold.
            continue
        score = round(hit.score, 4)
        if hit.selected:
            runbooks.append({**_document(runbook), "score": score, "selected": True})
            selected.append(runbook)
        else:
            runbooks.append(_neighbour(runbook, score))
    # Prerequisites of what the caller will follow. A foundation ranked as an
    # unselected neighbour still arrives here in full when a selected hit needs it.
    supporting, sent = _supporting(
        catalog.supporting_for([r.id for r in selected], attach=attach), selected, delivered
    )
    structured = {
        "catalog_revision": catalog.revision,
        "runbooks": runbooks,
        "supporting_runbooks": supporting,
    }
    return _tool_result(request_id, structured, results=len(runbooks), sent_in_full=sent)


def _get_runbook(
    request_id: Any,
    arguments: dict[str, Any],
    catalog: Catalog,
    delivered: Mapping[str, str] | None,
    attach: Sequence[str],
) -> McpReply:
    unknown = sorted(set(arguments) - {"id"})
    if unknown:
        return _tool_error(request_id, f"Unknown argument(s): {', '.join(unknown)}.")
    runbook_id = arguments.get("id")
    if not isinstance(runbook_id, str) or not runbook_id.strip():
        return _tool_error(request_id, "`id` must be a non-empty runbook id.")
    runbook_id = runbook_id.strip()
    runbook = catalog.get_public(runbook_id) if len(runbook_id) <= MAX_ID_CHARS else None
    if runbook is None:
        # One answer for unknown and hidden alike: a retired runbook is not a thing
        # a caller can fetch, and saying it exists would invite retries. The id is
        # not echoed — it is caller-controlled text.
        return _tool_error(
            request_id,
            "No runbook has that id. Use search_runbooks to find one by task.",
        )
    supporting, sent = _supporting(
        catalog.supporting_for([runbook.id], attach=attach), [runbook], delivered
    )
    structured = {
        "catalog_revision": catalog.revision,
        # Always in full, even when this session has it: fetching a runbook by id is
        # how a caller that lost one from its context gets it back.
        "runbook": _document(runbook),
        "supporting_runbooks": supporting,
    }
    return _tool_result(request_id, structured, results=1, sent_in_full=sent)


def _document(runbook: Runbook) -> dict[str, Any]:
    """The served form of a runbook — identical from either tool."""
    meta = runbook.meta
    return {
        "id": runbook.id,
        "title": runbook.title,
        "summary": runbook.summary,
        "when_to_use": runbook.when_to_use,
        "foundation": meta.foundation,
        "risk": meta.risk,
        "required_tools": list(meta.required_tools),
        "optional_tools": list(meta.optional_tools),
        "requires": list(meta.requires),
        "content_hash": runbook.content_hash,
        "instructions": runbook.body,
    }


def _neighbour(runbook: Runbook, score: float) -> dict[str, Any]:
    """An unselected hit: enough to recognise it, nothing to follow."""
    return {"id": runbook.id, "title": runbook.title, "score": score, "selected": False}


def _supporting(
    supporting: list[Runbook], sent_now: list[Runbook], delivered: Mapping[str, str] | None
) -> tuple[list[dict[str, Any]], tuple[tuple[str, str], ...]]:
    """Render the prerequisites, and list everything this reply sends in full.

    ``sent_now`` is what the reply already carries in full (the selected hits, or
    the fetched runbook). A prerequisite whose exact version the session was sent
    earlier is named with a note instead; any other arrives in full.
    """
    rendered: list[dict[str, Any]] = []
    in_full = list(sent_now)
    for runbook in supporting:
        if delivered is not None and delivered.get(runbook.id) == runbook.content_hash:
            rendered.append({"id": runbook.id, "note": SENT_EARLIER_NOTE})
            continue
        rendered.append(
            {
                "id": runbook.id,
                "title": runbook.title,
                "content_hash": runbook.content_hash,
                "instructions": runbook.body,
            }
        )
        in_full.append(runbook)
    return rendered, tuple((r.id, r.content_hash) for r in in_full)


def _tool_result(
    request_id: Any,
    structured: dict[str, Any],
    *,
    results: int,
    sent_in_full: tuple[tuple[str, str], ...] = (),
) -> McpReply:
    return _result(
        request_id,
        {
            # The text block is the same JSON: clients that predate
            # `structuredContent` read this one, and the spec asks for both.
            # Compact: a caller that prints the whole result pays for both copies.
            "content": [
                {
                    "type": "text",
                    "text": json.dumps(structured, ensure_ascii=False, separators=(",", ":")),
                }
            ],
            "structuredContent": structured,
            "isError": False,
        },
        method="tools/call",
        results=results,
        sent_in_full=sent_in_full,
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
