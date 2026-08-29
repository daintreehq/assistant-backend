"""Remove every curated example a given generation run contributed.

    python -m training.prune_run 2026-08-29-worktrees --dry-run
    python -m training.prune_run 2026-08-29-worktrees
    python -m training.compile_corpus

"Drop everything from the run with the bad prompt" is one of the two questions
the flat corpus could not answer — there was no run id on a row, and the
assembler fed its own output back in, so a bad batch was welded in from the
moment it landed. Recording ``run`` per example is what makes the question
answerable; this is what makes it actionable, because doing it by hand across
forty-odd YAML files is the kind of cleanup that never happens.

It edits the CURATED plane only. The journal stays in ``training/journals/`` —
that plane is the raw record and is append-only, so pruning is a statement about
what the corpus should teach, not a rewriting of what a generator returned. Run
``compile_corpus`` afterwards to rebuild the artifact.

A file emptied of every example is DELETED rather than left as a stub, because
an empty curated file is indistinguishable from a runbook nobody has written
data for yet, and ``compile_corpus`` would have to decide which it was.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "src"))

from training.corpus_source import (  # noqa: E402
    CORPUS,
    source_files,
    write_runbook_file,
    write_shared_file,
)


def prune(run: str, *, root: Path | None = None, dry_run: bool = False) -> dict[str, int]:
    """Drop every example stamped with ``run``. Returns removals per source file.

    Separated from ``main`` so it is testable against a throwaway corpus: this
    is the one code path in the curated plane that DELETES, and verifying it by
    running it on the real tree is not a test anybody would repeat.
    """
    base = root or CORPUS
    removed: dict[str, int] = {}

    # The WHOLE tree is loaded and validated before anything is written. Mutating
    # while the loader is still walking meant a malformed file late in the tree
    # left the earlier ones already rewritten and the run half-pruned.
    for source_file in list(source_files(base)):
        keep = [ex for ex in source_file.examples if ex.run != run]
        gone = len(source_file.examples) - len(keep)
        if not gone:
            continue
        removed[source_file.path] = gone
        if dry_run:
            continue

        target = base / source_file.path
        if not keep:
            # Deleted rather than left as a stub: an empty curated file is
            # indistinguishable from a runbook nobody has written data for yet,
            # and the loader would have to decide which it was.
            target.unlink()
            continue
        # File-level defaults are PRESERVED, never recomputed from what remains.
        # Recomputing lets a deletion flip a majority and re-key every surviving
        # row, turning a prune into a whole-file rewrite.
        if source_file.runbook is not None:
            write_runbook_file(target, source_file.runbook, keep, run=source_file.run)
        else:
            write_shared_file(
                target,
                keep[0].source,
                keep,
                run=source_file.run,
                docs=source_file.docs,
                empty_kind=source_file.empty_kind,
            )
    return removed


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("run", help="the run id to remove, as stamped by ingest_journal --run")
    ap.add_argument(
        "--dry-run",
        action="store_true",
        help="report what would go without writing anything",
    )
    args = ap.parse_args()

    removed = prune(args.run, dry_run=args.dry_run)
    total = sum(removed.values())
    if not total:
        print(f"no examples carry run {args.run!r} — nothing to do")
        return 0

    for path, count in sorted(removed.items()):
        print(f"  {count:5d}  {path}")
    verb = "would remove" if args.dry_run else "removed"
    print(f"\n{verb} {total} example(s) across {len(removed)} file(s)")
    if not args.dry_run:
        print("\nNow run:  python -m training.compile_corpus")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
