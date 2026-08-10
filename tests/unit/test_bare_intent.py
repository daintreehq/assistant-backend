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


def test_bound_is_measured_on_stripped_text() -> None:
    """Surrounding whitespace must not push a real candidate over the bound."""
    text = "Let me read the results."
    assert _detect("\n\n  " + text + "   \n\n" + " " * BARE_INTENT_MAX_CHARS)


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
