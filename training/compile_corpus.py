"""Compile the curated corpus into the training artifact. Pure, and test-gated.

    training/corpus/**.yaml  ──►  training/data/selection.jsonl
                                  training/data/corpus-lock.json

``compile(sources)`` is a FUNCTION. Run it twice and you get the same bytes; run
it after deleting a source file and the rows are gone. Neither was true of the
``assemble_data.py`` this replaces, which took the committed artifact as its own
input (``--base``, on by default) and could therefore only ever grow.

## The gates, and why each one is here

**Contamination.** The 150-odd cases in ``benchmarks/runbook_selection/cases/``
are the held-out evaluation set and the only evidence any of this works.
Generating agents are told not to read them, but "was told not to" is not a
control — two writers inventing a query for "rename my terminals" can easily
land on the same short sentence. So overlap is MEASURED, not assumed away, and
the gate covers HISTORY as well as the query, because history is part of the
rendered model input and a benchmark message pasted into a generated history
leaks exactly as much.

**Conflicting labels.** Rows are keyed on the SERVING render of their input —
the exact string the model trains on — plus the active set. Two rows with the
same rendered input and different answers are a contradiction the gradients
cannot resolve, so the first wins and the rest are COUNTED, which is how a noisy
generator shows up in a report instead of in the weights.

**Unknown ids.** Validated before a row can claim a dedupe key. Recording the
key first let an unknown-id row reserve a slot, get dropped, and take the next
valid row for the same input down with it as a duplicate.

**Foundation unpairing.** The backend pairs a domain foundation with the
specific it selected, downstream of the classifier. A row labelling both teaches
the classifier to do a job something else already does, so the foundation is
stripped from any multi-id answer — before the dedupe key is computed, or a
"specific+foundation" row and its specific-only twin would not collide.

**Ownership.** A runbook file's rows must select that runbook and no other.
Placement is mechanical (``corpus_source``), so a violation is a mistake rather
than a judgement call, and it fails the build rather than quietly landing a row
where no one will look for it.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
from collections import Counter
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "src"))

from benchmarks.runbook_selection.cases import load_cases  # noqa: E402
from daintree_assistant_server.runbooks.catalog import (  # noqa: E402
    default_runbooks_dir,
    load_catalog,
)
from training.common import render_training_query  # noqa: E402
from training.corpus_source import (  # noqa: E402
    CORPUS,
    SOURCE_SECTIONS,
    CorpusError,
    Example,
    source_files,
)

DATA = ROOT / "training" / "data"
CORPUS_OUT = DATA / "selection.jsonl"
LOCK_OUT = DATA / "corpus-lock.json"

LOCK_SCHEMA_VERSION = 1


def norm(text: str) -> str:
    """Normalise a query for duplicate and contamination comparison."""
    return " ".join(re.sub(r"[^a-z0-9]+", " ", text.lower()).split())


def _benchmark_strings() -> set[str]:
    """Protect every evaluation suite, including sealed holdout rounds.

    Read these mechanically for exclusion only. Case text must never be fed to
    a data writer or used as a source of training examples.

    Both host models' suites (``cases.py``, ``host_model``): an inbox-only case is
    held out from training as much as a control one, whichever catalog trains.
    """
    from benchmarks.runbook_selection.cases import CASES_DIR, HOST_MODELS
    from daintree_assistant_server.runbooks.catalog import load_catalogs

    catalogs = load_catalogs()
    out: set[str] = set()
    suites = [CASES_DIR, *sorted((CASES_DIR.parent / "holdout").glob("*/"))]
    for suite in suites:
        for model in HOST_MODELS:
            for case in load_cases(catalogs.for_model(model), cases_dir=suite, model=model):
                for msg in case.messages:
                    if isinstance(msg.content, str):
                        out.add(norm(msg.content))
                if case.turn_context:
                    out.add(norm(case.turn_context))
    out.discard("")
    return out


def _check_ownership(source_file: Any, ex: Example) -> None:
    """Placement is mechanical, so a violation is a mistake, not a judgement call.

    Enforced in BOTH directions. The forward half is obvious — a runbook file may
    only hold rows that select its runbook. The reverse half matters just as
    much and is easier to miss: a single-runbook row sitting in a shared file
    compiles perfectly well and is simply filed where nobody auditing that
    runbook will ever see it. It is only checked for sources that HAVE a runbook
    section, because `no_task`, `off_domain` and `multi_runbook` have nowhere
    else to live.

    ``expect`` is already canonical here — ``corpus_source`` unpairs the domain
    foundation at load — so ``[X, daintree.foundation]`` reads as ``[X]`` and is
    judged on the answer it will actually train, not on its spelling.
    """
    expect = tuple(ex.expect)
    if source_file.runbook is not None:
        if expect != (source_file.runbook,):
            raise CorpusError(
                f"{source_file.path}: a runbook file may only hold rows that select "
                f"{source_file.runbook!r}, but one selects {list(expect)}. "
                f"Rows answering none or several runbooks belong in corpus/shared/."
            )
        return
    if len(expect) == 1 and ex.source in SOURCE_SECTIONS:
        raise CorpusError(
            f"{source_file.path}: {expect[0]!r} is the only answer to "
            f"{ex.query[:60]!r}, so it belongs in corpus/runbooks/{expect[0]}.yaml "
            f"under `{SOURCE_SECTIONS[ex.source]}`, not in a shared file."
        )


def compile_corpus(
    *, corpus_root: Path | None = None, quiet: bool = False
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """Gate every curated example and return (rows, lock)."""
    catalog = load_catalog(default_runbooks_dir())
    valid_ids = set(catalog.selectable_ids)
    held_out = _benchmark_strings()

    stats: Counter[str] = Counter()
    per_file: dict[str, dict[str, Any]] = {}
    seen_inputs: dict[str, tuple[str, ...]] = {}
    rows: list[dict[str, Any]] = []

    def admit(ex: Example) -> bool:
        query = " ".join(ex.query.split())
        if not query:
            stats["dropped_empty"] += 1
            return False

        # `expect` arrives canonical: `corpus_source` unpairs the domain
        # foundation at load, so every consumer — this, the ownership check, and
        # anything reading an `Example` — sees the answer that will be trained.
        expect = list(ex.expect)

        if norm(query) in held_out or any(
            norm(h.partition(":")[2] or h) in held_out for h in ex.history
        ):
            stats["dropped_contaminated"] += 1
            return False

        confusable = [c for c in ex.confusable_with if c not in expect]
        if len(confusable) != len(ex.confusable_with):
            stats["confusable_overlap_stripped"] += 1

        bad = [i for i in [*expect, *ex.active, *confusable] if i not in valid_ids]
        if bad:
            stats["dropped_unknown_id"] += 1
            return False

        input_key = norm(render_training_query(query, list(ex.history))) + "|" + ",".join(ex.active)
        signature = (*sorted(expect), f"docs={ex.is_docs}")
        if input_key in seen_inputs:
            # A CONFLICT and a DUPLICATE are both dropped, but only one of them
            # is a symptom: two rows that render identically and disagree about
            # the answer are a contradiction the gradients cannot resolve, and
            # counting them apart is what makes a noisy generator visible.
            conflict = seen_inputs[input_key] != signature
            stats["dropped_conflicting_label" if conflict else "dropped_duplicate"] += 1
            return False
        seen_inputs[input_key] = signature

        stats[ex.source] += 1
        rows.append({
            "query": query,
            "history": list(ex.history),
            "active_ids": list(ex.active),
            "expect_ids": expect,
            "is_docs": ex.is_docs,
            # Which KIND of empty this row teaches — an acknowledgement and a
            # weather question are both empty but not the same lesson.
            "empty_kind": ex.empty_kind if not expect else None,
            # Authored, not mined: the runbooks a HUMAN judged this confusable
            # with. `train_retriever` unions these with what it mines from the
            # live encoder, because the two answer different questions — what a
            # person thinks is confusable, and what this checkpoint gets wrong.
            "confusable_with": confusable,
            "source": ex.source,
            # Provenance the flat corpus never had: which generation run wrote
            # this row, and which curated file it can be edited in.
            "run": ex.run,
            "origin": ex.origin,
        })
        return True

    # Materialised before anything is read from it, so a malformed file late in
    # the tree is discovered before any of this reports counts for the ones
    # before it. `prune_run` relies on the same property for a different reason.
    for source_file in list(source_files(corpus_root)):
        kept = 0
        for ex in source_file.examples:
            _check_ownership(source_file, ex)
            kept += admit(ex)
        per_file[source_file.path] = {
            "examples": len(source_file.examples),
            "rows": kept,
            "runbook": source_file.runbook,
        }

    lock = _build_lock(rows, per_file, stats, corpus_root, valid_ids)
    if not quiet:
        _report(rows, stats)
    return rows, lock


def _build_lock(
    rows: list[dict[str, Any]],
    per_file: dict[str, dict[str, Any]],
    stats: Counter[str],
    corpus_root: Path | None,
    valid_ids: set[str],
) -> dict[str, Any]:
    base = corpus_root or CORPUS
    for path, entry in per_file.items():
        entry["sha256"] = hashlib.sha256((base / path).read_bytes()).hexdigest()

    return {
        "schema_version": LOCK_SCHEMA_VERSION,
        "generated_by": "python -m training.compile_corpus",
        "rows": len(rows),
        "compiled_sha256": hashlib.sha256(serialise(rows).encode()).hexdigest(),
        # The LABEL SPACE, not the catalog revision. A runbook body edit changes
        # the revision and changes nothing about what this corpus can label, so
        # hashing the revision would fail the corpus check on every prose fix.
        # Which ids exist is the thing a corpus can actually go stale against.
        "label_space_sha256": hashlib.sha256(
            "\n".join(sorted(valid_ids)).encode()
        ).hexdigest(),
        "label_space_size": len(valid_ids),
        "by_source": dict(sorted(Counter(r["source"] for r in rows).items())),
        "by_run": dict(sorted(Counter(r["run"] for r in rows).items())),
        "dropped": {k: v for k, v in sorted(stats.items()) if k.startswith("dropped_")},
        "adjusted": {
            k: v
            for k, v in sorted(stats.items())
            if k in ("foundation_unpaired", "confusable_overlap_stripped")
        },
        "sources": dict(sorted(per_file.items())),
    }


def serialise(rows: list[dict[str, Any]]) -> str:
    return "".join(json.dumps(r) + "\n" for r in rows)


def serialise_lock(lock: dict[str, Any]) -> str:
    return json.dumps(lock, indent=2) + "\n"


def _report(rows: list[dict[str, Any]], stats: Counter[str]) -> None:
    print("compiled:")
    for key, value in sorted(stats.items()):
        print(f"  {key:32} {value}")
    print(f"\n  selection.jsonl  {len(rows)} rows")
    if stats["dropped_contaminated"]:
        print(
            f"\n  NOTE: dropped {stats['dropped_contaminated']} queries colliding "
            f"with the held-out benchmark corpus."
        )


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument(
        "--check",
        action="store_true",
        help="compile and compare against the committed artifact without writing; "
        "exit non-zero if they differ. This is the corpus reproducibility check.",
    )
    args = ap.parse_args()

    rows, lock = compile_corpus(quiet=args.check)
    payload = serialise(rows)

    if args.check:
        committed = CORPUS_OUT.read_text(encoding="utf-8") if CORPUS_OUT.is_file() else ""
        if committed != payload:
            print(
                f"training/data/selection.jsonl is stale: committed {len(committed.splitlines())} "
                f"rows, corpus compiles to {len(rows)}.\n"
                f"Run:  python -m training.compile_corpus",
                file=sys.stderr,
            )
            return 1
        # The lockfile too, or this command does not mean what it says. A
        # comment-only edit to a curated file changes that file's source hash
        # and no compiled row, so checking only the JSONL reports success on a
        # tree whose committed lock no longer describes it.
        committed_lock = LOCK_OUT.read_text(encoding="utf-8") if LOCK_OUT.is_file() else ""
        if committed_lock != serialise_lock(lock):
            print(
                "training/data/corpus-lock.json is stale.\n"
                "Run:  python -m training.compile_corpus",
                file=sys.stderr,
            )
            return 1
        print(f"selection.jsonl and corpus-lock.json match the curated corpus ({len(rows)} rows)")
        return 0

    DATA.mkdir(parents=True, exist_ok=True)
    CORPUS_OUT.write_text(payload, encoding="utf-8", newline="\n")
    LOCK_OUT.write_text(serialise_lock(lock), encoding="utf-8", newline="\n")
    print(f"\nwrote {CORPUS_OUT.relative_to(ROOT)} and {LOCK_OUT.relative_to(ROOT)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
