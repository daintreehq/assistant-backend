"""The exporter's refusals, which are the only thing standing between a raw
trace and a dataset.

Each test here is a way a bad example could otherwise be manufactured. They are
worth more than the happy path: an exporter that produces examples is easy, and
an exporter that reliably declines to is the whole point.
"""

from __future__ import annotations

import gzip
import json
from pathlib import Path
from typing import Any

import pytest

from daintree_assistant_server.tracing.canonical import canonical_bytes, sha256_hex
from daintree_assistant_server.tracing.models import TRACE_SCHEMA_VERSION
from training.export_context_traces import Skipped, export_trace

KEY = b"redaction-key"


def _write_trace(
    root: Path,
    trace_id: str = "trc_1",
    *,
    complete: bool = True,
    training_eligible: bool = True,
    schema_version: int = TRACE_SCHEMA_VERSION,
    status: str = "completed",
    events: list[dict[str, Any]] | None = None,
    blobs: dict[str, Any] | None = None,
    corrupt_blob: bool = False,
) -> Path:
    directory = root / trace_id
    (directory / "blobs").mkdir(parents=True, exist_ok=True)

    blobs = (
        blobs
        if blobs is not None
        else {
            "request": {"messages": ["hello"]},
            "executor": {"messages": [{"role": "user", "content": "hello"}]},
            "final": {"role": "assistant", "content": "hi back"},
        }
    )
    manifest_blobs = []
    digests: dict[str, str] = {}
    for name, payload in blobs.items():
        raw = canonical_bytes(payload)
        digest = sha256_hex(raw)
        digests[name] = digest
        stored = b"tampered" if corrupt_blob else raw
        (directory / "blobs" / f"{digest}.json.gz").write_bytes(gzip.compress(stored))
        manifest_blobs.append({"sha256": digest, "byte_length": len(raw)})

    # The DEFAULT is a realistic minimum turn. The exporter requires these three,
    # and a helper that omitted them was manufacturing traces production never
    # produces — which is exactly how a fixture stops testing anything.
    base_events = [
        {
            "event_id": "e1",
            "sequence": 1,
            "event_type": "request.validated",
            "attributes": {},
            "content_ref": {"sha256": digests["request"]},
        },
        {
            "event_id": "e2",
            "sequence": 2,
            "event_type": "executor.request",
            "attributes": {},
            "content_ref": {"sha256": digests.get("executor", digests["request"])},
        },
        {
            "event_id": "e3",
            "sequence": 3,
            "event_type": "turn.completed",
            "attributes": {},
            "content_ref": {"sha256": digests.get("final", digests["request"])},
        },
    ]
    if events is None:
        events = base_events
    else:
        # Callers pass the events they care about; keep the required ones so a
        # test about tool linkage is not silently also a test about completeness.
        events = base_events + events
    (directory / "events.jsonl").write_text(
        "\n".join(json.dumps(e) for e in events) + "\n", encoding="utf-8"
    )

    (directory / "manifest.json").write_bytes(
        canonical_bytes(
            {
                "trace_id": trace_id,
                "header": {
                    "schema_version": schema_version,
                    "training_eligible": training_eligible,
                    "eligibility_reason": "internal_controlled_build",
                    "turn_id_hash": "hmac256:abc",
                    "session_key": "hmac256:def",
                },
                "outcome": {"status": status},
                "blobs": manifest_blobs,
                "invocations": [],
            }
        )
    )
    if complete:
        (directory / "complete.json").write_bytes(canonical_bytes({"trace_id": trace_id}))
    return directory


def test_a_well_formed_eligible_trace_exports(tmp_path: Path) -> None:
    result = export_trace(_write_trace(tmp_path), redaction_key=KEY)
    assert not isinstance(result, Skipped)
    assert result["current_request"] == {"messages": ["hello"]}
    assert result["schema_version"] == TRACE_SCHEMA_VERSION
    # Provenance: which exporter and which redaction policy built this row.
    assert result["exporter_version"] >= 1
    assert result["redaction_policy_sha256"]


def test_a_trace_without_a_completion_marker_is_never_exported(tmp_path: Path) -> None:
    """The marker is written last, so its absence means the trace is a PREFIX."""
    result = export_trace(_write_trace(tmp_path, complete=False), redaction_key=KEY)
    assert isinstance(result, Skipped)
    assert "completion marker" in result.reason


def test_an_ineligible_trace_is_never_exported(tmp_path: Path) -> None:
    """Recorded eligibility is the only consent signal that exists.

    Ignoring it here would make every capture gate upstream decorative.
    """
    result = export_trace(_write_trace(tmp_path, training_eligible=False), redaction_key=KEY)
    assert isinstance(result, Skipped)
    assert "not training-eligible" in result.reason


def test_a_blob_that_does_not_match_its_hash_fails_the_whole_trace(tmp_path: Path) -> None:
    """Not just the blob. A store that produced one bad object has no claim on
    the rest, and a partially-verified example is not a verified one."""
    result = export_trace(_write_trace(tmp_path, corrupt_blob=True), redaction_key=KEY)
    assert isinstance(result, Skipped)
    assert "does not match its hash" in result.reason


def test_an_unknown_schema_version_is_refused(tmp_path: Path) -> None:
    """Reading a durable record with the wrong reader is how a field silently
    changes meaning."""
    result = export_trace(
        _write_trace(tmp_path, schema_version=TRACE_SCHEMA_VERSION + 1), redaction_key=KEY
    )
    assert isinstance(result, Skipped)
    assert "schema version" in result.reason


def test_an_incomplete_outcome_is_refused(tmp_path: Path) -> None:
    result = export_trace(_write_trace(tmp_path, status="incomplete"), redaction_key=KEY)
    assert isinstance(result, Skipped)
    assert "incomplete" in result.reason


def test_an_orphaned_tool_result_fails_the_trace(tmp_path: Path) -> None:
    """A result with no call teaches a model that results appear from nowhere."""
    events = [
        {
            "event_id": "e1",
            "sequence": 1,
            "event_type": "tool.result.observed",
            "attributes": {"call_id": "call_never_opened"},
        }
    ]
    result = export_trace(_write_trace(tmp_path, events=events), redaction_key=KEY)
    assert isinstance(result, Skipped)
    assert "orphaned tool result" in result.reason


def test_a_matched_tool_call_and_result_pass(tmp_path: Path) -> None:
    events = [
        {
            "event_id": "e1",
            "sequence": 1,
            "event_type": "tool.call.observed",
            "attributes": {"call_id": "call_1"},
        },
        {
            "event_id": "e2",
            "sequence": 2,
            "event_type": "tool.result.observed",
            "attributes": {"call_id": "call_1"},
        },
    ]
    result = export_trace(_write_trace(tmp_path, events=events), redaction_key=KEY)
    assert not isinstance(result, Skipped)


def test_a_call_id_reused_after_being_answered_is_accepted(tmp_path: Path) -> None:
    """Ids come from the MODEL and are only unique within an open batch.

    `services/validation.py` deliberately permits reuse once the first call is
    answered — a global uniqueness rule here would reject coherent conversations.
    """
    events = [
        {"event_id": "e1", "sequence": 1, "event_type": "tool.call.observed",
         "attributes": {"call_id": "c1"}},
        {"event_id": "e2", "sequence": 2, "event_type": "tool.result.observed",
         "attributes": {"call_id": "c1"}},
        {"event_id": "e3", "sequence": 3, "event_type": "tool.call.observed",
         "attributes": {"call_id": "c1"}},
        {"event_id": "e4", "sequence": 4, "event_type": "tool.result.observed",
         "attributes": {"call_id": "c1"}},
    ]
    result = export_trace(_write_trace(tmp_path, events=events), redaction_key=KEY)
    assert not isinstance(result, Skipped)


def test_secrets_in_captured_content_are_redacted_on_the_way_out(tmp_path: Path) -> None:
    """The raw trace keeps them; the derivative must not."""
    secret = "sk-EEEEEEEEEEEEEEEEEEEEEEEE"
    directory = _write_trace(
        tmp_path,
        blobs={"request": {"messages": [f"my key is {secret}"]}},
    )
    result = export_trace(directory, redaction_key=KEY)

    assert not isinstance(result, Skipped)
    rendered = json.dumps(result)
    assert secret not in rendered
    assert "<REDACTED:API_KEY:" in rendered
    # >= 1, not == 1: this fixture points several events at the one blob, so the
    # same secret is reached more than once. What matters is that every occurrence
    # was replaced and the count is a real tally rather than a constant — the
    # clean-trace test below pins the zero case.
    assert result["redaction_count"] >= 1


def test_a_clean_trace_reports_no_redactions(tmp_path: Path) -> None:
    """The counter must be a real measurement, not a constant."""
    result = export_trace(_write_trace(tmp_path), redaction_key=KEY)
    assert not isinstance(result, Skipped)
    assert result["redaction_count"] == 0


def test_a_gcs_shaped_trace_with_a_compressed_event_stream_exports(tmp_path: Path) -> None:
    """The two sinks write the event stream differently, and both are production.

    The filesystem sink writes `events.jsonl`; the GCS sink writes
    `events.jsonl.gz`. An exporter that read only the first returned an EMPTY
    event list for every trace that came from production — and, before required
    events existed, exported those as rows with request, input and output all
    null. Silent hollow rows are the worst possible failure for a dataset.
    """
    directory = _write_trace(tmp_path, trace_id="trc_gcs")
    plain = directory / "events.jsonl"
    (directory / "events.jsonl.gz").write_bytes(gzip.compress(plain.read_bytes()))
    plain.unlink()

    result = export_trace(directory, redaction_key=KEY)
    assert not isinstance(result, Skipped), getattr(result, "reason", None)
    assert result["current_request"] == {"messages": ["hello"]}


def test_a_trace_with_no_event_stream_at_all_is_refused(tmp_path: Path) -> None:
    """A missing stream must be a REFUSAL, not an empty one."""
    directory = _write_trace(tmp_path)
    (directory / "events.jsonl").unlink()

    result = export_trace(directory, redaction_key=KEY)
    assert isinstance(result, Skipped)
    assert "no event stream" in result.reason


@pytest.mark.parametrize("missing", ["request.validated", "executor.request", "turn.completed"])
def test_a_trace_missing_an_essential_event_is_refused(tmp_path: Path, missing: str) -> None:
    """Each of these IS the example; without one there is only a shell.

    Caught before assembly so the refusal names the real reason, rather than
    surfacing later as a row full of nulls that looks like a modelling problem.
    """
    directory = _write_trace(tmp_path)
    kept = [
        line
        for line in (directory / "events.jsonl").read_text().splitlines()
        if line.strip() and json.loads(line)["event_type"] != missing
    ]
    (directory / "events.jsonl").write_text("\n".join(kept) + "\n", encoding="utf-8")

    result = export_trace(directory, redaction_key=KEY)
    assert isinstance(result, Skipped)
    assert missing in result.reason


@pytest.mark.parametrize("status", ["failed", "cancelled", "incomplete"])
def test_only_a_completed_turn_becomes_a_training_example(
    tmp_path: Path, status: str
) -> None:
    """An allowlist, not a denylist.

    `failed` and `cancelled` traces are real and worth KEEPING — a preparation
    failure is a turn worth studying — but they have no executor output, so
    exporting one would teach a model that the right completion of a request is
    nothing at all. Rejecting only `incomplete` let exactly that through.
    """
    result = export_trace(_write_trace(tmp_path, status=status), redaction_key=KEY)
    assert isinstance(result, Skipped)
    assert "not 'completed'" in result.reason
