"""Server-Sent Events parsing helpers.

Minimal SSE reader for OpenAI-style streams: events are separated by blank
lines, payload arrives on ``data:`` lines, and the stream terminates with a
``data: [DONE]`` marker.
"""

from __future__ import annotations

import codecs
import json
from collections.abc import AsyncIterator
from typing import Any

DONE_MARKER = "[DONE]"

# Bound on a single un-terminated SSE line. A well-behaved upstream never comes
# close (chunks are small); an upstream that never sends a newline would otherwise
# grow this buffer without limit, so cap it and fail loudly instead.
MAX_SSE_LINE_BYTES = 8 * 1024 * 1024


class SseProtocolError(ValueError):
    """The upstream byte stream violated the SSE framing contract."""


async def iter_sse_lines(chunks: AsyncIterator[bytes]) -> AsyncIterator[str]:
    """Split a raw byte stream into SSE lines.

    Deliberately NOT ``httpx.Response.aiter_lines``: that splits on
    ``str.splitlines()``, which also breaks on U+0085, U+2028 and U+2029. All
    three are legal UNESCAPED inside a JSON string (RFC 8259 mandates escaping
    only U+0000-U+001F, ``"`` and ``\\``), so a model emitting one mid-content
    would have its ``data:`` line cut in two — the tail is dropped as a non-data
    field and the truncated JSON becomes a non-retryable 502 mid-generation.

    SSE (per the EventSource spec) recognises exactly CRLF, LF and CR, so split
    on bytes and decode incrementally — which also keeps a multi-byte character
    straddling a network-chunk boundary intact.
    """
    # STRICT decoding: this is a JSON-over-HTTP stream, so it must be valid UTF-8.
    # errors="replace" would fabricate U+FFFD inside model content and hand downstream
    # a payload that silently differs from what the upstream sent — fail closed instead.
    decoder = codecs.getincrementaldecoder("utf-8")()
    # ``line`` accumulates only the CURRENT INCOMPLETE line. A newline-rich stream keeps
    # it short; a pathologically long single line is bounded by MAX_SSE_LINE_BYTES.
    line = ""
    # A CR at the very end of a chunk is ambiguous: it terminates a line either way,
    # but we cannot yet tell a lone CR from the first half of a CRLF. Hold it back so
    # a CRLF straddling a chunk boundary is not mistaken for two terminators (which
    # would split one multi-line event into two).
    pending_cr = False

    def _checked(candidate: str) -> str:
        # Check BEFORE yielding: a completed line is just as unbounded as the tail, so
        # capping only the carry-over would let an oversized line through whenever the
        # chunk that crossed the limit also carried the newline.
        if len(candidate.encode("utf-8")) > MAX_SSE_LINE_BYTES:
            raise SseProtocolError("upstream SSE line exceeded the size limit")
        return candidate

    async for chunk in chunks:
        try:
            text = decoder.decode(chunk)
        except UnicodeDecodeError as exc:
            raise SseProtocolError("upstream SSE stream is not valid UTF-8") from exc
        if not text:
            continue
        if pending_cr:
            pending_cr = False
            if text[0] == "\n":  # the held CR was really a CRLF
                text = text[1:]
            yield _checked(line)
            line = ""
            if not text:
                continue
        if text.endswith("\r"):
            text, pending_cr = text[:-1], True
        # parts[0] completes the line in progress, parts[1:-1] are whole lines, and
        # parts[-1] is the new incomplete tail. A single part means no terminator yet.
        parts = text.replace("\r\n", "\n").replace("\r", "\n").split("\n")
        line += parts[0]
        if len(parts) > 1:
            yield _checked(line)
            for complete in parts[1:-1]:
                yield _checked(complete)
            line = parts[-1]
        _checked(line)

    try:
        line += decoder.decode(b"", final=True)
    except UnicodeDecodeError as exc:
        raise SseProtocolError("upstream SSE stream ended mid-character") from exc
    if pending_cr or line:
        yield _checked(line)


async def iter_sse_data(lines: AsyncIterator[str]) -> AsyncIterator[str]:
    """Yield the ``data`` payload of each SSE event from a line iterator.

    Lines are expected without trailing newlines (as produced by
    :func:`iter_sse_lines`). Comment lines (starting with ``:``) and non-data
    fields are ignored.
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
        _validate_tool_calls(delta.get("tool_calls"), where="delta")
    return chunk


def _validate_tool_calls(tool_calls: Any, *, where: str) -> None:
    """Shared tool-call shape check for both the stream and non-stream paths."""
    if tool_calls is None:
        return
    if not isinstance(tool_calls, list):
        raise ChunkShapeError(f"{where} 'tool_calls' is not a list")
    for call in tool_calls:
        if not isinstance(call, dict):
            raise ChunkShapeError("tool_calls entry is not an object")
        function = call.get("function")
        if function is not None and not isinstance(function, dict):
            raise ChunkShapeError("tool call 'function' is not an object")


def validate_message_shape(choices: Any, usage: Any) -> None:
    """Structural check for a NON-streaming completion body.

    The mirror of :func:`_validate_chunk_shape` for the ``message`` shape, kept
    beside it so the two paths cannot drift: consumers index straight into
    ``choices[0]["message"]`` and read ``content``/``tool_calls`` off it, so the
    same malformed upstream must fail the same way on both paths (a 502), not as
    an ``AttributeError`` 500 on one of them.
    """
    if usage is not None and not isinstance(usage, dict):
        raise ChunkShapeError("'usage' is not an object")
    if not isinstance(choices, list):
        raise ChunkShapeError("'choices' is not a list")
    for choice in choices:
        if not isinstance(choice, dict):
            raise ChunkShapeError("choices entry is not an object")
        message = choice.get("message")
        if message is None:
            continue
        if not isinstance(message, dict):
            raise ChunkShapeError("'message' is not an object")
        for key in ("content", "reasoning_content"):
            if message.get(key) is not None and not isinstance(message[key], str):
                raise ChunkShapeError(f"message {key!r} is not a string")
        _validate_tool_calls(message.get("tool_calls"), where="message")


def parse_chunk(data: str) -> dict[str, Any] | None:
    """Parse one SSE data payload into a validated chunk dict.

    Returns ``None`` for the ``[DONE]`` marker. Raises ``json.JSONDecodeError``
    on malformed JSON and :class:`ChunkShapeError` on valid JSON with an
    unusable structure, so the caller can map both to an upstream error.
    """
    if data.strip() == DONE_MARKER:
        return None
    return _validate_chunk_shape(json.loads(data))
