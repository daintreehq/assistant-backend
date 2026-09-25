"""The runbook-search MCP endpoint, driven over HTTP the way an MCP client drives it.

The selector is the scripted seam, so these tests are about the WIRE — the
handshake in both protocol eras, the tool contract, the transport's status
codes — not about ranking quality. The real search is exercised against the
shipped artifact in ``tests/unit/test_runbook_search.py``.
"""

from __future__ import annotations

import json
from typing import Any

from fastapi.testclient import TestClient

from daintree_assistant_server.services.runbook_mcp import (
    MAX_RESULTS_CAP,
    SUPPORTED_PROTOCOL_VERSIONS,
    TOOL_NAME,
)

URL = "/v1/daintree/mcp"
HEADERS = {"Accept": "application/json, text/event-stream"}


def _rpc(
    client: TestClient,
    method: str,
    params: dict[str, Any] | None = None,
    *,
    request_id: Any = 1,
    headers: dict[str, str] | None = None,
) -> Any:
    body: dict[str, Any] = {"jsonrpc": "2.0", "method": method}
    if request_id is not None:
        body["id"] = request_id
    if params is not None:
        body["params"] = params
    return client.post(URL, json=body, headers={**HEADERS, **(headers or {})})


def _call(client: TestClient, arguments: dict[str, Any], **kwargs: Any) -> Any:
    return _rpc(client, "tools/call", {"name": TOOL_NAME, "arguments": arguments}, **kwargs)


def test_legacy_handshake_then_search_without_any_session(client: TestClient) -> None:
    """A 2025-era client: initialize, initialized, list, call. No session id is
    ever issued, and nothing after the handshake depends on it having happened."""
    init = _rpc(
        client,
        "initialize",
        {
            "protocolVersion": "2025-06-18",
            "capabilities": {},
            "clientInfo": {"name": "test", "version": "0"},
        },
    )
    assert init.status_code == 200
    assert init.headers["content-type"].startswith("application/json")
    assert "mcp-session-id" not in init.headers
    result = init.json()["result"]
    assert result["protocolVersion"] == "2025-06-18"
    assert result["capabilities"] == {"tools": {"listChanged": False}}
    assert result["serverInfo"]["name"] == "daintree-runbooks"

    initialized = _rpc(client, "notifications/initialized", request_id=None)
    assert initialized.status_code == 202
    assert initialized.content == b""

    listed = _rpc(client, "tools/list", headers={"MCP-Protocol-Version": "2025-06-18"})
    tools = listed.json()["result"]["tools"]
    assert [t["name"] for t in tools] == [TOOL_NAME]
    assert tools[0]["inputSchema"]["required"] == ["query"]
    assert tools[0]["annotations"]["readOnlyHint"] is True


def test_initialize_with_an_unknown_version_is_answered_with_the_newest_handshake_one(
    client: TestClient,
) -> None:
    result = _rpc(client, "initialize", {"protocolVersion": "2030-01-01"}).json()["result"]
    # 2026-07-28 has no initialize; the newest revision that DOES is the answer.
    assert result["protocolVersion"] == "2025-11-25"


def test_stateless_2026_client_discovers_and_calls_with_no_handshake(client: TestClient) -> None:
    meta = {
        "io.modelcontextprotocol/protocolVersion": "2026-07-28",
        "io.modelcontextprotocol/clientCapabilities": {},
    }
    headers = {"MCP-Protocol-Version": "2026-07-28", "Mcp-Method": "server/discover"}
    discovered = _rpc(client, "server/discover", {"_meta": meta}, headers=headers)
    assert discovered.status_code == 200
    assert discovered.json()["result"]["supportedVersions"] == list(SUPPORTED_PROTOCOL_VERSIONS)

    called = _rpc(
        client,
        "tools/call",
        {"name": TOOL_NAME, "arguments": {"query": "rename terminals"}, "_meta": meta},
        headers={
            "MCP-Protocol-Version": "2026-07-28",
            "Mcp-Method": "tools/call",
            "Mcp-Name": TOOL_NAME,
        },
    )
    assert called.status_code == 200
    assert called.json()["result"]["isError"] is False


def test_search_returns_full_runbooks_as_structured_json(client: TestClient, app: Any) -> None:
    selector = app.state.container.selector
    catalog = app.state.container.catalog_manager.catalog
    first, second = sorted(catalog.selectable_ids)[:2]
    selector.search_hits = [(second, 0.97, True), (first, 0.41, False)]

    response = _call(client, {"query": "  do the thing  ", "max_results": 2})

    assert response.status_code == 200
    result = response.json()["result"]
    assert result["isError"] is False
    structured = result["structuredContent"]
    assert structured["catalog_revision"] == catalog.revision
    assert [r["id"] for r in structured["runbooks"]] == [second, first]
    top = structured["runbooks"][0]
    runbook = catalog.get(second)
    assert top["instructions"] == runbook.body
    assert top["title"] == runbook.title
    assert top["when_to_use"] == runbook.when_to_use
    assert (top["score"], top["selected"]) == (0.97, True)
    # The text block carries the same JSON, for clients predating structuredContent.
    assert json.loads(result["content"][0]["text"]) == structured
    # The query reaches the search trimmed, with the caller's limit.
    assert selector.search_requests == [("do the thing", 2)]


def test_max_results_defaults_to_five(client: TestClient, app: Any) -> None:
    response = _call(client, {"query": "anything"})
    assert len(response.json()["result"]["structuredContent"]["runbooks"]) == 5
    assert app.state.container.selector.search_requests == [("anything", 5)]


def test_bad_arguments_are_tool_errors_the_model_can_read(client: TestClient) -> None:
    """isError results, not JSON-RPC errors: the spec routes argument problems back
    to the model so it can fix the call, where a protocol error would be eaten."""
    for arguments in (
        {},
        {"query": "   "},
        {"query": "x", "max_results": 0},
        {"query": "x", "max_results": MAX_RESULTS_CAP + 1},
        {"query": "x", "max_results": True},
        {"query": "x", "limit": 3},
    ):
        response = _call(client, arguments)
        assert response.status_code == 200, arguments
        result = response.json()["result"]
        assert result["isError"] is True, arguments
        assert result["content"][0]["text"], arguments


def test_a_failed_search_is_a_retryable_tool_error_not_an_empty_answer(
    client: TestClient, app: Any
) -> None:
    app.state.container.selector.search_fails = True
    result = _call(client, {"query": "anything"}).json()["result"]
    assert result["isError"] is True
    assert "unavailable" in result["content"][0]["text"]


def test_unknown_tool_and_method_are_protocol_errors(client: TestClient) -> None:
    unknown_tool = _rpc(client, "tools/call", {"name": "nope", "arguments": {}}).json()
    assert unknown_tool["error"]["code"] == -32602
    unknown_method = _rpc(client, "resources/list").json()
    assert unknown_method["error"]["code"] == -32601
    assert unknown_method["id"] == 1


def test_ping_is_answered(client: TestClient) -> None:
    assert _rpc(client, "ping", request_id="p").json() == {"jsonrpc": "2.0", "id": "p", "result": {}}


def test_unsupported_protocol_version_names_the_supported_ones(client: TestClient) -> None:
    response = _rpc(client, "tools/list", headers={"MCP-Protocol-Version": "2024-11-05"})
    assert response.status_code == 400
    error = response.json()["error"]
    assert error["code"] == -32022
    assert error["data"] == {
        "supported": list(SUPPORTED_PROTOCOL_VERSIONS),
        "requested": "2024-11-05",
    }


def test_an_unsupported_version_that_is_not_a_date_is_not_echoed(client: TestClient) -> None:
    """A credential pasted into the wrong header must not come back in the body."""
    response = _rpc(client, "tools/list", headers={"MCP-Protocol-Version": "sk-secret-value"})
    assert response.status_code == 400
    assert "sk-secret-value" not in response.text


def test_headers_that_contradict_the_body_are_refused(client: TestClient) -> None:
    wrong_method = _rpc(client, "tools/list", headers={"Mcp-Method": "tools/call"})
    assert wrong_method.status_code == 400
    assert wrong_method.json()["error"]["code"] == -32020

    wrong_name = _call(client, {"query": "x"}, headers={"Mcp-Name": "other_tool"})
    assert wrong_name.status_code == 400
    assert wrong_name.json()["error"]["code"] == -32020

    wrong_version = _rpc(
        client,
        "tools/list",
        {"_meta": {"io.modelcontextprotocol/protocolVersion": "2025-11-25"}},
        headers={"MCP-Protocol-Version": "2026-07-28"},
    )
    assert wrong_version.status_code == 400
    assert wrong_version.json()["error"]["code"] == -32020


def test_transport_rejections(client: TestClient) -> None:
    parse = client.post(
        URL, content=b"{not json", headers={**HEADERS, "Content-Type": "application/json"}
    )
    assert parse.status_code == 400
    assert parse.json()["error"]["code"] == -32700

    batch = client.post(URL, json=[{"jsonrpc": "2.0", "id": 1, "method": "ping"}], headers=HEADERS)
    assert batch.status_code == 400
    assert batch.json()["error"]["code"] == -32600

    not_rpc = client.post(URL, json={"id": 1, "method": "ping"}, headers=HEADERS)
    assert not_rpc.status_code == 400

    bool_id = client.post(
        URL, json={"jsonrpc": "2.0", "id": True, "method": "ping"}, headers=HEADERS
    )
    assert bool_id.status_code == 400

    wrong_type = client.post(
        URL, content=b"{}", headers={**HEADERS, "Content-Type": "text/plain"}
    )
    assert wrong_type.status_code == 415

    wrong_accept = client.post(
        URL, json={"jsonrpc": "2.0", "id": 1, "method": "ping"}, headers={"Accept": "text/html"}
    )
    assert wrong_accept.status_code == 406


def test_a_client_response_is_accepted_without_a_body(client: TestClient) -> None:
    response = client.post(URL, json={"jsonrpc": "2.0", "id": 9, "result": {}}, headers=HEADERS)
    assert response.status_code == 202
    assert response.content == b""


def test_cross_origin_browser_requests_are_refused(client: TestClient) -> None:
    body = {"jsonrpc": "2.0", "id": 1, "method": "ping"}
    foreign = client.post(URL, json=body, headers={**HEADERS, "Origin": "https://evil.example"})
    assert foreign.status_code == 403
    same = client.post(URL, json=body, headers={**HEADERS, "Origin": "http://testserver"})
    assert same.status_code == 200


def test_get_and_delete_are_405_with_allow_post(client: TestClient) -> None:
    for method in ("GET", "DELETE"):
        response = client.request(method, URL)
        assert response.status_code == 405, method
        assert response.headers["allow"] == "POST", method


def test_not_ready_refuses_before_searching(client: TestClient, app: Any) -> None:
    app.state.ready = False
    response = _call(client, {"query": "anything"})
    assert response.status_code == 503
    assert app.state.container.selector.search_requests == []
