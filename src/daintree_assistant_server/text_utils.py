"""Shared text helpers: sanitization, byte capping, and content extraction.

Used by both the selector (building evidence) and the prompt assembler
(framing untrusted local context). All user-provided text is treated as data.
"""

from __future__ import annotations

import re

from .contracts.openai import ContentPart, ImageContentPart, TextContentPart

# Characters permitted in addition to printable text.
_ALLOWED_CONTROL = {"\n", "\t"}

# Any run of whitespace (incl. newlines/tabs) collapses to one space for
# single-line fields, so an untrusted value cannot inject extra lines.
_WHITESPACE_RUN = re.compile(r"\s+")


def strip_control_chars(text: str) -> str:
    """Remove control characters except newline and tab; normalize CRLF."""
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    return "".join(
        ch for ch in text if ch in _ALLOWED_CONTROL or ch == " " or ch.isprintable()
    )


def has_lone_surrogate(text: str) -> bool:
    """True when ``text`` cannot be encoded as UTF-8 (an unpaired surrogate).

    ``json.loads`` accepts ``"\\ud800"`` and pydantic passes it through, but httpx
    serializes the outbound request with ``ensure_ascii=False`` and a STRICT utf-8
    encode — so such a value survives every size check and then raises deep in the
    provider call, after the selector has already run and been billed. Requests
    carrying one must be rejected up front (fail closed, like every other malformed
    input) rather than surfacing as a 500 mid-turn.
    """
    try:
        text.encode("utf-8")
    except UnicodeEncodeError:
        return True
    return False


def utf8_len(text: str) -> int:
    """UTF-8 byte length that never raises on a lone surrogate.

    ``json.loads`` accepts ``"\\ud800"`` and pydantic passes it straight through, so
    a hostile-but-parseable request can carry an unpaired surrogate all the way to a
    byte-length check. Plain ``.encode("utf-8")`` raises ``UnicodeEncodeError`` there,
    turning what should be a 400 into a 500 — every size check must use this instead.
    """
    return len(text.encode("utf-8", errors="replace"))


def cap_bytes(text: str, max_bytes: int) -> tuple[str, bool]:
    """Truncate ``text`` so its UTF-8 encoding fits ``max_bytes``.

    Returns ``(text, truncated)``. Truncation happens on a character boundary.
    Surrogate-safe: see :func:`utf8_len`.
    """
    encoded = text.encode("utf-8", errors="replace")
    if len(encoded) <= max_bytes:
        return text, False
    # Trim to a safe character boundary.
    truncated = encoded[:max_bytes].decode("utf-8", errors="ignore")
    return truncated, True


def sanitize_field(text: str | None, max_bytes: int) -> tuple[str, bool]:
    """Strip control chars and cap a single untrusted text field.

    Newlines and tabs are preserved; use :func:`sanitize_line` for values that
    must render on a single line, and :func:`frame_data_block` for multiline
    values that must not be able to break out of their section.
    """
    if not text:
        return "", False
    cleaned = strip_control_chars(text)
    return cap_bytes(cleaned, max_bytes)


def sanitize_line(text: str | None, max_bytes: int) -> str:
    """Collapse an untrusted value to a single safe line, then cap its bytes.

    All whitespace (including newlines and tabs) becomes a single space so the
    value cannot inject extra lines or fake headings into a system message.
    Returns ``""`` for empty input.
    """
    if not text:
        return ""
    cleaned = _WHITESPACE_RUN.sub(" ", strip_control_chars(text)).strip()
    capped, _ = cap_bytes(cleaned, max_bytes)
    return capped


def neutralize_delimiter(text: str, tag: str) -> str:
    """Defuse any occurrence of ``<tag>`` / ``</tag>`` inside ``text``.

    Matched the way a lenient reader would — case-insensitively, tolerating
    internal whitespace, and tolerating **backslashes**, because the audience is
    a language model rather than an XML parser. ``</TAG>``, ``</tag >``,
    ``< /tag>`` and ``<\\/tag>`` are all spellings a model may read as closing the
    block.

    The backslash tolerance is not hypothetical: an earlier version defused
    ``</tag>`` by rewriting it to ``<\\/tag>`` — which is itself a widely
    recognised escaped-closing-tag spelling, and which that same regex then let
    through untouched when supplied directly. Escaping *into* an alias of the
    thing being escaped is worse than not escaping at all, because it looks
    handled.

    So the delimiter is not escaped, it is **replaced with a different bracket**:
    ``</goal>`` becomes ``[/goal]``. Square brackets are not the delimiter under
    any spelling, so the result cannot be re-read as a tag, and the substitution
    is idempotent — running it twice changes nothing.

    Split out from :func:`frame_data_block` so every framing helper defuses
    identically: a second convention that forgot this step would be a breakout
    hole that looks, in the rendered prompt, exactly like a safe one.
    """
    return re.sub(
        rf"<[\s\\/]*{re.escape(tag)}\s*/?\s*>",
        lambda m: "[" + m.group(0)[1:-1] + "]",
        text,
        flags=re.IGNORECASE,
    )


def frame_data_block(text: str | None, max_bytes: int, *, tag: str) -> str:
    """Frame multiline untrusted text as an inert, delimited data block.

    Newlines are preserved, but any attempt to close the block early is
    neutralized by escaping the delimiter, so embedded ``</tag>`` cannot end the
    framing and inject following text as a new instruction. Returns ``""`` for
    empty/blank input.
    """
    cleaned, _ = sanitize_field(text, max_bytes)
    if not cleaned.strip():
        return ""
    return f"<{tag}>\n{neutralize_delimiter(cleaned, tag)}\n</{tag}>"


def frame_value(text: str | None, *, tag: str) -> str:
    """Frame one ALREADY-SANITIZED single-line value as inert data.

    The counterpart to :func:`frame_data_block` for the many short fields —
    a turn goal, a memory, a workflow digest line — that are collapsed to one
    line by :func:`sanitize_line` before they ever reach a template.

    Collapsing to a single line already stops a value forging a heading or an
    extra line. What it does *not* stop is the value simply reading as prose the
    model might obey, which is precisely what the live injection benchmark
    measured: the two carriers rendered bare (``turn_goal``, ``workflow_digest``)
    leaked, while the two rendered inside a delimited block did not. This puts
    the leaking carriers behind the same visible boundary.

    Returns ``""`` for empty input so a template can omit the field entirely.
    """
    if not text:
        return ""
    return f"<{tag}>{neutralize_delimiter(text, tag)}</{tag}>"


def message_text(content: str | list[ContentPart] | None) -> str:
    """Flatten message content to text, replacing images with typed markers."""
    if content is None:
        return ""
    if isinstance(content, str):
        return content
    parts: list[str] = []
    for part in content:
        if isinstance(part, TextContentPart):
            parts.append(part.text)
        elif isinstance(part, ImageContentPart):
            url = part.image_url.url or ""
            if url.startswith("data:"):
                media = url[5:].split(";", 1)[0] or "image"
                parts.append(f"[image: {media}]")
            else:
                parts.append("[image]")
    return "\n".join(parts)


def estimate_tokens(text: str) -> int:
    """A cheap, deterministic token estimate (~4 chars per token)."""
    return (len(text) + 3) // 4


def head_tail(text: str, limit: int) -> str:
    """Return a bounded head+tail excerpt of ``text``."""
    if len(text) <= limit:
        return text
    half = max(1, (limit - 20) // 2)
    return f"{text[:half]}\n…[truncated]…\n{text[-half:]}"
