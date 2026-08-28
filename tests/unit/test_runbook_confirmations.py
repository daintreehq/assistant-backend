"""The runbook confirmation gate — what speaks, what is written off, what stays quiet.

The mechanism replaces the Cerebras fast-preamble stage, so the properties worth
pinning are the ones that made that stage safe: the line is shown at most once,
it is never shown for something the turn merely tolerated, and nothing here can
fail a turn.
"""

from __future__ import annotations

import pytest

from daintree_assistant_server.runbooks.catalog import Catalog
from daintree_assistant_server.runbooks.confirmations import (
    CAP_ANNOUNCED_RUNBOOKS,
    choose_opener,
    fold_announced,
)
from daintree_assistant_server.runbooks.models import Runbook, RunbookMeta

THRESHOLD = 0.90


def _runbook(rid: str, *, confirmation: str | None = None, foundation: bool = False) -> Runbook:
    return Runbook(
        meta=RunbookMeta(
            id=rid,
            title=rid,
            summary="s",
            whenToUse="w",
            confirmation=confirmation,
            foundation=foundation,
        ),
        body="body\n\nReport back.\n",
    )


def _catalog(*runbooks: Runbook) -> Catalog:
    return Catalog(runbooks)


def _choose(**kwargs: object):  # type: ignore[no-untyped-def]
    base: dict[str, object] = {
        "scored_active_ids": [],
        "scores": {},
        "already_announced": [],
        "threshold": THRESHOLD,
    }
    base.update(kwargs)
    return choose_opener(**base)  # type: ignore[arg-type]


# ---- the happy path -------------------------------------------------------


def test_a_confident_first_load_speaks() -> None:
    catalog = _catalog(_runbook("a.one", confirmation="I'll take a look."))
    decision = _choose(
        scored_active_ids=["a.one"], scores={"a.one": 0.97}, catalog=catalog
    )
    assert decision.text == "I'll take a look."
    assert decision.runbook_id == "a.one"
    assert decision.announced == ("a.one",)


def test_the_threshold_is_inclusive() -> None:
    """A runbook landing exactly on the floor speaks.

    The floor is a documented product number, not a strict inequality nobody
    chose — a runbook at exactly the configured confidence should behave like the
    configured confidence, rather than racing its own boundary in a float.
    """
    catalog = _catalog(_runbook("a.one", confirmation="hi"))
    assert _choose(
        scored_active_ids=["a.one"], scores={"a.one": THRESHOLD}, catalog=catalog
    ).text == "hi"


# ---- the three suppression rules -----------------------------------------


def test_a_near_miss_is_neither_shown_nor_written_off() -> None:
    """The rule that makes the floor a statement about CERTAINTY.

    A runbook loaded below the floor keeps its confirmation for a later turn that
    actually commits to the topic. Writing it off here would let one vague
    mention burn the announcement.
    """
    catalog = _catalog(_runbook("a.one", confirmation="hi"))
    decision = _choose(
        scored_active_ids=["a.one"], scores={"a.one": 0.80}, catalog=catalog
    )
    assert decision.text is None
    assert decision.announced == ()


def test_an_already_announced_runbook_stays_quiet() -> None:
    catalog = _catalog(_runbook("a.one", confirmation="hi"))
    decision = _choose(
        scored_active_ids=["a.one"],
        scores={"a.one": 1.0},
        already_announced=["a.one"],
        catalog=catalog,
    )
    assert decision.text is None
    assert decision.announced == ()


def test_only_the_top_speaks_and_the_rest_are_written_off() -> None:
    """Both halves matter and they are separable.

    Showing one is about not stacking server-authored sentences; SPENDING the
    others is about not announcing, three rounds into the work, a runbook that
    was live when the user was first told what the turn was about.
    """
    catalog = _catalog(
        _runbook("a.one", confirmation="first"),
        _runbook("a.two", confirmation="second"),
        _runbook("a.three", confirmation="third"),
    )
    decision = _choose(
        scored_active_ids=["a.two", "a.one", "a.three"],
        scores={"a.one": 0.99, "a.two": 0.95, "a.three": 0.93},
        catalog=catalog,
    )
    assert decision.text == "first"
    assert sorted(decision.announced) == ["a.one", "a.three", "a.two"]
    assert sorted(decision.suppressed) == ["a.three", "a.two"]


def test_a_tie_is_broken_deterministically() -> None:
    catalog = _catalog(_runbook("a.zzz", confirmation="z"), _runbook("a.aaa", confirmation="a"))
    for order in (["a.zzz", "a.aaa"], ["a.aaa", "a.zzz"]):
        decision = _choose(
            scored_active_ids=order,
            scores={"a.zzz": 1.0, "a.aaa": 1.0},
            catalog=catalog,
        )
        assert decision.runbook_id == "a.aaa"


# ---- who has no score, and therefore no voice ----------------------------


def test_an_unscored_runbook_never_speaks() -> None:
    """Covers a paired domain foundation AND the server-pinned docs runbook.

    Neither was ranked, so neither appears in ``scores`` — and neither is
    something the user asked for by name. Requiring a score is what expresses
    that without this module knowing how either of them got into the set.
    """
    catalog = _catalog(
        _runbook("a.one", confirmation="specific"),
        _runbook("a.base", confirmation="foundation", foundation=True),
    )
    decision = _choose(
        scored_active_ids=["a.one", "a.base"],
        scores={"a.one": 0.99},
        catalog=catalog,
    )
    assert decision.text == "specific"
    assert decision.announced == ("a.one",)


def test_a_reused_active_set_announces_nothing() -> None:
    """A continuation round: nothing newly loaded, and no scores to rank.

    This is the shape of a tool-continuation, a fail-open and a background wake
    all at once — none of them ran the selector, so ``scores`` is empty and the
    gate falls out with no special case for any of them.
    """
    catalog = _catalog(_runbook("a.one", confirmation="hi"))
    assert _choose(scored_active_ids=[], scores={}, catalog=catalog).text is None


def test_a_runbook_with_no_opener_is_silent_and_not_written_off() -> None:
    """Silence costs nothing, so it must not cost a token entry either."""
    catalog = _catalog(_runbook("a.one"))
    decision = _choose(
        scored_active_ids=["a.one"], scores={"a.one": 1.0}, catalog=catalog
    )
    assert decision.text is None
    assert decision.announced == ()


def test_a_score_for_an_unknown_id_cannot_speak() -> None:
    catalog = _catalog(_runbook("a.one", confirmation="hi"))
    decision = _choose(
        scored_active_ids=["a.gone"], scores={"a.gone": 1.0}, catalog=catalog
    )
    assert decision.text is None


# ---- the announced set ----------------------------------------------------


def test_fold_is_append_only_and_ordered_oldest_first() -> None:
    assert fold_announced(["a", "b"], ["c"]) == ["a", "b", "c"]


def test_folding_an_id_it_already_holds_does_not_move_it() -> None:
    """Re-appending would make the OLDEST entry the last one the cap evicts."""
    assert fold_announced(["a", "b", "c"], ["a"]) == ["a", "b", "c"]


def test_the_byte_budget_bounds_the_token_the_count_cap_cannot() -> None:
    """The count cap alone lets the server issue a token it would REJECT.

    A runbook id is bounded only by ``RunbookMeta``'s 256-character ceiling, so
    64 of them encode to ~26 KiB against the codec's 16 KiB ``MAX_TOKEN_BYTES``.
    The server would then have minted a token that 413s on the caller's very next
    round, naming the client's own state field for a limit the server chose.
    Today's ids are ~46 characters so this is a cliff rather than a live fault —
    which is exactly why it is bounded here rather than left to the catalog.
    """
    from daintree_assistant_server.state.codec import MAX_TOKEN_BYTES, StateCodec

    long_ids = [("x" * 250) + str(i) for i in range(CAP_ANNOUNCED_RUNBOOKS)]
    folded = fold_announced([], long_ids)
    assert len(folded) < CAP_ANNOUNCED_RUNBOOKS, "the byte budget must bite first"

    codec = StateCodec(
        current_key=b"k" * 32, current_kid="kid", salt=b"s", ttl_seconds=3600
    )
    token = codec.issue(
        session_id="s",
        active_runbooks=long_ids[:3],
        catalog_revision="sha256:" + "a" * 64,
        turn_id="t" * 64,
        instruction_revision=999_999,
        docs_query="q" * 120,
        profile="subagent",
        announced_runbooks=folded,
    )
    assert len(token.encode("utf-8")) <= MAX_TOKEN_BYTES, (
        "the server issued a state token it would itself refuse as oversized"
    )


def test_the_byte_budget_does_not_touch_the_real_catalog() -> None:
    """A bound that trimmed real traffic would be a bug, not a guard."""
    from daintree_assistant_server.runbooks.catalog import load_catalog

    ids = [r.id for r in load_catalog().runbooks]
    assert fold_announced([], ids) == ids


def test_the_cap_drops_the_oldest() -> None:
    prior = [f"r{i}" for i in range(CAP_ANNOUNCED_RUNBOOKS)]
    folded = fold_announced(prior, ["new"])
    assert len(folded) == CAP_ANNOUNCED_RUNBOOKS
    assert folded[-1] == "new"
    assert "r0" not in folded
    assert folded[0] == "r1"


# ---- the packaged catalog -------------------------------------------------


def test_every_packaged_opener_is_reachable_by_the_gate() -> None:
    """A confirmation on a runbook the gate can never score is dead text.

    A FOUNDATION is paired in by admission rather than ranked, and a
    SERVER-PINNED runbook is hidden from the selector entirely, so neither can
    ever carry a score — a confirmation on one would ship, cost bytes in every
    content hash, and never once be shown.
    """
    from daintree_assistant_server.config import DOCS_RUNBOOK_ID
    from daintree_assistant_server.runbooks.catalog import load_catalog

    catalog = load_catalog()
    for runbook in catalog.runbooks:
        if runbook.confirmation is None:
            continue
        assert not runbook.meta.foundation, (
            f"{runbook.id} is a domain foundation and carries a confirmation, which can "
            "never be shown: admission pairs a foundation in without a score"
        )
        assert runbook.id != DOCS_RUNBOOK_ID, (
            f"{runbook.id} is server-pinned and hidden from the selector, so it is "
            "never scored and its confirmation can never be shown"
        )


@pytest.mark.parametrize("threshold", [0.0, 0.5, 0.9, 1.0])
def test_the_gate_is_total_over_the_packaged_catalog(threshold: float) -> None:
    """It runs inline on the first-byte path, so it may not raise. Ever."""
    from daintree_assistant_server.runbooks.catalog import load_catalog

    catalog = load_catalog()
    ids = [r.id for r in catalog.runbooks]
    decision = choose_opener(
        scored_active_ids=[*ids, "not.a.runbook"],
        scores=dict.fromkeys(ids, 0.95),
        already_announced=[],
        catalog=catalog,
        threshold=threshold,
    )
    assert decision.text is None or decision.text in {r.confirmation for r in catalog.runbooks}


def test_a_near_miss_recovers_even_while_it_stays_active() -> None:
    """The other half of the near-miss rule, and the half that was broken.

    Eligibility used to be the turn's NEWLY LOADED ids. A runbook admitted at
    0.80 stays in the active set, so a later instruction re-scoring it at 0.97
    found it absent from that list and its caption was suppressed permanently —
    the exact opposite of what a floor that "keeps its confirmation for a later turn"
    is supposed to mean. Reading the SCORED ACTIVE set instead is what makes the
    documented rule true.
    """
    catalog = _catalog(_runbook("a.one", confirmation="hi"))
    # Turn one: admitted, below the floor, nothing shown or spent.
    first = _choose(
        scored_active_ids=["a.one"], scores={"a.one": 0.80}, catalog=catalog
    )
    assert first.text is None and first.announced == ()
    # Turn two: still active — so NOT newly loaded — and now confident.
    second = _choose(
        scored_active_ids=["a.one"],
        scores={"a.one": 0.97},
        already_announced=list(first.announced),
        catalog=catalog,
    )
    assert second.text == "hi"


def test_a_nan_score_cannot_clear_the_floor() -> None:
    """A NaN fails every comparison, so ``score < threshold`` is False for one.

    Left unguarded it clears the floor it is least entitled to clear, and then
    sorts ahead of a legitimate winner and writes it off unheard — the caption is
    lost AND spent. Nothing observed produces a NaN; the guard costs one call and
    makes the floor mean what it says.
    """
    catalog = _catalog(
        _runbook("a.nan", confirmation="from nowhere"), _runbook("a.real", confirmation="earned")
    )
    decision = _choose(
        scored_active_ids=["a.nan", "a.real"],
        scores={"a.nan": float("nan"), "a.real": 0.97},
        catalog=catalog,
    )
    assert decision.text == "earned"
    assert decision.announced == ("a.real",)


@pytest.mark.asyncio
async def test_the_real_selector_reports_a_score_per_admitted_runbook() -> None:
    """The bridge the integration tests cannot see.

    `ScriptedSelector` fills `scores` with one scalar for every selected id, so a
    production regression that returned `{}` — or copied the top confidence to
    every runbook — would leave every confirmation integration test green while the
    gate lost its per-runbook input. Only a test against `LocalSelector` itself
    catches that, so this asserts the two properties the gate actually depends
    on: every admitted id HAS a score, and the scores are read from the ballot
    rather than being one repeated number.
    """
    from types import SimpleNamespace

    from daintree_assistant_server.contracts.openai import ChatMessage
    from daintree_assistant_server.runbooks.local.selector import LocalSelector
    from daintree_assistant_server.runbooks.local.setwise import Selection

    catalog = _catalog(
        _runbook("a.one", confirmation="Mode one — a boundary worth stating here"),
        _runbook("a.two", confirmation="Mode two — a different boundary stated here"),
    )
    selection = Selection(
        runbook_ids=["a.one", "a.two"],
        docs_wanted=False,
        cardinality=2,
        scores={"a.one": 0.97},
        ranked=(("a.one", 0.97), ("a.two", 0.61)),
    )

    class _Pool:
        async def run(self, fn: object) -> Selection:
            return selection

    selector = LocalSelector(
        classifier=SimpleNamespace(query_fits=lambda _t: True, select=lambda **_k: selection),
        pool=_Pool(),  # type: ignore[arg-type]
        catalog_manager=SimpleNamespace(catalog=catalog),  # type: ignore[arg-type]
        settings=SimpleNamespace(max_active_runbooks=3),  # type: ignore[arg-type]
    )
    decision = await selector.select(
        [ChatMessage(role="user", content="do the thing")], [], catalog=catalog
    )

    assert set(decision.scores) == {"a.one", "a.two"}, (
        "every admitted runbook must carry a score, or the gate silently skips it"
    )
    assert decision.scores["a.one"] == pytest.approx(0.97)
    # Read from the BALLOT, not from `confidence`. `scores` is deliberately wider
    # than the top-five `Selection.scores` log line for the same reason.
    assert decision.scores["a.two"] == pytest.approx(0.61)
