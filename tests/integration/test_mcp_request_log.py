"""The ``mcp_request`` log line: a bounded, content-free record of each request.

Every outcome a runbook search or fetch can have is told apart by fixed labels
and counts, and nothing the caller wrote (query, id, method or tool name) nor any
procedure served reaches the log plane.
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from typing import Any

import pytest
from fakes import ScriptedSelector
from fastapi.testclient import TestClient

from daintree_assistant_server.api import mcp as mcp_api
from daintree_assistant_server.runbook_mcp import GET_TOOL_NAME, TOOL_NAME
from daintree_assistant_server.runbooks.local import runtime
from daintree_assistant_server.runbooks.local.runtime import InferencePool

URL = "/v1/daintree/mcp"
HEADERS = {"Accept": "application/json"}

#: Stands in for everything a caller might send that must never be logged.
SECRET = "sk-live-hunter2 rotate the prod database password"


class _Recorder:
    def __init__(self) -> None:
        self.lines: list[tuple[str, dict[str, Any]]] = []

    def _record(self, event: str, **fields: Any) -> None:
        self.lines.append((event, fields))

    info = warning = error = debug = _record


@pytest.fixture
def logs(monkeypatch: pytest.MonkeyPatch) -> Iterator[_Recorder]:
    recorder = _Recorder()
    monkeypatch.setattr(mcp_api, "logger", recorder)
    monkeypatch.setattr(runtime, "_log", recorder)
    yield recorder
    # Whatever else a test asserts, no line carries the caller's text.
    assert SECRET not in json.dumps(recorder.lines, default=str)
    assert "hunter2" not in json.dumps(recorder.lines, default=str)


def _post(client: TestClient, method: str, params: dict[str, Any] | None = None) -> Any:
    body: dict[str, Any] = {"jsonrpc": "2.0", "id": 1, "method": method}
    if params is not None:
        body["params"] = params
    return client.post(URL, json=body, headers=HEADERS)


def _tool(client: TestClient, name: str, arguments: Any) -> Any:
    return _post(client, "tools/call", {"name": name, "arguments": arguments})


def _line(logs: _Recorder) -> dict[str, Any]:
    requests = [fields for event, fields in logs.lines if event == "mcp_request"]
    assert len(requests) == 1
    return requests[0]


def _selector(app: Any) -> ScriptedSelector:
    selector = app.state.container.selector
    assert isinstance(selector, ScriptedSelector)
    return selector


def test_a_search_with_selections(client: TestClient, app: Any, logs: _Recorder) -> None:
    first, second, third = sorted(app.state.container.catalog.selectable_ids)[:3]
    _selector(app).search_hits = [(first, 0.9, True), (second, 0.5, False), (third, 0.4, False)]
    response = _tool(client, TOOL_NAME, {"query": SECRET, "max_results": 3})
    assert response.json()["result"]["isError"] is False

    line = _line(logs)
    assert line["rpc_method"] == "tools/call"
    assert line["tool"] == TOOL_NAME
    assert line["outcome"] == "selected"
    assert (line["ranked"], line["selected"]) == (3, 1)
    # The domain foundation arrives in full with the selected procedure.
    assert line["supporting_full"] >= 1
    assert line["supporting_named"] == 0
    assert line["inference"] is None  # the scripted seam ran no pool
    assert line["bytes"] == len(response.content)
    assert line["duration_ms"] >= 0


def test_a_search_that_selects_nothing(client: TestClient, app: Any, logs: _Recorder) -> None:
    _tool(client, TOOL_NAME, {"query": SECRET, "max_results": 4})
    line = _line(logs)
    assert line["outcome"] == "none_selected"
    assert (line["ranked"], line["selected"]) == (4, 0)
    assert (line["supporting_full"], line["supporting_named"]) == (0, 0)


@pytest.mark.parametrize(
    "arguments",
    [
        {"query": ""},
        {"query": SECRET, "max_results": 0},
        {"query": SECRET, SECRET: 1},
        SECRET,
    ],
    ids=["empty-query", "bad-max", "unknown-argument", "not-an-object"],
)
def test_invalid_search_arguments(client: TestClient, logs: _Recorder, arguments: Any) -> None:
    response = _tool(client, TOOL_NAME, arguments)
    assert response.json()["result"]["isError"] is True
    line = _line(logs)
    assert (line["tool"], line["outcome"]) == (TOOL_NAME, "invalid_arguments")
    assert line["ranked"] is None and line["selected"] is None


def test_an_exact_fetch(client: TestClient, logs: _Recorder) -> None:
    response = _tool(client, GET_TOOL_NAME, {"id": "daintree.terminals.rename"})
    assert response.json()["result"]["isError"] is False
    line = _line(logs)
    assert (line["tool"], line["outcome"]) == (GET_TOOL_NAME, "found")
    assert line["ranked"] is None and line["selected"] is None
    assert line["supporting_full"] >= 1
    assert line["bytes"] == len(response.content)


def test_a_fetch_of_an_unknown_id(client: TestClient, logs: _Recorder) -> None:
    response = _tool(client, GET_TOOL_NAME, {"id": SECRET})
    assert response.json()["result"]["isError"] is True
    assert _line(logs)["outcome"] == "unknown_id"


def test_an_invalid_fetch(client: TestClient, logs: _Recorder) -> None:
    _tool(client, GET_TOOL_NAME, {"id": SECRET, "extra": SECRET})
    line = _line(logs)
    assert (line["tool"], line["outcome"]) == (GET_TOOL_NAME, "invalid_arguments")


def test_an_unknown_tool_is_not_named(client: TestClient, logs: _Recorder) -> None:
    response = _tool(client, SECRET, {})
    assert response.json()["error"]["code"] == -32602
    line = _line(logs)
    assert (line["rpc_method"], line["tool"], line["outcome"]) == (
        "tools/call",
        None,
        "unknown_tool",
    )


@pytest.mark.parametrize("notification", [False, True])
def test_an_unknown_method_is_not_named(
    client: TestClient, logs: _Recorder, notification: bool
) -> None:
    body: dict[str, Any] = {"jsonrpc": "2.0", "method": SECRET}
    if not notification:
        body["id"] = 1
    client.post(URL, json=body, headers=HEADERS)
    assert _line(logs)["rpc_method"] == "other"


def test_a_search_the_pool_refuses(client: TestClient, app: Any, logs: _Recorder) -> None:
    # No admission slots: every search is refused at the door.
    _selector(app).pool = pool = InferencePool(queue_depth=0)
    try:
        response = _tool(client, TOOL_NAME, {"query": SECRET})
    finally:
        pool.close()
    assert response.json()["result"]["isError"] is True
    line = _line(logs)
    assert (line["outcome"], line["inference"]) == ("unavailable", "pool_full")
    assert line["queue_wait_ms"] is None and line["rank_ms"] is None
    assert line["ranked"] is None
    assert ("runbook_rank_pool_full", {"depth": 0}) in logs.lines


def test_a_search_that_times_out(client: TestClient, app: Any, logs: _Recorder) -> None:
    selector = _selector(app)
    selector.pool = pool = InferencePool(timeout_seconds=0.05)
    selector.rank_seconds = 0.3
    try:
        response = _tool(client, TOOL_NAME, {"query": SECRET})
    finally:
        pool.close()
    assert response.json()["result"]["isError"] is True
    line = _line(logs)
    assert (line["outcome"], line["inference"]) == ("unavailable", "timeout")
    assert line["rank_ms"] is None


def test_a_search_whose_model_fails(client: TestClient, app: Any, logs: _Recorder) -> None:
    selector = _selector(app)
    selector.pool = pool = InferencePool()
    selector.search_fails = True
    try:
        _tool(client, TOOL_NAME, {"query": SECRET})
    finally:
        pool.close()
    line = _line(logs)
    assert (line["outcome"], line["inference"]) == ("unavailable", "failed")
    assert line["queue_wait_ms"] is not None


def test_a_pooled_search_reports_its_wait_and_rank_time(
    client: TestClient, app: Any, logs: _Recorder
) -> None:
    selector = _selector(app)
    selector.pool = pool = InferencePool()
    selector.rank_seconds = 0.02
    try:
        _tool(client, TOOL_NAME, {"query": SECRET, "max_results": 2})
    finally:
        pool.close()
    line = _line(logs)
    assert (line["outcome"], line["inference"]) == ("none_selected", "ok")
    assert line["queue_wait_ms"] >= 0
    assert line["rank_ms"] >= 15
    assert line["ranked"] == 2
