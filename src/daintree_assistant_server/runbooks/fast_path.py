"""The deterministic no-task fast path.

The runbook selector is a model call on the turn's first-byte path. On a bare
greeting it costs a round trip to conclude what a lookup can conclude for free —
and that turn is exactly the one a tester judges the product's speed by.

**A false skip is far more expensive than the call it saves.** The assistant runs
the whole turn without the runbook it needed and nothing in the response says so.
So this recognises a CLOSED LIST OF EXACT PHRASES rather than reasoning about
what a message means.

That design is a direct response to what a token-bag version let through:

* ``"ok ｃｌｏｓｅ ａｌｌ ｔｅｒｍｉｎａｌｓ"`` — an ASCII word matcher extracted only
  ``ok`` from fullwidth text and declared the turn task-free. Same for
  ``"ok — 关闭所有终端"`` and ``"ok, закрой терминалы"``. Recognising *fragments*
  is not the same as recognising the *whole input*, and only the latter is safe.
* ``"right all this"``, ``"no thanks"``, ``"thank roger"`` — every word was in the
  acknowledgement vocabulary, and the combination is an imperative. A bag of
  allowed tokens composes into things no individual token licensed.

Hence: normalize, reject anything carrying a character outside a strict ASCII
set, then require the ENTIRE normalized phrase to be one this module knows.
Adding a phrase is a deliberate, reviewable act; no combination of existing ones
widens the surface.

**Only when nothing is in progress.** An acknowledgement clears the active set,
and that is correct only on a turn with no live work — which is what the selector
prompt itself says. A "thanks" while agents are running, on a background wake, or
with a live workflow digest is not evidence the work is over. The request-level
gate (:func:`can_skip_selection`) refuses all of those.
"""

from __future__ import annotations

import re
import unicodedata

from ..config import DOCS_RUNBOOK_ID
from ..contracts.daintree_api import RespondRequest
from ..contracts.openai import ChatMessage
from ..text_utils import message_text

# Longest utterance considered at all. NOT a safety boundary — every false skip
# found in review was 11-22 characters — just a cheap early exit.
MAX_FAST_PATH_CHARS = 60

# Characters a recognised phrase may contain AFTER normalization. Anything else —
# a digit, a non-ASCII letter, a symbol, an emoji, a fullwidth lookalike — means
# the input carries content this module cannot read, and unread content is never
# provably task-free.
_ALLOWED_CHARS = re.compile(r"^[a-z' ]+$")

# Punctuation that is purely decorative around an acknowledgement. Everything
# else — including "?" — disqualifies the phrase by surviving into the check above.
_STRIPPABLE = ".,!-—–…:;\"“”‘’"

# THE closed list. Each entry is a complete normalized phrase; a message is
# task-free only if it matches one exactly.
#
# Deliberately NOT a vocabulary. Adding an entry widens the surface by exactly one
# phrase a human read and approved, rather than by every sentence assemblable
# from a word list.
#
# Consent words are absent on purpose: a bare "yes", "sure" or "no" is far more
# often an ANSWER to a question the assistant just asked than an acknowledgement,
# and clearing the active set while the user consents to the workflow it governs
# is the worst outcome this module could produce.
_NO_TASK_PHRASES = frozenset(
    {
        # Greetings.
        "hi",
        "hi there",
        "hey",
        "hey there",
        "hello",
        "hello there",
        "hiya",
        "yo",
        "greetings",
        "good morning",
        "good afternoon",
        "good evening",
        "morning",
        "you there",
        "are you there",
        # Thanks.
        "thanks",
        "thank you",
        "thanks a lot",
        "thanks so much",
        "thank you so much",
        "many thanks",
        "thanks very much",
        "cheers",
        "ta",
        "ty",
        "thx",
        "much appreciated",
        "appreciated",
        "appreciate it",
        # Acknowledgements.
        "ok",
        "okay",
        "ok cool",
        "ok great",
        "ok thanks",
        "okay thanks",
        "ok perfect",
        "ok nice",
        "cool",
        "cool thanks",
        "nice",
        "nice one",
        "great",
        "great thanks",
        "great stuff",
        "perfect",
        "perfect thanks",
        "awesome",
        "awesome thanks",
        "lovely",
        "excellent",
        "brilliant",
        "sweet",
        "got it",
        "understood",
        "noted",
        "makes sense",
        "sounds good",
        "sounds great",
        "looks good",
        "all good",
        "no worries",
        "no problem",
        "np",
    }
)


def is_no_task_utterance(messages: list[ChatMessage]) -> bool:
    """Whether the newest user message is a recognised task-free phrase.

    The TEXT half of the decision. :func:`can_skip_selection` adds the context
    half, and only the pair is safe to act on.
    """
    latest = _latest_user_message(messages)
    if latest is None or len(latest) > MAX_FAST_PATH_CHARS:
        return False
    phrase = _normalize(latest)
    if phrase is None or phrase not in _NO_TASK_PHRASES:
        return False
    # An acknowledgement answering a question is CONSENT, not an acknowledgement.
    return not _question_may_be_pending(messages)


def can_skip_selection(request: RespondRequest, *, prior_active_ids: list[str]) -> bool:
    """Whether this whole REQUEST may skip selection and clear the active set.

    Text alone is not enough. Clearing is correct only when nothing is in
    progress, and a bare acknowledgement is no evidence of that — the user may be
    thanking the assistant while three agents keep working. So every signal that
    something is live disqualifies the turn:

    * an existing active runbook set (a workflow is governing this conversation);
    * a background wake, which has no new user message to be task-free about;
    * a stated goal, live workflow digests or runs, async operations, or resumed
      watchers.

    What survives is the case actually worth optimising — the cold opening turn
    of a session, where there is nothing to clear and nothing in flight...

    ...and the turn AFTER a standalone documentation answer, which is the same
    situation wearing an active set. The server pins the docs runbook for the turn
    it retrieved pages for; it is not a workflow and nothing about it is in
    progress, so "thanks" landing on top of it should not buy a selector call to
    conclude what this function already knows. It is excluded from the evidence,
    not from the clear — the fast path still resolves to an empty set, which is
    exactly right, since the next instruction gets its own docs decision.
    """
    if request.selection.force or request.selection.policy == "always":
        return False
    if [sid for sid in prior_active_ids if sid != DOCS_RUNBOOK_ID]:
        return False

    turn = request.turn
    if turn is not None:
        if turn.is_wake:
            return False
        if turn.goal or turn.workflow_state or turn.workflow_runs:
            return False
        if turn.async_operations or turn.resumed_watchers:
            return False

    return is_no_task_utterance(request.input.messages)


def _normalize(text: str) -> str | None:
    """Reduce ``text`` to a comparable phrase, or ``None`` if unreadable.

    Returns ``None`` — never a partial reading — whenever the input carries
    anything outside the allowed set. That is the fix for the fullwidth and
    Cyrillic bypasses: an unrecognised character means unread content.
    """
    # NFKC folds fullwidth and other compatibility forms onto their ASCII
    # equivalents, so a lookalike is either normalized into the alphabet the
    # allowlist uses or rejected below — never silently skipped.
    folded = unicodedata.normalize("NFKC", text).strip().lower()
    # Decorative punctuation becomes a separator wherever it appears, so
    # "ok, cool" and "perfect — thanks" normalize onto their listed forms.
    for char in _STRIPPABLE:
        folded = folded.replace(char, " ")
    collapsed = " ".join(folded.split())
    if not collapsed or not _ALLOWED_CHARS.match(collapsed):
        return None
    return collapsed


def _latest_user_message(messages: list[ChatMessage]) -> str | None:
    """The newest user message, or ``None`` if the last turn was not the user's.

    Reads the LAST message rather than searching backwards: a tool result or
    assistant turn newest means a CONTINUATION round, which has its own cadence
    rule and must not be short-circuited here.
    """
    if not messages:
        return None
    last = messages[-1]
    if last.role != "user":
        return None
    text = message_text(last.content)
    return text.strip() if text else None


def _question_may_be_pending(messages: list[ChatMessage]) -> bool:
    """Whether the assistant might be waiting on an answer.

    Biased hard toward "maybe". Every ambiguity returns ``True`` and costs one
    selector call; a wrong ``False`` clears the runbooks governing the very
    workflow the user just consented to.

    Treated as pending:

    * an assistant turn with tool calls — the question may live in a
      ``user.askMultipleChoice`` call rather than in prose;
    * an assistant turn with no visible text;
    * an intervening user message, which may have been "one sec" rather than an
      answer.
    """
    for message in reversed(messages[:-1]):
        if message.role == "assistant":
            if message.tool_calls:
                return True
            text = message_text(message.content) or ""
            if not text.strip():
                return True
            return "?" in text
        if message.role == "user":
            return True
    return False
