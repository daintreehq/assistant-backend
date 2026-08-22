"""The model's own declaration of whether this reply ends the turn.

The backend cannot know a turn is over until it is over, and by then there is no
generation time left to spend on anything. The only party that knows whether more
tool calls are coming is the model, so it says — on the first line of the reply,
before a single visible character.

**Whether it is ASKED to say so is conditional.** The rule ships only when a
compaction producer exists (`PromptAssembler.response_declaration`, wired from
`compactor.available`), which today is never. This module stays wired regardless:
scanning is a prefix probe, and a model that emits the marker unprompted must
still not show it to a user.

That position is the whole point. A marker at the END tells us nothing the stream
terminating did not already say. A marker at the FRONT buys the entire remaining
generation for work that can run in parallel with it, which is most of the latency
budget a turn has.

It is a CONTROL TOKEN, not content, and the guarantee is precise: a marker
written where the prompt asks for it — first, under ``MAX_LEADING_WHITESPACE``
characters of whitespace — never reaches the client. Past that bound the reply is
read as having no declaration and forwarded BYTE FOR BYTE, which is the
conservative failure: no compaction is started, and nothing the model wrote is
lost. Tolerating unbounded whitespace instead would mean either holding an
unbounded prefix, or discarding leading spaces that a reply opening with an
indented code block genuinely meant.

Two failures matter and they are not the same size. A leaked
``[[DAINTREE:FINAL]]`` in the terminal is obvious and someone files it. A marker
that is stripped but leaves a stray space or newline behind shifts the first
visible character of every reply, renders as a subtly different paragraph, and
nobody files anything — so the stripping here removes the marker AND the
whitespace the model put around it, and touches nothing after that.

Two entry points that MUST agree, for the same reason ``bare_intent`` keeps its
probe and its predicate in step: ``DeclarationScanner`` for the streaming path,
where the marker can straddle any chunk boundary, and ``strip_declaration`` for a
buffered reply. The batch function is implemented ON the scanner rather than
beside it, so there is only one parser and the two can never drift.

Deliberately PREFIX-ONLY. A marker repeated in the middle of a reply is left
alone: catching one would need a rolling tail buffer on every delta (delaying all
visible output to guard against a cosmetic bug), and it would mangle the one reply
that legitimately quotes the marker — a user asking what it means.
"""

from __future__ import annotations

from enum import Enum, StrEnum

# The two declarations, spelled as one ASCII line each. Bracketed and namespaced
# rather than XML- or comment-shaped: `[[DAINTREE:` cannot be produced by ordinary
# prose or Markdown, survives any tokenizer without depending on a model-specific
# special token, and — unlike an HTML comment — a leak is VISIBLE in the terminal
# rather than silently swallowed by a Markdown renderer, which is the side to err
# on for a bug we want reported.
FINAL_MARKER = "[[DAINTREE:FINAL]]"
TOOLS_MARKER = "[[DAINTREE:TOOLS]]"

# How much leading whitespace may precede the marker before the reply is read as
# having no declaration at all. Strictly zero would be more precise and is what
# the prompt asks for — but a single stray newline from the model would then
# silently disable the feature for the whole turn, and whitespace ahead of the
# first visible character is never meaningful in a Markdown reply. Bounded so a
# reply that simply opens with blank lines resolves promptly instead of holding.
MAX_LEADING_WHITESPACE = 8


class Declaration(StrEnum):
    """What the reply said about itself, once the opening is settled."""

    #: No tool calls follow; this reply completes the turn.
    FINAL = "final"
    #: At least one tool call follows in this same reply.
    TOOLS = "tools"
    #: No usable declaration. Never treated as FINAL — a missed compaction costs
    #: one turn's worth of savings, a wrong one freezes history mid-task.
    MISSING = "missing"


class _Stage(Enum):
    #: Still deciding whether the reply opened with a marker.
    SCANNING = 1
    #: Marker consumed; swallowing the whitespace it was written on.
    EAT_SEPARATOR = 2
    #: Settled. Every further character passes through untouched.
    OPEN = 3


class DeclarationScanner:
    """Removes the leading declaration from a reply arriving in fragments.

    Feed it every content fragment in order and forward what it returns. While
    the opening is ambiguous it returns ``""`` and holds — bounded by the marker's
    own length, so at most ~30 characters of a real reply are ever delayed, an
    order of magnitude inside the bare-intent gate that already sits behind it.

    ``finish`` must be called once the reply has ended: a stream that stops
    mid-marker ("[[DAINT") left real characters held, and they are still owed to
    the client.
    """

    def __init__(self) -> None:
        self._stage = _Stage.SCANNING
        self._buffer = ""
        self._declaration: Declaration | None = None

    @property
    def declaration(self) -> Declaration | None:
        """The settled declaration, or ``None`` while the opening is ambiguous.

        Monotonic: once it is not ``None`` it never changes again, which is what
        makes it safe for a caller to act on the instant it appears rather than
        waiting for the reply to end.
        """
        return self._declaration

    def feed(self, text: str) -> str:
        """The visible text of one fragment — the marker, if any, removed."""
        if self._stage is _Stage.OPEN:
            return text
        if not text:
            return ""
        self._buffer += text
        return self._drain(final=False)

    def finish(self) -> str:
        """Release anything still held, now that no more text is coming."""
        if self._stage is _Stage.OPEN:
            return ""
        return self._drain(final=True)

    # ---- internals ------------------------------------------------------

    def _drain(self, *, final: bool) -> str:
        if self._stage is _Stage.SCANNING:
            if not self._resolve(final=final):
                return ""
            if self._declaration is Declaration.MISSING:
                # Not a declaration, so not ours: hand back every byte received,
                # leading whitespace included.
                out, self._buffer = self._buffer, ""
                self._stage = _Stage.OPEN
                return out
            self._stage = _Stage.EAT_SEPARATOR
        # The marker is gone; what remains is the whitespace it was written on.
        # Trailing spaces go unconditionally (they cannot be meaningful at the
        # start of a reply) and then exactly ONE line terminator, so a reply that
        # deliberately opens with a blank line still does.
        rest = self._buffer.lstrip(" \t")
        if rest.startswith("\r\n"):
            rest = rest[2:]
        elif not final and rest in ("", "\r"):
            # A lone "\r" may yet be half of "\r\n", and an empty buffer says
            # nothing at all. Both resolve on the next fragment.
            self._buffer = rest
            return ""
        elif rest[:1] in ("\n", "\r"):
            rest = rest[1:]
        self._stage = _Stage.OPEN
        self._buffer = ""
        return rest

    def _resolve(self, *, final: bool) -> bool:
        """Settle whether the reply opened with a marker.

        ``False`` means the answer still depends on characters that have not
        arrived. On ``True`` the declaration is set, and for a real marker the
        marker and the whitespace before it are already off the buffer.
        """
        buffer = self._buffer
        lead = len(buffer) - len(buffer.lstrip())
        if lead > MAX_LEADING_WHITESPACE:
            self._declaration = Declaration.MISSING
            return True
        body = buffer[lead:]
        if not body:
            # Whitespace only so far. Under the bound, so a marker may still
            # follow — unless the reply has ended, in which case none did.
            if final:
                self._declaration = Declaration.MISSING
                return True
            return False
        for marker, declaration in (
            (FINAL_MARKER, Declaration.FINAL),
            (TOOLS_MARKER, Declaration.TOOLS),
        ):
            if body.startswith(marker):
                self._declaration = declaration
                self._buffer = body[len(marker) :]
                return True
        if not final and (
            FINAL_MARKER.startswith(body) or TOOLS_MARKER.startswith(body)
        ):
            # Still a plausible prefix of one of them; hold for more.
            return False
        self._declaration = Declaration.MISSING
        return True


def strip_declaration(text: str) -> tuple[Declaration, str]:
    """Split a COMPLETE reply into its declaration and its visible text.

    The non-streaming path's half of the contract, and implemented on the same
    scanner the stream uses so the two can never disagree about where the marker
    ends and the reply begins.
    """
    scanner = DeclarationScanner()
    visible = scanner.feed(text) + scanner.finish()
    return scanner.declaration or Declaration.MISSING, visible
