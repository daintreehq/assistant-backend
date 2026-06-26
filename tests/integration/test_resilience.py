"""Retry, degradation, and streaming-failure behavior (sections 6.8, 10.3, 18)."""

from __future__ import annotations

from fakes import FakeStream, make_result, tool_call_for

from daintree_assistant_server.errors import upstream_error, upstream_rate_limited


def _body(**daintree: object) -> dict:
    return {
        "model": "daintree-assistant",
        "messages": [{"role": "user", "content": "hi"}],
        "daintree": {"protocol_version": 1, "session_id": "s", "turn_id": "t1", **daintree},
    }


def test_nonstream_retries_before_success(client, auth, fake_provider) -> None:
    fake_provider.tool_call_result = tool_call_for([])
    calls = [0]

    def complete(_req):
        calls[0] += 1
        if calls[0] < 3:
            raise upstream_rate_limited()
        return make_result(content="recovered")

    fake_provider.complete_result = complete
    r = client.post("/v1/chat/completions", headers=auth, json=_body())
    assert r.status_code == 200
    assert calls[0] == 3  # 1 try + 2 retries
    assert r.json()["choices"][0]["message"]["content"] == "recovered"


def test_nonstream_persistent_error_surfaces(client, auth, fake_provider) -> None:
    fake_provider.tool_call_result = tool_call_for([])
    fake_provider.complete_error = upstream_rate_limited(retry_after="5")
    r = client.post("/v1/chat/completions", headers=auth, json=_body())
    assert r.status_code == 429
    assert r.json()["error"]["code"] == "upstream_rate_limited"
    assert r.headers.get("retry-after") == "5"


def test_nonstream_non_retryable_error(client, auth, fake_provider) -> None:
    fake_provider.tool_call_result = tool_call_for([])
    fake_provider.complete_error = upstream_error("bad gateway")  # not retryable
    r = client.post("/v1/chat/completions", headers=auth, json=_body())
    assert r.status_code == 502
    assert r.json()["error"]["code"] == "upstream_error"


def test_stream_open_retries_before_success(client, auth, fake_provider) -> None:
    fake_provider.tool_call_result = tool_call_for([])
    calls = [0]

    def factory(_req):
        calls[0] += 1
        if calls[0] < 2:
            raise upstream_rate_limited()
        return FakeStream(
            [
                {"id": "x", "object": "chat.completion.chunk", "created": 1, "model": "m",
                 "choices": [{"index": 0, "delta": {"content": "hi"}, "finish_reason": None}]},
                {"id": "x", "object": "chat.completion.chunk", "created": 1, "model": "m",
                 "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}]},
            ]
        )

    fake_provider.stream_factory = factory
    body = _body()
    body["stream"] = True
    with client.stream("POST", "/v1/chat/completions", headers=auth, json=body) as s:
        assert s.status_code == 200
        text = "".join(s.iter_text())
    assert calls[0] == 2
    assert "data: [DONE]" in text


def test_stream_open_persistent_error_is_json_not_stream(client, auth, fake_provider) -> None:
    fake_provider.tool_call_result = tool_call_for([])
    fake_provider.stream_error = upstream_rate_limited()
    body = _body()
    body["stream"] = True
    # Failure before public headers -> ordinary JSON error, not a stream.
    r = client.post("/v1/chat/completions", headers=auth, json=body)
    assert r.status_code == 429
    assert r.json()["error"]["code"] == "upstream_rate_limited"


def test_stream_breaks_after_first_token_no_fabricated_finish(client, auth, fake_provider) -> None:
    fake_provider.tool_call_result = tool_call_for([])

    def factory(_req):
        return FakeStream(
            [
                {"id": "x", "object": "chat.completion.chunk", "created": 1, "model": "m",
                 "choices": [{"index": 0, "delta": {"content": "partial"}, "finish_reason": None}]},
                {},  # placeholder; error raised before reaching it
            ],
            error_at=1,
            error=upstream_error("stream broke"),
        )

    fake_provider.stream_factory = factory
    body = _body()
    body["stream"] = True
    with client.stream("POST", "/v1/chat/completions", headers=auth, json=body) as s:
        assert s.status_code == 200  # headers already sent
        text = "".join(s.iter_text())
    assert "partial" in text
    assert "data: [DONE]" not in text  # no fabricated successful finish


def test_stream_closes_upstream_on_completion(client, auth, fake_provider) -> None:
    fake_provider.tool_call_result = tool_call_for([])
    fake_provider.stream_chunks = [
        {"id": "x", "object": "chat.completion.chunk", "created": 1, "model": "m",
         "choices": [{"index": 0, "delta": {"content": "hi"}, "finish_reason": "stop"}]},
    ]
    body = _body()
    body["stream"] = True
    with client.stream("POST", "/v1/chat/completions", headers=auth, json=body) as s:
        list(s.iter_text())
    assert fake_provider.last_stream is not None
    assert fake_provider.last_stream.closed is True
