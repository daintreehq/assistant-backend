"""The runbook-search MCP endpoint, driven over HTTP the way an MCP client drives it.

The selector is the scripted seam, so these tests are about the WIRE — the
handshake in both protocol eras, the tool contract, the transport's status
codes — not about ranking quality. The real search is exercised against the
shipped artifact in ``tests/unit/test_runbook_search.py``.
"""

from __future__ import annotations

import contextlib
import json
from typing import Any

from fastapi.testclient import TestClient
from jsonschema import Draft202012Validator

from daintree_assistant_server.runbook_mcp import (
    DEFAULT_MAX_RESULTS,
    GET_TOOL_DEFINITION,
    GET_TOOL_NAME,
    MAX_RESULTS_CAP,
    SENT_EARLIER_NOTE,
    SUPPORTED_PROTOCOL_VERSIONS,
    TOOL_DEFINITION,
    TOOL_NAME,
)
from daintree_assistant_server.runbooks.catalog import DOCS_RUNBOOK_ID
from daintree_assistant_server.runbooks.host_state import HostState

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


def _get(client: TestClient, arguments: dict[str, Any], **kwargs: Any) -> Any:
    return _rpc(client, "tools/call", {"name": GET_TOOL_NAME, "arguments": arguments}, **kwargs)


def _assert_schema(definition: dict[str, Any], structured: dict[str, Any]) -> None:
    errors = [e.message for e in Draft202012Validator(definition["outputSchema"]).iter_errors(
        structured
    )]
    assert not errors, errors


def test_legacy_handshake_issues_a_session_nothing_depends_on(client: TestClient) -> None:
    """A 2025-era client: initialize, initialized, list, call. The handshake issues a
    session id, but nothing after it depends on the client sending it back."""
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
    assert init.headers["mcp-session-id"]
    result = init.json()["result"]
    assert result["protocolVersion"] == "2025-06-18"
    assert result["capabilities"] == {"tools": {"listChanged": False}}
    assert result["serverInfo"]["name"] == "daintree-runbooks"

    initialized = _rpc(client, "notifications/initialized", request_id=None)
    assert initialized.status_code == 202
    assert initialized.content == b""

    listed = _rpc(client, "tools/list", headers={"MCP-Protocol-Version": "2025-06-18"})
    tools = listed.json()["result"]["tools"]
    assert [t["name"] for t in tools] == [TOOL_NAME, GET_TOOL_NAME]
    assert tools[0]["inputSchema"]["required"] == ["query"]
    assert tools[1]["inputSchema"]["required"] == ["id"]
    assert all(t["annotations"]["readOnlyHint"] is True for t in tools)


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
    selector = app.state.container.selectors["inbox"]
    catalog = app.state.container.catalogs.inbox
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
    assert top["content_hash"] == runbook.content_hash
    # An unselected neighbour is named, not delivered: the caller will not follow it.
    neighbour = catalog.get(first)
    assert structured["runbooks"][1] == {
        "id": first,
        "title": neighbour.title,
        "score": 0.41,
        "selected": False,
    }
    # The text block carries the same JSON, for clients predating structuredContent.
    assert json.loads(result["content"][0]["text"]) == structured
    _assert_schema(TOOL_DEFINITION, structured)
    # The query reaches the search trimmed, with the caller's limit.
    assert selector.search_requests == [("do the thing", 2)]


def test_search_delivers_each_prerequisite_once_outside_the_ranking(
    client: TestClient, app: Any
) -> None:
    """Two selected specifics sharing a foundation bring it once, in
    `supporting_runbooks`, and it does not count against `max_results`."""
    catalog = app.state.container.catalogs.inbox
    app.state.container.selectors["inbox"].search_hits = [
        ("daintree.terminals.rename", 0.9, True),
        ("daintree.agents.ask-and-relay", 0.6, True),
    ]
    structured = _call(client, {"query": "x", "max_results": 2}).json()["result"][
        "structuredContent"
    ]
    assert [r["id"] for r in structured["runbooks"]] == [
        "daintree.terminals.rename",
        "daintree.agents.ask-and-relay",
    ]
    supporting = [r["id"] for r in structured["supporting_runbooks"]]
    assert supporting == ["daintree.foundation"]
    foundation = catalog.get("daintree.foundation")
    # Read because a selected runbook needs it, not chosen: no routing text or flags.
    assert structured["supporting_runbooks"][0] == {
        "id": foundation.id,
        "title": foundation.title,
        "content_hash": foundation.content_hash,
        "instructions": foundation.body,
    }
    _assert_schema(TOOL_DEFINITION, structured)


def test_a_foundation_ranked_as_a_neighbour_still_arrives_in_full_for_a_selected_hit(
    client: TestClient, app: Any
) -> None:
    catalog = app.state.container.catalogs.inbox
    app.state.container.selectors["inbox"].search_hits = [
        ("daintree.terminals.rename", 0.9, True),
        ("daintree.foundation", 0.1, False),
    ]
    structured = _call(client, {"query": "x", "max_results": 2}).json()["result"][
        "structuredContent"
    ]
    assert "instructions" not in structured["runbooks"][1]
    assert [r["id"] for r in structured["supporting_runbooks"]] == ["daintree.foundation"]
    assert (
        structured["supporting_runbooks"][0]["instructions"]
        == catalog.get("daintree.foundation").body
    )
    _assert_schema(TOOL_DEFINITION, structured)


def test_nothing_selected_delivers_no_procedures_at_all(client: TestClient, app: Any) -> None:
    """No runbook fits: the caller carries on, so no procedure is worth its context."""
    app.state.container.selectors["inbox"].search_hits = [
        ("daintree.terminals.rename", 0.2, False),
        ("daintree.agents.ask-and-relay", 0.1, False),
    ]
    structured = _call(client, {"query": "x", "max_results": 2}).json()["result"][
        "structuredContent"
    ]
    assert all(not r["selected"] and "instructions" not in r for r in structured["runbooks"])
    assert structured["supporting_runbooks"] == []
    _assert_schema(TOOL_DEFINITION, structured)


def test_the_output_schema_refuses_a_neighbour_carrying_a_procedure(
    client: TestClient, app: Any
) -> None:
    app.state.container.selectors["inbox"].search_hits = [("daintree.terminals.rename", 0.2, False)]
    structured = _call(client, {"query": "x", "max_results": 1}).json()["result"][
        "structuredContent"
    ]
    structured["runbooks"][0]["instructions"] = "1. Rename."
    errors = list(Draft202012Validator(TOOL_DEFINITION["outputSchema"]).iter_errors(structured))
    assert errors


def test_a_foundation_that_is_itself_a_hit_is_not_sent_twice(
    client: TestClient, app: Any
) -> None:
    app.state.container.selectors["inbox"].search_hits = [
        ("daintree.foundation", 0.9, True),
        ("daintree.terminals.rename", 0.5, True),
    ]
    structured = _call(client, {"query": "x", "max_results": 2}).json()["result"][
        "structuredContent"
    ]
    assert [r["id"] for r in structured["runbooks"]][0] == "daintree.foundation"
    assert structured["supporting_runbooks"] == []


def test_max_results_defaults_to_three(client: TestClient, app: Any) -> None:
    response = _call(client, {"query": "anything"})
    assert DEFAULT_MAX_RESULTS == 3
    assert len(response.json()["result"]["structuredContent"]["runbooks"]) == 3
    assert app.state.container.selectors["inbox"].search_requests == [("anything", 3)]


def test_each_search_is_narrowed_by_the_host_state_its_own_request_reports(
    client: TestClient, app: Any
) -> None:
    """Read from every POST and never stored: a later request without the headers is
    an unreported caller again, offered the whole catalog."""
    selector = app.state.container.selectors["inbox"]
    catalog = app.state.container.catalogs.inbox
    none = catalog.excluded_for(HostState("none"))
    lane = catalog.excluded_for(HostState("configured", lane_reachable=True))
    assert none and lane and none != lane

    _call(client, {"query": "q"}, headers={"Daintree-Hosts": "none"})
    _call(
        client,
        {"query": "q"},
        headers={"Daintree-Hosts": "configured", "Daintree-Lane": "reachable"},
    )
    _call(client, {"query": "q"})
    assert selector.search_excludes == [none, lane, frozenset()]


def test_a_malformed_host_state_filters_nothing(client: TestClient, app: Any) -> None:
    """Fails open, and is never an error: an unknown value is not a definite no."""
    for headers in (
        {"Daintree-Hosts": "several"},
        {"Daintree-Lane": "reachable"},
        {"Daintree-Hosts": "connected", "Daintree-Lane": "maybe"},
    ):
        response = _call(client, {"query": "q"}, headers=headers)
        assert response.status_code == 200
        assert response.json()["result"]["isError"] is False
    assert app.state.container.selectors["inbox"].search_excludes == [frozenset()] * 3


def test_get_runbook_ignores_the_host_state(client: TestClient) -> None:
    """Exact retrieval of a named id: the state narrows what a search offers only."""
    result = _get(
        client, {"id": "daintree.hosts.swarm-fan-out"}, headers={"Daintree-Hosts": "none"}
    ).json()["result"]
    assert result["isError"] is False
    assert result["structuredContent"]["runbook"]["id"] == "daintree.hosts.swarm-fan-out"


def test_get_runbook_returns_the_exact_runbook_and_its_foundation(
    client: TestClient, app: Any
) -> None:
    catalog = app.state.container.catalogs.inbox
    result = _get(client, {"id": "daintree.terminals.rename"}).json()["result"]
    assert result["isError"] is False
    structured = result["structuredContent"]
    runbook = catalog.get("daintree.terminals.rename")
    assert structured["catalog_revision"] == catalog.revision
    assert structured["runbook"]["id"] == runbook.id
    assert structured["runbook"]["instructions"] == runbook.body
    assert structured["runbook"]["content_hash"] == runbook.content_hash
    # Exact retrieval ranks nothing, so it claims no confidence.
    assert "score" not in structured["runbook"] and "selected" not in structured["runbook"]
    assert [r["id"] for r in structured["supporting_runbooks"]] == ["daintree.foundation"]
    assert json.loads(result["content"][0]["text"]) == structured
    _assert_schema(GET_TOOL_DEFINITION, structured)
    # No classifier involved.
    assert app.state.container.selectors["inbox"].search_requests == []


def test_get_runbook_of_the_foundation_has_no_supporting_runbooks(client: TestClient) -> None:
    structured = _get(client, {"id": "daintree.foundation"}).json()["result"][
        "structuredContent"
    ]
    assert structured["runbook"]["foundation"] is True
    assert structured["supporting_runbooks"] == []


def test_get_runbook_refuses_unknown_and_hidden_ids_alike(client: TestClient) -> None:
    """The retired docs runbook is loaded for the corpus, and exact retrieval
    must not become a second road to it."""
    answers = []
    for runbook_id in ("daintree.nope", DOCS_RUNBOOK_ID, "x" * 300):
        result = _get(client, {"id": runbook_id}).json()["result"]
        assert result["isError"] is True, runbook_id
        assert "structuredContent" not in result, runbook_id
        answers.append(result["content"][0]["text"])
    assert len(set(answers)) == 1
    assert DOCS_RUNBOOK_ID not in answers[0]


def test_get_runbook_argument_problems_are_tool_errors(client: TestClient) -> None:
    for arguments in ({}, {"id": ""}, {"id": "   "}, {"id": 7}, {"id": "a.b", "extra": 1}):
        result = _get(client, arguments).json()["result"]
        assert result["isError"] is True, arguments
        assert result["content"][0]["text"], arguments


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
    app.state.container.selectors["inbox"].search_fails = True
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
    assert _rpc(client, "ping", request_id="p").json() == {
        "jsonrpc": "2.0",
        "id": "p",
        "result": {"resultType": "complete"},
    }


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
    assert app.state.container.selectors["inbox"].search_requests == []


def test_no_credential_is_needed(client: TestClient) -> None:
    """Anyone can add this server: there is no auth to fail."""
    response = _call(client, {"query": "rename terminals"})
    assert response.status_code == 200
    assert response.json()["result"]["isError"] is False


def test_an_oversized_body_is_refused_before_parsing(client: TestClient) -> None:
    response = client.post(
        URL,
        content=b"{" + b" " * (128 * 1024) + b"}",
        headers={**HEADERS, "Content-Type": "application/json"},
    )
    assert response.status_code == 413
    assert response.json()["error"]["code"] == -32600


def test_a_malformed_origin_is_refused_not_a_crash(client: TestClient) -> None:
    body = {"jsonrpc": "2.0", "id": 1, "method": "ping"}
    response = client.post(URL, json=body, headers={**HEADERS, "Origin": "https://["})
    assert response.status_code == 403


def test_deeply_nested_json_is_a_parse_error_not_a_crash(client: TestClient) -> None:
    nested = b"[" * 50_000 + b"]" * 50_000
    response = client.post(
        URL, content=nested, headers={**HEADERS, "Content-Type": "application/json"}
    )
    assert response.status_code in (400, 413)
    if response.status_code == 400:
        assert response.json()["error"]["code"] == -32700


def test_2026_results_carry_result_type_and_cache_hints(client: TestClient) -> None:
    """2026-07-28 requires `resultType` on every result, and `ttlMs` plus
    `cacheScope` on the cacheable ones (`tools/list`, `server/discover`)."""
    for method in ("server/discover", "tools/list"):
        result = _rpc(client, method).json()["result"]
        assert result["resultType"] == "complete", method
        assert result["ttlMs"] >= 0 and result["cacheScope"] == "public", method
    assert _call(client, {"query": "x"}).json()["result"]["resultType"] == "complete"
    assert _call(client, {}).json()["result"]["resultType"] == "complete"


def _open_session(client: TestClient) -> dict[str, str]:
    init = _rpc(client, "initialize", {"protocolVersion": "2025-06-18"})
    return {"Mcp-Session-Id": init.headers["mcp-session-id"]}


def _supporting(response: Any) -> list[dict[str, Any]]:
    structured = response.json()["result"]["structuredContent"]
    return list(structured["supporting_runbooks"])


def test_a_session_is_sent_its_foundation_in_full_once(client: TestClient, app: Any) -> None:
    catalog = app.state.container.catalogs.inbox
    foundation = catalog.get("daintree.foundation")
    app.state.container.selectors["inbox"].search_hits = [("daintree.terminals.rename", 0.9, True)]
    session = _open_session(client)

    first = _call(client, {"query": "x", "max_results": 1}, headers=session)
    assert _supporting(first)[0]["instructions"] == foundation.body

    later = _call(client, {"query": "y", "max_results": 1}, headers=session)
    structured = later.json()["result"]["structuredContent"]
    assert structured["supporting_runbooks"] == [
        {"id": "daintree.foundation", "note": SENT_EARLIER_NOTE}
    ]
    assert json.loads(later.json()["result"]["content"][0]["text"]) == structured
    _assert_schema(TOOL_DEFINITION, structured)
    # The selected procedure itself is never withheld.
    assert structured["runbooks"][0]["instructions"] == catalog.get(
        "daintree.terminals.rename"
    ).body

    # A caller that lost it fetches it by id, and gets it whole.
    fetched = _get(client, {"id": "daintree.foundation"}, headers=session)
    assert fetched.json()["result"]["structuredContent"]["runbook"]["instructions"] == (
        foundation.body
    )


def test_get_runbook_shares_the_sessions_record(client: TestClient) -> None:
    session = _open_session(client)
    fetched = _get(client, {"id": "daintree.terminals.rename"}, headers=session)
    assert "instructions" in _supporting(fetched)[0]
    again = _get(client, {"id": "daintree.agents.ask-and-relay"}, headers=session)
    assert _supporting(again) == [{"id": "daintree.foundation", "note": SENT_EARLIER_NOTE}]
    _assert_schema(GET_TOOL_DEFINITION, again.json()["result"]["structuredContent"])


def test_a_foundation_fetched_by_id_counts_as_sent(client: TestClient, app: Any) -> None:
    app.state.container.selectors["inbox"].search_hits = [("daintree.terminals.rename", 0.9, True)]
    session = _open_session(client)
    _get(client, {"id": "daintree.foundation"}, headers=session)
    searched = _call(client, {"query": "x", "max_results": 1}, headers=session)
    assert _supporting(searched) == [{"id": "daintree.foundation", "note": SENT_EARLIER_NOTE}]


def test_a_new_handshake_starts_over(client: TestClient, app: Any) -> None:
    """A client that reconnects initializes again, and its new session has seen nothing."""
    app.state.container.selectors["inbox"].search_hits = [("daintree.terminals.rename", 0.9, True)]
    old = _open_session(client)
    _call(client, {"query": "x", "max_results": 1}, headers=old)
    new = _open_session(client)
    assert new != old
    assert "instructions" in _supporting(_call(client, {"query": "x"}, headers=new))[0]


def test_no_session_or_a_malformed_one_is_always_sent_everything(
    client: TestClient, app: Any
) -> None:
    app.state.container.selectors["inbox"].search_hits = [("daintree.terminals.rename", 0.9, True)]
    for headers in ({}, {"Mcp-Session-Id": "x" * 500}):
        for _ in range(2):
            response = _call(client, {"query": "x", "max_results": 1}, headers=headers)
            assert "instructions" in _supporting(response)[0], headers


def test_an_id_this_instance_never_issued_is_adopted_not_refused(
    client: TestClient, app: Any
) -> None:
    """A sibling instance issued it, or this one restarted: serve it, and send
    everything the first time rather than trust a record that is not here."""
    app.state.container.selectors["inbox"].search_hits = [("daintree.terminals.rename", 0.9, True)]
    foreign = {"Mcp-Session-Id": "issued-elsewhere"}
    first = _call(client, {"query": "x", "max_results": 1}, headers=foreign)
    assert first.status_code == 200
    assert "instructions" in _supporting(first)[0]
    assert "note" in _supporting(_call(client, {"query": "x"}, headers=foreign))[0]


def test_a_changed_foundation_is_sent_again(client: TestClient, app: Any) -> None:
    app.state.container.selectors["inbox"].search_hits = [("daintree.terminals.rename", 0.9, True)]
    session = _open_session(client)
    _call(client, {"query": "x", "max_results": 1}, headers=session)
    record = app.state.container.sessions.delivered(session["Mcp-Session-Id"])
    record["daintree.foundation"] = "sha256:an-older-version"
    again = _call(client, {"query": "x", "max_results": 1}, headers=session)
    assert "instructions" in _supporting(again)[0]


def test_a_sessionless_revision_ignores_a_session_header(client: TestClient, app: Any) -> None:
    """2026-07-28 has no sessions, so an id it happens to carry changes nothing."""
    app.state.container.selectors["inbox"].search_hits = [("daintree.terminals.rename", 0.9, True)]
    session = _open_session(client)
    meta = {"io.modelcontextprotocol/protocolVersion": "2026-07-28"}
    for _ in range(2):
        response = _rpc(
            client,
            "tools/call",
            {"name": TOOL_NAME, "arguments": {"query": "x", "max_results": 1}, "_meta": meta},
            headers=session,
        )
        assert "instructions" in _supporting(response)[0]


def test_a_reply_that_fails_to_render_marks_nothing_as_sent(client: TestClient, app: Any) -> None:
    """A lone surrogate is a valid JSON string id that cannot be encoded as UTF-8:
    the reply never leaves, so the session must not believe it was sent."""
    app.state.container.selectors["inbox"].search_hits = [("daintree.terminals.rename", 0.9, True)]
    session = _open_session(client)
    body = (
        '{"jsonrpc":"2.0","id":"\\ud800","method":"tools/call","params":'
        '{"name":"search_runbooks","arguments":{"query":"x","max_results":1}}}'
    )
    with contextlib.suppress(Exception):
        client.post(
            URL,
            content=body,
            headers={**HEADERS, **session, "Content-Type": "application/json"},
        )
    assert app.state.container.sessions.delivered(session["Mcp-Session-Id"]) == {}


# ---- one catalog per host model (issue #159) ------------------------------------

CONTROL = {"Daintree-Host-Control": "1"}


def test_each_model_is_served_from_its_own_catalog(client: TestClient, app: Any) -> None:
    """The model picks the catalog before anything else: the revision, the hits a
    search can name, exact retrieval and supporting delivery all come from it."""
    catalogs = app.state.container.catalogs
    lane = "daintree.hosts.handle-hub-message"
    assert lane in catalogs.inbox and lane not in catalogs.control

    inbox = _get(client, {"id": lane}).json()["result"]
    assert inbox["isError"] is False
    assert inbox["structuredContent"]["catalog_revision"] == catalogs.inbox.revision
    assert _get(client, {"id": lane}, headers=CONTROL).json()["result"]["isError"] is True

    app.state.container.selectors["control"].search_hits = [(lane, 0.9, True)]
    searched = _call(client, {"query": "q"}, headers=CONTROL).json()["result"]
    assert searched["structuredContent"]["runbooks"] == []
    assert searched["structuredContent"]["catalog_revision"] == catalogs.control.revision


def test_an_unknown_model_is_served_the_inbox_catalog(client: TestClient, app: Any) -> None:
    for value in ("0", "2", "true", ""):
        structured = _get(
            client, {"id": "daintree.terminals.rename"}, headers={"Daintree-Host-Control": value}
        ).json()["result"]["structuredContent"]
        assert structured["catalog_revision"] == app.state.container.catalogs.inbox.revision


def test_a_connected_control_caller_gets_the_hosts_foundation_with_every_runbook(
    client: TestClient, app: Any
) -> None:
    catalogs = app.state.container.catalogs
    hosts_foundation = catalogs.control.get("daintree.hosts.foundation")
    connected = {**CONTROL, "Daintree-Hosts": "connected"}
    fetched = _get(client, {"id": "daintree.terminals.rename"}, headers=connected)
    supporting = _supporting(fetched)
    assert [r["id"] for r in supporting] == ["daintree.foundation", "daintree.hosts.foundation"]
    assert supporting[1]["instructions"] == hosts_foundation.body
    _assert_schema(GET_TOOL_DEFINITION, fetched.json()["result"]["structuredContent"])

    app.state.container.selectors["control"].search_hits = [
        ("daintree.terminals.rename", 0.9, True),
        ("daintree.agents.ask-and-relay", 0.8, True),
        ("daintree.foundation", 0.1, False),
    ]
    searched = _call(client, {"query": "q"}, headers=connected)
    assert [r["id"] for r in _supporting(searched)] == [
        "daintree.foundation",
        "daintree.hosts.foundation",
    ]
    # Only what is delivered in full brings anything with it.
    app.state.container.selectors["control"].search_hits = [
        ("daintree.terminals.rename", 0.9, False)
    ]
    assert _supporting(_call(client, {"query": "q"}, headers=connected)) == []


def test_no_other_caller_gets_the_hosts_foundation_with_a_plain_runbook(
    client: TestClient,
) -> None:
    for headers in (
        CONTROL,
        {**CONTROL, "Daintree-Hosts": "none"},
        {**CONTROL, "Daintree-Hosts": "configured"},
        {"Daintree-Hosts": "connected"},
        {"Daintree-Hosts": "connected", "Daintree-Lane": "reachable"},
    ):
        fetched = _get(client, {"id": "daintree.terminals.rename"}, headers=headers)
        assert [r["id"] for r in _supporting(fetched)] == ["daintree.foundation"], headers


def test_a_control_callers_search_is_narrowed_within_its_own_catalog(
    client: TestClient, app: Any
) -> None:
    catalogs = app.state.container.catalogs
    selector = app.state.container.selectors["control"]
    _call(client, {"query": "q"}, headers={**CONTROL, "Daintree-Hosts": "none"})
    # A lane header means nothing to a control caller: its snapshot stays reported.
    _call(
        client,
        {"query": "q"},
        headers={**CONTROL, "Daintree-Hosts": "configured", "Daintree-Lane": "maybe"},
    )
    assert selector.search_excludes == [
        catalogs.control.excluded_for(HostState("none")),
        catalogs.control.excluded_for(HostState("configured")),
    ]


def test_a_session_across_models_resends_what_differs(client: TestClient, app: Any) -> None:
    """The session record is by content hash, so a runbook the other model words
    differently arrives in full, and one both share is named."""
    catalogs = app.state.container.catalogs
    session = _open_session(client)
    connected = {**CONTROL, "Daintree-Hosts": "connected", **session}
    _get(client, {"id": "daintree.hosts.connect-host"}, headers=connected)

    later = _get(
        client,
        {"id": "daintree.hosts.connect-host"},
        headers={"Daintree-Hosts": "connected", **session},
    )
    by_id = {r["id"]: r for r in _supporting(later)}
    for runbook_id in ("daintree.foundation", "daintree.hosts.foundation"):
        same = catalogs.inbox.get(runbook_id).content_hash == (
            catalogs.control.get(runbook_id).content_hash
        )
        assert ("note" in by_id[runbook_id]) is same, runbook_id
        if not same:
            assert by_id[runbook_id]["instructions"] == catalogs.inbox.get(runbook_id).body
