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
    assert not _detect("Let me check the config. Meanwhile the roster already shows all five idle.")


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
        # A promise to report back is a real final answer. Only the determiner reached
        # the deferral veto, so the pronoun spellings of the SAME promise were being
        # re-rolled — throwing away a correct reply and billing a second round.
        "The agent is still running. I'll update you when it finishes.",
        "I'll update you when the run finishes.",
        "I'll update you when they finish.",
        "I'll update you when that lands.",
        "I'll update you when done.",
        "I will update you when we finish.",
        "I will update you when I know more.",
        # Enumerating the subject leaves the same hole one subject over, which is why
        # the conjunction is matched bare.
        "I will delete the branch when CI passes.",
        # A conditional MUTATION is the worst possible false positive: the re-roll
        # nudge would ask the next draft to perform it unconditionally.
        "I will delete the branch if CI passes.",
        "I will delete the branch if it passes.",
        "I will delete the branch if approved.",
        "I will delete the branch after CI passes.",
        "I will delete the branch after it passes.",
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


@pytest.mark.parametrize(
    "text",
    [
        # Performative "call": a sign-off, not a tool call. Re-rolling one of these
        # throws away a correct final answer and bills a second generation for it.
        "Everything is green. I'm going to call it here.",
        "The suite is clean. I'll call it a day.",
        "Both PRs are merged. Let me call it done.",
        "I'll call this done.",
        "I'm going to call that done.",
        # "check with <someone>" consults a human; no tool was dropped.
        "I'll check with the team.",
        "Let me check with you before touching the migration.",
        "I'll check in with the reviewer.",
    ],
)
def test_rejects_performative_and_human_consult_idioms(text: str) -> None:
    assert not _detect(text)


@pytest.mark.parametrize(
    "text",
    [
        # The idiom must not swallow a clause whose OWN verb is a real action.
        "I'll update the docs and call it done.",
        "I'll read the votes, then I'll call it a day.",
        # Plain "check" is untouched: only "check with" is a consultation.
        "I'll check the logs.",
        "Let me check the roster before the next batch.",
        # The same two carve-outs, reached through the participle path.
        "Calling terminal.read now.",
        "Checking the roster before the next batch.",
    ],
)
def test_idiom_carve_out_does_not_over_reach(text: str) -> None:
    assert _detect(text)


# ---- the subject-less status line the prompt prescribes (issue #29) ---------


@pytest.mark.parametrize(
    "text",
    [
        # Verbatim from main/base/40-communication.md, which scripts this exact shape
        # for continuation rounds — so it is the highest-frequency announcement there
        # is, and an opener-only pattern was blind to every one of them.
        "Search returned 3 candidates under src/terrain. Reading the closest match.",
        # The line on its own: position 0 is a sentence start.
        "Reading the closest match.",
        "Checking the worktree now.",
        # The dropped subject may also be spelled out, and a connective may front it.
        "I'm inspecting the failing test.",
        "I am polling the queue.",
        "I’m reading the closest match.",
        "The suite is red. Now reading the first failure.",
        # Dotted tool names and paths are objects in their own right — no determiner
        # precedes them, and they are the likeliest object this product has.
        "Votes are in. Reading terminal.awaitAll output.",
        "Opening src/app.py now.",
        "Reading TODO.md now.",
        # A real sentence may end in a one-letter token; that still starts the next.
        "Use plan A. Reading the logs now.",
        # A plural head noun is inflected like a verb, but it CLOSES the phrase.
        "Checking the logs.",
        "Reading the votes now.",
        # Spot checks across the rest of the mirrored verb list.
        "Deleting the stale branch.",
        "Spawning the fallback worker.",
        "Kicking off the release job.",
        "Looking into the failing migration.",
    ],
)
def test_detects_bare_participle_status_lines(text: str) -> None:
    assert _detect(text)


@pytest.mark.parametrize(
    "text",
    [
        # A second predicate makes the participle DESCRIBE an action rather than
        # promise one. The first case is the one test_learning_trigger.py guards live.
        "Running the suite takes 4 minutes.",
        "Reading the config is slow.",
        "Reading the config isn't useful.",
        "Checking the roster required a second pass.",
        "Reading that file is unnecessary.",
        # The verb a second predicate draws on is an OPEN set, which is why the veto
        # is a suffix rule and not a list.
        "Reading the config revealed the bug.",
        "Checking the logs confirmed a timeout.",
        "Running the tests succeeded.",
        "Inspecting the payload reveals the missing field.",
        "Retrieving a missing row returns None.",
        "Editing that field preserves whitespace.",
        "Looking at the trace explains the timeout.",
        # An inflected word is the object's head noun only immediately after the
        # determiner. One word further along it is a predicate, however the sentence
        # ends — "the service succeeds" is a sentence, "the logs" is a noun phrase.
        "Checking the service succeeds.",
        "Checking the service succeeds now.",
        "Opening the file failed.",
        "Opening the file failed again.",
        "Creating the client now succeeds.",
        "Loading the config now works.",
        # A compound hides its inflection from a whole-token test.
        "Loading the plugin hard-failed.",
        "Checking the config passed/failed.",
        "Reading the file load.failed.",
        # An irregular past or a modal carries no inflection at all, so the suffix
        # rule cannot see it and the closed list has to.
        "Opening the file broke lint.",
        "Opening the file lost data.",
        "Running the benchmark grew the cache.",
        "Deleting the branch may help.",
        "Deleting the branch might help.",
        "Deleting the branch must wait.",
        # A zero-inflection irregular past is spelled exactly like its stem, so the
        # suffix rule cannot see it either and the closed list is the only thing
        # standing between these and a discarded correct answer.
        "I reproduced it. Loading the plugin blew up.",
        "Root cause found. Reading the config set the stale flag.",
        "Confirmed the regression. Deleting the branch undid the fix.",
        "Reading the manifest hid the real error.",
        "Opening the socket burst the send buffer.",
        "Reading the config let the stale value through.",
        # "cannot" is one word: \bcan\b never reaches inside it, and the contraction
        # rule only covers "can't".
        "The guard is in place. Reading the file cannot fail.",
        "Deleting the branch shall wait until the release.",
        # An appositive noun-phrase fragment describes code rather than announcing an
        # action; a preposition heading a phrase after the object is the tell.
        "The loader is the bottleneck. Reading the whole file into memory.",
        "Found the leak in session.py. Opening a new client per request.",
        "One antipattern stands out. Calling the API inside the retry loop.",
        # Two gerunds coordinated into one subject.
        "Checking the config and reading the file help.",
        # A measurement or a status pair is not a tool name: the dotted object has to
        # start with a letter and carry a dot.
        "Reading 3.5x.",
        "Polling on/off.",
        "Checking passes/fails.",
        # An en dash delivers content exactly as an em dash does.
        "Checking the worktree – clean.",
        # A gerund subject usually reaches its complement through a preposition, and
        # requiring a direct object is what rejects the whole class.
        "Fetching from cache avoids the timeout.",
        "Querying by slug returns 404.",
        "Executing under zsh fails on line 12.",
        "Spawning two workers reproduces the race.",
        "Invoking it directly bypasses the guard.",
        "Reading while idle is harmless.",
        # A status fragment or a noun modifier has no object phrase at all.
        "Checking complete.",
        "Reading complete.",
        "Running clean.",
        "Deleting disabled.",
        "Polling interval unchanged.",
        "Calling convention unchanged.",
        "Opening brace unchanged.",
        "Launching locally works again.",
        # A conditional or subordinated mutation is the worst thing to re-roll.
        "Deleting that branch is unsafe.",
        "Deleting the branch while CI is running is unsafe.",
        "Deleting the branch, which is protected, is unsafe.",
        # A participle mid-sentence is a reduced clause inside a delivered answer, not
        # a status line — only a SENTENCE start opens the participle path.
        "One cause stands out — reading the stale cache.",
        "The handler crashed, reading a null config.",
        "The latency comes from I/O; reading each file sequentially.",
        "Looking at the logs, I found a stale cache entry.",
        # A period after an enumerator or an abbreviation does not end a sentence, so
        # it must not mint a start for the participle path either. A path segment ends
        # in a slash, which is why that counts as a left edge for the enumerator.
        "Costs: 1. Reading from disk.",
        "Disk I/O is the bottleneck, e.g. Reading the cache.",
        "The generated file is docs/1. Reading notes.md.",
        # An em dash delivers the result inline exactly as a colon does.
        "Checking the worktree — clean.",
        "Reading the closest match — it contains the fix.",
        # The base verb is absent from _ACTION because prose can satisfy it, so its
        # participle stays absent too. "I'll list the three failures." is False.
        "Listing the three failures.",
        "Writing up what I found.",
        # "looking" without its preposition is a sign-off, not an action.
        "Looking good.",
        # The performative and human-consult idioms reach the same verbs without an
        # opener, so the carve-out has to cover this grammar as well.
        "Everything is green. Calling it done.",
        "I'm calling it a day.",
        "Checking with the team.",
        "I am checking in with the reviewer.",
        "Everything is green. Calling it.",
        "Just checking in.",
        # The participle path inherits the deferral and contrast vetoes.
        "Reading the results tomorrow.",
        "Checking the logs, but the credentials are likely stale.",
    ],
)
def test_rejects_participles_that_are_not_announcements(text: str) -> None:
    assert not _detect(text)


def test_participle_vetoes_do_not_leak_onto_opener_clauses() -> None:
    """The participle vetoes are scoped to the participle path alone.

    "I'll check the config is valid." carries a finite verb and no object determiner,
    so it would be rejected twice over if the two paths shared one rule. An opener
    declares the intent by itself, and this reply's result is unchanged by any of the
    participle work.
    """
    assert _detect("I'll check the config is valid.")
    assert _detect("I'll read them, one read per terminal, batched.")


@pytest.mark.parametrize(
    "text",
    [
        # Narrowing the participle path costs coverage, and this is where. Each of
        # these is a real dead round the detector now lets through; a missed dead round
        # costs one wasted turn, while re-rolling a delivered answer costs a correct
        # reply and a second generation. These are the cheap side on purpose.
        "Checking whether the service is healthy.",
        "Reading from the audit log.",
        "Search returned three matches, now reading the closest match.",
        "The build is done; reading the log.",
        # An inflected word one slot past the determiner cannot be told from a
        # predicate, so the adjective-plus-plural object goes with it.
        "Reading the failing tests.",
        # A slashed name with no dot is indistinguishable from a status pair.
        "Opening src/tests output.",
        # The opener path pays for the bare "when"/"if" deferral the same way.
        "Let me check when CI failed.",
        "Let me check if it exists.",
    ],
)
def test_narrowed_participle_path_tolerates_these_false_negatives(text: str) -> None:
    assert not _detect(text)


@pytest.mark.parametrize(
    "text",
    [
        # The bullets ARE the content: the reply answered its own opener, so there is
        # no dangling promise to re-roll.
        "Let me check the worktree\n- clean\n- no pending changes",
        "Let me check the worktree\n* clean\n+ no pending changes",
        "Let me check the worktree\n1. clean\n2) no pending changes",
        # Tight markers and Unicode bullets are block content too. Every marker only
        # ever narrows detection, so recognising more of them is the safe direction.
        "Let me check the worktree\n-clean\n-dirty",
        "Let me check the worktree\n1)clean\n2)dirty",
        "Let me check the worktree\n• clean\n• no pending changes",
        # …as are the other block shapes an answer arrives in.
        "Let me check the worktree\n> clean",
        "Let me check the worktree\n| status | clean |",
        "Let me check the worktree\n```text\nclean\n```",
        "Let me check the worktree\n## Result\nclean",
        # A plan being PRESENTED. The ordered marker's own period mints a clause start,
        # so the block skip is what keeps these from reading as dead rounds.
        "Here's the plan.\n1. I'll inspect the current implementation.",
        "1. I'll inspect the current implementation.",
        "1. Reading the closest match.",
    ],
)
def test_rejects_content_delivered_as_a_block(text: str) -> None:
    assert not _detect(text)


def test_block_awareness_does_not_disturb_plain_wrapping() -> None:
    """Only a block line keeps its newline; ordinary wrapping still collapses."""
    assert _detect("Search returned 3 candidates.\nReading the closest\nmatch.")


def test_conditional_check_is_a_tolerated_false_negative() -> None:
    """ "if the" is read as deferral even when it is really a complement.

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
