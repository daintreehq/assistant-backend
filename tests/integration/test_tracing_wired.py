"""A real turn, traced end to end — the test that the WIRING works.

Every other tracing test exercises the package in isolation. This one drives an
actual `/v1/daintree/respond` request through the orchestrator with capture on and
asserts that the trace the turn produced is complete and contains what a training
example needs. Without it, the whole subsystem could be correct and connected to
nothing, and every unit test would still pass.
"""

from __future__ import annotations

import gzip
import json
from pathlib import Path
from typing import Any

import pytest

from daintree_assistant_server.tracing import (
    DefaultTraceRecorder,
    FilesystemTraceSink,
    decide,
)

USER_TEXT = "QRTZ-user-asked-this-9931"


def _blob_text(directory: Path) -> str:
    return "".join(
        gzip.decompress(p.read_bytes()).decode("utf-8", "replace")
        if p.suffix == ".gz"
        else p.read_bytes().decode("utf-8", "replace")
        for p in (directory / "blobs").iterdir()
    )


def test_a_non_streaming_turn_writes_a_complete_trace(
    client: Any, auth: dict[str, str], tmp_path: Path
) -> None:
    """The default container traces nothing, so this must be explicit."""
    container = client.app.state.container
    recorder = DefaultTraceRecorder(
        sink=FilesystemTraceSink(tmp_path),
        identity_key=b"test-identity-key",
        eligibility=decide(mode="full_internal", is_hardened=False),
        max_event_bytes=8 * 1024 * 1024,
        max_pending_bytes=32 * 1024 * 1024,
        flush_grace_seconds=2.0,
    )
    container.orchestrator.trace_recorder = recorder

    from conftest import respond_body  # noqa: PLC0415 - test-local helper

    response = client.post(
        "/v1/daintree/respond",
        headers=auth,
        json=respond_body([{"role": "user", "content": USER_TEXT}]),
    )
    assert response.status_code == 200, response.text

    traces = [p for p in tmp_path.iterdir() if p.is_dir()]
    assert len(traces) == 1, "exactly one trace per request round"
    directory = traces[0]

    # Complete, per the marker that is written last.
    assert (directory / "complete.json").exists()
    manifest = json.loads((directory / "manifest.json").read_text())
    assert manifest["outcome"]["status"] == "completed"

    # The events a training example is built from all fired.
    events = [
        json.loads(line)
        for line in (directory / "events.jsonl").read_text().splitlines()
        if line.strip()
    ]
    kinds = {e["event_type"] for e in events}
    assert {"request.validated", "runbook.active_set.resolved", "executor.request"} <= kinds
    assert "turn.completed" in kinds

    # And the exact request content is recoverable.
    assert USER_TEXT in _blob_text(directory)


def test_the_default_container_traces_nothing(
    client: Any, auth: dict[str, str], tmp_path: Path
) -> None:
    """The guarantee that matters most: capture is opt-in, and a deployment that
    configured nothing writes nothing anywhere."""
    from conftest import respond_body  # noqa: PLC0415 - test-local helper

    response = client.post(
        "/v1/daintree/respond",
        headers=auth,
        json=respond_body([{"role": "user", "content": USER_TEXT}]),
    )
    assert response.status_code == 200, response.text
    assert list(tmp_path.iterdir()) == []
    assert client.app.state.container.orchestrator.trace_recorder.enabled is False


def test_a_streaming_turn_completes_its_trace_on_the_terminal_event(
    client: Any, auth: dict[str, str], tmp_path: Path
) -> None:
    """The stream path terminates the trace in its `finally`, which is the only
    point a clean finish, an error and a client disconnect all reach."""
    container = client.app.state.container
    container.orchestrator.trace_recorder = DefaultTraceRecorder(
        sink=FilesystemTraceSink(tmp_path),
        identity_key=b"test-identity-key",
        eligibility=decide(mode="full_internal", is_hardened=False),
        max_event_bytes=8 * 1024 * 1024,
        max_pending_bytes=32 * 1024 * 1024,
        flush_grace_seconds=2.0,
    )

    from conftest import respond_body  # noqa: PLC0415 - test-local helper

    with client.stream(
        "POST",
        "/v1/daintree/respond",
        headers=auth,
        json=respond_body(
            [{"role": "user", "content": USER_TEXT}], generation={"stream": True}
        ),
    ) as response:
        assert response.status_code == 200
        body = "".join(response.iter_text())

    assert "event: done" in body
    traces = [p for p in tmp_path.iterdir() if p.is_dir()]
    assert len(traces) == 1
    assert (traces[0] / "complete.json").exists()
    manifest = json.loads((traces[0] / "manifest.json").read_text())
    assert manifest["outcome"]["status"] == "completed"


def test_a_turn_that_fails_in_preparation_still_terminates_its_trace(
    client: Any, auth: dict[str, str], tmp_path: Path
) -> None:
    """A failed preparation is a turn worth studying, so it must say so.

    Without termination the scope is simply abandoned: pending uploads never
    drained, no terminal marker, and the record left indistinguishable from an
    instance that was killed mid-turn.
    """
    container = client.app.state.container
    container.orchestrator.trace_recorder = DefaultTraceRecorder(
        sink=FilesystemTraceSink(tmp_path),
        identity_key=b"test-identity-key",
        eligibility=decide(mode="full_internal", is_hardened=False),
        max_event_bytes=8 * 1024 * 1024,
        max_pending_bytes=32 * 1024 * 1024,
        flush_grace_seconds=2.0,
    )

    # Make assembly fail AFTER the scope is open, the way a real preparation
    # failure does.
    original = container.orchestrator.assembler.build_main_request_with_segments

    def explode(*args: Any, **kwargs: Any) -> Any:
        raise RuntimeError("assembly blew up")

    container.orchestrator.assembler.build_main_request_with_segments = explode  # type: ignore[method-assign]
    try:
        from conftest import respond_body  # noqa: PLC0415 - test-local helper

        # TestClient re-raises an unhandled server exception rather than turning
        # it into a 500, so the failure surfaces here — which is fine: what this
        # test is about is what the trace did on the way out.
        with pytest.raises(RuntimeError, match="assembly blew up"):
            client.post(
                "/v1/daintree/respond",
                headers=auth,
                json=respond_body([{"role": "user", "content": USER_TEXT}]),
            )
    finally:
        container.orchestrator.assembler.build_main_request_with_segments = original  # type: ignore[method-assign]

    traces = [p for p in tmp_path.iterdir() if p.is_dir()]
    assert len(traces) == 1, "the attempt must still be recorded"
    manifest = json.loads((traces[0] / "manifest.json").read_text())
    assert manifest["outcome"]["status"] == "failed"
    # And the request that caused it is recoverable, which is the point.
    assert USER_TEXT in _blob_text(traces[0])


def test_the_trace_records_the_accepted_model_call_and_no_raw_identity(
    client: Any, auth: dict[str, str], tmp_path: Path
) -> None:
    """Two properties that only a real turn can demonstrate.

    First, `invocations` must contain the call that actually produced the answer.
    It previously held only DISCARDED drafts, so an ordinary turn recorded no
    model call at all and a reader would conclude the reply arrived from nowhere.

    Second — and this is the one that matters — no blob may carry the raw session
    id, the raw turn id, or the signed state token. A pseudonymous header is
    worthless if the request stored beside it has the same ids in the clear, and a
    stored state token is a replayable credential (section 7.29).
    """
    container = client.app.state.container
    container.orchestrator.trace_recorder = DefaultTraceRecorder(
        sink=FilesystemTraceSink(tmp_path),
        identity_key=b"test-identity-key",
        eligibility=decide(mode="full_internal", is_hardened=False),
        max_event_bytes=8 * 1024 * 1024,
        max_pending_bytes=32 * 1024 * 1024,
        flush_grace_seconds=2.0,
    )

    from conftest import respond_body  # noqa: PLC0415 - test-local helper

    body = respond_body(
        [{"role": "user", "content": USER_TEXT}],
        session_id="SESSION-RAW-4417",
        turn_id="TURN-RAW-8823",
    )
    response = client.post("/v1/daintree/respond", headers=auth, json=body)
    assert response.status_code == 200, response.text
    issued_state = response.json().get("state")
    assert issued_state, "the turn should have issued a state token to check against"

    directory = next(p for p in tmp_path.iterdir() if p.is_dir())
    manifest = json.loads((directory / "manifest.json").read_text())

    accepted = [inv for inv in manifest["invocations"] if inv["accepted"]]
    assert accepted, f"no accepted invocation recorded: {manifest['invocations']}"
    assert accepted[0]["stage"] == "executor"
    assert accepted[0]["usage"] is not None

    # Nothing identity-bearing anywhere in the stored bytes.
    everything = _blob_text(directory) + (directory / "header.json").read_text()
    assert "SESSION-RAW-4417" not in everything, "the raw session id reached storage"
    assert "TURN-RAW-8823" not in everything, "the raw turn id reached storage"
    assert issued_state not in everything, "a signed state token reached storage"
    # ...while the pseudonyms and the real content did.
    assert "hmac256:" in (directory / "header.json").read_text()
    assert USER_TEXT in _blob_text(directory)
