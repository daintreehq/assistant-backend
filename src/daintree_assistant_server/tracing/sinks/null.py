"""The sink that stores nothing, and costs as close to nothing as it can.

Every deployment holds this unless an operator configured otherwise, so it sits
on the hot path of every request. It allocates nothing, awaits nothing, and
returns immediately — which is what makes it safe for the orchestrator to call
the recorder unconditionally instead of guarding every call site with a flag.

A guard at every call site is the alternative, and it is worse: it is a dozen
places to forget, and each one is a branch that only ever takes one direction in
production and is therefore never exercised.
"""

from __future__ import annotations

from typing import Any


class NullTraceSink:
    """Discards everything. Shared and stateless."""

    async def open(self, trace_id: str, header: dict[str, Any]) -> None:
        return None

    async def put_blob(self, trace_id: str, digest: str, raw: bytes, media_type: str) -> None:
        return None

    async def put_events(self, trace_id: str, events: list[dict[str, Any]]) -> None:
        return None

    async def finish(self, trace_id: str, manifest: dict[str, Any]) -> None:
        return None

    async def aclose(self) -> None:
        return None
