"""Fold a generation run's raw journal into the curated corpus.

    training/journals/<run>.jsonl  ──►  training/corpus/**.yaml

This is the ONLY writer of curated files other than a human with an editor, and
it is deliberately the smaller half of what ``assemble_data.py`` used to do.
That script read journals AND the committed artifact, applied every gate, and
wrote ``selection.jsonl`` — which made the artifact its own input and made
"regenerate" mean "append". Here the two halves are split: this one lands new
examples in the source plane, and ``compile_corpus`` turns the source plane into
the artifact. Deleting a bad run now means deleting its rows from a YAML file
and recompiling, which the old shape could not express at all.

**Ingesting the same journal twice is a no-op.** An example already present with
the same query, history, active set and answer is skipped and counted. The old
pipeline had the opposite property — the retired ``augment_context.py`` measured
a second application taking the corpus from 9,903 rows to 10,405 — and shipped a
one-shot guard against itself rather than being safe to re-run.

**The journal is archived, not consumed.** ``training/journals/`` is committed
and append-only: it is the only record of what a generating agent actually
returned, and the corpus above it is a curated view that a human is expected to
edit and prune. Before this, journals were gitignored and thrown away, which is
why every row that predates the split carries ``run: pre-split`` and can be
traced no further.

Gates do NOT run here. A malformed id or a benchmark collision is caught at
compile time, against the whole corpus, where it can be reported once — not
scattered across ingests that each saw a different slice.
"""

from __future__ import annotations

import argparse
import json
import shutil
import sys
from collections import Counter, defaultdict
from dataclasses import replace
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "src"))

from daintree_assistant_server.runbooks.catalog import (  # noqa: E402
    default_runbooks_dir,
    load_catalog,
)
from training.corpus_source import (  # noqa: E402
    CORPUS,
    SOURCE_SECTIONS,
    CorpusError,
    Example,
    SourceFile,
    source_files,
    write_runbook_file,
    write_shared_file,
)

JOURNALS = ROOT / "training" / "journals"


def _identity(ex: Example) -> tuple[Any, ...]:
    """What makes two curated examples the same example.

    Deliberately NOT the compile-time dedupe key: that one keys on the rendered
    model input, which is the right question for "do these two rows teach the
    same thing" and the wrong one for "have I already filed this". Two queries
    can render identically after history budgeting and still be worth keeping as
    separate authored lines.

    Whitespace is COLLAPSED, not merely stripped. ``_examples_from`` collapses
    internal runs of whitespace on the way in, so an existing curated row that
    was hand-edited to contain a double space would not have matched its own
    journal on a re-ingest, and would have been added a second time.
    """
    return (
        " ".join(ex.query.split()),
        tuple(" ".join(h.split()) for h in ex.history),
        ex.active,
        tuple(sorted(ex.expect)),
        ex.source,
    )


def _load_journal(path: Path) -> list[dict[str, Any]]:
    out = []
    for line in path.read_text().splitlines():
        if not line.strip():
            continue
        row = json.loads(line)
        if row.get("type") == "result" and isinstance(row.get("result"), dict):
            out.append(row["result"])
    return out


def _examples_from(results: list[dict[str, Any]], run: str) -> list[Example]:
    """Translate the generator's bundle shapes into curated examples.

    The shapes are the generating agents' output schemas, defined in
    ``.claude/skills/retrain-selector``. They are separated by what an item
    CONTAINS rather than by which agent produced it, so a re-ingest from any mix
    of journals labels them the same way.
    """
    out: list[Example] = []

    def add(
        query: str,
        expect: list[str],
        *,
        source: str,
        history: list[str] | None = None,
        active: list[str] | None = None,
        confusable_with: list[str] | None = None,
        is_docs: bool = False,
        empty_kind: str | None = None,
    ) -> None:
        # Canonicalise the foundation pairing HERE rather than leaving it to the
        # compile gate. The backend pairs a domain foundation with the specific
        # downstream, so "specific + foundation" is never a training target and
        # compiling would strip it anyway — but a curated file is meant to show
        # the label that will actually be trained, and it also decides which file
        # the row lands in. Left alone, a row that becomes single-id at compile
        # time would sit in `shared/` forever, filed by a shape it no longer has.
        if len(expect) > 1 and "daintree.foundation" in expect:
            expect = [i for i in expect if i != "daintree.foundation"]
        out.append(
            Example(
                query=" ".join(str(query).split()),
                expect=tuple(expect),
                source=source,
                origin="",  # assigned when the example is placed in a file
                run=run,
                history=tuple(history or ()),
                active=tuple(active or ()),
                confusable_with=tuple(confusable_with or ()),
                is_docs=is_docs,
                # Same rule the LOADER applies (`_parse_example`): `empty_kind`
                # records which flavour of "nothing to load" a row teaches, so it
                # is meaningless on a row that selects something. Normalising it
                # here as well is what keeps a re-ingest quiet — comparing a
                # generator's raw `no_task` against the loader's stored `None`
                # reported 96 label conflicts that were purely representational.
                empty_kind=empty_kind if not expect else None,
            )
        )

    for block in (r for r in results if "runbook_id" in r):
        sid = block["runbook_id"]
        for p in block.get("positives", []):
            add(p["query"], [sid], source="positive")
        for n in block.get("boundary_negatives", []):
            # The runbook this query SOUNDS like is the confusable one; the
            # answer is whatever the generator says is correct.
            add(
                n["query"],
                list(n["correct_runbook_ids"]),
                source="boundary",
                confusable_with=[sid],
            )
        for f in block.get("followups", []):
            add(
                f["query"],
                list(f["keeps"]),
                source="followup",
                history=list(f.get("history", [])),
                active=list(f.get("keeps", [])),
            )

    for bundle in (r for r in results if "items" in r):
        items = bundle["items"]
        if not items:
            continue
        shape = set(items[0].keys())
        if "kind" in shape and "runbook_ids" in shape:
            for it in items:
                add(
                    it["query"],
                    list(it.get("runbook_ids", [])),
                    source="off_domain" if it["kind"] == "off_domain" else "near_miss",
                    empty_kind="off_domain",
                )
        elif "kind" in shape:
            for it in items:
                add(
                    it["query"], [], source="no_task",
                    history=list(it.get("history", [])), empty_kind="no_task",
                )
        elif "is_docs" in shape:
            for it in items:
                add(
                    it["query"],
                    list(it.get("runbook_ids", [])),
                    source="docs",
                    history=list(it.get("history", [])),
                    active=list(it.get("active_ids", [])),
                    is_docs=bool(it["is_docs"]),
                    empty_kind="no_task",
                )
        elif "active_ids" in shape:
            for it in items:
                q = str(it["query"]).strip()
                if q.startswith("["):
                    kind = "wake"  # a runtime event, not a user message
                elif not it["expect_ids"]:
                    kind = "no_task_stateful"
                else:
                    kind = "topic_shift"
                add(
                    q, list(it["expect_ids"]), source=kind,
                    history=list(it.get("history", [])),
                    active=list(it.get("active_ids", [])),
                )
        elif "expect_ids" in shape:
            for it in items:
                add(it["query"], list(it["expect_ids"]), source="multi_runbook")
        elif "runbook_ids" in shape:
            for it in items:
                ids = list(it.get("runbook_ids", []))
                add(
                    it["query"], ids,
                    source="foundation" if ids == ["daintree.foundation"]
                    else "foundation_boundary",
                )
        else:
            raise CorpusError(f"unrecognised generated bundle shape: {sorted(shape)}")
    return out


def _home(ex: Example) -> tuple[str, str | None]:
    """The ownership rule: (relative path, runbook the file belongs to)."""
    if len(ex.expect) == 1 and ex.source in SOURCE_SECTIONS:
        return f"runbooks/{ex.expect[0]}.yaml", ex.expect[0]
    return f"shared/{ex.source.replace('_', '-')}.yaml", None


def ingest(
    incoming: list[Example],
    *,
    run: str,
    valid: set[str],
    root: Path | None = None,
) -> Counter[str]:
    """Merge generated examples into the curated tree. Returns what happened.

    Separated from ``main`` so the two properties that matter can be tested
    against a throwaway corpus: that re-ingesting the same examples changes
    nothing, and that a second, DIFFERENT confusability judgement for a query
    already on disk is merged rather than dropped as a duplicate.
    """
    base = root or CORPUS
    existing: dict[str, list[Example]] = defaultdict(list)
    known: dict[str, SourceFile] = {}
    # identity -> where that example lives, so a repeat can be MERGED INTO the
    # row already on disk rather than only recognised and dropped.
    seen: dict[tuple[Any, ...], tuple[str, int]] = {}
    for f in list(source_files(base)):
        existing[f.path] = list(f.examples)
        known[f.path] = f
        for i, e in enumerate(f.examples):
            seen.setdefault(_identity(e), (f.path, i))

    stats: Counter[str] = Counter()
    touched: set[str] = set()

    for ex in incoming:
        if not ex.query:
            stats["skipped_empty"] += 1
            continue
        # Unknown ids are refused HERE as well as at compile time, because an
        # unknown id in a curated file is a typo somebody has to find later,
        # whereas one caught now names the journal it came from.
        if any(i not in valid for i in (*ex.expect, *ex.active, *ex.confusable_with)):
            stats["skipped_unknown_id"] += 1
            continue
        key = _identity(ex)
        if key in seen:
            # An exact repeat is a no-op, which is what makes re-ingesting a
            # journal safe. But `_identity` deliberately excludes the fields that
            # two generators can legitimately DISAGREE about for the same query,
            # and dropping the second judgement whole would lose data now that
            # `confusable_with` reaches the loss. So: union the confusability
            # notes, and report a contradictory label rather than silently
            # keeping whichever arrived first.
            path, position = seen[key]
            prior = existing[path][position]
            merged = tuple(sorted(set(prior.confusable_with) | set(ex.confusable_with)))
            if merged != prior.confusable_with:
                existing[path][position] = replace(prior, confusable_with=merged)
                touched.add(path)
                stats["merged_confusable_with"] += 1
            elif (prior.is_docs, prior.empty_kind) != (ex.is_docs, ex.empty_kind):
                stats["conflicting_label_kept_existing"] += 1
            else:
                stats["skipped_already_present"] += 1
            continue
        home, runbook = _home(ex)
        seen[key] = (home, len(existing[home]))
        existing[home].append(replace(ex, origin=home))
        if home not in known:
            # A file this run is creating. Only here may the defaults be chosen;
            # for a file that already exists they are whatever it already says.
            known[home] = SourceFile(
                path=home,
                runbook=runbook,
                run=run,
                docs=ex.is_docs,
                empty_kind=ex.empty_kind,
            )
        touched.add(home)
        stats[f"added_{ex.source}"] += 1

    for path in sorted(touched):
        examples = existing[path]
        target = base / path
        meta = known[path]
        if meta.runbook is not None:
            write_runbook_file(target, meta.runbook, examples, run=meta.run)
        else:
            write_shared_file(
                target,
                examples[0].source,
                examples,
                run=meta.run,
                docs=meta.docs,
                empty_kind=meta.empty_kind,
            )

    return stats


def main() -> int:
    ap = argparse.ArgumentParser(description="Fold generator journals into the curated corpus.")
    ap.add_argument("journal", nargs="+", help="one or more raw generation journals")
    ap.add_argument(
        "--run",
        required=True,
        help="run id stamped on every new example, e.g. 2026-08-29-worktree-runbooks. "
        "This is the provenance the flat corpus never had; make it identify the "
        "generation, not the day you happened to import it.",
    )
    ap.add_argument(
        "--no-archive",
        action="store_true",
        help="do not copy the journals into training/journals/ (they are the raw record; "
        "skip only when re-ingesting one that is already archived)",
    )
    args = ap.parse_args()

    catalog = load_catalog(default_runbooks_dir())
    valid = set(catalog.selectable_ids)

    results: list[dict[str, Any]] = []
    for path in args.journal:
        rows = _load_journal(Path(path))
        print(f"  {path}: {len(rows)} agent results")
        results.extend(rows)

    incoming = _examples_from(results, args.run)
    print(f"journal: {len(incoming)} generated examples")

    stats = ingest(incoming, run=args.run, valid=valid)

    if not args.no_archive:
        JOURNALS.mkdir(parents=True, exist_ok=True)
        for path in args.journal:
            dest = JOURNALS / f"{args.run}--{Path(path).name}"
            # The raw plane is APPEND-ONLY, and a plain copy is not. Two journals
            # sharing a basename, or a re-used run id, would silently overwrite
            # the only record of what a generator actually returned — the one
            # thing in this layout that cannot be rebuilt from anything else.
            if dest.exists() and dest.read_bytes() != Path(path).read_bytes():
                raise SystemExit(
                    f"{dest.relative_to(ROOT)} already exists with different content. "
                    f"The journal archive is append-only; choose a --run that names "
                    f"this generation, or rename the incoming journal."
                )
            shutil.copy2(path, dest)
            print(f"archived {dest.relative_to(ROOT)}")

    print("\ningested:")
    for key, value in sorted(stats.items()):
        print(f"  {key:32} {value}")
    print(f"\n  {sum(v for k, v in stats.items() if k.startswith('added_'))} example(s) added")
    print("\nNow run:  python -m training.compile_corpus")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
