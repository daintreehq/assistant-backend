"""The training corpus is a BUILD ARTIFACT, and this is what makes that true.

``training/data/selection.jsonl`` is committed — it is the record of what the
shipped weights were trained on, and at ~3 MB it is far under any threshold that
would justify DVC or git-lfs. But committed is not the same as authoritative:
the curated YAML under ``training/corpus/`` is the source, and without a gate
the two drift the moment somebody edits the artifact directly, which is exactly
the trap the old single-file corpus was.

These run in ordinary CI, not in the training environment. The compile path
touches no torch, no transformers and no model weights — only the catalog, the
benchmark cases and the serving-side query renderer — which is what lets the
corpus be gated on every push rather than only when someone retrains.
"""

from __future__ import annotations

import json
from collections import Counter
from pathlib import Path

import pytest

from training.compile_corpus import CORPUS_OUT, LOCK_OUT, compile_corpus, serialise
from training.corpus_source import (
    CORPUS,
    SECTION_SOURCES,
    Example,
    load_examples,
    source_files,
)

pytestmark = pytest.mark.filterwarnings("ignore")


@pytest.fixture(scope="module")
def compiled() -> tuple[list[dict], dict]:
    return compile_corpus(quiet=True)


def test_committed_artifact_matches_the_curated_corpus(compiled: tuple[list[dict], dict]) -> None:
    """The gate. Edit the YAML, run `python -m training.compile_corpus`, commit both."""
    rows, _ = compiled
    assert CORPUS_OUT.read_text() == serialise(rows), (
        "training/data/selection.jsonl is stale — it is compiled from "
        "training/corpus/, never edited by hand. Run: python -m training.compile_corpus"
    )


def test_lockfile_matches_the_compiled_corpus(compiled: tuple[list[dict], dict]) -> None:
    rows, lock = compiled
    committed = json.loads(LOCK_OUT.read_text())
    assert committed == lock, "corpus-lock.json is stale; recompile"
    assert committed["rows"] == len(rows)


def test_compiling_twice_gives_the_same_bytes() -> None:
    """Purity, which the cumulative ``assemble_data`` could not offer.

    It read the committed artifact as its own ``--base``, so a second run
    appended rather than reproduced — the retired ``augment_context`` measured
    9,903 rows becoming 10,405 that way.
    """
    first, _ = compile_corpus(quiet=True)
    second, _ = compile_corpus(quiet=True)
    assert serialise(first) == serialise(second)


def test_every_curated_file_parses_and_is_owned() -> None:
    """Ownership is mechanical, so a violation is a mistake rather than a judgement."""
    for source_file in source_files():
        assert source_file.examples, f"{source_file.path} has no examples"
        if source_file.runbook is None:
            continue
        for example in source_file.examples:
            assert tuple(example.expect) == (source_file.runbook,), (
                f"{source_file.path} holds a row answering {list(example.expect)}; "
                f"rows answering none or several runbooks belong in corpus/shared/"
            )


def test_runbook_files_are_named_for_a_real_runbook() -> None:
    from daintree_assistant_server.runbooks.catalog import default_runbooks_dir, load_catalog

    selectable = set(load_catalog(default_runbooks_dir()).selectable_ids)
    named = {
        Path(f.path).stem for f in source_files() if f.runbook is not None
    }
    assert named <= selectable, f"curated files for unknown runbooks: {sorted(named - selectable)}"


def test_no_row_leaks_the_held_out_benchmark(compiled: tuple[list[dict], dict]) -> None:
    """The benchmark is the only evidence any of this works.

    A leak fails nothing on its own — it just quietly turns the held-out score
    into a training-set score, which would make every number in the report
    meaningless. The compile gate drops collisions; this asserts none survived.
    """
    from daintree_assistant_server.runbooks.catalog import default_runbooks_dir, load_catalog
    from training.compile_corpus import _benchmark_strings, norm

    rows, _ = compiled
    held_out = _benchmark_strings(load_catalog(default_runbooks_dir()))
    leaked = [r["query"] for r in rows if norm(r["query"]) in held_out]
    assert not leaked, f"{len(leaked)} compiled rows collide with the benchmark: {leaked[:3]}"


def test_every_row_carries_provenance(compiled: tuple[list[dict], dict]) -> None:
    """`run` and `origin` are the two questions the flat corpus could not answer."""
    rows, _ = compiled
    for row in rows:
        assert row["run"], f"row without a run id: {row['query'][:60]}"
        assert row["origin"], f"row without an origin file: {row['query'][:60]}"
    origins = {r["origin"] for r in rows}
    on_disk = {f.path for f in source_files()}
    assert origins <= on_disk


def test_confusable_with_never_names_the_answer(compiled: tuple[list[dict], dict]) -> None:
    """A runbook cannot be both the answer and a counter-example of itself."""
    rows, _ = compiled
    for row in rows:
        overlap = set(row["confusable_with"]) & set(row["expect_ids"])
        assert not overlap, f"{row['query'][:60]}: {sorted(overlap)} is both answer and confusable"


def test_confusable_with_is_carried_by_the_boundary_rows(compiled: tuple[list[dict], dict]) -> None:
    """The authored-confusability signal exists and is reachable.

    It spent its whole life as ``hard_negatives``, written on 2,042 rows, named
    in two docstrings as an input to the loss, and read by nothing. A count here
    is what stops that happening again silently.
    """
    rows, _ = compiled
    carrying = [r for r in rows if r["confusable_with"]]
    assert len(carrying) > 1000, f"only {len(carrying)} rows carry confusable_with"
    assert all(r["source"] in ("boundary", "foundation_boundary") for r in carrying)


def test_every_section_name_maps_to_a_source() -> None:
    """A section a human can write that the compiler silently ignores is a trap."""
    used = {ex.source for f in source_files() if f.runbook is not None for ex in f.examples}
    assert used <= set(SECTION_SOURCES.values())


def test_no_runbook_is_starved_of_examples() -> None:
    """A runbook with no training rows loses every contested turn (measured 4/14).

    Not a hard floor on the count — the catalog's spread is wide on purpose —
    but a runbook with NO file at all is an omission nothing else reports.
    """
    from daintree_assistant_server.runbooks.catalog import default_runbooks_dir, load_catalog

    selectable = set(load_catalog(default_runbooks_dir()).selectable_ids)
    counts = Counter(
        sid for ex in load_examples() for sid in ex.expect
    )
    missing = sorted(selectable - set(counts))
    assert not missing, f"selectable runbooks with no training examples: {missing}"


def test_the_corpus_readme_exists() -> None:
    """The layout is only maintainable if the rule for where a row goes is written down."""
    assert (CORPUS / "README.md").is_file()


# ---------------------------------------------------------------------------
# Round-trip. The highest-severity failure mode in this layout is not an error —
# it is a field that writes, reads back different, and trains slightly wrong.
# One such bug existed and was caught by hand (`is_docs` was dropped entirely by
# the runbook-file writer, losing the label on 26 rows); these are what stop the
# next one being found the same way.
# ---------------------------------------------------------------------------

#: Strings chosen to break a naive YAML writer. The first group are the type
#: coercions — bare `yes`/`no`/`on`/`off`/`null`/`123` round-trip as bool, None
#: or int unless the dumper quotes them, and a training query really can be the
#: single word "no". The rest are YAML's structural sigils in leading position.
_ADVERSARIAL = [
    "yes", "no", "on", "off", "true", "false", "null", "~", "123", "1.5",
    "2026-08-29", "- dash lead", ": colon lead", "#hash lead", "  padded  ",
    "tab\there", "quote's and \"double\"", "emoji 🚀 ok", "{brace}", "[bracket]",
    "*star", "&anchor", "multi\nline", "|pipe", ">gt", "end colon:", "%percent",
    "@at", "`tick", "0o17", "0x1f", ".inf", ".nan",
]


def test_a_shared_file_round_trips_adversarial_queries(tmp_path: Path) -> None:
    from training.corpus_source import write_shared_file

    (tmp_path / "runbooks").mkdir()
    (tmp_path / "shared").mkdir()
    written = [
        Example(
            query=q,
            expect=(),
            source="no_task",
            origin="shared/no-task.yaml",
            run="probe",
            empty_kind="no_task",
        )
        for q in _ADVERSARIAL
    ]
    write_shared_file(
        tmp_path / "shared" / "no-task.yaml",
        "no_task",
        written,
        run="probe",
        empty_kind="no_task",
    )
    (read_back,) = [f for f in source_files(tmp_path) if f.path.startswith("shared/")]
    assert [e.query for e in read_back.examples] == _ADVERSARIAL
    assert all(e.empty_kind == "no_task" for e in read_back.examples)
    assert all(e.run == "probe" for e in read_back.examples)


def test_a_runbook_file_round_trips_every_optional_field(tmp_path: Path) -> None:
    """History, active set, confusability, docs label and run, all at once.

    `docs` is the one that has already gone wrong: the writer omitted it from
    runbook files on the assumption that a row answering a runbook is never a
    documentation turn, and 26 rows in the real corpus are both.
    """
    from training.corpus_source import write_runbook_file

    (tmp_path / "runbooks").mkdir()
    (tmp_path / "shared").mkdir()
    runbook = "daintree.terminals.rename"
    written = [
        Example(
            query=q,
            expect=(runbook,),
            source="boundary" if i % 2 else "positive",
            origin=f"runbooks/{runbook}.yaml",
            run="probe" if i % 3 else "other-run",
            history=(f"user: {q}", "assistant: ok") if i % 2 else (),
            active=(runbook,) if i % 4 else (),
            confusable_with=("daintree.foundation",) if i % 2 else (),
            is_docs=bool(i % 5),
        )
        for i, q in enumerate(_ADVERSARIAL)
    ]
    write_runbook_file(
        tmp_path / "runbooks" / f"{runbook}.yaml", runbook, written, run="probe"
    )
    (read_back,) = [f for f in source_files(tmp_path) if f.path.startswith("runbooks/")]

    # Sections regroup an example by its source, so the file's order is not the
    # order it was written in. Compare as a set of complete field tuples —
    # `origin` excluded, since the writer does not carry it and the loader
    # derives it from the path.
    def fields(example: Example) -> tuple[object, ...]:
        return (
            example.query,
            example.expect,
            example.source,
            example.run,
            example.history,
            example.active,
            example.confusable_with,
            example.is_docs,
            example.empty_kind,
        )

    assert {fields(e) for e in read_back.examples} == {fields(e) for e in written}


def test_an_unknown_key_in_an_example_is_refused(tmp_path: Path) -> None:
    """A silently-ignored key is a row that does not teach what its author thinks.

    The likely typo is a plural — `confusables`, `histories` — and without this
    the row still parses, still trains, and quietly carries none of the meaning
    the author was adding.
    """
    import yaml

    from training.corpus_source import CorpusError, source_files

    (tmp_path / "runbooks").mkdir()
    (tmp_path / "shared").mkdir()
    (tmp_path / "shared" / "no-task.yaml").write_text(
        yaml.safe_dump(
            {"source": "no_task", "examples": [{"query": "hi", "confusables": ["x"]}]}
        )
    )
    with pytest.raises(CorpusError, match="unknown key"):
        list(source_files(tmp_path))


# ---------------------------------------------------------------------------
# The two paths that MUTATE the curated plane. Both were verified by hand while
# they were being written, which is not a test anybody repeats — and once a
# writer corrupts a file, recompiling makes the artifact and the lockfile agree
# with the corruption, so the gates above would go green over it.
# ---------------------------------------------------------------------------


def _tiny_corpus(root: Path) -> None:
    (root / "runbooks").mkdir(parents=True, exist_ok=True)
    (root / "shared").mkdir(parents=True, exist_ok=True)


def _example(query: str, **kwargs: object) -> Example:
    from training.corpus_source import Example as E

    base: dict[str, object] = {
        "query": query,
        "expect": ("daintree.terminals.rename",),
        "source": "positive",
        "origin": "",
        "run": "run-a",
    }
    base.update(kwargs)
    return E(**base)  # type: ignore[arg-type]


def test_ingesting_the_same_examples_twice_changes_nothing(tmp_path: Path) -> None:
    """The property the old cumulative pipeline did not have.

    `augment_context` measured a second application taking 9,903 rows to 10,405
    and shipped a guard against its own re-run rather than being safe to repeat.
    """
    from training.ingest_journal import ingest

    _tiny_corpus(tmp_path)
    valid = {"daintree.terminals.rename", "daintree.foundation"}
    incoming = [_example("rename my terminals"), _example("give the tabs better names")]

    first = ingest(incoming, run="run-a", valid=valid, root=tmp_path)
    assert sum(v for k, v in first.items() if k.startswith("added_")) == 2
    after_first = (tmp_path / "runbooks" / "daintree.terminals.rename.yaml").read_text()

    second = ingest(incoming, run="run-a", valid=valid, root=tmp_path)
    assert second["skipped_already_present"] == 2
    assert not [k for k in second if k.startswith("added_")]
    assert (tmp_path / "runbooks" / "daintree.terminals.rename.yaml").read_text() == after_first


def test_a_second_confusability_judgement_is_merged_not_dropped(tmp_path: Path) -> None:
    """`_identity` excludes `confusable_with`, so a repeat would otherwise lose it.

    Two boundary generators can produce the same query and answer while naming
    DIFFERENT runbooks as the thing it could be mistaken for. Both judgements are
    real, and the field now reaches the loss, so keeping only whichever arrived
    first is training-data loss rather than deduplication.
    """
    from training.corpus_source import source_files
    from training.ingest_journal import ingest

    _tiny_corpus(tmp_path)
    valid = {"daintree.terminals.rename", "daintree.foundation", "daintree.diagnostics"}
    query = "name that tab something useful"

    ingest(
        [_example(query, source="boundary", confusable_with=("daintree.foundation",))],
        run="run-a", valid=valid, root=tmp_path,
    )
    stats = ingest(
        [_example(query, source="boundary", confusable_with=("daintree.diagnostics",))],
        run="run-b", valid=valid, root=tmp_path,
    )
    assert stats["merged_confusable_with"] == 1

    (only,) = [e for f in source_files(tmp_path) for e in f.examples]
    assert only.confusable_with == ("daintree.diagnostics", "daintree.foundation")


def test_pruning_a_run_leaves_the_other_runs_untouched(tmp_path: Path) -> None:
    from training.corpus_source import source_files
    from training.ingest_journal import ingest
    from training.prune_run import prune

    _tiny_corpus(tmp_path)
    valid = {"daintree.terminals.rename", "daintree.foundation"}
    ingest([_example("keep me", run="good")], run="good", valid=valid, root=tmp_path)
    ingest([_example("drop me", run="bad")], run="bad", valid=valid, root=tmp_path)

    assert prune("bad", root=tmp_path, dry_run=True)
    surviving = [e.query for f in source_files(tmp_path) for e in f.examples]
    assert sorted(surviving) == ["drop me", "keep me"], "--dry-run must write nothing"

    prune("bad", root=tmp_path)
    assert [e.query for f in source_files(tmp_path) for e in f.examples] == ["keep me"]


def test_pruning_every_example_deletes_the_file(tmp_path: Path) -> None:
    """An emptied file is removed, not left as a stub.

    An empty curated file is indistinguishable from a runbook nobody has written
    data for yet, and the loader would have to guess which it was.
    """
    from training.ingest_journal import ingest
    from training.prune_run import prune

    _tiny_corpus(tmp_path)
    valid = {"daintree.terminals.rename", "daintree.foundation"}
    ingest([_example("only row", run="bad")], run="bad", valid=valid, root=tmp_path)
    target = tmp_path / "runbooks" / "daintree.terminals.rename.yaml"
    assert target.is_file()

    prune("bad", root=tmp_path)
    assert not target.exists()


def test_nothing_is_being_silently_gated_out_of_the_corpus(
    compiled: tuple[list[dict], dict],
) -> None:
    """A dropped row is a curated example that is not training, and nobody is told.

    The compile gates FAIL OPEN by design — an unknown id, a benchmark collision
    or a contradictory label drops one row rather than failing the build, which
    is right when the input is a fresh generator dump. But the curated plane is
    hand-edited, so a standing drop means a file on disk that does not mean what
    it appears to. Fix it at the source and this goes back to zero.
    """
    _, lock = compiled
    assert lock["dropped"] == {}, (
        f"the curated corpus is losing rows at compile: {lock['dropped']}. "
        f"Each one is an example that reads as training data and is not."
    )


def test_no_runbook_is_starved_of_COMPILED_rows(compiled: tuple[list[dict], dict]) -> None:
    """Counted after the gates, not before.

    An earlier version of this counted curated examples, so a runbook whose rows
    were all dropped at compile still read as covered.
    """
    from daintree_assistant_server.runbooks.catalog import default_runbooks_dir, load_catalog

    rows, _ = compiled
    selectable = set(load_catalog(default_runbooks_dir()).selectable_ids)
    covered = Counter(sid for r in rows for sid in r["expect_ids"])
    missing = sorted(selectable - set(covered))
    assert not missing, f"selectable runbooks with no COMPILED training rows: {missing}"


def test_duplicate_yaml_keys_are_refused(tmp_path: Path) -> None:
    """PyYAML keeps the last duplicate key and says nothing.

    A hand-merged file with two `positives:` keys loses the first list entirely,
    still parses, still compiles, and the next ingest or prune rewrites the file
    without it — permanent, invisible loss of training data. That is the exact
    shape this whole layout exists to prevent, so it is a hard error.
    """
    from training.corpus_source import CorpusError, source_files

    (tmp_path / "runbooks").mkdir()
    (tmp_path / "shared").mkdir()
    (tmp_path / "runbooks" / "daintree.terminals.rename.yaml").write_text(
        "runbook: daintree.terminals.rename\n"
        "positives:\n  - first list\n"
        "positives:\n  - second list\n",
        encoding="utf-8",
    )
    with pytest.raises(CorpusError, match="duplicate key"):
        list(source_files(tmp_path))


def test_a_single_runbook_row_may_not_hide_in_a_shared_file(tmp_path: Path) -> None:
    """Ownership is enforced in BOTH directions.

    A single-runbook row in a shared file compiles perfectly well; it is simply
    filed where nobody auditing that runbook will ever look for it.
    """
    from training.compile_corpus import compile_corpus
    from training.corpus_source import CorpusError

    (tmp_path / "runbooks").mkdir()
    (tmp_path / "shared").mkdir()
    (tmp_path / "shared" / "boundary.yaml").write_text(
        "source: boundary\n"
        "examples:\n"
        "  - query: rename the tabs\n"
        "    expect: [daintree.terminals.rename]\n",
        encoding="utf-8",
    )
    with pytest.raises(CorpusError, match="belongs in corpus/runbooks/"):
        compile_corpus(corpus_root=tmp_path, quiet=True)


def test_a_foundation_pairing_is_canonical_at_load_not_at_compile(tmp_path: Path) -> None:
    """`[X, daintree.foundation]` reads as `[X]`, so it is not "misfiled" for a
    shape it does not have once normalised — the check that rejected it used to
    run before the normalisation that fixes it.
    """
    from training.corpus_source import source_files

    (tmp_path / "runbooks").mkdir()
    (tmp_path / "shared").mkdir()
    (tmp_path / "runbooks" / "daintree.terminals.rename.yaml").write_text(
        "runbook: daintree.terminals.rename\n"
        "positives:\n"
        "  - query: rename the tabs\n"
        "    expect: [daintree.terminals.rename, daintree.foundation]\n",
        encoding="utf-8",
    )
    (only,) = [e for f in source_files(tmp_path) for e in f.examples]
    assert only.expect == ("daintree.terminals.rename",)
