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


def cap_bytes(text: str, max_bytes: int) -> tuple[str, bool]:
    """Truncate ``text`` so its UTF-8 encoding fits ``max_bytes``.

    Returns ``(text, truncated)``. Truncation happens on a character boundary.
    """
    encoded = text.encode("utf-8")
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
    neutralized = cleaned.replace(f"</{tag}>", f"<\\/{tag}>").replace(f"<{tag}>", f"<\\{tag}>")
    return f"<{tag}>\n{neutralized}\n</{tag}>"


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
