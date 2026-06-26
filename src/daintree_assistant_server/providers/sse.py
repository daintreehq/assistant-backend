"""Server-Sent Events parsing helpers.

Minimal SSE reader for OpenAI-style streams: events are separated by blank
lines, payload arrives on ``data:`` lines, and the stream terminates with a
``data: [DONE]`` marker.
"""

from __future__ import annotations

import json
from collections.abc import AsyncIterator
from typing import Any

DONE_MARKER = "[DONE]"


async def iter_sse_data(lines: AsyncIterator[str]) -> AsyncIterator[str]:
    """Yield the ``data`` payload of each SSE event from a line iterator.

    Lines are expected without trailing newlines (as produced by
    ``httpx.Response.aiter_lines``). Comment lines (starting with ``:``) and
    non-data fields are ignored.
    """
    buffer: list[str] = []
    async for raw in lines:
        line = raw.rstrip("\r")
        if line == "":
            if buffer:
                yield "\n".join(buffer)
                buffer = []
            continue
        if line.startswith(":"):
            continue
        if line.startswith("data:"):
            buffer.append(line[5:].lstrip(" "))
        # Other SSE fields (event:, id:, retry:) are intentionally ignored.
    if buffer:
        yield "\n".join(buffer)


def parse_chunk(data: str) -> dict[str, Any] | None:
    """Parse one SSE data payload into a chunk dict.

    Returns ``None`` for the ``[DONE]`` marker. Raises ``json.JSONDecodeError``
    on malformed JSON so the caller can decide how to handle it.
    """
    if data.strip() == DONE_MARKER:
        return None
    chunk: dict[str, Any] = json.loads(data)
    return chunk
