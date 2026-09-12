"""The documentation MCP client — the one MCP the server itself calls.

Every wire fact asserted here was verified against the live endpoint
(``https://daintree.org/api/mcp``) on 2026-08-14:

* a bare ``tools/call`` is answered without an ``initialize`` handshake — the
  server is stateless and issues no ``Mcp-Session-Id``;
* the reply is ``application/json``, not an event stream;
* ``Accept`` must list BOTH ``application/json`` and ``text/event-stream`` or the
  server answers ``406 Not Acceptable``;
* the tool's payload arrives as a JSON *string* inside a text content block.

The client's whole job is to turn that into a few sanitized sections or nothing
at all. "Nothing at all" is a first-class outcome, not an error path: the turn
proceeds without documentation, which is what happened before this existed.
"""

from __future__ import annotations

import json
from typing import Any

import httpx
import pytest

from daintree_assistant_server.services.docs_mcp import (
    DOCS_BLOCK_MAX_BYTES,
    DOCS_MCP_PROTOCOL_VERSION,
    DOCS_MCP_URL,
    DOCS_SECTION_MAX_BYTES,
    DocsLookupError,
    DocsMcpClient,
    DocsSection,
    render_docs_sections,
)

# Captured verbatim from the live server (trimmed to two hits). The `chunks`
# array repeating `chunkText`, and the `score`/`routeFile` fields, are exactly
# what the client is expected to throw away.
LIVE_HIT = {
    "url": "/docs/worktrees",
    "title": "Worktrees",
    "sectionTitle": "PR Checkout",
    "snippet": "…the Create Worktree button in the Code Forge panel's PR list.",
    "chunkText": "Worktrees — PR Checkout\n\nYou can create a worktree straight from a "
    "pull request with the **Create Worktree** button.",
    "score": 2.109121,
    "routeFile": "src/routes/docs/worktrees/+page.svelte",
    "chunks": [{"sectionTitle": "PR Checkout", "chunkText": "…repeated…"}],
}


def _payload(*hits: dict[str, Any]) -> dict[str, Any]:
    """A JSON-RPC success envelope in the server's real shape."""
    return {
        "jsonrpc": "2.0",
        "id": 1,
        "result": {
            "content": [
                {
                    "type": "text",
                    # The tool's answer is a JSON STRING inside a text block — an
                    # MCP convention, so the client decodes one level deeper.
                    "text": json.dumps({"q": "worktrees", "results": list(hits)}),
                }
            ]
        },
    }


def _client(handler: Any) -> DocsMcpClient:
    transport = httpx.MockTransport(handler)
    return DocsMcpClient(httpx.AsyncClient(transport=transport))


def _ok(body: dict[str, Any]) -> Any:
    return lambda _request: httpx.Response(200, json=body)


@pytest.mark.asyncio
async def test_a_live_shaped_response_becomes_sections() -> None:
    sections = await _client(_ok(_payload(LIVE_HIT))).search("worktrees", timeout=2.0)
    assert len(sections) == 1
    assert sections[0].path == "/docs/worktrees"
    assert sections[0].title == "Worktrees"
    assert sections[0].section_title == "PR Checkout"
    assert "Create Worktree" in sections[0].text


@pytest.mark.asyncio
async def test_the_request_is_one_stateless_tools_call() -> None:
    """No initialize, no notifications/initialized — one POST.

    The handshake is three round-trips before the call we want, on the turn's
    first-byte path. The server is stateless and does not need it.
    """
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, json=_payload(LIVE_HIT))

    await _client(handler).search("worktrees", timeout=2.0)
    assert len(seen) == 1
    body = json.loads(seen[0].content)
    assert body["method"] == "tools/call"
    assert body["params"]["name"] == "search"
    assert body["params"]["arguments"]["query"] == "worktrees"


@pytest.mark.asyncio
async def test_the_accept_header_lists_both_media_types() -> None:
    """The server answers 406 without both, even though it replies with JSON.

    Asserted because it is invisible in the happy path and would only surface as
    every docs lookup silently failing in production.
    """
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, json=_payload(LIVE_HIT))

    await _client(handler).search("worktrees", timeout=2.0)
    accept = seen[0].headers["accept"]
    assert "application/json" in accept
    assert "text/event-stream" in accept
    assert seen[0].headers["mcp-protocol-version"] == DOCS_MCP_PROTOCOL_VERSION


@pytest.mark.asyncio
async def test_an_event_stream_reply_is_parsed_too() -> None:
    """Streamable HTTP permits it; a server that switched must not break docs."""
    body = "event: message\ndata: " + json.dumps(_payload(LIVE_HIT)) + "\n\n"
    handler = lambda _r: httpx.Response(  # noqa: E731
        200, text=body, headers={"content-type": "text/event-stream"}
    )
    sections = await _client(handler).search("worktrees", timeout=2.0)
    assert [s.path for s in sections] == ["/docs/worktrees"]


@pytest.mark.asyncio
async def test_results_are_capped_to_top_k() -> None:
    hits = [dict(LIVE_HIT, url=f"/docs/page-{i}") for i in range(10)]
    sections = await _client(_ok(_payload(*hits))).search("x", top_k=3, timeout=2.0)
    assert len(sections) == 3


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "url",
    [
        pytest.param("https://example.com/evil", id="absolute"),
        pytest.param("", id="empty"),
        pytest.param(None, id="missing"),
        pytest.param("/", id="bare_slash"),
        pytest.param("docs/worktrees", id="relative"),
        # Protocol-relative: a client that renders this as a link navigates
        # OFF-SITE, which is exactly what a citation must not do.
        pytest.param("//evil.com/docs", id="protocol_relative"),
        pytest.param("/\\evil.com/docs", id="backslash_protocol_relative"),
        pytest.param("/docs//evil.com", id="embedded_double_slash"),
        pytest.param("/docs:evil", id="embedded_colon"),
    ],
)
async def test_a_hit_that_is_not_a_site_path_is_dropped(url: Any) -> None:
    """A cited URL is shown to the user, so a wrong one is worse than one fewer.

    Nothing repairs these: the model is told to build a citation by prepending
    `https://daintree.org`, so anything that is not purely a same-site path can
    become a link to somewhere that is not the documentation.
    """
    sections = await _client(_ok(_payload(dict(LIVE_HIT, url=url), LIVE_HIT))).search(
        "x", timeout=2.0
    )
    assert [s.path for s in sections] == ["/docs/worktrees"]


@pytest.mark.asyncio
async def test_an_ordinary_docs_path_still_survives_the_guard() -> None:
    """The control: the guard must not reject the paths the server actually returns."""
    real = ["/docs/worktrees", "/docs/code-forge", "/blog/2026-launch", "/download"]
    hits = [dict(LIVE_HIT, url=p) for p in real]
    sections = await _client(_ok(_payload(*hits))).search("x", top_k=10, timeout=2.0)
    assert [s.path for s in sections] == real


@pytest.mark.asyncio
async def test_an_oversized_response_is_refused_before_parsing() -> None:
    """The output caps bound the PROMPT; this bounds memory on the way there."""
    huge = {"jsonrpc": "2.0", "id": 1, "result": {"content": [{"type": "text", "text": "x" * 5_000_000}]}}
    with pytest.raises(DocsLookupError):
        await _client(_ok(huge)).search("x", timeout=2.0)


class _ChunkStream(httpx.AsyncByteStream):
    """A body that arrives in pieces and declares no length.

    The Content-Length pre-check is free but only a hint: a chunked response has
    none, and a hostile one can lie. This is the shape that proves the bound is
    enforced while READING rather than after — and it records its own closure, so
    a test can assert the stream is released.
    """

    def __init__(self, chunk: bytes, count: int) -> None:
        self._chunk = chunk
        self._count = count
        self.yielded = 0
        self.closed = False

    async def __aiter__(self) -> Any:
        for _ in range(self._count):
            self.yielded += 1
            yield self._chunk

    async def aclose(self) -> None:
        self.closed = True


@pytest.mark.asyncio
async def test_a_chunked_flood_is_cut_off_mid_read() -> None:
    """No Content-Length to check, so the counter has to do the work.

    Buffering first and checking after would prevent PARSING an oversized reply
    without preventing it reaching memory — which is the entire point.
    """
    stream = _ChunkStream(b"x" * 100_000, 200)  # 20 MB if fully read
    handler = lambda _r: httpx.Response(200, stream=stream)  # noqa: E731
    with pytest.raises(DocsLookupError):
        await _client(handler).search("x", timeout=2.0)
    assert stream.yielded * 100_000 < 20_000_000, "the read must have been abandoned early"


@pytest.mark.asyncio
async def test_the_response_stream_is_always_released() -> None:
    """A streamed read holds a connection until it is closed — including on error."""
    stream = _ChunkStream(b'{"nope"', 1)
    handler = lambda _r: httpx.Response(200, stream=stream)  # noqa: E731
    with pytest.raises(DocsLookupError):
        await _client(handler).search("x", timeout=2.0)
    assert stream.closed is True


@pytest.mark.asyncio
async def test_a_reply_to_a_different_request_is_refused() -> None:
    """An envelope carrying someone else's id is not this search's answer.

    Folding it in would put an unrelated payload in front of the model framed as
    Daintree documentation.
    """
    body = dict(_payload(LIVE_HIT), id=99)
    with pytest.raises(DocsLookupError):
        await _client(_ok(body)).search("x", timeout=2.0)


@pytest.mark.asyncio
async def test_an_sse_message_split_across_data_lines_is_reassembled() -> None:
    """The spec joins repeated `data:` lines with a newline before parsing.

    Parsing them one at a time would make every large streamed result look like
    garbage — a failure that only appears once a result is big enough to split.
    """
    # Split the way a producer actually splits: on the payload's own newlines.
    # (The spec joins `data:` lines WITH a newline, so a pretty-printed JSON
    # document round-trips; an arbitrary mid-token split would not, and no
    # producer emits one.)
    lines = json.dumps(_payload(LIVE_HIT), indent=1).splitlines()
    body = "event: message\n" + "".join(f"data: {line}\n" for line in lines) + "\n"
    handler = lambda _r: httpx.Response(  # noqa: E731
        200, text=body, headers={"content-type": "text/event-stream"}
    )
    sections = await _client(handler).search("worktrees", timeout=2.0)
    assert [s.path for s in sections] == ["/docs/worktrees"]


@pytest.mark.asyncio
async def test_an_over_long_path_is_dropped_not_truncated() -> None:
    """Truncating would produce a well-formed citation to a DIFFERENT page.

    A shortened path still looks like a real one, so the wrong answer would be
    delivered with full confidence. Length is a reason to drop the hit.
    """
    long_path = "/docs/" + "a" * 400
    sections = await _client(
        _ok(_payload(dict(LIVE_HIT, url=long_path), LIVE_HIT))
    ).search("x", timeout=2.0)
    assert [s.path for s in sections] == ["/docs/worktrees"]


@pytest.mark.asyncio
async def test_a_redirect_is_refused_rather_than_followed() -> None:
    """A 3xx is the one way a reply could move this call to a host we did not choose."""
    handler = lambda _r: httpx.Response(  # noqa: E731
        302, headers={"location": "https://evil.example/api/mcp"}
    )
    with pytest.raises(DocsLookupError):
        await _client(handler).search("x", timeout=2.0)


@pytest.mark.asyncio
async def test_a_hit_with_no_text_is_dropped() -> None:
    empty = dict(LIVE_HIT, chunkText="", snippet="")
    assert await _client(_ok(_payload(empty))).search("x", timeout=2.0) == []


@pytest.mark.asyncio
async def test_the_snippet_is_used_when_the_chunk_text_is_missing() -> None:
    snippet_only = dict(LIVE_HIT, chunkText="")
    sections = await _client(_ok(_payload(snippet_only))).search("x", timeout=2.0)
    assert "Create Worktree" in sections[0].text


# ---------------------------------------------------------------------------
# Every failure is a DocsLookupError, which the caller turns into "no docs".
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize("status", [400, 404, 429, 500, 503])
async def test_an_http_error_raises_rather_than_returning_junk(status: int) -> None:
    with pytest.raises(DocsLookupError):
        await _client(lambda _r: httpx.Response(status, json={})).search("x", timeout=2.0)


@pytest.mark.asyncio
async def test_a_transport_failure_raises_a_docs_error() -> None:
    """httpx exceptions must not escape as themselves.

    The caller catches `DocsLookupError`; a bare `ConnectError` reaching the
    orchestrator's narrow handler would fail the turn over a documentation
    lookup that was never required.
    """

    def handler(_request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("docs host unreachable")

    with pytest.raises(DocsLookupError):
        await _client(handler).search("x", timeout=2.0)


@pytest.mark.asyncio
async def test_a_timeout_raises_a_docs_error() -> None:
    def handler(_request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("slow docs host")

    with pytest.raises(DocsLookupError):
        await _client(handler).search("x", timeout=0.01)


@pytest.mark.asyncio
async def test_a_json_rpc_error_is_not_read_as_a_result() -> None:
    body = {"jsonrpc": "2.0", "id": 1, "error": {"code": -32000, "message": "Not Acceptable"}}
    with pytest.raises(DocsLookupError):
        await _client(_ok(body)).search("x", timeout=2.0)


@pytest.mark.asyncio
async def test_a_tool_error_inside_a_successful_envelope_is_caught() -> None:
    """MCP reports tool failures inside a 200 result.

    Missing that would fold an error message into the prompt framed as Daintree
    documentation — the worst possible thing to hand a model that was told to
    answer from it.
    """
    body = {
        "jsonrpc": "2.0",
        "id": 1,
        "result": {"isError": True, "content": [{"type": "text", "text": "index offline"}]},
    }
    with pytest.raises(DocsLookupError):
        await _client(_ok(body)).search("x", timeout=2.0)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "body",
    [
        {"jsonrpc": "2.0", "id": 1},  # no result at all
        {"jsonrpc": "2.0", "id": 1, "result": {"content": []}},  # no blocks
        {"jsonrpc": "2.0", "id": 1, "result": {"content": [{"type": "text", "text": "{"}]}},
        {"jsonrpc": "2.0", "id": 1, "result": {"content": [{"type": "text", "text": "[]"}]}},
    ],
)
async def test_a_malformed_payload_raises_rather_than_half_parsing(body: dict[str, Any]) -> None:
    with pytest.raises(DocsLookupError):
        await _client(_ok(body)).search("x", timeout=2.0)


@pytest.mark.asyncio
async def test_a_non_json_body_raises() -> None:
    handler = lambda _r: httpx.Response(200, text="<html>gateway</html>")  # noqa: E731
    with pytest.raises(DocsLookupError):
        await _client(handler).search("x", timeout=2.0)


@pytest.mark.asyncio
async def test_an_empty_result_list_is_not_an_error() -> None:
    """A search that matched nothing answered correctly. It just found nothing."""
    assert await _client(_ok(_payload())).search("x", timeout=2.0) == []


def test_the_url_is_the_fixed_product_endpoint() -> None:
    """A product URL, not configuration — one address for every caller."""
    assert DOCS_MCP_URL == "https://daintree.org/api/mcp"
    assert DocsMcpClient(httpx.AsyncClient()).url == DOCS_MCP_URL


# ---------------------------------------------------------------------------
# Rendering. This text lands in a prompt, so its size is a cost.
# ---------------------------------------------------------------------------


def test_rendering_carries_the_path_for_citation() -> None:
    text = render_docs_sections(
        [DocsSection(path="/docs/worktrees", title="Worktrees", section_title="PR", text="body")]
    )
    assert "/docs/worktrees" in text
    assert "Worktrees — PR" in text
    assert "body" in text


def test_a_huge_result_set_is_capped_before_it_reaches_the_prompt() -> None:
    """Docs pages run to tens of kilobytes; this block rides the volatile tail.

    Uncapped it would dwarf the runbook bodies beside it and be re-encoded on the
    next turn for a question that will not be asked again.
    """
    huge = [
        DocsSection(path=f"/docs/p{i}", title="T", section_title="S", text="x" * 8000)
        for i in range(10)
    ]
    assert len(render_docs_sections(huge).encode("utf-8")) <= DOCS_BLOCK_MAX_BYTES


# --------------------------------------------------------------------------
# Permanent vs transient failure
# --------------------------------------------------------------------------
#
# Both fail open — the difference is only how loudly the server says so. Added
# after 2026-08-20, when the endpoint began answering 401 to every method and
# every docs lookup in production failed silently for hours: the failure WAS
# logged, at the same `info` level as a search that matched nothing, so nothing
# distinguished a dead integration from ordinary weather.


@pytest.mark.parametrize("status", [401, 403, 404, 405, 406, 410, 415, 501])
@pytest.mark.asyncio
async def test_a_rejected_lookup_is_marked_permanent(status: int) -> None:
    handler = lambda _r: httpx.Response(status, json={})  # noqa: E731
    with pytest.raises(DocsLookupError) as caught:
        await _client(handler).search("worktrees", timeout=2.0)
    assert caught.value.status == status
    assert caught.value.is_permanent, (
        "auth/permission/gone will fail identically on every turn until a human "
        "acts, which is an operator signal rather than one turn's bad luck"
    )


@pytest.mark.parametrize("status", [500, 502, 503, 429])
@pytest.mark.asyncio
async def test_a_server_side_failure_is_not_permanent(status: int) -> None:
    """A 5xx or a rate limit is weather: the next turn may well succeed."""
    handler = lambda _r: httpx.Response(status, json={})  # noqa: E731
    with pytest.raises(DocsLookupError) as caught:
        await _client(handler).search("worktrees", timeout=2.0)
    assert caught.value.status == status
    assert not caught.value.is_permanent


@pytest.mark.asyncio
async def test_a_redirect_is_permanent_because_this_client_refuses_to_follow() -> None:
    """Not following redirects is the security posture, so a redirecting server
    is one this client can never read — no retry changes that."""
    handler = lambda _r: httpx.Response(302, headers={"location": "https://x/"})  # noqa: E731
    with pytest.raises(DocsLookupError) as caught:
        await _client(handler).search("worktrees", timeout=2.0)
    assert caught.value.status == 302
    assert caught.value.is_permanent


@pytest.mark.asyncio
async def test_a_failure_that_was_not_a_status_carries_none() -> None:
    """A 200 that fails to parse did not fail BECAUSE of a status.

    `status` is the non-success status that caused the failure, not the response
    status — recording 200 here would imply the HTTP call was the problem.
    """
    handler = lambda _r: httpx.Response(200, text="not json")  # noqa: E731
    with pytest.raises(DocsLookupError) as caught:
        await _client(handler).search("worktrees", timeout=2.0)
    assert caught.value.status is None
    assert not caught.value.is_permanent


# ---------------------------------------------------------------------------
# `get_page` — the server's own remedy for a snippet too thin to answer from.
# ---------------------------------------------------------------------------


def _page_payload(markdown: str) -> dict[str, Any]:
    return {
        "jsonrpc": "2.0",
        "id": 1,
        "result": {
            "content": [
                {
                    "type": "text",
                    "text": json.dumps(
                        {"url": "/docs/worktrees/create", "markdown": markdown}
                    ),
                }
            ]
        },
    }


@pytest.mark.asyncio
async def test_todays_server_shape_carries_no_chunktext_and_falls_back_to_snippet() -> None:
    """The shape the LIVE server returns, captured 2026-09-12.

    ``LIVE_HIT`` above carries `chunkText` and was captured on 2026-08-14. The
    endpoint no longer returns that field in either `groupBy` mode — hits carry
    url, title, sectionTitle, snippet, score and chunks — so every section now
    takes the `snippet` fallback. That is not a bug in the client and it is not
    free either: `snippet` is an ellipsized preview, which is why the orchestrator
    enriches the top hit through `fetch_page`.

    Pinned so that a server that starts returning `chunkText` again is a visible
    change rather than a silent one, and so the fallback is never mistaken for the
    only path.
    """
    today = {
        "url": "/docs/worktrees/create",
        "title": "Create a Worktree | Daintree Docs",
        "sectionTitle": "Bulk Worktree Creation",
        "snippet": "…Selecting several issues in the Code Forge panel…",
        "score": 1.93,
        "chunks": [{"sectionTitle": "Bulk Worktree Creation", "snippet": "…"}],
    }
    assert "chunkText" not in today

    sections = await _client(_ok(_payload(today))).search("worktrees", timeout=1.0)

    assert len(sections) == 1
    assert sections[0].text == "…Selecting several issues in the Code Forge panel…"
    assert sections[0].url == "https://daintree.org/docs/worktrees/create"


@pytest.mark.asyncio
async def test_fetch_page_returns_the_page_markdown() -> None:
    markdown = "## Creating a Worktree\n\nOpen the dialog from the **+** button."
    page = await _client(_ok(_page_payload(markdown))).fetch_page(
        "/docs/worktrees/create", timeout=1.0
    )
    assert page == markdown


@pytest.mark.asyncio
async def test_fetch_page_sends_path_not_url() -> None:
    """The schema requires `path`; sending `url` is an input-validation error.

    Verified against the live endpoint: ``{"url": "/docs/x"}`` is refused with
    "expected string, received undefined". The tool's description says "call
    get_page with the result URL", which is what makes this the easy mistake.
    """
    seen: dict[str, Any] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen.update(json.loads(request.content.decode())["params"])
        return httpx.Response(200, json=_page_payload("# page"))

    await _client(handler).fetch_page("/docs/worktrees/create", timeout=1.0)

    assert seen["name"] == "get_page"
    assert seen["arguments"] == {"path": "/docs/worktrees/create"}


@pytest.mark.asyncio
async def test_fetch_page_refuses_a_path_that_is_not_a_site_path() -> None:
    """Re-validated here rather than trusted from the caller.

    ``//evil.com/x`` is a protocol-relative URL that navigates off-site. The path
    reaching this method has already been checked once, and a module that
    validates on one path and assumes on the other is one refactor away from not
    validating at all.
    """
    called: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        called.append(str(request.url))
        return httpx.Response(200, json=_page_payload("# page"))

    client = _client(handler)
    for path in ("//evil.com/x", "https://evil.com/x", "/docs/x y", "not-a-path"):
        with pytest.raises(DocsLookupError):
            await client.fetch_page(path, timeout=1.0)
    assert called == [], "a refused path must not reach the network"


@pytest.mark.asyncio
async def test_fetch_page_caps_a_large_page_to_a_sections_budget() -> None:
    """A real page runs to tens of kilobytes; a section's budget is 4 KiB.

    The cap is applied rather than raised: this text REPLACES a section's text, so
    it has to live inside a section's budget or the whole-block cap stops meaning
    what it says.
    """
    page = await _client(_ok(_page_payload("x" * (DOCS_SECTION_MAX_BYTES * 4)))).fetch_page(
        "/docs/worktrees/create", timeout=1.0
    )
    assert len(page.encode("utf-8")) <= DOCS_SECTION_MAX_BYTES


@pytest.mark.asyncio
async def test_a_page_tool_error_inside_a_200_is_still_a_failure() -> None:
    """The same rule the search path has: `isError` is not success."""
    body = {
        "jsonrpc": "2.0",
        "id": 1,
        "result": {"isError": True, "content": [{"type": "text", "text": "no such page"}]},
    }
    with pytest.raises(DocsLookupError):
        await _client(_ok(body)).fetch_page("/docs/nope", timeout=1.0)


@pytest.mark.asyncio
async def test_a_page_reply_answering_a_different_request_is_refused() -> None:
    """Correlation is an envelope property, so it covers both tools."""
    body = dict(_page_payload("# page"), id=999)
    with pytest.raises(DocsLookupError):
        await _client(_ok(body)).fetch_page("/docs/worktrees/create", timeout=1.0)
