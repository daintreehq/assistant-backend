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
