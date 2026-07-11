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


class ChunkShapeError(ValueError):
    """Valid JSON whose structure is not a usable OpenAI-shaped stream chunk."""


def _validate_chunk_shape(chunk: Any) -> dict[str, Any]:
    """Cheap structural check over a decoded chunk.

    Downstream consumers index straight into ``chunk["choices"][0]["delta"]``
    and read ``content``/``reasoning_content``/``tool_calls`` off the delta, so
    a malformed-but-valid-JSON payload must be rejected HERE as an upstream
    protocol violation instead of surfacing later as an ``AttributeError`` in
    the orchestrator. Deliberately shallow — shape only, not a full schema.
    """
    if not isinstance(chunk, dict):
        raise ChunkShapeError("chunk root is not a JSON object")
    error = chunk.get("error")
    if error is not None and not isinstance(error, dict):
        raise ChunkShapeError("'error' is not an object")
    usage = chunk.get("usage")
    if usage is not None and not isinstance(usage, dict):
        raise ChunkShapeError("'usage' is not an object")
    choices = chunk.get("choices")
    if choices is None:
        return chunk  # error/usage-only chunks carry no choices
    if not isinstance(choices, list):
        raise ChunkShapeError("'choices' is not a list")
    for choice in choices:
        if not isinstance(choice, dict):
            raise ChunkShapeError("choices entry is not an object")
        if "delta" not in choice:
            continue
        delta = choice["delta"]
        if not isinstance(delta, dict):
            raise ChunkShapeError("'delta' is not an object")
        for key in ("content", "reasoning_content"):
            if delta.get(key) is not None and not isinstance(delta[key], str):
                raise ChunkShapeError(f"delta {key!r} is not a string")
        tool_calls = delta.get("tool_calls")
        if tool_calls is not None:
            if not isinstance(tool_calls, list):
                raise ChunkShapeError("delta 'tool_calls' is not a list")
            for call in tool_calls:
                if not isinstance(call, dict):
                    raise ChunkShapeError("tool_calls entry is not an object")
                function = call.get("function")
                if function is not None and not isinstance(function, dict):
                    raise ChunkShapeError("tool call 'function' is not an object")
    return chunk


def parse_chunk(data: str) -> dict[str, Any] | None:
    """Parse one SSE data payload into a validated chunk dict.

    Returns ``None`` for the ``[DONE]`` marker. Raises ``json.JSONDecodeError``
    on malformed JSON and :class:`ChunkShapeError` on valid JSON with an
    unusable structure, so the caller can map both to an upstream error.
    """
    if data.strip() == DONE_MARKER:
        return None
    return _validate_chunk_shape(json.loads(data))
