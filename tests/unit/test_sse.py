"""Direct tests for the SSE byte/line reader.

``iter_sse_data`` had no direct coverage, which is how the ``aiter_lines``
Unicode-splitting bug survived: every streaming test fed it pre-split, ASCII,
well-formed lines. These tests drive the reader from RAW BYTES the way the real
transport does, including the failure shapes a real upstream produces.
"""

from __future__ import annotations

import json
from collections.abc import AsyncIterator

import pytest

from daintree_assistant_server.providers.sse import (
    MAX_SSE_LINE_BYTES,
    SseProtocolError,
    iter_sse_data,
    iter_sse_lines,
    parse_chunk,
)


async def _bytes(*chunks: bytes) -> AsyncIterator[bytes]:
    for c in chunks:
        yield c


async def _collect(*chunks: bytes) -> list[str]:
    return [d async for d in iter_sse_data(iter_sse_lines(_bytes(*chunks)))]


def _chunk(content: str) -> str:
    # ensure_ascii=False mirrors a non-Python upstream: RFC 8259 requires escaping
    # only U+0000-U+001F, '"' and '\', so these characters go out RAW on the wire.
    return json.dumps({"choices": [{"delta": {"content": content}}]}, ensure_ascii=False)


@pytest.mark.asyncio
async def test_basic_event() -> None:
    assert await _collect(b'data: {"a": 1}\n\n') == ['{"a": 1}']


@pytest.mark.asyncio
async def test_crlf_and_lone_cr_are_line_terminators() -> None:
    assert await _collect(b'data: {"a": 1}\r\n\r\n') == ['{"a": 1}']
    assert await _collect(b'data: {"a": 1}\r\r') == ['{"a": 1}']


@pytest.mark.asyncio
async def test_comments_and_other_fields_ignored() -> None:
    wire = b': keep-alive\nevent: message\nid: 7\nretry: 100\ndata: {"a": 1}\n\n'
    assert await _collect(wire) == ['{"a": 1}']


@pytest.mark.asyncio
@pytest.mark.parametrize("cp", [0x85, 0x2028, 0x2029])
async def test_unicode_line_chars_do_not_split_an_event(cp: int) -> None:
    """U+0085/U+2028/U+2029 are legal unescaped in JSON and must NOT end a line.

    ``str.splitlines()`` (what ``httpx.aiter_lines`` uses) treats all three as
    line breaks, which truncated the JSON mid-string and surfaced as a
    non-retryable 502 partway through generation.
    """
    content = f"a{chr(cp)}b"
    payload = _chunk(content)
    assert chr(cp) in payload  # the producer really did leave it raw

    got = await _collect(f"data: {payload}\n\n".encode())

    assert got == [payload]
    parsed = parse_chunk(got[0])
    assert parsed is not None
    assert parsed["choices"][0]["delta"]["content"] == content


@pytest.mark.asyncio
async def test_event_split_across_network_chunks() -> None:
    assert await _collect(b'data: {"a"', b": 1}\n", b"\n") == ['{"a": 1}']


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "wire",
    [
        b'data: {"a": 1}\n\n',
        b'data: {"a": 1}\r\n\r\n',
        b"data: one\r\ndata: two\r\n\r\n",  # multi-line event, CRLF terminated
        b"data: one\ndata: two\n\n",
        b': ping\r\ndata: {"a": 1}\r\n\r\ndata: [DONE]\r\n\r\n',
    ],
)
async def test_chunk_boundary_is_irrelevant(wire: bytes) -> None:
    """Splitting the byte stream anywhere must not change the parsed events.

    This is the invariant a stream reader lives or dies by. It specifically pins
    the CRLF-straddling-a-boundary case: if the CR ends one network chunk and the
    LF starts the next, a naive normaliser sees TWO terminators and splits one
    multi-line event into two.
    """
    whole = await _collect(wire)
    for cut in range(1, len(wire)):
        assert await _collect(wire[:cut], wire[cut:]) == whole, f"differs when cut at {cut}"


@pytest.mark.asyncio
async def test_multibyte_char_split_across_chunk_boundary() -> None:
    payload = _chunk("héllo—wörld")
    raw = f"data: {payload}\n\n".encode()
    # Cut mid-character: find a byte index inside a multi-byte sequence.
    cut = next(i for i in range(1, len(raw)) if raw[i] & 0xC0 == 0x80)
    got = await _collect(raw[:cut], raw[cut:])
    assert got == [payload]
    parsed = parse_chunk(got[0])
    assert parsed is not None
    assert parsed["choices"][0]["delta"]["content"] == "héllo—wörld"


@pytest.mark.asyncio
async def test_multiline_data_is_joined_with_newline() -> None:
    assert await _collect(b"data: one\ndata: two\n\n") == ["one\ntwo"]


@pytest.mark.asyncio
async def test_trailing_event_without_blank_line_is_flushed() -> None:
    assert await _collect(b'data: {"a": 1}') == ['{"a": 1}']


@pytest.mark.asyncio
async def test_done_marker_parses_to_none() -> None:
    got = await _collect(b"data: [DONE]\n\n")
    assert got == ["[DONE]"]
    assert parse_chunk(got[0]) is None


@pytest.mark.asyncio
async def test_oversized_unterminated_line_is_rejected() -> None:
    # A newline-less upstream would otherwise grow the buffer without bound.
    blob = b"data: " + b"x" * (MAX_SSE_LINE_BYTES + 1)
    with pytest.raises(SseProtocolError):
        await _collect(blob)


@pytest.mark.asyncio
async def test_oversized_line_rejected_when_the_newline_arrives_with_it() -> None:
    """The cap must be checked BEFORE yielding, not only on the carry-over tail.

    Capping only the incomplete tail lets an oversized line through whenever the
    chunk that crosses the limit also carries its terminator.
    """
    head = b"data: " + b"x" * (MAX_SSE_LINE_BYTES - len(b"data: "))
    with pytest.raises(SseProtocolError):
        await _collect(head, b"x\n\n")


@pytest.mark.asyncio
async def test_invalid_utf8_fails_closed() -> None:
    """A JSON-over-HTTP stream must be valid UTF-8.

    errors="replace" would fabricate U+FFFD inside model content and hand the
    consumer a payload that silently differs from what upstream sent.
    """
    with pytest.raises(SseProtocolError):
        await _collect(b'data: {"choices":[{"delta":{"content":"a\xffb"}}]}\n\n')


@pytest.mark.asyncio
async def test_stream_ending_mid_character_fails_closed() -> None:
    with pytest.raises(SseProtocolError):
        await _collect(b'data: {"a": "\xe2\x82')  # truncated 3-byte sequence
