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

# Verbs that name a tool-shaped action. Conservative on purpose: a synonym outside
# this list is a false negative, which is the side we choose to err on.
_ACTION = (
    r"(?:check|search|run|look|query|call|invoke|execute|fetch|find|inspect|"
    r"read|open|retrieve|browse|list|test|verify|poll|send|write|edit|update|"
    r"create|delete|apply|launch|load|fire|pull|grab|start|spawn|kick\s+off)"
)

# The announcement must OPEN a clause (string start, or just past a sentence/clause
# boundary) and run to the END of the reply — that is what makes it the point of the
# message rather than an aside inside a real answer. Nothing sentence-ending may
# follow the action verb, so "Let me check the docs. Here is what I already know…"
# is not a match.
_BARE_INTENT_RE = re.compile(
    rf"(?:^|(?<=[.!;:,—])\s)\s*{_OPENER}\s+(?:now\s+|next\s+|just\s+|quickly\s+)?"
    rf"{_ACTION}\b[^.!?]*[.!]?\s*$",
    re.IGNORECASE,
)

# A tail that defers the action — waiting on the user, on a condition, or on later
# — is not the failure shape. Such a reply correctly ends the turn.
_DEFERRED_RE = re.compile(
    r"\b(?:after\s+you|once\s+you|once\s+the|when\s+you|when\s+the|unless|until|"
    r"if\s+you|if\s+the|would\s+you|do\s+you\s+want|let\s+me\s+know|"
    r"with\s+your\s+permission|your\s+call|later|tomorrow|tonight|"
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
    stripped = (text or "").strip()
    if not stripped or len(stripped) > BARE_INTENT_MAX_CHARS:
        return False
    if "?" in stripped:
        return False
    # Collapse newlines/runs so a clause boundary reads the same however the model
    # wrapped it. The length bound above is enforced on the RAW text, so the
    # streaming buffer stays genuinely bounded.
    normalized = " ".join(stripped.split())
    if _DEFERRED_RE.search(normalized):
        return False
    return bool(_BARE_INTENT_RE.search(normalized))
