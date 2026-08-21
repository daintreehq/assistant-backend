"""Guards on the training corpus that only fail LOUDLY here.

Both of these protect against silent damage — a corpus that still parses, still
trains, and still exports, while the lesson it was supposed to teach is gone.
Neither is a model-quality assertion; they are structural, offline, and cost
nothing, which is the only reason a check on `training/` belongs in the shipped
test suite at all.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

from benchmarks.skill_selection.cases import load_cases
from daintree_assistant_server.skills.catalog import default_skills_dir, load_catalog

REPO_ROOT = Path(__file__).resolve().parents[2]
CORPUS = REPO_ROOT / "training" / "data" / "selection.jsonl"


def _norm(text: str) -> str:
    """``assemble_data._norm``, duplicated deliberately.

    Importing it would make this test depend on `training/` being importable,
    which it is not in every environment the suite runs in. The function is four
    tokens long and its job is to be stable; if it ever changes, this guard going
    green on a stale copy is a smaller risk than the guard not running at all.
    """
    return " ".join(re.sub(r"[^a-z0-9]+", " ", text.lower()).split())


def _benchmark_strings() -> set[str]:
    out: set[str] = set()
    for case in load_cases(load_catalog(default_skills_dir())):
        for msg in case.messages:
            if isinstance(msg.content, str):
                out.add(_norm(msg.content))
        if case.turn_context:
            out.add(_norm(case.turn_context))
    out.discard("")
    return out


@pytest.mark.skipif(not CORPUS.is_file(), reason="training corpus not present")
def test_no_query_carries_both_documentation_labels() -> None:
    """The exact bug that produced a near-total documentation-lookup outage.

    Three assembler branches (`no_task_stateful`, `topic_shift`, `wake`) hardcode
    `is_docs=False` for every row they emit, so a product question that happened
    to arrive with history was trained as NOT a docs turn — while the SAME
    question, asked cold, sat in the docs bundle labelled True. Sixty-four of
    those contradictions were in the corpus at once, and the head did the
    rational thing with a perfectly-separable signal: it learned that context
    means "not documentation", and answered a confident no.

    Absence of docs positives with context is a gap and trains slowly. A
    CONTRADICTION trains fast and in the wrong direction, which is why this is
    asserted rather than merely counted.
    """
    labels: dict[str, set[bool]] = {}
    for line in CORPUS.read_text().splitlines():
        if not line.strip():
            continue
        row = json.loads(line)
        labels.setdefault(_norm(row["query"]), set()).add(bool(row["is_docs"]))
    conflicting = sorted(q for q, seen in labels.items() if len(seen) > 1)
    assert not conflicting, (
        f"{len(conflicting)} query/queries carry BOTH is_docs labels: "
        f"{conflicting[:3]}. Resolve in training/augment_context.py, which "
        f"propagates the docs bundle's label and asserts this same invariant."
    )


@pytest.mark.skipif(not CORPUS.is_file(), reason="training corpus not present")
def test_a_benchmark_greeting_never_collides_with_a_corpus_greeting() -> None:
    """A booby trap for whoever adds the next greeting-shaped benchmark case.

    `assemble_data`'s contamination gate drops any training row whose query OR
    HISTORY matches a held-out benchmark string — correctly, since a benchmark
    message pasted into a generated history is exactly as much leakage as one in
    the query. But the social lead-in rows carry their greeting IN the history,
    so wording a benchmark greeting the same way as a corpus one deletes the
    rows that teach the lesson the case is checking. The gate only prints a
    non-failing note, the corpus still trains, and the case simply starts
    failing for a reason nobody can see in the diff.

    So the two vocabularies must stay disjoint, and that is asserted here rather
    than left as a comment somebody has to find.
    """
    social = pytest.importorskip(
        "training.augment_context", reason="training/ not importable here"
    ).SOCIAL
    held_out = _benchmark_strings()
    collisions = sorted(
        {_norm(s) for pair in social for s in pair} & held_out
    )
    assert not collisions, (
        f"greeting(s) used by BOTH the corpus augmentation and a benchmark case: "
        f"{collisions}. Reword the benchmark case — matching a corpus greeting "
        f"silently drops every social lead-in row that carries it."
    )


# ---------------------------------------------------------------------------
# Shortcut cross-tabs.
#
# The docs head learned "is anything in the context?" instead of "is this a
# documentation question?" — 5/120 recall on docs turns with a skill loaded —
# because the corpus made context a lossless negative signal and the benchmark
# shared the assumption. It cost a live outage of documentation lookup and a
# workaround that still runs today: a whole second pass over the bare question,
# ~8ms, because the head cannot be trusted to read that logit off a context-laden
# query (`setwise.py`).
#
# The general rule, and the only defence against the next one:
#
#   A feature is safe only when the training data shows it across ALL relevant
#   labels in realistic proportions.
#
# These cross-tab the structural features against the labels and fail when one
# gets close to predicting the other. They are counting, not modelling — cheap,
# offline, and they would have caught the original.
# ---------------------------------------------------------------------------


def _corpus_rows() -> list[dict]:
    return [
        json.loads(line)
        for line in CORPUS.read_text().splitlines()
        if line.strip()
    ]


def _rate(rows: list[dict], present: bool, label) -> tuple[float, int]:  # type: ignore[no-untyped-def]
    subset = [r for r in rows if label(r) is present]
    if not subset:
        return 0.0, 0
    return sum(1 for r in subset if r.get("is_docs")) / len(subset), len(subset)


@pytest.mark.skipif(not CORPUS.is_file(), reason="training corpus not present")
def test_history_does_not_predict_the_documentation_label() -> None:
    """The repaired half of the original shortcut.

    In the 8,640-row corpus this was written against, every one of the 207 docs
    positives had NO history: 0 of 2,116 with, 207 of 6,524 (3.17%) without. The
    risk ratio was infinite and the head took the free answer. The augmentation
    added history-bearing docs rows; this is the assertion that they stay.

    Measured today: 6.49% with history against 4.44% without, a ratio of 1.46.
    """
    rows = _corpus_rows()
    with_history, n_with = _rate(rows, True, lambda r: bool(r.get("history")))
    without, n_without = _rate(rows, False, lambda r: bool(r.get("history")))
    assert n_with > 500 and n_without > 500, "too few rows either side to judge"
    # Positives on BOTH sides, not just a comfortable ratio. A corpus with zero
    # docs rows anywhere scores a ratio of 0 and would sail through the bound
    # below while teaching the head nothing at all.
    assert with_history > 0.0 and without > 0.0, "one side carries no docs positives"
    # Symmetric, because the shortcut is just as harmful reversed: "history
    # present implies docs" is the same free answer wearing the other sign.
    ratio = max(with_history, without) / min(with_history, without)
    assert ratio < 2.0, (
        f"documentation label is {ratio:.2f}x more likely on one side of "
        f"'history present' ({with_history:.4f} with, {without:.4f} without) — "
        f"the head can learn the structural feature instead of the question"
    )


@pytest.mark.skipif(not CORPUS.is_file(), reason="training corpus not present")
def test_the_active_set_remains_a_KNOWN_shortcut_and_does_not_worsen() -> None:
    """The half that was NOT repaired, ratcheted at its current size.

    Measured on today's corpus: a documentation positive is 17.58x rarer on a row
    with an active skill (0.358%, 7 of 1,956) than without (6.293%, 492 of
    7,818). That is the same shape as the history shortcut on the other axis, and
    it is why the bare-question second pass in ``setwise.py`` is still
    load-bearing rather than a historical note — with only seven active-bearing
    positives the head is severely underexposed to that combination, so the
    contextual query is not where its docs logit can be read.

    The ceiling is 20x, not a round 25x, and the difference is the whole point:
    at these denominators, losing ONE of the seven positives gives 20.5x and
    fails. That makes it a ratchet rather than a permission slip for a 50%
    regression.

    Closing it is corpus work — docs questions asked mid-workflow, with one and
    two skills loaded (`training/EXPERIMENTS.md`). Until someone does it, this is
    the record of how far there is to go.
    """
    rows = _corpus_rows()
    with_active, n_with = _rate(rows, True, lambda r: bool(r.get("active_ids")))
    without, _ = _rate(rows, False, lambda r: bool(r.get("active_ids")))
    assert n_with > 500, "too few active-bearing rows to judge"
    positives = sum(1 for r in rows if r.get("active_ids") and r.get("is_docs"))
    assert positives >= 7, (
        f"only {positives} docs positives carry an active set — the ratio below "
        f"is measured against a shrinking numerator"
    )
    ratio = max(with_active, without) / max(min(with_active, without), 1e-9)
    assert ratio < 20.0, (
        f"documentation positives are now {ratio:.1f}x rarer with an active set "
        f"than without ({with_active:.4f} vs {without:.4f}) — this was 17.6x when "
        f"the guard was written and the fix is docs rows that carry an active "
        f"set, not a wider threshold here"
    )


@pytest.mark.skipif(not CORPUS.is_file(), reason="training corpus not present")
def test_the_active_set_does_not_predict_how_many_skills_a_turn_needs() -> None:
    """The cardinality head reads the same rows; it must not learn the same trick.

    A corpus where "something is loaded" implied a particular count would give it
    a lossless shortcut of exactly the kind the docs head took — and cardinality
    is the more damaging place for one, because a wrong count of zero drops every
    skill the turn scored, however confidently (see the reserved-candidate sweep
    in ``skills/local/candidates.py``).

    Compared as DISTRIBUTIONS. An earlier version put independent floors on each
    side and never compared them, which a corpus of 98% zero-count active rows
    against 88% one-count inactive rows would have passed cleanly.
    """
    rows = _corpus_rows()
    shares = {}
    for present in (True, False):
        counts = [
            min(len(r.get("expect_ids") or []), 3)
            for r in rows
            if bool(r.get("active_ids")) is present
        ]
        shares[present] = {n: counts.count(n) / len(counts) for n in range(4)}

    # Total-variation distance: half the summed absolute difference, so 0 is
    # identical and 1 is disjoint. Measured 0.016.
    tv = 0.5 * sum(abs(shares[True][n] - shares[False][n]) for n in range(4))
    assert tv < 0.05, (
        f"cardinality distributions differ by {tv:.3f} between rows with and "
        f"without an active set: {shares[True]} vs {shares[False]}"
    )

    # And class 3 is a KNOWN hole rather than a covered case: 14 rows without an
    # active set, 0 with. Left as a note rather than a failure because three
    # skills is already at `max_active_skills` and the combination may simply not
    # arise — but recorded, so it is a decision rather than an oversight.
    assert shares[True][3] == 0.0 and shares[False][3] > 0.0, (
        "three-skill coverage has moved; decide whether active rows now need it"
    )
