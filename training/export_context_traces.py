"""Turn raw traces into training derivatives, refusing everything that is not one.

This is the ONLY path by which captured content becomes a dataset, which makes it
the place every gate has to actually hold. It is deliberately paranoid and
deliberately offline: it runs from a workstation or a batch job under a SEPARATE
identity from the serving one, which holds write-only access to the trace bucket.

Every refusal below is a refusal to produce data, never a repair:

* **No completion marker → skip.** The marker is written last, so a trace without
  one is a prefix — an instance that died mid-turn. Its events are real but its
  blobs may not all be there, and an example built from a partial trace is wrong
  in a way nothing downstream can detect.
* **A blob whose bytes do not hash to its name → skip the whole trace.** Not just
  the blob: a store that produced one bad object has no claim on the others.
* **Wrong schema version → skip.** Reading a durable record with the wrong
  reader is how a field silently changes meaning.
* **Not training-eligible → skip.** Recorded eligibility is the only consent
  signal that exists; ignoring it here would make the capture gate decorative.
* **An orphaned tool result → skip.** A result with no call is a broken
  dependency, and a model trained on it learns that results appear from nowhere.

Secrets are redacted on the way out and never repaired in place: the raw trace is
the record, and a redactor that rewrote it would be destroying evidence rather
than protecting anyone.

Usage:
    python -m training.export_context_traces <trace-root> --out examples.jsonl \\
        --redaction-key-file key.txt
"""

from __future__ import annotations

import argparse
import gzip
import hashlib
import json
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "src"))

from daintree_assistant_server.tracing.models import TRACE_SCHEMA_VERSION  # noqa: E402
from daintree_assistant_server.tracing.redaction import redact  # noqa: E402

#: Bumped when the EXPORT shape changes, independently of the trace schema. A
#: dataset records which exporter built it, because "why does this example look
#: like that" is otherwise unanswerable a year later.
EXPORTER_VERSION = 1

COMPLETION_MARKER = "complete.json"

#: Without these there is no example, only a shell. Kept small on purpose: a
#: fast-response event is optional (the stage may be off), a tool event is
#: optional (most turns have none), but a request and an executor call are what
#: an example IS.
REQUIRED_EVENTS = frozenset({"request.validated", "executor.request", "turn.completed"})


@dataclass(frozen=True)
class Skipped:
    trace_id: str
    reason: str


def _read_blob(directory: Path, digest: str) -> bytes | None:
    for path in (directory / "blobs").glob(f"{digest}.*"):
        raw = path.read_bytes()
        return gzip.decompress(raw) if path.suffix == ".gz" else raw
    return None


def _load_events(directory: Path) -> list[dict[str, Any]] | None:
    """Read the event stream from EITHER sink's representation.

    The filesystem sink writes `events.jsonl`; the GCS sink writes
    `events.jsonl.gz`. Reading only the first silently produced an empty list for
    every trace that came from production — and because nothing downstream
    required any particular event, those exported as rows with `current_request`,
    `executor_input` and `executor_output` all null. A dataset of hollow rows is
    worse than no dataset, so a missing stream is now `None` (a refusal) rather
    than `[]` (an empty one).
    """
    plain = directory / "events.jsonl"
    gzipped = directory / "events.jsonl.gz"
    if plain.exists():
        raw = plain.read_bytes()
    elif gzipped.exists():
        raw = gzip.decompress(gzipped.read_bytes())
    else:
        return None
    return [
        json.loads(line)
        for line in raw.decode("utf-8").splitlines()
        if line.strip()
    ]


def _tool_graph_is_sound(events: list[dict[str, Any]]) -> str | None:
    """Every observed result must name a call that was observed first.

    Call ids are NOT globally unique — the transcript validator deliberately
    allows one to be reused in a later turn once the first is answered — so this
    matches within the trace in order rather than against a global set.
    """
    open_calls: set[str] = set()
    for event in events:
        attributes = event.get("attributes") or {}
        call_id = attributes.get("call_id")
        if event.get("event_type") == "tool.call.observed" and call_id:
            open_calls.add(str(call_id))
        elif event.get("event_type") == "tool.result.observed":
            if not call_id:
                return "tool result with no call_id"
            if str(call_id) not in open_calls:
                return f"orphaned tool result for call {call_id}"
            open_calls.discard(str(call_id))
    return None


def _redact_tree(value: Any, key: bytes, counter: list[int]) -> Any:
    """Redact every string in a nested structure, counting what was replaced."""
    if isinstance(value, str):
        result = redact(value, key=key)
        counter[0] += result.count
        return result.text
    if isinstance(value, dict):
        return {k: _redact_tree(v, key, counter) for k, v in value.items()}
    if isinstance(value, list):
        return [_redact_tree(v, key, counter) for v in value]
    return value


def export_trace(directory: Path, *, redaction_key: bytes) -> dict[str, Any] | Skipped:
    """One trace to one example, or a reason it is not one."""
    trace_id = directory.name

    if not (directory / COMPLETION_MARKER).exists():
        return Skipped(trace_id, "no completion marker (trace is a prefix)")

    manifest_path = directory / "manifest.json"
    if not manifest_path.exists():
        return Skipped(trace_id, "no manifest")
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))

    header = manifest.get("header") or {}
    if header.get("schema_version") != TRACE_SCHEMA_VERSION:
        return Skipped(
            trace_id,
            f"schema version {header.get('schema_version')} != {TRACE_SCHEMA_VERSION}",
        )
    if not header.get("training_eligible"):
        return Skipped(trace_id, f"not training-eligible ({header.get('eligibility_reason')})")
    # An ALLOWLIST, not a denylist. `failed` and `cancelled` traces are real and
    # worth keeping — a preparation failure is a turn worth studying — but they
    # have no executor output, so exporting one as a training example would teach
    # a model that the correct completion of a request is nothing at all.
    status = manifest.get("outcome", {}).get("status")
    if status != "completed":
        return Skipped(trace_id, f"outcome is {status!r}, not 'completed'")

    # Verify EVERY blob before using any of them.
    for entry in manifest.get("blobs", []):
        digest = entry["sha256"]
        raw = _read_blob(directory, digest)
        if raw is None:
            return Skipped(trace_id, f"manifest names a missing blob {digest[:12]}")
        if hashlib.sha256(raw).hexdigest() != digest:
            return Skipped(trace_id, f"blob {digest[:12]} does not match its hash")

    events = _load_events(directory)
    if events is None:
        return Skipped(trace_id, "no event stream (events.jsonl[.gz] missing)")
    if (problem := _tool_graph_is_sound(events)) is not None:
        return Skipped(trace_id, problem)

    # The events an example cannot be built without. Checked BEFORE assembling
    # anything, so the refusal names the real reason rather than surfacing later
    # as a row full of nulls.
    present = {event.get("event_type") for event in events}
    missing = sorted(REQUIRED_EVENTS - present)
    if missing:
        return Skipped(trace_id, f"missing required event(s): {', '.join(missing)}")

    def content_for(event_type: str) -> Any:
        for event in events:
            if event.get("event_type") != event_type:
                continue
            ref = event.get("content_ref")
            if not ref:
                return None
            raw = _read_blob(directory, ref["sha256"])
            return json.loads(raw.decode("utf-8")) if raw else None
        return None

    redactions = [0]
    example = {
        "example_id": f"ex_{trace_id[4:]}",
        "trace_id": trace_id,
        "turn_group": header.get("turn_id_hash"),
        "session_key": header.get("session_key"),
        "schema_version": TRACE_SCHEMA_VERSION,
        "exporter_version": EXPORTER_VERSION,
        "current_request": content_for("request.validated"),
        "active_runbooks": content_for("runbook.active_set.resolved"),
        "fast_response_output": content_for("fast_response.response"),
        "executor_input": content_for("executor.request"),
        "executor_output": content_for("turn.completed"),
        "final_outcome": manifest.get("outcome"),
        "source_manifest": manifest.get("invocations", []),
    }
    example = _redact_tree(example, redaction_key, redactions)
    example["redaction_count"] = redactions[0]
    example["redaction_policy_sha256"] = hashlib.sha256(
        f"typed_redaction/v{EXPORTER_VERSION}".encode()
    ).hexdigest()[:16]
    return example


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("root", type=Path, help="directory holding <trace_id>/ folders")
    parser.add_argument("--out", type=Path, required=True, help="JSONL destination")
    parser.add_argument(
        "--redaction-key-file",
        type=Path,
        required=True,
        help="file holding the keying material for redaction tags",
    )
    args = parser.parse_args(argv)

    key = args.redaction_key_file.read_bytes().strip()
    if not key:
        print("redaction key file is empty", file=sys.stderr)
        return 2

    exported = 0
    skipped: list[Skipped] = []
    with args.out.open("w", encoding="utf-8") as handle:
        for directory in sorted(p for p in args.root.iterdir() if p.is_dir()):
            result = export_trace(directory, redaction_key=key)
            if isinstance(result, Skipped):
                skipped.append(result)
                continue
            handle.write(json.dumps(result, ensure_ascii=False, sort_keys=True) + "\n")
            exported += 1

    print(f"exported {exported} example(s) to {args.out}")
    if skipped:
        print(f"skipped {len(skipped)}:")
        for entry in skipped:
            print(f"  {entry.trace_id}: {entry.reason}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
