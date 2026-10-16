"""What survives the tokenizer — measured against the real one, before it matters.

`query_document` builds a string. `SetwiseClassifier` then truncates it at 256
tokens, and everything past that is simply not evidence. The string builder
cannot see the cap and the cap cannot see the string builder, so the seam between
them is invisible in both files and in every accuracy number: a query whose tail
was cut still scores, it just scores on less.

`fit_query` closes it, and these tests are what says so. They use the SHIPPED
tokenizer rather than a character heuristic, because no character budget can
stand in for one: 320 characters of one unbroken word is 3 tokens, the same 320
characters of path-like tool output is 320, and real benchmark queries run from
1.25 to 5.20 characters per token.

That 100x spread is not a curiosity, it is the whole reason this file exists. The
first version of these tests padded history with ``"c" * 320`` and concluded the
newest instruction always survived. It does not: with token-dense history — a
file listing, a stack trace, a diff — three prior messages inside the SAME 320
character budget push the instruction out entirely. The fixture was measuring the
tokenizer's handling of one long nonsense word.
"""

from __future__ import annotations

import itertools
from dataclasses import dataclass
from pathlib import Path

import pytest

from benchmarks.runbook_selection.cases import load_cases
from daintree_assistant_server.runbooks.catalog import default_runbooks_dir, load_catalog
from daintree_assistant_server.runbooks.local.documents import (
    ACTIVE_PREFIX,
    QUERY_HISTORY_CHARS,
    fit_query,
    query_document,
)
from daintree_assistant_server.runbooks.local.setwise import MAX_TOKENS, SetwiseClassifier

ARTIFACT = (
    Path(__file__).resolve().parents[2]
    / "src" / "daintree_assistant_server" / "runbooks" / "local" / "artifacts" / "selector"
)

pytestmark = pytest.mark.skipif(
    not (ARTIFACT / "retriever" / "tokenizer.json").is_file(),
    reason="selector model not fetched (training/fetch_selector.sh)",
)

#: Non-repeating prose. Repeated text tokenizes far more efficiently than real
#: writing, so a fixture built from it reports a threshold twice too generous.
_WORDS = [
    "investigate", "the", "failing", "deployment", "pipeline", "because",
    "staging", "rejected", "the", "migration", "and", "rollback", "left",
    "orphaned", "records", "in", "three", "shards",
]


def _prose(words: int) -> str:
    return " ".join(itertools.islice(itertools.cycle(_WORDS), words))


@dataclass
class _Msg:
    role: str
    content: str


@pytest.fixture(scope="module")
def live():  # type: ignore[no-untyped-def]
    """The real classifier and catalog. Module-scoped — the tokenizers are the
    only expensive part and every test here wants the same ones."""
    clf = SetwiseClassifier(ARTIFACT)
    return clf, load_catalog(default_runbooks_dir())


def _survives(clf: SetwiseClassifier, text: str, needle: str) -> bool:
    """Is ``needle`` still present after the encoder's cap has been applied?

    Encoded with the SERVING tokenizer (capped) and decoded with the counting
    one (uncapped), so this reads exactly what the model received. The decoder
    normalises case and spacing, which is why needles here are lowercase and
    written the way it renders them.
    """
    return needle in clf._counter_tok.decode(clf._retriever_tok.encode(text).ids)


# --------------------------------------------------------------------------
# what the fitter guarantees
# --------------------------------------------------------------------------


def test_every_real_benchmark_query_fits_without_shrinking(live) -> None:  # type: ignore[no-untyped-def]
    """Nothing the model has ever been judged on reaches the shrink path.

    This is what makes the fitter safe to land without a retrain: the default
    render is returned byte-identical whenever it fits, and every case in the
    corpus fits with room. If one did not, its recorded score would have been
    measured on a query the model only partly received.
    """
    clf, catalog = live
    over = []
    for case in load_cases(catalog):
        visible = [s for s in case.active if s in catalog.selectable_ids]
        prefix = ACTIVE_PREFIX.format(", ".join(visible)) if visible else ""
        plain = query_document(case.messages, turn_context=case.turn_context)
        if not clf.query_fits(prefix + plain):
            over.append(case.id)
        # ...and the fitter is a no-op on it, to the byte.
        fitted = fit_query(
            case.messages, turn_context=case.turn_context,
            fits=clf.query_fits, prefix=prefix,
        )
        assert fitted == plain, f"{case.id}: fitter changed a query that already fit"
    assert not over, f"cases over the {MAX_TOKENS}-token cap: {over}"


def test_the_newest_instruction_survives_token_dense_history(live) -> None:  # type: ignore[no-untyped-def]
    """The defect the first draft of this file asserted was impossible.

    ``query_document`` renders prior turns BEFORE the newest message and the
    tokenizer truncates from the right, so history dense enough to fill the cap
    pushes the actual instruction out. 320 characters of path-like output is 320
    tokens; three prior messages share that one budget and still reach it.
    """
    clf, _ = live
    dense = ("a/b:c-d_e." * 40)[:QUERY_HISTORY_CHARS]
    messages = [
        _Msg("user", dense),
        _Msg("assistant", dense),
        _Msg("tool", dense),
        _Msg("user", "rename every terminal so I can tell the five agents apart"),
    ]
    plain = query_document(messages)
    assert not _survives(clf, plain, "rename every terminal"), (
        "fixture no longer reproduces the defect — pick denser history"
    )
    assert _survives(
        clf, fit_query(messages, fits=clf.query_fits), "rename every terminal"
    )


def test_turn_context_survives_a_long_user_message(live) -> None:  # type: ignore[no-untyped-def]
    """The other half. The turn context is rendered LAST, so it goes first.

    It carries the current goal and the top workflow digest
    (``ChatOrchestrator._selector_turn_context``) — the signal a resumed workflow
    routes on — and it was being deleted with no error anywhere, because a
    truncated query still scores.
    """
    clf, _ = live
    context = "current goal: rebuild staging | top active workflow — status: waiting"
    messages = [_Msg("user", _prose(400))]

    assert not _survives(
        clf, query_document(messages, turn_context=context), "rebuild staging"
    ), "fixture no longer overflows — lengthen the message"
    assert _survives(
        clf,
        fit_query(messages, turn_context=context, fits=clf.query_fits),
        "rebuild staging",
    )


def test_an_over_long_message_keeps_BOTH_of_its_ends(live) -> None:  # type: ignore[no-untyped-def]
    """Head and tail, because either can hold the actual request.

    Right-truncation keeps the opening and drops the close, which is the wrong
    half for "here is the log: ... so why is it failing?". Keeping both ends
    costs the middle, which is the part that is context for either reading.
    """
    clf, _ = live
    opening = "rename the terminals and tell me which are stuck"
    closing = "and do the git worktree cleanup afterwards"
    messages = [_Msg("user", f"{opening} {_prose(400)} {closing}")]

    fitted = fit_query(messages, fits=clf.query_fits)
    assert _survives(clf, fitted, "rename the terminals")
    assert _survives(clf, fitted, "worktree cleanup afterwards")


def test_the_loaded_prefix_is_counted_against_the_cap(live) -> None:  # type: ignore[no-untyped-def]
    """The prefix is prepended downstream, so the fitter has to count it itself.

    Ignoring it would let a three-runbook active set push the query back over the
    cap after the fitter had declared it safe — and the prefix is the only place
    the active set enters the QUERY, so losing its tail is losing the state.
    """
    clf, _ = live
    prefix = ACTIVE_PREFIX.format(
        "daintree.terminals.rename, daintree.foundation, "
        "daintree.worktree.create-plain-worktree"
    )
    messages = [_Msg("user", _prose(400))]
    fitted = fit_query(messages, fits=clf.query_fits, prefix=prefix)
    assert clf.query_fits(prefix + fitted)


def test_the_fitter_gives_up_gracefully_rather_than_looping(live) -> None:  # type: ignore[no-untyped-def]
    """A turn context alone can exceed the cap, and there is nothing to trade.

    It returns the smallest render and lets the encoder truncate — a hard cut is
    still better than refusing to rank, which is what an exception here would
    become once ``InferencePool`` turned it into a degrade-open.
    """
    clf, _ = live
    huge = _prose(2000)
    fitted = fit_query([_Msg("user", "go")], turn_context=huge, fits=clf.query_fits)
    assert fitted  # no exception, no empty string
