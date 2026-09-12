"""The Daintree documentation MCP, called from the server.

The public docs server (``https://daintree.org/api/mcp``) is the ONE MCP the
backend can speak to. Everything else the assistant reaches is local to the
user's machine — terminals, worktrees, the Daintree control plane behind a
per-session token — and belongs to the CLI. The docs server is no-auth,
stateless, read-only, and identical for every caller, so nothing about it needs
to run there.

Answering a "how do I use Daintree?" question server-side turns a full
round-trip (model asks for a tool → CLI executes → CLI sends the result back →
model answers) into one outbound HTTP call the user never waits a second turn
for.

**The tradeoff, stated up front:** docs answers now depend on the backend. Today
they survive a backend outage and a Daintree outage independently, and usage
questions get asked precisely when something is broken. That is the price of
removing the round-trip, and it is why every failure here is soft: a docs lookup
that errors, times out, or returns nothing simply leaves the turn without docs
context. The model still answers; it just answers unaided.

**Why this is not the MCP SDK.** The protocol's ``initialize`` handshake plus
``notifications/initialized`` is three round-trips before the one call we want,
on the turn's first-byte path. This server is stateless — it issues no
``Mcp-Session-Id`` and accepts a bare ``tools/call`` (verified against the live
endpoint 2026-08-14) — so a single POST is both correct and the whole client.
The one non-obvious wire requirement is the ``Accept`` header: the server
answers ``406 Not Acceptable`` unless the caller accepts BOTH ``application/json``
and ``text/event-stream``, even though it replies with plain JSON.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import Any

import httpx

from ..observability import get_logger
from ..text_utils import cap_bytes, sanitize_field, sanitize_line

logger = get_logger("daintree.docs")

# A fixed product URL, not configuration: the docs live at one address for every
# caller. Read at call time so a test can point it at a local fake — the same
# posture as HEARTBEAT_INTERVAL_SECONDS, and for the same reason (one shipped
# optimum, no env toggle).
DOCS_MCP_URL = "https://daintree.org/api/mcp"

# The version we speak. Sent on every call; the server echoes its own in the
# initialize response we deliberately never ask for.
DOCS_MCP_PROTOCOL_VERSION = "2025-06-18"

# How many ranked sections to keep. The server defaults to 10 and allows 100,
# both far past what belongs in a prompt: each result carries the full text of
# its chunk, and the top few are what actually answer a usage question. Kept
# small on purpose — this block rides the volatile tail, where every byte is
# re-encoded on the next turn.
DOCS_SEARCH_TOP_K = 4

# Hard ceiling for the outbound call, *further* clamped by whatever is left of
# the turn's preparation budget (see `deadlines.py`). The docs server sits in
# front of a vector index and answers in well under a second; a slow one must
# cost the turn a little first-byte latency, never the turn.
DOCS_LOOKUP_TIMEOUT_SECONDS = 4.0

# Per-section and whole-block byte ceilings. A single doc page can run to tens of
# kilobytes and the search result repeats each chunk's text under `chunks`, so an
# uncapped fold-in would dwarf the runbook bodies it sits beside.
DOCS_SECTION_MAX_BYTES = 4 * 1024
DOCS_BLOCK_MAX_BYTES = 12 * 1024

# Refuse to read a response larger than this. The output caps above bound what
# reaches the PROMPT; this bounds what reaches memory on the way there. A search
# for ten sections is tens of kilobytes, so 4 MiB is far above any real answer
# and far below a size that hurts — and a Cloud Run instance serving concurrent
# turns should not be one misbehaving upstream away from an OOM.
DOCS_RESPONSE_MAX_BYTES = 4 * 1024 * 1024

# Where a returned path lives. The full URL is built HERE and rendered into the
# prompt ready to cite, rather than the model being asked to prepend an origin to
# a path: concatenation is the step where a citation can go wrong, and it is not
# a step that needs a language model.
DOCS_SITE_ORIGIN = "https://daintree.org"

# The shape a documentation path may have — an allowlist (see `_is_site_path`).
_SITE_PATH = re.compile(r"/[A-Za-z0-9._~/-]+")
DOCS_PATH_MAX_CHARS = 200

_SEARCH_TOOL = "search"

# The server's own remedy for a snippet too thin to answer from, named in the
# `search` tool's description. Its argument is `path`, NOT `url` — the description
# says "call get_page with the result URL" and the schema requires `path`, and
# sending `url` is answered with an input-validation error (verified against the
# live endpoint 2026-09-12).
_PAGE_TOOL = "get_page"

# One request per connection, so a constant id is enough — and it is what makes
# the reply checkable: an envelope carrying a different id is not our answer.
_REQUEST_ID = 1


class DocsLookupError(Exception):
    """The docs server did not answer usefully.

    Always caught: a docs lookup is an enhancement to the turn, never a
    precondition for it.

    ``status`` is the NON-SUCCESS HTTP status that caused the failure, or
    ``None`` when the failure was not a status at all — a parse error, an
    oversized body, a JSON-RPC error inside a 200. It is deliberately not "the
    status of the response", which for those cases is 200 and would say nothing
    about what went wrong.

    It exists because not every failure deserves the same volume. A timeout, a
    5xx or an unparseable body is ordinary weather on a third-party call. A 401
    is not: it means the integration is rejected and will fail identically on
    every turn until a human acts. Logging the two at the same level is how a
    TOTAL outage hides — on 2026-08-20 this endpoint began answering 401 to every
    method, every docs lookup in production failed for hours, and the only signal
    was an `info` line indistinguishable from a search that matched nothing.
    """

    def __init__(self, message: str, *, status: int | None = None) -> None:
        super().__init__(message)
        self.status = status

    @property
    def is_permanent(self) -> bool:
        """Whether retrying the same request unchanged cannot succeed.

        Auth and permission (401/403), gone or wrong address (404/410), and the
        protocol-level refusals (405/406/415/501) — those last say the server
        will not speak the shape this client speaks, which no retry alters. A
        redirect counts too: this client refuses to follow one as a security
        posture, so a server that insists on redirecting is one we will never
        read. 5xx and 429 are excluded on purpose; they are the cases where the
        next turn genuinely may succeed.
        """
        status = self.status
        if status is None:
            return False
        return status in (401, 403, 404, 405, 406, 410, 415, 501) or 300 <= status < 400


@dataclass(frozen=True)
class DocsSection:
    """One ranked documentation section, already sanitized and capped.

    ``path`` is the site path the server returned (``/docs/worktrees``), already
    proven to be a same-site path and nothing else. ``url`` is that path resolved
    against the documentation origin — built here so the citation the model is
    handed is complete, rather than an instruction to assemble one.
    """

    path: str
    title: str
    section_title: str
    text: str

    @property
    def url(self) -> str:
        return f"{DOCS_SITE_ORIGIN}{self.path}"


@dataclass(frozen=True)
class DocsLookup:
    """A completed docs search: what was asked, and what came back."""

    query: str
    sections: list[DocsSection]


class DocsMcpClient:
    """Two tools, one POST each. See the module docstring for why it is not the SDK."""

    def __init__(self, client: httpx.AsyncClient, *, url: str | None = None) -> None:
        self._client = client
        self._url = url

    @property
    def url(self) -> str:
        # Resolved per call rather than captured at construction, so the module
        # constant stays the single source of truth (and stays monkeypatchable).
        return self._url or DOCS_MCP_URL

    async def search(
        self,
        query: str,
        *,
        top_k: int = DOCS_SEARCH_TOP_K,
        timeout: float,
    ) -> list[DocsSection]:
        """Run the docs server's ``search`` tool. Raises :class:`DocsLookupError`."""
        result = await self._call(
            _SEARCH_TOOL, {"query": query, "topK": top_k}, timeout=timeout
        )
        return _sections_from_result(result, top_k=top_k)

    async def fetch_page(self, path: str, *, timeout: float) -> str:
        """A page's indexed markdown, for when the ranked snippet cannot answer.

        The search tool's own description prescribes this: *"If snippets lack
        detail, call get_page with the result URL for the page's indexed
        markdown."* It is a SECOND round trip, so it is the caller's job to
        decide the turn can afford one — and the caller's job to treat a failure
        here as "no enrichment", never as "no documentation". See
        ``ChatOrchestrator._enrich_top_section``.

        ``path`` is re-validated rather than trusted. It reaches here from a hit
        this module already checked, but the check is cheap and this value goes
        into an outbound request: a module that validates on one path and assumes
        on the other is one refactor away from not validating at all.
        """
        if len(path) > DOCS_PATH_MAX_CHARS or not _is_site_path(path):
            raise DocsLookupError("refusing to fetch a path that is not a site path")
        result = await self._call(_PAGE_TOOL, {"path": path}, timeout=timeout)
        return _markdown_from_result(result)

    async def _call(
        self, tool: str, arguments: dict[str, Any], *, timeout: float
    ) -> dict[str, Any]:
        """One POST, one JSON-RPC result. Every wire property lives HERE.

        Both tools share it deliberately. The redirect refusal, the bounded read,
        the id correlation and the ``isError`` check are security properties
        rather than conveniences, and a second copy of this method is a second
        place for one of them to be forgotten.
        """
        payload = {
            "jsonrpc": "2.0",
            "id": _REQUEST_ID,
            "method": "tools/call",
            "params": {"name": tool, "arguments": arguments},
        }
        request = self._client.build_request(
            "POST",
            self.url,
            json=payload,
            headers={
                "Content-Type": "application/json",
                # BOTH, or the server answers 406 — see the module docstring.
                "Accept": "application/json, text/event-stream",
                "MCP-Protocol-Version": DOCS_MCP_PROTOCOL_VERSION,
            },
            timeout=timeout,
        )
        try:
            # STREAMED rather than buffered, purely so the size bound is a bound.
            # `post()` reads and decompresses the whole body before returning, so
            # a check on the result would prevent parsing an oversized reply
            # without preventing it reaching memory first — and a Cloud Run
            # instance serving concurrent turns should not be one misbehaving
            # upstream away from an OOM.
            response = await self._client.send(
                request,
                stream=True,
                # Redirects are NOT followed, and that is the security posture
                # rather than a default left unexamined: a 3xx is the one way a
                # reply could move this request to a host we did not choose. A
                # redirecting docs server is a docs server we do not read.
                follow_redirects=False,
            )
        except httpx.HTTPError as exc:
            raise DocsLookupError(f"docs request failed: {type(exc).__name__}") from exc
        try:
            if response.status_code >= 400:
                raise DocsLookupError(
                    f"docs server returned {response.status_code}",
                    status=response.status_code,
                )
            if response.status_code >= 300:
                raise DocsLookupError(
                    f"docs server redirected ({response.status_code})",
                    status=response.status_code,
                )
            body = await _read_bounded(response)
        finally:
            await response.aclose()
        return _parse_result(body, response=response)


async def _read_bounded(response: httpx.Response) -> str:
    """Read the body, refusing to hold more than the cap in memory.

    Declared ``Content-Length`` is checked first because it is free, but it is
    only a hint — a chunked or lying response has none, so the decoded bytes are
    counted as they arrive and the read is abandoned the moment it passes the
    cap.
    """
    declared = response.headers.get("content-length")
    if declared and declared.isdigit() and int(declared) > DOCS_RESPONSE_MAX_BYTES:
        raise DocsLookupError("docs response declared a size past the bound")
    chunks: list[bytes] = []
    total = 0
    try:
        async for chunk in response.aiter_bytes():
            total += len(chunk)
            if total > DOCS_RESPONSE_MAX_BYTES:
                raise DocsLookupError("docs response exceeded the size bound")
            chunks.append(chunk)
    except httpx.HTTPError as exc:
        raise DocsLookupError(f"docs read failed: {type(exc).__name__}") from exc
    return b"".join(chunks).decode("utf-8", errors="replace")


def _parse_result(body: str, *, response: httpx.Response) -> dict[str, Any]:
    """The ``result`` object from a JSON-RPC reply, or raise.

    Shared by both tools: everything here is about the ENVELOPE, and neither the
    correlation check nor the tool-error check has anything to do with which tool
    was called.
    """
    envelope = _json_body(body, response.headers.get("content-type") or "")
    # Correlate the reply to the request. A JSON-RPC message carrying someone
    # else's id is not this search's answer, and folding it in would put an
    # unrelated payload in front of the model as Daintree documentation.
    if envelope.get("id") != _REQUEST_ID:
        raise DocsLookupError("docs response did not answer this request")
    if "error" in envelope:
        error = envelope.get("error")
        message = error.get("message") if isinstance(error, dict) else None
        raise DocsLookupError(f"docs server error: {message or 'unknown'}")
    result = envelope.get("result")
    if not isinstance(result, dict):
        raise DocsLookupError("docs response carried no result")
    # A tool that failed reports it INSIDE a successful JSON-RPC result. Treating
    # that as success would fold an error message into the prompt as if it were
    # documentation.
    if result.get("isError"):
        raise DocsLookupError("docs server reported a tool error")
    return result


def _markdown_from_result(result: dict[str, Any]) -> str:
    """The page's markdown, sanitized and capped to a section's budget.

    Capped to ``DOCS_SECTION_MAX_BYTES`` rather than given a ceiling of its own:
    this text REPLACES a section's text, so it has to live inside a section's
    budget or the block cap stops meaning what it says. A real page runs to tens
    of kilobytes, so this truncates, and truncating from the front is deliberate —
    a documentation page opens with what it is and how to do the thing, and the
    reference tail is what a reader skips too.
    """
    for block in result.get("content") or []:
        if not isinstance(block, dict) or block.get("type") != "text":
            continue
        try:
            payload = json.loads(block.get("text") or "")
        except (json.JSONDecodeError, TypeError):
            continue
        if not isinstance(payload, dict):
            continue
        markdown, _ = sanitize_field(_as_text(payload.get("markdown")), DOCS_SECTION_MAX_BYTES)
        if markdown.strip():
            return markdown
        # A page with no markdown is not an error to retry — it is a page with
        # nothing to add, and the ranked snippet we already hold stands.
        raise DocsLookupError("docs page carried no markdown")
    raise DocsLookupError("docs page response carried no content")


def _json_body(body: str, content_type: str) -> dict[str, Any]:
    """Decode the JSON-RPC envelope from either transport shape.

    The live server replies ``application/json``, but Streamable HTTP permits a
    ``text/event-stream`` reply to the same POST, and a server that switched
    would otherwise turn every docs answer into a parse failure. Handling both is
    a few lines; discovering the difference in production is not.
    """
    if "text/event-stream" in content_type.lower():
        return _sse_message(body)
    try:
        parsed = json.loads(body)
    except ValueError as exc:
        raise DocsLookupError("docs response was not JSON") from exc
    if not isinstance(parsed, dict):
        # A JSON-RPC BATCH (a top-level array) lands here too. Nothing we send
        # can produce one, and guessing which element answered us is worse than
        # answering without documentation.
        raise DocsLookupError("docs response was not a JSON object")
    return parsed


def _sse_message(body: str) -> dict[str, Any]:
    """The first JSON-RPC response carried by an event stream.

    ``data:`` may repeat within one event, and the spec joins those lines with a
    newline before the payload is parsed — so a message split across lines has to
    be reassembled rather than parsed line by line, or a large result would
    silently look like garbage.
    """
    for event in body.replace("\r\n", "\n").split("\n\n"):
        data = "\n".join(
            line[len("data:") :].lstrip()
            for line in event.split("\n")
            if line.startswith("data:")
        )
        if not data:
            continue
        try:
            payload = json.loads(data)
        except json.JSONDecodeError:
            continue
        if isinstance(payload, dict) and ("result" in payload or "error" in payload):
            return payload
    raise DocsLookupError("docs event stream carried no JSON-RPC message")


def _sections_from_result(result: dict[str, Any], *, top_k: int) -> list[DocsSection]:
    """Pull ranked sections out of the MCP content blocks.

    The tool returns its payload as a JSON *string* inside a text content block —
    an MCP convention, not a quirk — so this decodes one level deeper than the
    envelope. Each raw hit also carries `score`, `routeFile` and a `chunks` array
    that repeats the text it already gave us; all of it is dropped. What reaches
    the model is what a person answering the question would quote.
    """
    for block in result.get("content") or []:
        if not isinstance(block, dict) or block.get("type") != "text":
            continue
        try:
            payload = json.loads(block.get("text") or "")
        except (json.JSONDecodeError, TypeError):
            continue
        if not isinstance(payload, dict):
            continue
        hits = payload.get("results")
        if not isinstance(hits, list):
            continue
        sections: list[DocsSection] = []
        for hit in hits:
            if len(sections) >= top_k:
                break
            if not isinstance(hit, dict):
                continue
            section = _section_from_hit(hit)
            if section is not None:
                sections.append(section)
        return sections
    raise DocsLookupError("docs response carried no search results")


def _is_site_path(path: str) -> bool:
    """Whether ``path`` is a same-site documentation path and nothing else.

    An allowlist, not a blocklist, because this value becomes a URL the user is
    invited to click. ``startswith("/")`` is nowhere near enough: ``//evil.com/x``
    and ``/\\evil.com/x`` are both PROTOCOL-RELATIVE URLs that navigate off-site,
    and percent-encoding, whitespace and Unicode slash/colon confusables all give
    a reader (human or model) ambiguous material to resolve. Real documentation
    paths are ASCII slugs, so requiring exactly that rejects every one of those
    spellings at once without a list of tricks to keep current.
    """
    return bool(_SITE_PATH.fullmatch(path)) and "//" not in path


def _section_from_hit(hit: dict[str, Any]) -> DocsSection | None:
    # Validated RAW, before any capping. Truncating first would silently turn an
    # over-long path into a shorter one that is still well-formed — a citation to
    # a different page, or to none, presented with full confidence. Length is a
    # reason to drop the hit, not to trim it.
    path = _as_text(hit.get("url")).strip()
    # A hit with no path cannot be cited, and an off-site one must not be
    # presented as Daintree documentation. Both are dropped rather than repaired:
    # this content is quoted to the user, so a wrong URL is worse than one fewer
    # result.
    if len(path) > DOCS_PATH_MAX_CHARS or not _is_site_path(path):
        return None
    # `chunkText` FIRST, `snippet` as the fallback — and today every hit takes the
    # fallback, because the live server returns no `chunkText` at all (measured
    # 2026-09-12 in both `groupBy` modes: hits carry url, title, sectionTitle,
    # snippet, score, chunks). It is kept rather than deleted because the tests
    # above were written against a response that had it, so an older server
    # contract is the likeliest explanation and a client that stops reading a
    # richer field the day it returns is a silent downgrade.
    #
    # What matters is what the fallback COSTS: `snippet` is a ~200-character
    # preview, ellipsized, and on many questions it names the right page while
    # stating none of its steps. That is why `fetch_page` exists — the caller
    # enriches the top section when the turn can afford a second round trip.
    text, _ = sanitize_field(
        _as_text(hit.get("chunkText")) or _as_text(hit.get("snippet")),
        DOCS_SECTION_MAX_BYTES,
    )
    if not text.strip():
        return None
    return DocsSection(
        path=path,
        title=sanitize_line(_as_text(hit.get("title")), 200),
        section_title=sanitize_line(_as_text(hit.get("sectionTitle")), 200),
        text=text,
    )


def _as_text(value: Any) -> str:
    return value if isinstance(value, str) else ""


def render_docs_sections(sections: list[DocsSection]) -> str:
    """Flatten sections to the text that goes in the prompt, under one cap.

    The whole-block cap is applied after joining rather than per section: three
    short sections and one long one is a normal result shape, and a per-section
    budget would truncate the long one while leaving the budget unspent.
    """
    parts: list[str] = []
    for section in sections:
        heading = " — ".join(p for p in (section.title, section.section_title) if p)
        # The complete URL, not a path plus an instruction to prepend an origin.
        parts.append(f"## {heading or section.path}\nURL: {section.url}\n\n{section.text}")
    capped, truncated = cap_bytes("\n\n".join(parts), DOCS_BLOCK_MAX_BYTES)
    if truncated:
        logger.info("docs_context_truncated", sections=len(sections))
    return capped
