"""The training-trace plane: the properties that make it safe to turn on.

Grouped by the promise each one keeps, because these are not ordinary unit tests —
they are the reasons it is acceptable for this service to record user content at
all. Doc reference: section 7.34, "Trace acceptance criteria".
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any

import pytest

from daintree_assistant_server.config import Settings
from daintree_assistant_server.tracing import (
    DefaultTraceRecorder,
    FilesystemTraceSink,
    NullTraceRecorder,
    NullTraceSink,
    TraceOutcome,
    decide,
)
from daintree_assistant_server.tracing.canonical import (
    canonical_bytes,
    canonical_sha256,
    wire_sha256,
)
from daintree_assistant_server.tracing.redaction import redact
from daintree_assistant_server.tracing.scrub import scrub_request
from daintree_assistant_server.tracing.sinks.filesystem import COMPLETION_MARKER


def _recorder(sink: Any, **overrides: Any) -> DefaultTraceRecorder:
    kwargs: dict[str, Any] = {
        "sink": sink,
        "identity_key": b"trace-identity-key",
        "eligibility": decide(mode="full_internal", is_hardened=False),
        "max_event_bytes": 8 * 1024 * 1024,
        "max_pending_bytes": 32 * 1024 * 1024,
        "flush_grace_seconds": 1.0,
    }
    kwargs.update(overrides)
    return DefaultTraceRecorder(**kwargs)


async def _start(recorder: DefaultTraceRecorder, **overrides: Any) -> Any:
    kwargs: dict[str, Any] = {
        "request_id": "req_1",
        "session_id": "ses-1",
        "turn_id": "turn-1",
    }
    kwargs.update(overrides)
    return await recorder.start_trace(**kwargs)


# ---------------------------------------------------------------------------
# Canonical vs wire encoding — two hashes because they answer two questions.
# ---------------------------------------------------------------------------


def test_canonical_hashing_ignores_key_order_and_wire_hashing_does_not() -> None:
    """The single most confusable thing in this package.

    A trace blob must deduplicate across two recordings of semantically identical
    content, so it sorts. A provider prefix cache matches LITERAL bytes, so the
    wire hash must not sort — a sorted wire digest would call a genuine cache miss
    a hit, a false negative on the exact question the prompt manifest exists to
    answer. Both live here; conflating them breaks one of the two.
    """
    a = {"b": 1, "a": {"d": 2, "c": 3}}
    b = {"a": {"c": 3, "d": 2}, "b": 1}

    assert canonical_sha256(a) == canonical_sha256(b)
    assert wire_sha256(a) != wire_sha256(b)


def test_canonical_encoding_refuses_values_json_cannot_round_trip() -> None:
    """A blob an exporter cannot parse is not a durable record.

    NaN and Infinity are not JSON. Accepting them writes a file that a standard
    reader rejects months later, in a training pipeline, with no way back to the
    turn that produced it.
    """
    with pytest.raises(ValueError):
        canonical_bytes({"score": float("nan")})


def test_canonical_encoding_does_not_escape_non_ascii() -> None:
    """The same trap `context_budget` and the assembler both document.

    `ensure_ascii=True` turns one CJK character into six bytes, so a Japanese
    session would be stored and measured at several times its real size.
    """
    assert canonical_bytes({"t": "日本語"}) == '{"t":"日本語"}'.encode()


# ---------------------------------------------------------------------------
# The null path — it must change nothing at all.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_the_null_recorder_records_nothing_and_reports_nothing() -> None:
    """Every ordinary deployment holds this, on the hot path of every request."""
    recorder = NullTraceRecorder()
    assert recorder.enabled is False

    scope = await recorder.start_trace(request_id="r", session_id="s", turn_id="t")
    assert await scope.record_event("request.validated", content={"secret": "x"}) == ""
    assert scope.store_content({"secret": "x"}) is None
    await scope.complete(TraceOutcome(status="completed"))
    # Nothing to say, so nothing is added to the operational log record.
    assert scope.log_fields() == {}


@pytest.mark.asyncio
async def test_a_disabled_mode_hands_back_the_null_scope_even_from_a_real_recorder() -> None:
    """Configuration, not call sites, is what turns capture off."""
    recorder = _recorder(NullTraceSink(), eligibility=decide(mode="off", is_hardened=False))
    scope = await _start(recorder)
    assert scope.trace_id == ""


# ---------------------------------------------------------------------------
# The completion marker — the contract an exporter depends on.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_the_completion_marker_is_written_last(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Its presence means every object the manifest names is stored.

    Written first, it would make that claim before it was true — and an exporter
    has no way to detect the difference, so it would export a trace whose blobs
    are missing.

    This OBSERVES the real writes by intercepting `Path.write_bytes`, rather than
    subclassing the sink. A recording subclass that reimplements `_finish_sync`
    tests its own copy of the ordering and passes happily while production writes
    them the other way round — which is the one thing this test exists to catch.
    """
    order: list[str] = []
    real_write_bytes = Path.write_bytes

    def spy(self: Path, data: bytes) -> int:
        order.append(self.name)
        return real_write_bytes(self, data)

    monkeypatch.setattr(Path, "write_bytes", spy)

    recorder = _recorder(FilesystemTraceSink(tmp_path))
    scope = await _start(recorder)
    await scope.record_event("request.validated", content={"hello": "world"})
    await scope.complete(TraceOutcome(status="completed"))

    assert COMPLETION_MARKER in order, f"no marker was written: {order}"
    assert order[-1] == COMPLETION_MARKER, (
        f"the completion marker must be the LAST write, got {order}"
    )
    assert order.index("manifest.json") < order.index(COMPLETION_MARKER)
    # And the blob the manifest names landed before the manifest did.
    blob_writes = [i for i, name in enumerate(order) if name.endswith((".gz", ".partial"))]
    assert blob_writes, f"no blob was written: {order}"
    assert max(blob_writes) < order.index("manifest.json")


@pytest.mark.asyncio
async def test_an_interrupted_trace_has_no_completion_marker(tmp_path: Path) -> None:
    """The exporter's filter: a trace without one is a PREFIX of a trace."""
    recorder = _recorder(FilesystemTraceSink(tmp_path))
    scope = await _start(recorder)
    await scope.record_event("request.validated", content={"hello": "world"})
    # Deliberately never completed — the instance died mid-turn.
    directory = tmp_path / scope.trace_id
    assert not (directory / COMPLETION_MARKER).exists()


# ---------------------------------------------------------------------------
# Content, deduplication, and what must NOT be stored.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_identical_content_is_stored_once_within_a_trace(tmp_path: Path) -> None:
    """A 20-round tool conversation must not store its history 20 times."""
    recorder = _recorder(FilesystemTraceSink(tmp_path))
    scope = await _start(recorder)
    history = {"messages": [{"role": "user", "content": "the same long transcript"}]}
    for _ in range(5):
        await scope.record_event("executor.request", content=history)
    await scope.complete(TraceOutcome(status="completed"))

    blobs = list((tmp_path / scope.trace_id / "blobs").iterdir())
    assert len(blobs) == 1, "content-addressed storage should collapse the repeats"

    manifest = json.loads((tmp_path / scope.trace_id / "manifest.json").read_text())
    assert len(manifest["blobs"]) == 1
    assert manifest["event_count"] == 5


@pytest.mark.asyncio
async def test_metadata_mode_records_the_skeleton_and_none_of_the_content(
    tmp_path: Path,
) -> None:
    """The mode a general production deployment may safely run."""
    recorder = _recorder(
        FilesystemTraceSink(tmp_path), eligibility=decide(mode="metadata", is_hardened=False)
    )
    scope = await _start(recorder)
    await scope.record_event("request.validated", content={"secret": "user text"})
    await scope.complete(TraceOutcome(status="completed"))

    directory = tmp_path / scope.trace_id
    blobs = list((directory / "blobs").iterdir())
    assert blobs == [], "metadata mode must not write content"
    events = (directory / "events.jsonl").read_text()
    assert "request.validated" in events
    assert "user text" not in events


@pytest.mark.asyncio
async def test_a_stored_trace_carries_no_raw_session_id_and_no_raw_turn_id(
    tmp_path: Path,
) -> None:
    """Pseudonymous by construction; `request_id` is the deliberate exception."""
    recorder = _recorder(FilesystemTraceSink(tmp_path))
    scope = await _start(
        recorder, session_id="ses-secret-1234", turn_id="turn-secret-9876", request_id="req_abc"
    )
    await scope.complete(TraceOutcome(status="completed"))

    stored = (tmp_path / scope.trace_id / "header.json").read_text()
    assert "ses-secret-1234" not in stored
    assert "turn-secret-9876" not in stored
    assert "hmac256:" in stored
    # The one raw id, and the reason it is raw: it is the cross-plane handle that
    # already appears in operational logs.
    assert "req_abc" in stored


@pytest.mark.asyncio
async def test_turn_pseudonyms_are_scoped_to_their_session(tmp_path: Path) -> None:
    """A bare turn id is unique only WITHIN a session.

    Two clients that mint `turn-1` are two different subjects. Hashing the turn id
    alone would merge them under one pseudonym and silently corrupt the grouping
    the key exists to provide.
    """
    recorder = _recorder(NullTraceSink())
    a = await _start(recorder, session_id="ses-a", turn_id="turn-1")
    b = await _start(recorder, session_id="ses-b", turn_id="turn-1")
    assert a._header.turn_id_hash != b._header.turn_id_hash
    assert a._header.session_key != b._header.session_key


@pytest.mark.asyncio
async def test_a_different_identity_key_yields_different_pseudonyms() -> None:
    """The pseudonym is KEYED, so it is not a lookup table anyone can rebuild."""
    one = await _start(_recorder(NullTraceSink(), identity_key=b"key-one"))
    two = await _start(_recorder(NullTraceSink(), identity_key=b"key-two"))
    assert one._header.session_key != two._header.session_key


# ---------------------------------------------------------------------------
# Failure behaviour — a trace may never take a turn down with it.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_a_sink_that_raises_on_everything_never_reaches_the_caller() -> None:
    """Doc section 7.33. The whole reason this is safe to wire into the spine."""

    class BrokenSink(NullTraceSink):
        async def open(self, trace_id: str, header: dict[str, Any]) -> None:
            raise RuntimeError("storage down")

        async def put_blob(
            self, trace_id: str, digest: str, raw: bytes, media_type: str
        ) -> None:
            raise RuntimeError("storage down")

        async def put_events(self, trace_id: str, events: list[dict[str, Any]]) -> None:
            raise RuntimeError("storage down")

        async def finish(self, trace_id: str, manifest: dict[str, Any]) -> None:
            raise RuntimeError("storage down")

    # `open` is the one sink call made before a scope exists, so the scope's own
    # guard cannot cover it. It must still not raise: a storage outage — or, far
    # more likely, an expired credential — would otherwise fail a user turn, which
    # is the single thing this subsystem promises never to do. An untraced turn is
    # the correct degradation.
    scope = await _start(_recorder(BrokenSink()))
    assert scope.trace_id == "", "a failed open must degrade to the null scope"

    # And every call after a scope exists is swallowed and counted.
    live = await _start(_recorder(NullTraceSink()))
    live._sink = BrokenSink()  # type: ignore[assignment]
    await live.record_event("executor.request", content={"a": 1})
    await live.complete(TraceOutcome(status="completed"))
    assert live.failures > 0, "failures must be counted, not raised"


@pytest.mark.asyncio
async def test_an_oversized_payload_is_dropped_rather_than_truncated(tmp_path: Path) -> None:
    """A truncated blob filed under a hash of the WHOLE thing is a lie.

    Nothing downstream could detect it: the hash would not match the bytes, and an
    exporter verifying hashes would reject the trace for the wrong reason.
    """
    recorder = _recorder(FilesystemTraceSink(tmp_path), max_event_bytes=64)
    scope = await _start(recorder)
    await scope.record_event("executor.request", content={"big": "x" * 500})
    await scope.complete(TraceOutcome(status="completed"))

    manifest = json.loads((tmp_path / scope.trace_id / "manifest.json").read_text())
    assert manifest["blobs"] == []
    assert manifest["outcome"]["status"] == "incomplete"
    assert "oversize" in (manifest["outcome"]["incomplete_reason"] or "")


@pytest.mark.asyncio
async def test_backpressure_marks_the_trace_incomplete_rather_than_growing(
    tmp_path: Path,
) -> None:
    """Cloud Run memory is finite; an unbounded pending set is an OOM waiting."""
    recorder = _recorder(FilesystemTraceSink(tmp_path), max_pending_bytes=32)
    scope = await _start(recorder)
    for i in range(5):
        await scope.record_event("executor.request", content={"n": i, "pad": "y" * 100})
    await scope.complete(TraceOutcome(status="completed"))

    manifest = json.loads((tmp_path / scope.trace_id / "manifest.json").read_text())
    assert manifest["outcome"]["status"] == "incomplete"
    assert "backpressure" in (manifest["outcome"]["incomplete_reason"] or "")


@pytest.mark.asyncio
async def test_a_slow_sink_is_abandoned_at_the_grace_budget(tmp_path: Path) -> None:
    """A user does not hold a connection open for a training record."""

    class SlowSink(FilesystemTraceSink):
        async def put_blob(
            self, trace_id: str, digest: str, raw: bytes, media_type: str
        ) -> None:
            await asyncio.sleep(30)

    recorder = _recorder(SlowSink(tmp_path), flush_grace_seconds=0.05)
    scope = await _start(recorder)
    await scope.record_event("executor.request", content={"a": 1})

    loop = asyncio.get_running_loop()
    started = loop.time()
    await scope.complete(TraceOutcome(status="completed"))
    elapsed = loop.time() - started

    assert elapsed < 5.0, "complete() must not wait on a hung upload"
    manifest = json.loads((tmp_path / scope.trace_id / "manifest.json").read_text())
    assert manifest["outcome"]["status"] == "incomplete"


# ---------------------------------------------------------------------------
# Redaction — for training derivatives, never for the raw trace.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "text",
    [
        "OPENAI_API_KEY=sk-AAAAAAAAAAAAAAAAAAAAAAAA",
        "curl -H 'Authorization: Bearer eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9'",
        "-----BEGIN RSA PRIVATE KEY-----\nMIIEow==\n-----END RSA PRIVATE KEY-----",
        "Set-Cookie: session=abcdef1234567890",
        "AWS key AKIAIOSFODNN7EXAMPLE here",
    ],
)
def test_seeded_secrets_are_replaced_with_a_typed_marker(text: str) -> None:
    result = redact(text, key=b"redaction-key")
    assert result.count >= 1
    assert "<REDACTED:" in result.text


def test_redaction_preserves_provenance_without_the_secret() -> None:
    """A reviewer holding the RAW trace can find what was removed; the derivative
    carries only where and what type."""
    secret = "sk-BBBBBBBBBBBBBBBBBBBBBBBB"
    result = redact(f"key is {secret} ok", key=b"redaction-key")

    assert secret not in result.text
    assert secret not in json.dumps(result.as_dict())
    (one,) = result.redactions
    assert one.type == "API_KEY"
    assert one.start_byte == len("key is ")
    assert one.end_byte == len("key is ") + len(secret)


@pytest.mark.parametrize(
    "prefix",
    ["", "plain ascii ", "日本語のテキスト ", "emoji 🔐 then "],
)
def test_recorded_offsets_index_the_original_bytes_exactly(prefix: str) -> None:
    """Provenance is only useful if it points at the right bytes.

    The offsets are UTF-8 byte offsets into the ORIGINAL text, and they are
    accumulated incrementally rather than by re-encoding the prefix per match —
    which was quadratic over blobs this module is capped at 8 MiB of. Multi-byte
    text is where an incremental counter goes wrong, so it is what this checks.
    """
    secret = "sk-YYYYYYYYYYYYYYYYYYYYYYYY"
    original = f"{prefix}one {secret} two {secret} end"
    result = redact(original, key=b"offsets")

    raw = original.encode("utf-8")
    assert result.count == 2
    for entry in result.redactions:
        assert raw[entry.start_byte : entry.end_byte].decode("utf-8") == secret


def test_redaction_stays_linear_on_a_secret_dense_blob() -> None:
    """A quadratic here is not a security bug, but it is a batch export that
    never finishes. Doubling the input must not quadruple the time."""
    import time

    def elapsed(count: int) -> float:
        text = "SECRET=abcdefgh " * count
        started = time.perf_counter()
        redact(text, key=b"perf")
        return time.perf_counter() - started

    small = elapsed(4000)
    large = elapsed(16000)
    # 4x the input. Linear would be ~4x; quadratic ~16x. A generous bound, so
    # this fails on an algorithmic regression rather than on a slow machine.
    assert large < small * 8 + 0.5, f"redaction looks super-linear: {small=} {large=}"


def test_the_same_secret_correlates_and_a_different_one_does_not() -> None:
    """Keyed suffixes let an approved dataset see "this is the same credential"
    without exposing it, and without an unkeyed hash of a short secret being
    trivially reversible."""
    key = b"redaction-key"
    a = redact("sk-CCCCCCCCCCCCCCCCCCCCCCCC", key=key).text
    again = redact("sk-CCCCCCCCCCCCCCCCCCCCCCCC", key=key).text
    other = redact("sk-DDDDDDDDDDDDDDDDDDDDDDDD", key=key).text

    assert a == again
    assert a != other
    assert redact("sk-CCCCCCCCCCCCCCCCCCCCCCCC", key=b"other-key").text != a


def test_ordinary_content_is_left_completely_alone() -> None:
    """Anchored on STRUCTURE, not entropy: a git SHA and a UUID are content here,
    and a redactor that shreds them destroys the example it is preparing."""
    text = "commit 4f2a1c9d8e7b6a5f4e3d2c1b0a9f8e7d6c5b4a39 and id 550e8400-e29b-41d4-a716-446655440000"
    result = redact(text, key=b"redaction-key")
    assert result.count == 0
    assert result.text == text


# ---------------------------------------------------------------------------
# Configuration gates.
# ---------------------------------------------------------------------------


def test_full_content_capture_is_refused_in_a_hardened_environment() -> None:
    """There is no account model, so there is nobody who has consented."""
    settings = Settings(
        STATE_SIGNING_KEY_CURRENT="x" * 32,
        OPENAI_API_KEY="k",
        ENVIRONMENT="production",
        TRACE_CAPTURE_MODE="full_internal",
        TRACE_IDENTITY_KEY="y" * 32,
    )
    problems = settings.trace_problems()
    assert any("refused in production" in p for p in problems)
    # And the request-level decision refuses too, independently of readiness —
    # `build_container` is also called directly by tests and benchmarks.
    assert decide(mode="full_internal", is_hardened=True).capture_content is False


def test_content_capture_requires_a_dedicated_identity_key() -> None:
    settings = Settings(
        STATE_SIGNING_KEY_CURRENT="x" * 32,
        OPENAI_API_KEY="k",
        TRACE_CAPTURE_MODE="full_internal",
    )
    assert any("TRACE_IDENTITY_KEY" in p for p in settings.trace_problems())


def test_filesystem_traces_are_forbidden_in_a_hardened_environment() -> None:
    """Container-local storage for content-bearing traces disappears with the
    instance, and is not access-controlled."""
    settings = Settings(
        STATE_SIGNING_KEY_CURRENT="x" * 32,
        OPENAI_API_KEY="k",
        ENVIRONMENT="staging",
        TRACE_CAPTURE_MODE="metadata",
        TRACE_SINK="filesystem",
        TRACE_FILESYSTEM_DIR="/tmp/traces",
    )
    assert any("filesystem is forbidden" in p for p in settings.trace_problems())


def test_a_gcs_sink_without_a_bucket_fails_readiness() -> None:
    settings = Settings(
        STATE_SIGNING_KEY_CURRENT="x" * 32,
        OPENAI_API_KEY="k",
        TRACE_CAPTURE_MODE="metadata",
        TRACE_SINK="gcs",
    )
    assert any("TRACE_GCS_BUCKET" in p for p in settings.trace_problems())


def test_the_default_deployment_traces_nothing() -> None:
    """A service with no consent model records nothing unless told to."""
    settings = Settings(STATE_SIGNING_KEY_CURRENT="x" * 32, OPENAI_API_KEY="k")
    assert settings.trace_capture_mode == "off"
    assert settings.trace_problems() == []
    assert decide(mode=settings.trace_capture_mode, is_hardened=False).enabled is False


# ---------------------------------------------------------------------------
# In-flight accounting — a gauge, not a running total.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_the_in_flight_budget_is_released_when_a_write_settles(
    tmp_path: Path,
) -> None:
    """It bounds how much is in flight AT ONCE, not how much the trace stored.

    Only ever incrementing turns the budget into a per-trace total cap wearing a
    "pending" name, and a long tool-calling turn would then start silently
    dropping content once its cumulative bytes crossed the line — for a reason
    that has nothing to do with memory pressure.
    """
    recorder = _recorder(FilesystemTraceSink(tmp_path), max_pending_bytes=4096)
    scope = await _start(recorder)

    await scope.record_event("executor.request", content={"a": "x" * 200})
    assert scope._pending_bytes > 0, "a scheduled write must reserve its bytes"

    # Let the scheduled uploads actually run.
    await asyncio.sleep(0)
    while scope._pending:
        await asyncio.sleep(0)

    assert scope._pending_bytes == 0, "a settled write must release its reservation"
    assert scope._stored_bytes > 0, "the cumulative figure keeps counting"
    await scope.complete(TraceOutcome(status="completed"))


@pytest.mark.asyncio
async def test_a_failing_write_still_releases_its_reservation(tmp_path: Path) -> None:
    """Otherwise a run of failing uploads exhausts the budget and starts dropping
    content for a reason unrelated to how much is in flight."""

    class FailingSink(FilesystemTraceSink):
        async def put_blob(
            self, trace_id: str, digest: str, raw: bytes, media_type: str
        ) -> None:
            raise RuntimeError("storage down")

    recorder = _recorder(FailingSink(tmp_path), max_pending_bytes=4096)
    scope = await _start(recorder)
    await scope.record_event("executor.request", content={"a": "x" * 200})
    while scope._pending:
        await asyncio.sleep(0)

    assert scope._pending_bytes == 0
    assert scope.failures > 0
    await scope.complete(TraceOutcome(status="completed"))


@pytest.mark.asyncio
async def test_a_blob_that_fails_to_write_is_not_attested_to_by_the_marker(
    tmp_path: Path,
) -> None:
    """The completion marker's only claim is "the manifest's objects are stored".

    The failure this pins is an ordering one: most uploads are still in flight
    when termination begins, so checking for failures BEFORE the drain sees an
    empty set and writes a marker attesting to blobs that failed while it waited.
    """

    class FailingBlobSink(FilesystemTraceSink):
        async def put_blob(
            self, trace_id: str, digest: str, raw: bytes, media_type: str
        ) -> None:
            # Yield first, so the failure lands DURING the terminal drain rather
            # than before it — which is what makes the ordering observable.
            await asyncio.sleep(0)
            raise RuntimeError("storage down")

    recorder = _recorder(FailingBlobSink(tmp_path))
    scope = await _start(recorder)
    await scope.record_event("executor.request", content={"a": "x" * 50})
    await scope.complete(TraceOutcome(status="completed"))

    manifest = json.loads((tmp_path / scope.trace_id / "manifest.json").read_text())
    assert manifest["outcome"]["status"] == "incomplete"
    assert "blob_write_failed" in (manifest["outcome"]["incomplete_reason"] or "")
    # And the failed blob is not listed, so an exporter is not sent looking for an
    # object that was never written.
    assert manifest["blobs"] == []


@pytest.mark.asyncio
async def test_recording_after_termination_is_dropped(tmp_path: Path) -> None:
    """Nothing is left to drain an upload scheduled past the marker."""
    recorder = _recorder(FilesystemTraceSink(tmp_path))
    scope = await _start(recorder)
    await scope.complete(TraceOutcome(status="completed"))

    assert await scope.record_event("late.event", content={"a": 1}) == ""
    assert not scope._pending, "a post-close call must not schedule an upload"


def test_an_overlapping_short_match_cannot_suppress_a_longer_secret() -> None:
    """The leak that first-wins overlap resolution produced.

    `MY_SECRET=-----BEGIN RSA PRIVATE KEY-----…` matches ENV_SECRET at offset 0
    and PRIVATE_KEY a few characters later. Resolving by "first span wins" let the
    short match suppress the long one, and the entire key body then survived into
    the training derivative — a passing test suite and a leaked private key.
    """
    body = "MIIEowIBAAKCAQEAsecretmaterial\n" * 10
    text = (
        "MY_SECRET=-----BEGIN RSA PRIVATE KEY-----\n"
        + body
        + "-----END RSA PRIVATE KEY-----"
    )
    result = redact(text, key=b"overlap")

    assert "MIIEowIBAAKCAQEAsecretmaterial" not in result.text
    assert "-----BEGIN" not in result.text
    # Typed by the match that covered the most, which is the useful finding.
    assert "<REDACTED:PRIVATE_KEY:" in result.text


def test_the_correlation_suffix_is_wide_enough_not_to_collide() -> None:
    """At 16 bits a corpus of a few thousand secrets collides by birthday and
    starts asserting that unrelated credentials are the same one."""
    key = b"width"
    tags = {redact(f"sk-{i:024d}", key=key).text for i in range(4000)}
    assert len(tags) == 4000, "distinct secrets must not share a correlation tag"


@pytest.mark.parametrize("value", [None, "a string", 42, ["a", "list"]])
def test_scrubbing_a_non_mapping_returns_it_untouched(value: Any) -> None:
    """It is fed a dumped request, but must not corrupt anything else it is
    handed — a defensive return, not a transformation."""
    assert scrub_request(value) == value


def test_scrubbing_removes_server_identity_and_nothing_else() -> None:
    """Exact paths, not key names anywhere.

    Dropping every key called `state` at any depth would delete a tool argument
    named `state`, a workflow node's `state`, a terminal's `state` — ordinary
    content, silently corrupted, in a trace whose only purpose is to be exact.
    """
    dumped = {
        "session": {"id": "SES-1", "turn_id": "T-1", "round": 3},
        "state": "dst1.SIGNED.sig",
        "input": {
            "messages": [{"role": "user", "content": "set state to open"}],
            "tools": [
                {
                    "function": {
                        "name": "workflow",
                        "parameters": {"properties": {"state": {}, "id": {}}},
                    }
                }
            ],
        },
    }
    out = scrub_request(dumped)
    rendered = json.dumps(out)

    # Gone.
    assert "SES-1" not in rendered
    assert "T-1" not in rendered
    assert "SIGNED" not in rendered
    # Kept — both the non-identity session field and every legitimate use of the
    # same words inside content.
    assert out["session"]["round"] == 3
    assert "state" in out["input"]["tools"][0]["function"]["parameters"]["properties"]
    assert "id" in out["input"]["tools"][0]["function"]["parameters"]["properties"]
    assert "set state to open" in rendered


# ---------------------------------------------------------------------------
# The container builder — the gate that decides whether capture happens at all.
# ---------------------------------------------------------------------------


def _settings(**overrides: Any) -> Settings:
    base: dict[str, Any] = {
        "STATE_SIGNING_KEY_CURRENT": "x" * 32,
        "OPENAI_API_KEY": "k",
    }
    base.update(overrides)
    return Settings(**base)


def test_the_builder_returns_the_null_recorder_when_capture_is_off() -> None:
    """The default, and therefore the one that matters most."""
    from daintree_assistant_server.main import _build_trace_recorder

    recorder = _build_trace_recorder(_settings(), None)
    assert recorder.enabled is False


def test_the_builder_refuses_a_configuration_readiness_would_reject(
    tmp_path: Path,
) -> None:
    """`/readyz` already refuses these, but `build_container` is ALSO called
    directly by tests and benchmarks, which never call `/readyz`. A recorder that
    captured content because nobody happened to probe readiness would be the whole
    failure this gate exists to prevent."""
    from daintree_assistant_server.main import _build_trace_recorder

    # Content capture with no identity key: ids would be pseudonymised under an
    # empty key, which anyone holding the trace could recompute.
    settings = _settings(
        TRACE_CAPTURE_MODE="full_internal",
        TRACE_SINK="filesystem",
        TRACE_FILESYSTEM_DIR=str(tmp_path),
    )
    assert settings.trace_problems()
    assert _build_trace_recorder(settings, None).enabled is False


def test_the_builder_wires_a_filesystem_sink_when_fully_configured(
    tmp_path: Path,
) -> None:
    from daintree_assistant_server.main import _build_trace_recorder

    recorder = _build_trace_recorder(
        _settings(
            TRACE_CAPTURE_MODE="full_internal",
            TRACE_SINK="filesystem",
            TRACE_FILESYSTEM_DIR=str(tmp_path),
            TRACE_IDENTITY_KEY="y" * 32,
        ),
        None,
    )
    assert recorder.enabled is True
    assert isinstance(recorder._sink, FilesystemTraceSink)


def test_the_builder_falls_back_to_the_null_sink_when_gcs_has_no_client() -> None:
    """A sink it cannot construct must degrade to storing nothing, never to
    storing somewhere unintended."""
    from daintree_assistant_server.main import _build_trace_recorder

    recorder = _build_trace_recorder(
        _settings(
            TRACE_CAPTURE_MODE="metadata",
            TRACE_SINK="gcs",
            TRACE_GCS_BUCKET="bucket",
            TRACE_IDENTITY_KEY="y" * 32,
        ),
        None,
    )
    assert isinstance(recorder._sink, NullTraceSink)


def test_the_builder_wires_gcs_when_a_client_is_available() -> None:
    import httpx

    from daintree_assistant_server.main import _build_trace_recorder
    from daintree_assistant_server.tracing.sinks.gcs import GcsTraceSink

    def _ok(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200)

    # Never entered, so it needs no teardown: the builder only STORES the client.
    client = httpx.AsyncClient(transport=httpx.MockTransport(_ok))
    recorder = _build_trace_recorder(
        _settings(
            TRACE_CAPTURE_MODE="metadata",
            TRACE_SINK="gcs",
            TRACE_GCS_BUCKET="bucket",
            TRACE_IDENTITY_KEY="y" * 32,
        ),
        client,
    )
    assert isinstance(recorder._sink, GcsTraceSink)
