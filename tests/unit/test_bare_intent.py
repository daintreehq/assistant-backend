"""The bare-tool-intent detector: it must catch the production failures and
nothing else.

``LIVE_FAILURES`` are the three assistant replies captured verbatim in session
``ses_659b0fc9`` (issue #25) — three dead rounds in a row, each announcing reads
it never issued. They are the detector's reason to exist, so they are asserted
literally rather than paraphrased.
"""

from __future__ import annotations

import pytest

from daintree_assistant_server.services.bare_intent import (
    BARE_INTENT_MAX_CHARS,
    could_still_be_bare_intent,
    is_bare_tool_intent,
)

LIVE_FAILURES = [
    "All five votes are in. Let me read them, one read per terminal, batched.",
    (
        "You're right, my previous reply ended with a tool call intent but I only "
        "emitted the reads in the next sentence without actually calling them. "
        "Let me read all five votes now, in one batched reply."
    ),
    (
        "I have the live roster, all five agents are waiting at a prompt, meaning "
        "they finished their votes. Let me read each vote now in one batch."
    ),
]


def _detect(text: str, **overrides: object) -> bool:
    """The detector with the failure-shaped defaults; override one axis per test."""
    kwargs: dict[str, object] = {
        "finish_reason": "stop",
        "saw_tool_calls": False,
        "tools_callable": True,
    }
    kwargs.update(overrides)
    return is_bare_tool_intent(text, **kwargs)  # type: ignore[arg-type]


@pytest.mark.parametrize("text", LIVE_FAILURES)
def test_detects_the_observed_live_failures(text: str) -> None:
    assert _detect(text)


@pytest.mark.parametrize("text", LIVE_FAILURES)
def test_live_failures_are_within_the_streaming_bound(text: str) -> None:
    """The streaming gate can only catch what fits inside its hold window."""
    assert len(text) <= BARE_INTENT_MAX_CHARS
    assert could_still_be_bare_intent(text)


def test_matches_a_trailing_clause_after_a_preamble() -> None:
    """The announcement follows real content — a start-anchored pattern would
    have missed every production failure."""
    assert _detect("The build finished and the roster is stable. Let me check the logs.")


def test_matches_after_a_comma_boundary() -> None:
    assert _detect("Everything is staged, let me run the tests.")


def test_accepts_curly_and_ascii_apostrophes() -> None:
    assert _detect("I'll read the file now.")
    assert _detect("I’ll read the file now.")


def test_matches_across_wrapped_newlines() -> None:
    """Normalization collapses the model's own wrapping before matching."""
    assert _detect("Votes are in.\n\nLet me read\nall five now.")


# ---- false-positive protections -------------------------------------------


def test_rejects_a_plain_checkpoint() -> None:
    assert not _detect("All five agents finished and their votes are recorded.")


def test_rejects_an_open_ended_question() -> None:
    assert not _detect("Let me check the logs — which service should I start with?")


def test_rejects_let_me_know() -> None:
    assert not _detect("Done. Let me know if you want the full diff.")


@pytest.mark.parametrize(
    "text",
    [
        "I'll run the suite once you confirm the branch.",
        "I'll check the logs if you want me to.",
        "I'll read the results tomorrow.",
        "I'll update the ticket with your permission.",
    ],
)
def test_rejects_deferred_or_conditional_plans(text: str) -> None:
    """A deferred action correctly ends the turn — nothing was dropped."""
    assert not _detect(text)


def test_rejects_an_announcement_followed_by_real_content() -> None:
    """The announcement must be the END of the reply, not an aside."""
    assert not _detect(
        "Let me check the config. Meanwhile the roster already shows all five idle."
    )


def test_rejects_a_verb_outside_the_conservative_whitelist() -> None:
    """A false negative is the side this detector deliberately errs on."""
    assert not _detect("I'll think about the best ordering.")


def test_rejects_the_word_ill() -> None:
    """``I'll`` requires its apostrophe so the adjective can never open a match."""
    assert not _detect("The agent looks ill run the fallback.")


# ---- non-text axes ---------------------------------------------------------


@pytest.mark.parametrize("finish_reason", ["length", "tool_calls", None, ""])
def test_requires_an_exact_stop_finish(finish_reason: str | None) -> None:
    """``length`` is truncation — a different failure with a different remedy."""
    assert not _detect(LIVE_FAILURES[0], finish_reason=finish_reason)


def test_rejects_a_round_that_did_emit_tool_calls() -> None:
    assert not _detect(LIVE_FAILURES[0], saw_tool_calls=True)


def test_requires_callable_tools() -> None:
    """With nothing callable, the sentence dropped no action."""
    assert not _detect(LIVE_FAILURES[0], tools_callable=False)


@pytest.mark.parametrize("text", [None, "", "   \n  "])
def test_rejects_empty_content(text: str | None) -> None:
    assert not _detect(text)  # type: ignore[arg-type]


def test_enforces_the_character_bound() -> None:
    """A long reply is doing real work whatever its last sentence says."""
    padding = "Context. " * 30
    long_text = padding + "Let me read the results."
    assert len(long_text) > BARE_INTENT_MAX_CHARS
    assert not _detect(long_text)
    assert _detect("Let me read the results.")


def test_bound_uses_the_same_metric_as_the_streaming_probe() -> None:
    """The probe must be a SOUND prefix test for this predicate.

    The streaming gate releases on the probe, and once released the reply is public
    and can never be re-rolled. If the predicate measured a stripped length while
    the probe measured raw, a reply the probe had already let through could still
    match here — and only the non-streaming path would repair it.
    """
    padded = "\n\n" + "Let me read the results.".ljust(BARE_INTENT_MAX_CHARS, "x")
    assert len(padded) > BARE_INTENT_MAX_CHARS
    assert not could_still_be_bare_intent(padded)
    assert not _detect(padded)


@pytest.mark.parametrize("length", [BARE_INTENT_MAX_CHARS, BARE_INTENT_MAX_CHARS + 1])
def test_probe_and_predicate_agree_at_the_boundary(length: int) -> None:
    """Neither may admit a reply the other has ruled out."""
    tail = "Let me read the results."
    text = "x " * ((length - len(tail)) // 2) + tail
    text = text[:length] if len(text) > length else text.ljust(length, "y")
    assert could_still_be_bare_intent(text) == (len(text) <= BARE_INTENT_MAX_CHARS)
    if not could_still_be_bare_intent(text):
        assert not _detect(text)


# ---- regressions for defects found in review -------------------------------


@pytest.mark.parametrize(
    "text",
    [
        # Dotted TOOL NAMES are this product's convention, so this is the most
        # realistic dead round there is — and a naive [^.!?]* tail stops at the
        # dot in "terminal.read" and misses it entirely.
        "Let me call terminal.read now.",
        "I'll read terminal.awaitAll output now.",
        "I'll open src/app.py now.",
        # A colon inside a URL or a clock time is not a result-introducing colon.
        "Let me open https://example.com now.",
        # "until" bounds an immediate action rather than deferring it.
        "I'll poll until the job completes.",
        # These words modify the OBJECT; they do not defer the action.
        "I'll run it once more.",
        "I'll check the pending jobs.",
        "I'll read the entries after line 120.",
    ],
)
def test_detects_dotted_names_paths_and_bounded_actions(text: str) -> None:
    assert _detect(text)


def test_rightmost_announcement_wins() -> None:
    """A deferral in an EARLIER clause must not veto the actual announcement."""
    assert _detect("I'll run the suite tomorrow, then let me read the votes.")


@pytest.mark.parametrize(
    "text",
    [
        # A colon delivers the result INLINE — nothing was dropped.
        "The review found two defects, I'll list them here: stale logging, unsafe cleanup.",
        "Let me check the arithmetic: 120 + 135 = 255.",
        # A semicolon ends the clause: real content follows the announcement.
        "I'll open src/app.py; the fix is already there.",
        # A run-on sentence boundary is still a boundary.
        "Let me check the config.The roster already shows all five idle.",
        # A contrastive continuation means the announcement is not the whole point.
        "The config is valid, I'll check the logs, but stale credentials are likely.",
        # Deferral is not always spelled with a pronoun.
        "I'll delete the stale branch after approval.",
        "I'll run the suite once CI recovers.",
        "I'll delete the branch pending your review.",
        # A conditional MUTATION is the worst thing to re-roll: the nudge would ask
        # the next draft to perform it unconditionally.
        "I'll delete the branch if the tests pass.",
        "I'll delete the branch—your call.",
        # Satisfiable by continuing to write, so not a dropped action.
        "I'll list the three failures.",
        "I'll write up what I found.",
    ],
)
def test_rejects_inline_results_and_deferred_actions(text: str) -> None:
    assert not _detect(text)


def test_conditional_check_is_a_tolerated_false_negative() -> None:
    """"if the" is read as deferral even when it is really a complement.

    Keeping it costs this reply a re-roll it arguably deserves; dropping it would
    re-roll "I'll delete the branch if the tests pass", nudging the next draft to
    delete unconditionally. False negatives are the cheap error here.
    """
    assert not _detect("I'll check if the service is healthy.")


def test_deferral_is_scoped_to_the_announcement_clause() -> None:
    """An unrelated deferral in the preamble must not veto a genuine dead round."""
    assert _detect("The rerun is queued for tomorrow. Let me read the current votes.")


# ---- the streaming probe ---------------------------------------------------


def test_probe_starts_open_and_is_monotonic() -> None:
    assert could_still_be_bare_intent("")
    assert could_still_be_bare_intent("All five votes are in.")


def test_probe_closes_past_the_bound() -> None:
    assert not could_still_be_bare_intent("x" * (BARE_INTENT_MAX_CHARS + 1))


def test_probe_closes_on_a_question_mark() -> None:
    """A question ends the turn legitimately — release the stream early."""
    assert not could_still_be_bare_intent("Which terminal did you mean?")


def test_probe_never_reopens_once_closed() -> None:
    """Monotonicity is what makes an incremental check safe."""
    growing = "Which one? "
    assert not could_still_be_bare_intent(growing)
    for _ in range(5):
        growing += "more text "
        assert not could_still_be_bare_intent(growing)
