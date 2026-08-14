"""Retry, degradation, and streaming-failure behavior (sections 6.8, 10.3, 18)."""

from __future__ import annotations

from conftest import parse_sse, respond_body
from fakes import FakeStream, make_result, selection_result

from daintree_assistant_server.errors import upstream_error, upstream_rate_limited

URL = "/v1/daintree/respond"


def _body(**kwargs: object) -> dict:
    return respond_body([{"role": "user", "content": "rename my terminals"}], session_id="s", turn_id="t1", **kwargs)


def _stream_body() -> dict:
    return _body(generation={"stream": True})


def test_nonstream_retries_before_success(client, auth, fake_provider) -> None:
    fake_provider.selection_result = selection_result([])
    calls = [0]

    def complete(_req):
        calls[0] += 1
        if calls[0] < 2:
            raise upstream_rate_limited()
        return make_result(content="recovered")

    fake_provider.complete_result = complete
    r = client.post(URL, headers=auth, json=_body())
    assert r.status_code == 200
    assert calls[0] == 2  # 1 try + 1 retry (the full default budget)
    assert r.json()["message"]["content"] == "recovered"


def test_nonstream_persistent_error_surfaces(client, auth, fake_provider) -> None:
    fake_provider.selection_result = selection_result([])
    # A tiny Retry-After keeps the test fast: the retry helper honors the header
    # as a real wait (capped at 10s), so a large value here would sleep for real.
    fake_provider.complete_error = upstream_rate_limited(retry_after="0.05")
    r = client.post(URL, headers=auth, json=_body())
    assert r.status_code == 429
    assert r.json()["error"]["code"] == "upstream_rate_limited"
    assert r.headers.get("retry-after") == "0.05"
    # Default budget: 1 try + 1 retry = 2 attempts, all exhausted before surfacing.
    assert len(fake_provider.requests) == 2


def test_nonstream_non_retryable_error(client, auth, fake_provider) -> None:
    fake_provider.selection_result = selection_result([])
    fake_provider.complete_error = upstream_error("bad gateway")  # not retryable
    r = client.post(URL, headers=auth, json=_body())
    assert r.status_code == 502
    assert r.json()["error"]["code"] == "upstream_error"


def test_stream_open_retries_before_success(client, auth, fake_provider) -> None:
    fake_provider.selection_result = selection_result([])
    calls = [0]

    def factory(_req):
        calls[0] += 1
        if calls[0] < 2:
            raise upstream_rate_limited()
        return FakeStream(
            [
                {"id": "x", "object": "chat.completion.chunk", "created": 1, "model": "m",
                 "choices": [{"index": 0, "delta": {"content": "rename my terminals"}, "finish_reason": None}]},
                {"id": "x", "object": "chat.completion.chunk", "created": 1, "model": "m",
                 "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}]},
            ]
        )

    fake_provider.stream_factory = factory
    with client.stream("POST", URL, headers=auth, json=_stream_body()) as s:
        assert s.status_code == 200
        text = "".join(s.iter_text())
    assert calls[0] == 2
    assert "done" in [e for e, _ in parse_sse(text)]


def test_stream_open_persistent_error_is_terminal_sse_after_meta(
    client, auth, fake_provider
) -> None:
    fake_provider.selection_result = selection_result([])
    # Tiny Retry-After: the retry helper honors the header as a real wait.
    fake_provider.stream_error = upstream_rate_limited(retry_after="0.05")
    # Skill metadata commits the stream before provider connection begins. An
    # exhausted open retry therefore terminates honestly inside SSE.
    with client.stream("POST", URL, headers=auth, json=_stream_body()) as s:
        assert s.status_code == 200
        text = "".join(s.iter_text())
    events = parse_sse(text)
    assert [event for event, _ in events] == ["meta", "error"]
    assert events[-1][1]["error"]["code"] == "upstream_rate_limited"
    assert events[-1][1]["retry_after"] == "0.05"
    # Default budget: 1 try + 1 retry = 2 stream-open attempts before surfacing.
    assert len(fake_provider.stream_requests) == 2


def test_stream_breaks_after_first_token_no_fabricated_finish(client, auth, fake_provider) -> None:
    fake_provider.selection_result = selection_result([])

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
    with client.stream("POST", URL, headers=auth, json=_stream_body()) as s:
        assert s.status_code == 200  # headers already sent
        text = "".join(s.iter_text())
    events = parse_sse(text)
    kinds = [e for e, _ in events]
    streamed = "".join(d.get("content", "") for e, d in events if e == "delta")
    assert "partial" in streamed
    assert "error" in kinds       # terminal error event
    assert "done" not in kinds    # no fabricated successful finish
    error_event = next(data for event, data in events if event == "error")
    assert "retry_after" not in error_event


def test_stream_closes_upstream_on_completion(client, auth, fake_provider) -> None:
    fake_provider.selection_result = selection_result([])
    fake_provider.stream_chunks = [
        {"id": "x", "object": "chat.completion.chunk", "created": 1, "model": "m",
         "choices": [{"index": 0, "delta": {"content": "rename my terminals"}, "finish_reason": "stop"}]},
    ]
    with client.stream("POST", URL, headers=auth, json=_stream_body()) as s:
        list(s.iter_text())
    assert fake_provider.last_stream is not None
    assert fake_provider.last_stream.closed is True
