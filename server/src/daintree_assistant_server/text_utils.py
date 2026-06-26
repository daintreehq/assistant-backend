"""Shared text helpers: sanitization, byte capping, and content extraction.

Used by both the selector (building evidence) and the prompt assembler
(framing untrusted local context). All user-provided text is treated as data.
"""

from __future__ import annotations

from .contracts.openai import ContentPart, ImageContentPart, TextContentPart

# Characters permitted in addition to printable text.
_ALLOWED_CONTROL = {"\n", "\t"}


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
    """Strip control chars and cap a single untrusted text field."""
    if not text:
        return "", False
    cleaned = strip_control_chars(text)
    return cap_bytes(cleaned, max_bytes)


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
