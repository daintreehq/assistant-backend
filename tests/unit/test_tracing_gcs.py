"""What the GCS sink does is OURS to get right; what Cloud Storage does is not.

Nothing here has run against a real bucket — there is no bucket and CI has no
credentials. So this pins exactly the half that is ours: object paths, content
types, the auth header, and above all the ORDER of the final two writes. What the
API accepts is unpinned, and `TODO.md` carries that the same way it carries the
Together contract gap.
"""

from __future__ import annotations

from typing import Any

import httpx
import pytest

from daintree_assistant_server.tracing.sinks.gcs import COMPLETION_MARKER, GcsTraceSink


class _Capture:
    def __init__(self) -> None:
        self.requests: list[httpx.Request] = []

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        if "metadata.google.internal" in str(request.url):
            return httpx.Response(200, json={"access_token": "tok-123"})
        return httpx.Response(200, json={})

    @property
    def uploaded_names(self) -> list[str]:
        return [
            r.url.params["name"]
            for r in self.requests
            if "storage.googleapis.com" in str(r.url)
        ]


def _sink(capture: _Capture, **kwargs: Any) -> tuple[GcsTraceSink, httpx.AsyncClient]:
    client = httpx.AsyncClient(transport=httpx.MockTransport(capture.handler))
    return (
        GcsTraceSink(bucket="traces", prefix="raw/v1", client=client, **kwargs),
        client,
    )


@pytest.mark.asyncio
async def test_the_completion_marker_is_the_last_object_written() -> None:
    """The marker means "everything the manifest names is stored".

    Uploaded in parallel with the manifest, it could land first after a crash, and
    an exporter would then export a trace whose blobs are missing — which is the
    one failure the marker exists to make impossible.
    """
    capture = _Capture()
    sink, client = _sink(capture, access_token="tok")
    try:
        await sink.open("trc_1", {"trace_id": "trc_1"})
        await sink.put_blob("trc_1", "abc123", b"payload", "application/json")
        await sink.put_events("trc_1", [{"event_id": "e1"}])
        await sink.finish("trc_1", {"trace_id": "trc_1", "blobs": [{"sha256": "abc123"}]})
    finally:
        await client.aclose()

    names = capture.uploaded_names
    assert names[-1] == f"raw/v1/trc_1/{COMPLETION_MARKER}"
    assert names[-2] == "raw/v1/trc_1/manifest.json"


@pytest.mark.asyncio
async def test_objects_land_under_the_configured_prefix_and_trace_id() -> None:
    capture = _Capture()
    sink, client = _sink(capture, access_token="tok")
    try:
        await sink.open("trc_2", {"trace_id": "trc_2"})
        await sink.put_blob("trc_2", "deadbeef", b"{}", "application/json")
        await sink.put_events("trc_2", [{"event_id": "e"}])
    finally:
        await client.aclose()

    assert capture.uploaded_names == [
        "raw/v1/trc_2/header.json",
        "raw/v1/trc_2/blobs/deadbeef.json.gz",
        "raw/v1/trc_2/events.jsonl.gz",
    ]


@pytest.mark.asyncio
async def test_every_upload_carries_the_bearer_token() -> None:
    capture = _Capture()
    sink, client = _sink(capture, access_token="tok-abc")
    try:
        await sink.open("trc_3", {"trace_id": "trc_3"})
    finally:
        await client.aclose()

    uploads = [r for r in capture.requests if "storage.googleapis.com" in str(r.url)]
    assert uploads
    assert all(r.headers["Authorization"] == "Bearer tok-abc" for r in uploads)


@pytest.mark.asyncio
async def test_a_token_the_metadata_server_will_not_give_stops_the_write_quietly() -> None:
    """An unauthenticated sink stores nothing — the same outcome as an outage,
    which the scope above already handles. It must not raise its way out."""

    def refuse(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500, json={})

    client = httpx.AsyncClient(transport=httpx.MockTransport(refuse))
    sink = GcsTraceSink(bucket="traces", prefix="raw/v1", client=client)
    try:
        await sink.open("trc_4", {"trace_id": "trc_4"})  # must not raise
    finally:
        await client.aclose()


@pytest.mark.asyncio
async def test_an_upload_rejected_by_the_api_raises_for_the_scope_to_count() -> None:
    """The sink surfaces it; `TraceScope._guard` is what stops it reaching a turn.

    Swallowing it here instead would make every storage failure invisible, and
    `trace_write_failures` — the number an operator alerts on — would stay zero
    through a total outage.
    """

    def reject(request: httpx.Request) -> httpx.Response:
        if "metadata.google.internal" in str(request.url):
            return httpx.Response(200, json={"access_token": "t"})
        return httpx.Response(403, json={"error": "forbidden"})

    client = httpx.AsyncClient(transport=httpx.MockTransport(reject))
    sink = GcsTraceSink(bucket="traces", prefix="raw/v1", client=client)
    try:
        with pytest.raises(httpx.HTTPStatusError):
            await sink.open("trc_5", {"trace_id": "trc_5"})
    finally:
        await client.aclose()


@pytest.mark.asyncio
async def test_the_sink_never_closes_a_client_it_does_not_own() -> None:
    """It borrows the container's pool; closing it would tear down a client other
    components still hold."""
    capture = _Capture()
    sink, client = _sink(capture, access_token="tok")
    try:
        await sink.aclose()
        assert not client.is_closed
    finally:
        await client.aclose()


@pytest.mark.asyncio
async def test_a_metadata_token_is_reused_until_it_nears_expiry(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Fetching one per upload would triple the request count for no benefit."""
    capture = _Capture()
    client = httpx.AsyncClient(transport=httpx.MockTransport(capture.handler))
    sink = GcsTraceSink(bucket="traces", prefix="raw/v1", client=client)
    try:
        await sink.open("trc_a", {"trace_id": "trc_a"})
        await sink.put_blob("trc_a", "aa", b"{}", "application/json")
    finally:
        await client.aclose()

    token_fetches = [
        r for r in capture.requests if "metadata.google.internal" in str(r.url)
    ]
    assert len(token_fetches) == 1, "the token should be cached across uploads"


@pytest.mark.asyncio
async def test_an_expired_token_is_refetched_rather_than_used_forever(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Caching one forever is the obvious shortcut and the wrong one.

    Metadata tokens last about an hour. A long-lived Cloud Run instance would
    upload happily and then fail EVERY write for the rest of its life — a total
    loss of the corpus that no test of the happy path would ever notice.
    """
    capture = _Capture()

    def short_lived(request: httpx.Request) -> httpx.Response:
        capture.requests.append(request)
        if "metadata.google.internal" in str(request.url):
            return httpx.Response(200, json={"access_token": "t", "expires_in": 3600})
        return httpx.Response(200, json={})

    client = httpx.AsyncClient(transport=httpx.MockTransport(short_lived))
    sink = GcsTraceSink(bucket="traces", prefix="raw/v1", client=client)
    clock = [1000.0]
    monkeypatch.setattr(
        "daintree_assistant_server.tracing.sinks.gcs.time.monotonic", lambda: clock[0]
    )
    try:
        await sink.open("trc_b", {"trace_id": "trc_b"})
        # An hour later the cached token is past its refresh point.
        clock[0] += 3600
        await sink.put_blob("trc_b", "bb", b"{}", "application/json")
    finally:
        await client.aclose()

    token_fetches = [
        r for r in capture.requests if "metadata.google.internal" in str(r.url)
    ]
    assert len(token_fetches) == 2, "an expired token must be refetched"


@pytest.mark.asyncio
async def test_a_rejected_token_is_refreshed_once_and_the_upload_retried() -> None:
    """A 401 against a token we believed was live means our expiry estimate was
    wrong. Exactly one retry: anything else is a real failure, and retrying it
    would spend the flush budget twice for the same answer."""
    seen: list[str] = []

    def once_stale(request: httpx.Request) -> httpx.Response:
        if "metadata.google.internal" in str(request.url):
            seen.append("token")
            return httpx.Response(
                200, json={"access_token": f"tok{len(seen)}", "expires_in": 3600}
            )
        seen.append("upload")
        # First upload attempt is rejected; the retry succeeds.
        return httpx.Response(401 if seen.count("upload") == 1 else 200, json={})

    client = httpx.AsyncClient(transport=httpx.MockTransport(once_stale))
    sink = GcsTraceSink(bucket="traces", prefix="raw/v1", client=client)
    try:
        await sink.open("trc_c", {"trace_id": "trc_c"})  # must not raise
    finally:
        await client.aclose()

    assert seen.count("token") == 2, "the rejected token should have been refreshed"
    assert seen.count("upload") == 2, "exactly one retry"


@pytest.mark.asyncio
async def test_a_persistent_401_gives_up_rather_than_looping() -> None:
    def always_stale(request: httpx.Request) -> httpx.Response:
        if "metadata.google.internal" in str(request.url):
            return httpx.Response(200, json={"access_token": "t", "expires_in": 3600})
        return httpx.Response(401, json={})

    client = httpx.AsyncClient(transport=httpx.MockTransport(always_stale))
    sink = GcsTraceSink(bucket="traces", prefix="raw/v1", client=client)
    try:
        with pytest.raises(httpx.HTTPStatusError):
            await sink.open("trc_d", {"trace_id": "trc_d"})
    finally:
        await client.aclose()
