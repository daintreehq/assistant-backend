"""The sanitizers and bounds, tested directly.

`text_utils` is a security surface: every string a client supplies passes
through it before reaching a prompt, and its job is to bound the bytes and
neutralize anything that could read as structure. It had no test module of its
own — it was covered incidentally, mostly by the prompt-selector evidence tests,
because those exercised sanitization over a large corpus of adversarial message
shapes on the way to building a classifier prompt.

Those tests went with the selector, and took 22 percentage points of coverage on
this module with them (90% -> 68%, caught by `tools/coverage_floors.py`, which
exists precisely because a global percentage stays green while exactly this kind
of code goes dark). Incidental coverage of a security surface was the wrong
arrangement anyway; this is the module that should have had it.
"""

from __future__ import annotations

import pytest

from daintree_assistant_server.contracts.openai import (
    ImageContentPart,
    ImageUrl,
    TextContentPart,
)
from daintree_assistant_server.text_utils import (
    estimate_tokens,
    head_tail,
    message_text,
)

# ---------------------------------------------------------------------------
# message_text — flattening multimodal content for a text-only consumer.
# ---------------------------------------------------------------------------


def test_plain_string_content_passes_through() -> None:
    assert message_text("just text") == "just text"


def test_absent_content_is_empty_not_none() -> None:
    """Callers concatenate the result; `None` would raise at the join."""
    assert message_text(None) == ""


def test_an_image_becomes_a_TYPED_MARKER_never_its_bytes() -> None:
    """The whole reason this function exists.

    A data URL carries the entire image inline — megabytes of base64 that would
    otherwise be spliced into a prompt, a log line, or a selector's evidence.
    The marker names the media type so a reader can see WHAT was dropped without
    the payload coming with it.
    """
    part = ImageContentPart(
        type="image_url",
        image_url=ImageUrl(url="data:image/png;base64,iVBORw0KGgoAAAANSUhEUg=="),
    )
    out = message_text([part])
    assert out == "[image: image/png]"
    assert "iVBORw0KGgo" not in out, "the base64 payload reached the flattened text"


def test_a_remote_image_url_is_dropped_entirely() -> None:
    """A URL is untrusted text pointed at a third party.

    Keeping it would put an attacker-chosen address into the prompt, where a
    model might quote it back to the user as though it were ours.
    """
    part = ImageContentPart(
        type="image_url", image_url=ImageUrl(url="https://evil.example/track.png")
    )
    out = message_text([part])
    assert out == "[image]"
    assert "evil.example" not in out


def test_a_malformed_data_url_still_produces_a_marker() -> None:
    """`data:` with no media type must not yield an empty label.

    `url[5:].split(";", 1)[0]` is `""` here, and an f-string would happily render
    `[image: ]` — a marker that says nothing, in a position where the reader is
    being told what was removed.
    """
    part = ImageContentPart(type="image_url", image_url=ImageUrl(url="data:"))
    assert message_text([part]) == "[image: image]"


def test_mixed_parts_keep_their_order_and_are_newline_joined() -> None:
    parts = [
        TextContentPart(type="text", text="before"),
        ImageContentPart(
            type="image_url", image_url=ImageUrl(url="data:image/jpeg;base64,AAAA")
        ),
        TextContentPart(type="text", text="after"),
    ]
    assert message_text(parts) == "before\n[image: image/jpeg]\nafter"


def test_an_empty_part_list_is_empty_text() -> None:
    assert message_text([]) == ""


# ---------------------------------------------------------------------------
# estimate_tokens — a deterministic proxy, deliberately not a tokenizer.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("text", "expected"),
    [("", 0), ("a", 1), ("abc", 1), ("abcd", 1), ("abcde", 2), ("a" * 400, 100)],
)
def test_the_token_estimate_rounds_UP(text: str, expected: int) -> None:
    """Ceiling division, not floor.

    The estimate gates admission into bounded windows, so it has to err toward
    "this is bigger than you think". Rounding down lets a payload one character
    over a limit read as exactly at it.
    """
    assert estimate_tokens(text) == expected


def test_the_estimate_never_undercounts_a_short_string() -> None:
    """The off-by-one that a floor divide would produce, stated as a property."""
    for n in range(1, 64):
        assert estimate_tokens("x" * n) >= 1
        assert estimate_tokens("x" * n) * 4 >= n


# ---------------------------------------------------------------------------
# head_tail — bounded excerpts of oversized text.
# ---------------------------------------------------------------------------


def test_text_within_the_limit_is_returned_BYTE_IDENTICAL() -> None:
    """No marker, no reflow, no copy that differs.

    A caller downstream may compare this against the original, and an excerpt
    that "helpfully" normalized something would break a prefix cache for content
    that never needed truncating.
    """
    text = "short enough"
    assert head_tail(text, 100) is text or head_tail(text, 100) == text
    assert "truncated" not in head_tail(text, 100)


def test_an_oversized_excerpt_keeps_BOTH_ends() -> None:
    """The head alone loses the error at the bottom of a log; the tail alone
    loses the command that produced it. Both ends is the whole point."""
    text = "HEAD" + ("m" * 500) + "TAIL"
    out = head_tail(text, 100)
    assert out.startswith("HEAD")
    assert out.endswith("TAIL")
    assert "…[truncated]…" in out
    assert len(out) < len(text)


def test_the_truncation_marker_is_visible_to_a_reader() -> None:
    """Silent truncation is the failure mode here.

    A model handed a quietly-clipped excerpt reasons about it as if it were
    whole — concluding a log contains no error because the error was cut out.
    """
    out = head_tail("x" * 1000, 60)
    assert "truncated" in out


@pytest.mark.parametrize("limit", [0, 1, 2, 10, 20, 21])
def test_a_tiny_limit_does_not_crash_or_produce_a_negative_slice(limit: int) -> None:
    """`(limit - 20) // 2` goes negative below 20, and a negative slice silently
    reverses direction — returning text from the wrong end rather than failing.
    The `max(1, ...)` floor is what stops it, and nothing exercised it."""
    out = head_tail("abcdefghij" * 10, limit)
    assert "…[truncated]…" in out
    assert out.startswith("a"), "the head slice ran backwards"
    assert out.endswith("j"), "the tail slice ran backwards"


def test_the_limit_boundary_is_inclusive() -> None:
    """Exactly at the limit is not oversized."""
    text = "y" * 50
    assert head_tail(text, 50) == text
    assert "truncated" in head_tail(text, 49)
