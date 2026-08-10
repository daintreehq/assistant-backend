"""Detect a round that announced a tool call and then emitted none.

The main model sometimes closes a round with nothing but a sentence saying it is
about to act — "All five votes are in. Let me read them, one read per terminal,
batched." — and no tool call. ``finish_reason`` is ``stop``, ``tool_calls`` is
empty, and the turn is over having performed nothing. The CLI renders the
sentence and stops, which reads to the user as a hang.

Worse, it self-reinforces: the dead reply stays in the transcript as an in-context
example, and pattern completion beats the system prompt, so the next few rounds
repeat it. The prompt (``main/base/20-tool-discipline.md``) asks the model not to
do this; this module is the deterministic backstop for when asking does not work.

The predicate is intentionally BIASED TOWARD FALSE NEGATIVES. A missed dead round
costs one wasted round the user can re-prompt through — the status quo. A false
positive discards a correct, already-generated reply and bills a second
generation for it, so every rule here narrows rather than widens:

* only an exact ``stop`` finish (``length`` is truncation, a different failure);
* only when a tool was actually callable this round;
* only short replies (a long one is doing real work, whatever it says);
* only when the trailing clause is an IMMEDIATE first-person action announcement;
* never a question, and never a deferred or conditional plan.

Matching the trailing clause rather than the opening is load-bearing. All three
production failures placed the announcement AFTER a preamble ("You're right, my
previous reply ended with a tool call intent... Let me read all five votes now"),
so a start-anchored pattern would have caught none of them.

``could_still_be_bare_intent`` is the streaming half: a monotonic probe over the
partial reply that lets the stream release its held content the moment the reply
can no longer be this shape. Monotonic means once it goes false it stays false,
which is what makes it safe to check incrementally and stop holding forever.
"""

from __future__ import annotations

import re

# The longest reply still considered a candidate, in Unicode code points. The
# three observed production failures were 72, 196, and 139 characters; 256 clears
# the largest with margin while bounding how much of a legitimate streamed reply
# is ever held back (see ``could_still_be_bare_intent``). Deliberately a constant,
# not a setting: this is a product decision, and the repo does not take env
# toggles for behavior.
BARE_INTENT_MAX_CHARS = 256

# First-person openers that commit to acting RIGHT NOW. The apostrophe in "I'll"
# is required so the bare word "ill" can never open a match; the contractions that
# are disambiguated by their own trailing words ("im going to") can spell it either
# way.
_OPENER = (
    r"(?:let\s+me|allow\s+me\s+to|i['’]ll|i\s+will|"
    r"i['’]?m\s+going\s+to|i\s+am\s+going\s+to|i\s+need\s+to|"
    r"i['’]?m\s+about\s+to|one\s+moment\s+while\s+i)"
)

# Verbs that name a tool-shaped action — one that CANNOT be satisfied by prose in
# the same reply. Conservative on purpose: a synonym outside this list is a false
# negative, which is the side we choose to err on. Deliberately excluded are verbs
# a reply can honour by simply continuing to write ("I'll list them", "I'll write
# it up", "let me summarize"), because those are not dropped actions.
_ACTION = (
    r"(?:check|search|run|look|query|call|invoke|execute|fetch|find|inspect|"
    r"read|open|retrieve|browse|poll|send|edit|update|create|delete|"
    r"launch|load|fire|pull|spawn|kick\s+off)"
)

# The clause body: everything from the action verb to the end of the reply. A dot is
# only a sentence end when whitespace or the string end follows it, so dotted TOOL
# NAMES and file paths stay inside the clause — "Let me call terminal.read now." is
# the single most realistic dead round in this product, and a naive [^.!?]* tail
# stops dead at the dot in ``terminal.read`` and misses it entirely.
#
# A colon ends the clause too: "Let me check the arithmetic: 120 + 135 = 255" and
# "I'll list them here: ..." deliver their result INLINE, so nothing was dropped.
_CLAUSE_BODY = r"(?:[^.!?:]|[.!?](?!\s|$))*"

# The announcement must OPEN a clause (string start, or just past a sentence/clause
# boundary) and run to the END of the reply — that is what makes it the point of the
# message rather than an aside inside a real answer. Captured so the deferral test
# below can be scoped to the announcement itself.
_BARE_INTENT_RE = re.compile(
    rf"(?:^|(?<=[.!;:,—])\s)\s*({_OPENER}\s+(?:now\s+|next\s+|just\s+|quickly\s+)?"
    rf"{_ACTION}\b{_CLAUSE_BODY})[.!]?\s*$",
    re.IGNORECASE,
)

# A clause that defers STARTING the action — waiting on the user, on approval, on a
# condition, or on later — is not the failure shape: such a reply correctly ends the
# turn with nothing dropped. Matched against the announcement clause ALONE, never the
# whole reply, so an unrelated "once the build is green" in a preamble cannot veto a
# genuine trailing intent. Kept narrow at the other end too: "until" and "if the"
# are excluded because they usually bound or complete an immediate action ("poll
# until the job completes", "check if the service is healthy") rather than defer it.
_DEFERRED_RE = re.compile(
    r"\b(?:after\s+\w+|once\s+\w+|when\s+you|when\s+the|when\s+ready|unless|"
    r"if\s+you|would\s+you|do\s+you\s+want|let\s+me\s+know|pending|"
    r"with\s+your\s+permission|later|tomorrow|tonight|"
    r"in\s+the\s+meantime|first\s+tell\s+me)\b",
    re.IGNORECASE,
)


def could_still_be_bare_intent(text: str) -> bool:
    """Could this PARTIAL reply still turn out to be a bare intent statement?

    The streaming gate's release condition, and monotonic by construction: text
    only grows, so once the reply is too long or has asked a question, it can
    never become a candidate again and the stream can forward freely from then on.

    Returns ``True`` for the empty string — nothing has ruled it out yet.
    """
    if len(text) > BARE_INTENT_MAX_CHARS:
        return False
    # A question ends the turn legitimately; ``is_bare_tool_intent`` rejects it too,
    # so seeing one mid-stream is an early, cheap release.
    return "?" not in text


def is_bare_tool_intent(
    text: str | None,
    *,
    finish_reason: str | None,
    saw_tool_calls: bool,
    tools_callable: bool,
) -> bool:
    """Is this completed round an announcement of an action it never took?

    ``tools_callable`` must reflect what was actually sent upstream: a round with
    no tools offered (or ``tool_choice="none"``) cannot emit a call, so a sentence
    about acting is at worst awkward prose, never a dropped action.
    """
    if saw_tool_calls or not tools_callable or finish_reason != "stop":
        return False
    raw = text or ""
    # The bound is measured on the RAW text, exactly as ``could_still_be_bare_intent``
    # measures it. The two MUST agree: the streaming gate releases on the probe, and
    # once released the reply is public and this predicate never gets to run. A
    # stripped measurement here would make the probe an unsound prefix test — a reply
    # the probe had already let through could still match, and only the non-streaming
    # path would re-roll it.
    if not raw.strip() or len(raw) > BARE_INTENT_MAX_CHARS or "?" in raw:
        return False
    # Collapse the model's own wrapping so a clause boundary reads the same however
    # it broke the lines.
    normalized = " ".join(raw.split())
    match = _BARE_INTENT_RE.search(normalized)
    if match is None:
        return False
    return not _DEFERRED_RE.search(match.group(1))
