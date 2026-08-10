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
* only when the trailing clause is an IMMEDIATE action announcement — first-person
  ("Let me read them.") or the subject-less status line the prompt itself scripts
  ("Reading the closest match."), the latter only at a sentence start and only when
  no finite verb turns it into a description;
* never a question, and never a deferred or conditional plan;
* never content the reply already delivered as a list.

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

# The same verbs in participle form, for the subject-less status line the prompt
# itself prescribes: ``main/base/40-communication.md`` scripts a continuation round
# as "Search returned 3 candidates under src/terrain. Reading the closest match."
# That shape has no first-person opener, so it never reaches ``_ANNOUNCE_RE`` — the
# most frequent announcement in the system was the one the detector could not see.
#
# Spelled out rather than derived from ``_ACTION`` by appending "ing": English stem
# rules are irregular (run/running, invoke/invoking, poll/polling) and a wrong
# derivation would silently widen or narrow the pattern. The invariant is instead
# checkable by eye — every verb in ``_ACTION``, in its participle form, and nothing
# else. A participle whose base verb is absent above (``listing``, ``writing``,
# ``summarizing``) stays absent here too: those are satisfiable by continuing to
# write, so they are not dropped actions in EITHER form, and letting the two lists
# disagree would re-roll "Listing the three failures." while passing "I'll list the
# three failures."
#
# ``looking`` is the one entry that is not a bare swap: it requires its preposition
# so the sign-off "Looking good." can never open a match.
_ACTION_PARTICIPLE = (
    r"(?:checking|searching|running|looking\s+(?:up|at|into)|querying|calling|"
    r"invoking|executing|fetching|finding|inspecting|reading|opening|retrieving|"
    r"browsing|polling|sending|editing|updating|creating|deleting|"
    r"launching|loading|firing|pulling|spawning|kicking\s+off)"
)

# The clause body: everything from the action verb to the end of the reply.
#
# `.` and `:` are sentence/clause enders EXCEPT when a lowercase letter, digit, or
# slash follows immediately — that is the shape of a dotted tool name, a file path,
# or a URL, all of which belong INSIDE the clause. "Let me call terminal.read now."
# is the single most realistic dead round in this product (every tool is named that
# way), and a naive `[^.!?]*` tail stops dead at the dot in ``terminal.read`` and
# misses it. The same rule keeps `https://example.com` and `12:30` whole while still
# ending the clause on ".The" (a run-on sentence boundary) and on "here: ..." or
# "arithmetic: 120 + 135 = 255", where the result is delivered INLINE and so nothing
# was actually dropped. `;` `!` `?` always end it.
#
# The lookahead is scoped case-SENSITIVE with ``(?-i:…)``: the whole pattern compiles
# with IGNORECASE, under which a bare ``[a-z0-9/]`` also matches capitals and would
# read the run-on "config.The roster…" as one token instead of a sentence boundary.
#
# ``\n`` ends the clause too. Normalization (see ``_normalize``) collapses the model's
# own wrapping but KEEPS the newline in front of a list item, and this exclusion is
# what makes that newline load-bearing: a reply that delivered its content as bullets
# ("Let me check the worktree\n- clean\n- no pending changes") answered its own
# opener, and without this the bullets are swallowed into the clause and the reply
# reads as a dangling promise. A negated character class is not governed by
# ``DOTALL``, so the exclusion has to be written out.
_CLAUSE_BODY = r"(?:[^.!?:;\n]|[.:](?=(?-i:[a-z0-9/])))*"

# What sits between the start of a clause and its verb: an optional leading
# connective (so "…, then let me read the votes." still reads as one announcement)
# and the adverbs the model slips in ("I'll just check…"). Shared by the
# announcement pattern and the idiom carve-out below so the two cannot drift.
_LEAD = (
    r"(?:(?:then|so|now|next|okay|ok|alright|first)\s+)?"
    rf"{_OPENER}\s+(?:now\s+|next\s+|just\s+|quickly\s+)?"
)

# The participle line's equivalent lead. Structurally identical to ``_LEAD`` — same
# connectives, same adverbs — with the opener OPTIONAL and reduced to "I'm"/"I am",
# because the whole point of this shape is that the subject is dropped. Kept in step
# with ``_LEAD`` deliberately: the two describe the same sentence position.
_PARTICIPLE_LEAD = (
    r"(?:(?:then|so|now|next|okay|ok|alright|first)\s+)?"
    r"(?:i(?:['’]m|\s+am)\s+)?(?:now\s+|next\s+|just\s+|quickly\s+)?"
)

# The announcement itself, anchored to the END of the reply — that is what makes it
# the point of the message rather than an aside inside a real answer.
_ANNOUNCE_RE = re.compile(
    rf"{_LEAD}{_ACTION}\b{_CLAUSE_BODY}[.!]?\s*$",
    re.IGNORECASE,
)

# The subject-less variant. Tried separately from ``_ANNOUNCE_RE`` rather than folded
# into it as an alternation, because it carries a veto (``_is_bare_participle``) that
# must NOT apply to opener-led matches: "I'll check whether it is ready." is governed
# by the deferral rules alone and its behaviour is unchanged by any of this.
_PARTICIPLE_RE = re.compile(
    rf"{_PARTICIPLE_LEAD}{_ACTION_PARTICIPLE}\b{_CLAUSE_BODY}[.!]?\s*$",
    re.IGNORECASE,
)

# Clauses that wear the announcement's shape but promise no tool call. Anchored at
# BOTH ends and tested against a whole clause rather than searched inside one: the
# idiom only disqualifies the clause when it IS the clause. "I'll update the docs
# and call it done." still announces a real update, and a veto that fired on the
# tail would hide it — there the clause's own verb is "update", so this never
# matches.
#
# Two idioms, both observed as false positives:
#
# * performative "call" — "I'm going to call it here." is a sign-off, not a tool
#   call, and re-rolling it discards a correct final answer. The restriction to
#   "call it/this/that <word>" is what separates the idiom from the real thing
#   ("let me call terminal.read"), which never takes those objects.
# * "check with <someone>" — consulting a human, not a tool. Plain "I'll check the
#   logs" is unaffected because nothing there follows "check" with "with".
#
# Both carve-outs deliberately swallow the rest of the clause, so a real action
# trailing an idiom ("I'll check with the team and update the ticket") is missed.
# That is a false negative, the side this module always errs toward.
#
# Each idiom is spelled in BOTH leads' grammar, because the participle path reaches
# the same two verbs without an opener: "Calling it done." and "Checking with the
# team." are the same sign-off and the same human consult, and a veto anchored only
# on ``_LEAD`` would let them through as fresh false positives.
_IDIOM_RE = re.compile(
    rf"(?:{_LEAD}(?:call\s+(?:it|this|that)\s+(?:a\s+)?\w+|check(?:\s+in)?\s+with\b)"
    rf"|{_PARTICIPLE_LEAD}"
    rf"(?:calling\s+(?:it|this|that)\s+(?:a\s+)?\w+|checking(?:\s+in)?\s+with\b))"
    rf"{_CLAUSE_BODY}[.!]?\s*$",
    re.IGNORECASE,
)

# Where a new clause may begin.
_BOUNDARY_RE = re.compile(r"(?<=[.!;:,—])\s")

# Where a new SENTENCE may begin — a strict subset of the clause boundaries above.
# The participle path is restricted to these, and that restriction is what keeps it
# narrow: a participle after a comma, colon, or em dash is far more often a reduced
# adverbial clause inside a delivered answer ("One cause stands out — reading the
# stale cache.") than a status line. The prompt prescribes the status line as its own
# sentence, so requiring one costs no real coverage.
_SENTENCE_START_RE = re.compile(r"(?<=[.!;])\s")

# A Markdown list item. Two jobs, both about content the reply already DELIVERED:
# ``_normalize`` keeps the newline in front of such a line, and ``_announcement_clause``
# refuses to start a clause on one. The second is not redundant — an ordered marker
# ends in "." and so mints its own ``_BOUNDARY_RE`` start, which would otherwise read
# "Here's the plan.\n1. I'll inspect the current implementation." as a dead round.
_LIST_MARKER_RE = re.compile(r"^(?:[-*+]|\d+[.)])(?:\s|$)")

# A clause that defers STARTING the action — waiting on the user, on approval, or on
# later — is not the failure shape: such a reply correctly ends the turn with nothing
# dropped. Tested against the announcement clause ALONE, so an unrelated "tomorrow"
# in a preamble cannot veto a genuine trailing intent.
#
# Both edges are deliberate. "if the"/"would you" stay because dropping them turns a
# conditional mutation ("I'll delete the branch if the tests pass") into a re-roll
# that is then nudged to do it NOW — the worst possible false positive. "until" is
# absent because it bounds an immediate action ("poll until the job completes"), and
# "once more"/"after line 120"/"the pending jobs" are carved out because there the
# word modifies the object rather than deferring the action.
#
# What follows "when" is a subject, and the list has to cover the pronouns as well as
# the determiner: "I'll update you when the run finishes." and "I'll update you when
# it finishes." are the same promise to report back — a real final answer — and only
# the second was reaching this veto.
_DEFERRED_RE = re.compile(
    r"\b(?:after\s+(?:you|we|they|that|approval|review|sign|confirmation|"
    r"the\s+(?:build|tests?|run|deploy|merge|review))|"
    r"once\s+(?!more\b|again\b)\w+|when\s+(?:you|the|it|they|that|ready|done)|unless|"
    r"if\s+the|if\s+you|would\s+you|do\s+you\s+want|let\s+me\s+know|"
    r"pending\s+(?:your|approval|review|confirmation)|"
    r"with\s+your\s+permission|your\s+call\b(?!\s*stack)|"
    r"later|tomorrow|tonight|in\s+the\s+meantime|first\s+tell\s+me)\b",
    re.IGNORECASE,
)

# A contrastive continuation means substantive content follows the announcement, so
# the announcement is not the whole point of the reply.
_CONTRAST_RE = re.compile(
    r"\b(?:but|however|although|though|meanwhile|instead|whereas|that\s+said)\b",
    re.IGNORECASE,
)

# A participle carrying a finite verb is a STATEMENT about an action, not a promise to
# take one: "Running the suite takes 4 minutes." describes. This veto is the price of
# admitting subject-less clauses at all — without a subject there is nothing else to
# separate "Reading the closest match." from "Reading the config is slow."
#
# Every verb is spelled in its past tense too. A description is as often reported as
# predicted ("Checking the roster required a second pass."), and a gap here is a false
# POSITIVE — the expensive direction. Widening this veto only ever narrows the
# detector, so it is the safe side to be generous on.
_FINITE_VERB_RE = re.compile(
    r"\b(?:is|are|was|were|takes?|took|will|would|can|could|should|has|have|had"
    r"|seems?|seemed|looks?|looked|means?|meant|gives?|gave|costs?|cost"
    r"|requires?|required|needs?|needed)\b",
    re.IGNORECASE,
)

# …except when the finite verb belongs to a SUBORDINATE clause, where it says nothing
# about the participle: "Checking whether the service is healthy." still announces a
# check. Only a subordinator standing BEFORE the finite verb can excuse it.
_SUBORDINATOR_RE = re.compile(
    r"\b(?:whether|that|which|who|whose|because|while|if|until|since|where|what|how)\b",
    re.IGNORECASE,
)


def _normalize(raw: str) -> str:
    """Collapse the model's own line wrapping, but keep list items on their own lines.

    A clause boundary should read the same however the model broke its lines, which is
    why wrapping collapses to a single space. A Markdown list item is the exception:
    the newline in front of it is the only thing separating an opener from the content
    that ANSWERS it, and ``_CLAUSE_BODY`` stops on that newline.
    """
    out: list[str] = []
    for raw_line in raw.splitlines():
        line = " ".join(raw_line.split())
        if not line:
            continue
        if out:
            out.append("\n" if _LIST_MARKER_RE.match(line) else " ")
        out.append(line)
    return "".join(out)


def _starts_in_list_item(normalized: str, start: int) -> bool:
    """Whether this clause start sits on a delivered list item."""
    line_start = normalized.rfind("\n", 0, start) + 1
    return _LIST_MARKER_RE.match(normalized[line_start:]) is not None


def _is_bare_participle(clause: str) -> bool:
    """Whether the clause is a subject-less status line announcing an action."""
    if not _PARTICIPLE_RE.match(clause):
        return False
    finite = _FINITE_VERB_RE.search(clause)
    return finite is None or _SUBORDINATOR_RE.search(clause, 0, finite.start()) is not None


def _announcement_clause(normalized: str) -> str | None:
    """The RIGHTMOST end-anchored announcement clause, if the reply ends in one.

    Rightmost matters: "I'll run the suite tomorrow, then let me read the votes."
    has two candidate clauses, and only the last one is the actual announcement —
    scoping the deferral test to a leftmost match would let the preamble's
    "tomorrow" veto a genuine dead round.

    An idiom clause is SKIPPED rather than returned, so the scan keeps widening:
    "I'll read the votes, then I'll call it a day." still surfaces the genuine
    announcement in the enclosing clause. A clause starting on a list item is skipped
    the same way — it is content the reply delivered, not a promise it left open.

    The subject-less participle line is admitted only at a SENTENCE start, and only
    once its own finite-verb veto clears. Both restrictions are local to that path:
    an opener-led clause is judged exactly as it was before.
    """
    starts = [0, *(m.end() for m in _BOUNDARY_RE.finditer(normalized))]
    sentence_starts = {0, *(m.end() for m in _SENTENCE_START_RE.finditer(normalized))}
    for start in reversed(starts):
        if _starts_in_list_item(normalized, start):
            continue
        clause = normalized[start:]
        if _IDIOM_RE.match(clause):
            continue
        if _ANNOUNCE_RE.match(clause):
            return clause
        if start in sentence_starts and _is_bare_participle(clause):
            return clause
    return None


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
    normalized = _normalize(raw)
    clause = _announcement_clause(normalized)
    if clause is None:
        return False
    return not (_DEFERRED_RE.search(clause) or _CONTRAST_RE.search(clause))
