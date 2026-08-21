"""The declaration control line: recognised, stripped, and never leaked.

Two failures this file exists to prevent, and they are not the same size. A
leaked marker is obvious and gets reported. A marker that is stripped but shifts
the first visible character changes how every reply renders and nobody notices —
so most of these tests are about the BYTES either side of the marker, not about
the marker itself.
"""

from __future__ import annotations

import pytest

from daintree_assistant_server.services.response_declaration import (
    FINAL_MARKER,
    MAX_LEADING_WHITESPACE,
    TOOLS_MARKER,
    Declaration,
    DeclarationScanner,
    strip_declaration,
)


def _stream(text: str, *, size: int) -> tuple[Declaration | None, str]:
    """Push ``text`` through the scanner in fixed-size fragments."""
    scanner = DeclarationScanner()
    out = "".join(scanner.feed(text[i : i + size]) for i in range(0, len(text), size))
    out += scanner.finish()
    return scanner.declaration, out


# ---- recognition -------------------------------------------------------


@pytest.mark.parametrize(
    ("marker", "expected"),
    [(FINAL_MARKER, Declaration.FINAL), (TOOLS_MARKER, Declaration.TOOLS)],
)
def test_marker_is_recognised_and_removed(marker: str, expected: Declaration) -> None:
    declaration, visible = strip_declaration(f"{marker}\nThe answer is 42.")
    assert declaration is expected
    assert visible == "The answer is 42."


def test_marker_alone_leaves_no_visible_text() -> None:
    """A continuation round is often nothing but the marker and tool calls."""
    declaration, visible = strip_declaration(TOOLS_MARKER)
    assert declaration is Declaration.TOOLS
    assert visible == ""


def test_ordinary_reply_is_returned_byte_identical() -> None:
    text = "Here is the plan.\n\n1. Read the file\n2. Patch it\n"
    declaration, visible = strip_declaration(text)
    assert declaration is Declaration.MISSING
    assert visible == text


def test_an_empty_reply_is_missing_not_final() -> None:
    """MISSING must never read as FINAL: a wrong final is the expensive one."""
    assert strip_declaration("") == (Declaration.MISSING, "")


def test_a_marker_shaped_prefix_that_never_completes_is_released() -> None:
    """The held characters are real content and are still owed to the client."""
    assert strip_declaration("[[DAINT") == (Declaration.MISSING, "[[DAINT")


def test_a_near_miss_marker_is_not_a_declaration() -> None:
    declaration, visible = strip_declaration("[[DAINTREE:DONE]]\nhi")
    assert declaration is Declaration.MISSING
    assert visible == "[[DAINTREE:DONE]]\nhi"


def test_a_marker_after_the_first_line_is_left_alone() -> None:
    """Prefix-only by design — a reply may legitimately quote the marker."""
    text = f"The control line is {FINAL_MARKER} and it is stripped."
    assert strip_declaration(text) == (Declaration.MISSING, text)


# ---- the bytes around the marker ---------------------------------------


def test_exactly_one_line_terminator_is_eaten() -> None:
    """A deliberate blank line after the marker survives as a blank line."""
    _, visible = strip_declaration(f"{FINAL_MARKER}\n\nParagraph.")
    assert visible == "\nParagraph."


def test_crlf_is_eaten_whole() -> None:
    _, visible = strip_declaration(f"{FINAL_MARKER}\r\nHello")
    assert visible == "Hello"


def test_spaces_between_marker_and_reply_do_not_leak() -> None:
    _, visible = strip_declaration(f"{FINAL_MARKER}  \nHello")
    assert visible == "Hello"


def test_a_reply_on_the_marker_s_own_line_is_still_unshifted() -> None:
    _, visible = strip_declaration(f"{FINAL_MARKER} Hello")
    assert visible == "Hello"


def test_indentation_after_the_separator_is_preserved() -> None:
    """The first visible character is the model's, not the stripper's."""
    _, visible = strip_declaration(f"{FINAL_MARKER}\n    indented code")
    assert visible == "    indented code"


def test_bounded_leading_whitespace_still_declares() -> None:
    """A stray newline ahead of the marker must not disable the feature."""
    declaration, visible = strip_declaration(f"\n{FINAL_MARKER}\nHello")
    assert declaration is Declaration.FINAL
    assert visible == "Hello"


def test_unbounded_leading_whitespace_is_not_a_declaration() -> None:
    lead = "\n" * (MAX_LEADING_WHITESPACE + 1)
    text = f"{lead}{FINAL_MARKER}\nHello"
    assert strip_declaration(text) == (Declaration.MISSING, text)


def test_whitespace_only_reply_is_returned_intact() -> None:
    assert strip_declaration("  ") == (Declaration.MISSING, "  ")


# ---- chunk boundaries --------------------------------------------------


@pytest.mark.parametrize("split", range(1, len(FINAL_MARKER) + 6))
def test_a_marker_split_at_any_boundary_is_still_stripped(split: int) -> None:
    text = f"{FINAL_MARKER}\nHello world"
    scanner = DeclarationScanner()
    visible = scanner.feed(text[:split]) + scanner.feed(text[split:]) + scanner.finish()
    assert scanner.declaration is Declaration.FINAL
    assert visible == "Hello world"


@pytest.mark.parametrize("size", [1, 2, 3, 5, 7, 11, 64])
def test_streaming_agrees_with_the_buffered_stripper(size: int) -> None:
    """The two entry points must never disagree about where the reply begins."""
    for text in (
        f"{FINAL_MARKER}\nHello",
        f"{TOOLS_MARKER}\r\n",
        f"  {FINAL_MARKER}\n\nBody",
        "no marker at all",
        "[[DAINTREE:FIN",
        "",
    ):
        assert _stream(text, size=size) == strip_declaration(text)


def test_a_lone_carriage_return_waits_for_its_newline() -> None:
    scanner = DeclarationScanner()
    assert scanner.feed(f"{FINAL_MARKER}\r") == ""
    assert scanner.feed("\nHello") == "Hello"


# ---- monotonicity ------------------------------------------------------


def test_the_declaration_resolves_before_the_reply_ends() -> None:
    """The whole point: act on FINAL while there is still generation to overlap."""
    scanner = DeclarationScanner()
    scanner.feed(FINAL_MARKER)
    assert scanner.declaration is Declaration.FINAL


def test_later_text_cannot_change_a_settled_declaration() -> None:
    scanner = DeclarationScanner()
    scanner.feed(f"{TOOLS_MARKER}\n")
    scanner.feed(f"...and also {FINAL_MARKER}")
    scanner.finish()
    assert scanner.declaration is Declaration.TOOLS


def test_content_after_the_gate_opens_passes_through_untouched() -> None:
    scanner = DeclarationScanner()
    scanner.feed(f"{FINAL_MARKER}\n")
    assert scanner.feed("  spaced  ") == "  spaced  "
