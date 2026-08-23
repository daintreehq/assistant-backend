"""Local trace storage, for development and for deterministic tests.

This is the sink the trace tests run against, so it is written to have the same
ORDERING guarantees as the GCS one rather than merely the same method names — the
completion marker is written last, blobs are content-addressed and idempotent,
events append. A test double that is laxer than production proves nothing about
production.

**It may contain full user content.** The directory is gitignored and must never
be swept into a diagnostic bundle, a copytree, or an issue attachment.
"""

from __future__ import annotations

import asyncio
import gzip
import json
from pathlib import Path
from typing import Any

from ..canonical import ENCODING, canonical_bytes

#: Written last. Its presence is what makes a trace exportable.
COMPLETION_MARKER = "complete.json"

_EXTENSIONS = {
    "application/json": ".json.gz",
    "text/plain": ".txt.gz",
    "application/octet-stream": ".bin",
}


class FilesystemTraceSink:
    """Writes ``<root>/<trace_id>/`` with header, events, blobs and the marker."""

    def __init__(self, root: Path) -> None:
        self._root = root
        # Serializes appends to events.jsonl within this process. Trace writes are
        # concurrent by design (blobs upload while the model generates), and two
        # coroutines appending to one file is how a JSONL stream acquires a line
        # that is half of each.
        self._locks: dict[str, asyncio.Lock] = {}

    def _dir(self, trace_id: str) -> Path:
        return self._root / trace_id

    def _lock(self, trace_id: str) -> asyncio.Lock:
        lock = self._locks.get(trace_id)
        if lock is None:
            lock = asyncio.Lock()
            self._locks[trace_id] = lock
        return lock

    async def open(self, trace_id: str, header: dict[str, Any]) -> None:
        await asyncio.to_thread(self._open_sync, trace_id, header)

    def _open_sync(self, trace_id: str, header: dict[str, Any]) -> None:
        directory = self._dir(trace_id)
        (directory / "blobs").mkdir(parents=True, exist_ok=True)
        (directory / "header.json").write_bytes(canonical_bytes(header))

    async def put_blob(self, trace_id: str, digest: str, raw: bytes, media_type: str) -> None:
        await asyncio.to_thread(self._put_blob_sync, trace_id, digest, raw, media_type)

    def _put_blob_sync(self, trace_id: str, digest: str, raw: bytes, media_type: str) -> None:
        suffix = _EXTENSIONS.get(media_type, ".bin")
        path = self._dir(trace_id) / "blobs" / f"{digest}{suffix}"
        # Content-addressed: the same digest is by definition the same bytes, so a
        # repeat is a no-op rather than a rewrite. This is the deduplication that
        # keeps a 20-round tool conversation from storing its history 20 times.
        if path.exists():
            return
        path.parent.mkdir(parents=True, exist_ok=True)
        payload = raw if suffix.endswith(".bin") else gzip.compress(raw)
        # Write-then-rename so a reader never sees a half-written blob whose
        # bytes do not hash to the name it is filed under.
        tmp = path.with_suffix(path.suffix + ".partial")
        tmp.write_bytes(payload)
        tmp.replace(path)

    async def put_events(self, trace_id: str, events: list[dict[str, Any]]) -> None:
        if not events:
            return
        async with self._lock(trace_id):
            await asyncio.to_thread(self._put_events_sync, trace_id, events)

    def _put_events_sync(self, trace_id: str, events: list[dict[str, Any]]) -> None:
        path = self._dir(trace_id) / "events.jsonl"
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding=ENCODING) as handle:
            for event in events:
                handle.write(json.dumps(event, ensure_ascii=False, sort_keys=True) + "\n")

    async def finish(self, trace_id: str, manifest: dict[str, Any]) -> None:
        await asyncio.to_thread(self._finish_sync, trace_id, manifest)
        self._locks.pop(trace_id, None)

    def _finish_sync(self, trace_id: str, manifest: dict[str, Any]) -> None:
        directory = self._dir(trace_id)
        directory.mkdir(parents=True, exist_ok=True)
        # Manifest FIRST, marker SECOND. The marker means "everything the manifest
        # names is on disk"; writing it first would make that claim before it was
        # true, and an exporter has no way to detect the difference.
        (directory / "manifest.json").write_bytes(canonical_bytes(manifest))
        (directory / COMPLETION_MARKER).write_bytes(
            canonical_bytes({"trace_id": trace_id, "blobs": len(manifest.get("blobs", []))})
        )

    async def aclose(self) -> None:
        self._locks.clear()
