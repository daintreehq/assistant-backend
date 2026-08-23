"""The storage boundary for traces.

A sink stores three kinds of thing and nothing else: blobs (content, addressed by
hash), the event stream, and the terminal marker. Keeping the interface this
narrow is what lets the filesystem sink be a real test double for the GCS one
rather than an approximation of it.

**The completion marker is the contract.** ``finish()`` is the LAST write, and its
presence is the only signal that every object a manifest names actually landed.
An exporter that reads a trace without one is reading a prefix of a trace — an
instance that died mid-turn, a flush that ran out of grace — and must skip it.
That is why finish() takes the manifest: writing the marker and writing what it
attests to cannot be two decisions.
"""

from __future__ import annotations

from typing import Any, Protocol


class TraceSink(Protocol):
    """Where a trace's bytes go. Every method is best-effort by contract."""

    async def open(self, trace_id: str, header: dict[str, Any]) -> None:
        """Begin a trace. Writes the header; must be safe to call once."""
        ...

    async def put_blob(self, trace_id: str, digest: str, raw: bytes, media_type: str) -> None:
        """Store content addressed by its SHA-256.

        Content-addressed, so a repeated write of the same digest is a no-op. That
        is what keeps a long tool-calling turn from storing the whole conversation
        once per round.
        """
        ...

    async def put_events(self, trace_id: str, events: list[dict[str, Any]]) -> None:
        """Append events. Called more than once; must not truncate."""
        ...

    async def finish(self, trace_id: str, manifest: dict[str, Any]) -> None:
        """Write the manifest and then the completion marker, in that order.

        An implementation that writes the marker first has removed the only
        guarantee this interface offers.
        """
        ...

    async def aclose(self) -> None:
        """Release resources. Must be idempotent."""
        ...
