"""The promise that makes two planes worth having: content never crosses over.

`observability.py` is metadata-only and `tracing/` is content-bearing, and the
whole safety argument rests on the boundary between them holding. This drives a
REAL turn with full content capture on, then asserts that the sentinel strings
which reached the trace appear in no operational log record.

Doc reference: section 14.18. This is the test it asks for, and it is deliberately
end-to-end rather than a unit test of a formatter: the failure it guards against is
a future `logger.info(..., prompt=...)` added by someone who did not know, and only
a real turn would catch that.
"""

from __future__ import annotations

import gzip
import json
import re
from typing import Any

import pytest
from structlog.testing import capture_logs

from daintree_assistant_server.tracing import (
    DefaultTraceRecorder,
    FilesystemTraceSink,
    TraceOutcome,
    decide,
)

#: Distinctive enough that a substring match cannot be a coincidence.
USER_SENTINEL = "ZQXJ-user-instruction-sentinel-7741"
RUNBOOK_SENTINEL = "ZQXJ-runbook-body-sentinel-8852"
TOOL_ARG_SENTINEL = "ZQXJ-tool-argument-sentinel-9963"
OUTPUT_SENTINEL = "ZQXJ-executor-output-sentinel-1074"

ALL_SENTINELS = (USER_SENTINEL, RUNBOOK_SENTINEL, TOOL_ARG_SENTINEL, OUTPUT_SENTINEL)


@pytest.fixture
def captured_logs() -> Any:
    """Capture every structlog record emitted during the test.

    `structlog.testing.capture_logs` rather than a hand-rolled processor swap:
    `configure_logging` sets `cache_logger_on_first_use=True`, so any logger bound
    before the swap keeps its old chain and its records never arrive. That is
    invisible when this file runs alone and appears the moment the full suite
    runs, which is the worst way for a safety test to fail — silently, and only
    sometimes. The helper handles the cache; the assertion below still proves the
    capture worked before anything is concluded from its emptiness.
    """
    with capture_logs() as records:
        yield records


def test_a_real_traced_turn_puts_no_content_in_any_log_record(
    client: Any, auth: dict[str, str], tmp_path: Any, captured_logs: list[dict[str, Any]]
) -> None:
    """The end-to-end version, driving an actual request through the app.

    A unit test over the recorder cannot catch the failure this guards against —
    a future `logger.info(..., prompt=...)` added by someone who did not know
    there were two planes. Only a real turn, with capture on, can.
    """
    container = client.app.state.container
    container.orchestrator.trace_recorder = DefaultTraceRecorder(
        sink=FilesystemTraceSink(tmp_path),
        identity_key=b"identity",
        eligibility=decide(mode="full_internal", is_hardened=False),
        max_event_bytes=8 * 1024 * 1024,
        max_pending_bytes=32 * 1024 * 1024,
        flush_grace_seconds=2.0,
    )

    from conftest import respond_body  # noqa: PLC0415 - test-local helper

    response = client.post(
        "/v1/daintree/respond",
        headers=auth,
        json=respond_body([{"role": "user", "content": USER_SENTINEL}]),
    )
    assert response.status_code == 200, response.text

    # The capture is WORKING — this is what stops the assertion below being
    # vacuous. `respond` is the application's own per-turn record.
    events = [record.get("event") for record in captured_logs]
    assert "respond" in events, (
        f"the log capture never saw the application's own respond record: {events}"
    )

    # The turn's content really did reach the trace...
    traces = [p for p in tmp_path.iterdir() if p.is_dir()]
    assert traces, "nothing was traced; this test would be vacuous"
    stored = "".join(
        gzip.decompress(p.read_bytes()).decode("utf-8", "replace")
        if p.suffix == ".gz"
        else p.read_bytes().decode("utf-8", "replace")
        for p in (traces[0] / "blobs").iterdir()
    )
    assert USER_SENTINEL in stored

    # ...and none of it reached a log record.
    rendered = json.dumps(captured_logs, default=str)
    assert USER_SENTINEL not in rendered, (
        "user content leaked from the training-trace plane into an operational "
        "log record. Operational logs are metadata-only — see observability.py."
    )


@pytest.mark.asyncio
async def test_the_operational_metrics_a_trace_contributes_carry_no_hashes(
    tmp_path: Any,
) -> None:
    """A stable content hash in Cloud Logging is a cross-session fingerprint.

    Two turns whose logs carry the same digest are two turns carrying the same
    text — an inference about content, derived from a plane that promises to carry
    none. Counts are safe; hashes are not, and they live in the restricted
    manifest instead.
    """
    recorder = DefaultTraceRecorder(
        sink=FilesystemTraceSink(tmp_path),
        identity_key=b"identity",
        eligibility=decide(mode="full_internal", is_hardened=False),
        max_event_bytes=8 * 1024 * 1024,
        max_pending_bytes=32 * 1024 * 1024,
        flush_grace_seconds=1.0,
    )
    scope = await recorder.start_trace(request_id="r", session_id="s", turn_id="t")
    await scope.record_event("executor.request", content={"messages": ["hello"]})

    fields = scope.log_fields()
    rendered = json.dumps(fields)
    assert fields["trace_blob_count"] == 1
    # No long hex digest anywhere in what goes to the log.
    assert not re.search(r"\b[0-9a-f]{32,}\b", rendered), (
        f"a content hash reached the operational log fields: {fields}"
    )
    await scope.complete(TraceOutcome(status="completed"))
