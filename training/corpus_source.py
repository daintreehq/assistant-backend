"""The CURATED plane: hand-editable YAML that is the corpus's source of truth.

Three planes, and the distinction between them is the point:

``training/journals/``
    RAW. Append-only, immutable, committed. Exactly what a generating agent
    returned, with the run that produced it. Nothing reads these to train; they
    exist so a row can be traced back to the prompt and model that wrote it.

``training/corpus/``
    CURATED. What this module loads. Small YAML files a human edits, reviews in
    a diff, and deletes from. **This is the source of truth.**

``training/data/selection.jsonl``
    COMPILED. A build artifact, reproducible from the plane above by
    ``training.compile_corpus``, and gated in CI as such.

Before this split there was one file, ``selection.jsonl``, which was BOTH the
assembler's output AND its own input: a retrain read the committed corpus back
in as ``--base`` and appended to it. Three things followed, all of which this
layout exists to end. A row could never be removed by regenerating, only by
hand-editing 2.8 MB of JSONL. Provenance was one flat ``source`` string, so
"which run added this" and "drop everything from the run with the bad prompt"
were both unanswerable. And re-running a generator did not reproduce a corpus,
it grew one — the retired ``augment_context.py`` measured itself taking 9,903
rows to 10,405 on a second application, and shipped a guard against its own
re-run whose documented recovery was ``git show <commit>:...jsonl >``. Git was
the undo stack for the training data.

## The ownership rule

A row lives in the file for the runbook it SELECTS.

- ``expect`` is exactly one runbook  →  ``corpus/runbooks/<that id>.yaml``
- anything else (no runbook, or several)  →  ``corpus/shared/<source>.yaml``

That is the whole placement policy, and it is mechanical rather than editorial
so that two people fanning out the same journal produce the same tree. It also
puts a boundary case in the file of the runbook that WINS it, which is where
somebody auditing "why does this runbook lose to its neighbour" will look.

## Two file shapes

A **runbook file** carries examples that all label one runbook, grouped into
sections that name what kind of evidence they are (``positives``,
``boundaries``, …; ``SECTION_SOURCES`` is the table). A **shared file** carries
examples that label none or several, and states its ``source`` once at the top.

In both, an example may be a bare string — the query — which is what keeps a
file of forty plain positives forty lines long instead of a hundred and twenty.
Anything more than a query is a mapping.
"""

from __future__ import annotations

from collections.abc import Iterable, Iterator
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

ROOT = Path(__file__).resolve().parents[1]
CORPUS = ROOT / "training" / "corpus"
RUNBOOKS_DIR = "runbooks"
SHARED_DIR = "shared"

#: Section name in a runbook file → the ``source`` tag the compiled row carries.
#: The section name is the plural a human would write; the source is the
#: singular the corpus has always used, kept identical so per-source counts stay
#: comparable across the split.
SECTION_SOURCES = {
    "positives": "positive",
    "boundaries": "boundary",
    "followups": "followup",
    "topic_shifts": "topic_shift",
    "wakes": "wake",
    "near_misses": "near_miss",
    "docs": "docs",
    "foundation": "foundation",
    "foundation_boundaries": "foundation_boundary",
}

#: Reverse of the above, for the ingest path that has a source and needs a home.
SOURCE_SECTIONS = {v: k for k, v in SECTION_SOURCES.items()}

#: Fields an example mapping may carry. Anything else is a typo and is refused —
#: a silently-ignored key in training data is a row that does not teach what its
#: author believes it teaches, and nothing downstream would ever report it.
EXAMPLE_KEYS = {
    "query",
    "history",
    "active",
    "expect",
    "confusable_with",
    "docs",
    "empty_kind",
    "run",
}

#: The provenance stamp carried by every row that predates the three-plane
#: split. Their generating journals were session-local and gitignored, so they
#: are genuinely unattributable — recording that honestly is better than
#: inventing a run id for them.
LEGACY_RUN = "pre-split"

#: The domain foundation. The backend pairs it with the specific runbook the
#: classifier selected, so it is never half of a training answer.
FOUNDATION = "daintree.foundation"


class CorpusError(Exception):
    """A source file is malformed. Always fatal — never a skipped row."""


class _StrictLoader(yaml.SafeLoader):
    """``yaml.SafeLoader`` that REFUSES duplicate mapping keys.

    PyYAML accepts them and keeps the last, silently. In a hand-merged corpus
    file that is a whole section deleted with no error: two ``positives:`` keys
    after a bad conflict resolution and the first list is gone, the file still
    parses, the compile still succeeds, and the next ingest or prune rewrites the
    file without it — making the loss permanent and invisible. This is the exact
    silent-data-loss shape the three-plane layout exists to prevent, so it is a
    hard error rather than a warning.
    """


def _no_duplicate_keys(loader: _StrictLoader, node: yaml.MappingNode, deep: bool = False) -> Any:
    seen: set[Any] = set()
    for key_node, _ in node.value:
        key = loader.construct_object(key_node, deep=deep)
        if key in seen:
            raise CorpusError(f"duplicate key {key!r} at line {key_node.start_mark.line + 1}")
        seen.add(key)
    return yaml.SafeLoader.construct_mapping(loader, node, deep)  # type: ignore[arg-type]


_StrictLoader.add_constructor(
    yaml.resolver.BaseResolver.DEFAULT_MAPPING_TAG, _no_duplicate_keys
)


def _read_yaml(path: Path, origin: str) -> Any:
    """Parse one curated file. UTF-8 is explicit — the corpus contains emoji."""
    try:
        return yaml.load(path.read_text(encoding="utf-8"), Loader=_StrictLoader) or {}
    except CorpusError as exc:
        raise CorpusError(f"{origin}: {exc}") from None
    except yaml.YAMLError as exc:
        raise CorpusError(f"{origin}: {exc}") from exc


@dataclass(frozen=True)
class Example:
    """One curated training turn, before the compile gates run over it."""

    query: str
    expect: tuple[str, ...]
    source: str
    origin: str
    run: str
    history: tuple[str, ...] = ()
    active: tuple[str, ...] = ()
    #: Runbooks a human judged this query could plausibly be mistaken for. NOT a
    #: mined negative and not a model artefact: it is the "do not use for X"
    #: clause of a runbook's own ``whenToUse``, written down where a trainer can
    #: reach it. Mining finds what the CURRENT encoder confuses; this records
    #: what a person thinks is confusable, which survives a retrain.
    confusable_with: tuple[str, ...] = ()
    is_docs: bool = False
    empty_kind: str | None = None


@dataclass
class SourceFile:
    """One YAML file's contribution, for the lockfile and for error messages.

    The file-level defaults are carried back out as well as applied. An ingest
    appending to a shared file has to write it out whole, and recomputing the
    defaults from the new contents would let one added row flip a majority and
    re-key every other row in the file — a two-line change arriving as a
    five-hundred-line diff.
    """

    path: str
    runbook: str | None
    run: str = LEGACY_RUN
    docs: bool = False
    empty_kind: str | None = None
    examples: list[Example] = field(default_factory=list)


def _as_str_tuple(value: Any, *, where: str, key: str) -> tuple[str, ...]:
    if value is None:
        return ()
    if not isinstance(value, list) or not all(isinstance(v, str) for v in value):
        raise CorpusError(f"{where}: `{key}` must be a list of strings")
    return tuple(value)


def _parse_example(
    raw: Any,
    *,
    origin: str,
    source: str,
    default_expect: tuple[str, ...],
    default_run: str,
    default_docs: bool,
    default_empty_kind: str | None,
    position: str,
) -> Example:
    where = f"{origin} [{position}]"
    if isinstance(raw, str):
        raw = {"query": raw}
    if not isinstance(raw, dict):
        raise CorpusError(f"{where}: an example must be a string or a mapping")

    unknown = set(raw) - EXAMPLE_KEYS
    if unknown:
        raise CorpusError(f"{where}: unknown key(s) {sorted(unknown)}")

    query = raw.get("query")
    if not isinstance(query, str) or not query.strip():
        raise CorpusError(f"{where}: `query` is required and must be a non-empty string")

    expect = default_expect
    if "expect" in raw:
        expect = _as_str_tuple(raw["expect"], where=where, key="expect")
    # Canonicalise HERE, so every consumer sees the answer that will actually be
    # trained. The backend pairs a domain foundation with the specific it
    # selected, downstream of the classifier, so "specific + foundation" is never
    # a training target. Doing it at load rather than at compile is what lets the
    # ownership check run on the canonical value — otherwise `[X, foundation]`
    # sitting in `runbooks/X.yaml` is rejected as misfiled for a shape it does
    # not actually have once normalised.
    if len(expect) > 1 and FOUNDATION in expect:
        expect = tuple(i for i in expect if i != FOUNDATION)

    empty_kind = raw.get("empty_kind", default_empty_kind)
    if empty_kind is not None and not isinstance(empty_kind, str):
        raise CorpusError(f"{where}: `empty_kind` must be a string or absent")

    docs = raw.get("docs", default_docs)
    if not isinstance(docs, bool):
        raise CorpusError(f"{where}: `docs` must be true or false")

    run = raw.get("run", default_run)
    if not isinstance(run, str) or not run:
        raise CorpusError(f"{where}: `run` must be a non-empty string")

    return Example(
        query=query,
        expect=expect,
        source=source,
        origin=origin,
        run=run,
        history=_as_str_tuple(raw.get("history"), where=where, key="history"),
        active=_as_str_tuple(raw.get("active"), where=where, key="active"),
        confusable_with=_as_str_tuple(
            raw.get("confusable_with"), where=where, key="confusable_with"
        ),
        is_docs=docs,
        # An empty_kind on a row that DOES select a runbook is meaningless — the
        # field records which flavour of "nothing to load" a row teaches. Drop it
        # here rather than letting two spellings of the same row exist.
        empty_kind=empty_kind if not expect else None,
    )


def _load_runbook_file(path: Path, origin: str) -> SourceFile:
    data = _read_yaml(path, origin)
    if not isinstance(data, dict):
        raise CorpusError(f"{origin}: top level must be a mapping")

    runbook = data.get("runbook")
    if not isinstance(runbook, str) or not runbook:
        raise CorpusError(f"{origin}: `runbook` is required")
    if runbook != path.stem:
        raise CorpusError(f"{origin}: `runbook` is {runbook!r} but the filename says {path.stem!r}")

    default_run = data.get("run", LEGACY_RUN)
    if not isinstance(default_run, str) or not default_run:
        raise CorpusError(f"{origin}: `run` must be a non-empty string")

    unknown = set(data) - set(SECTION_SOURCES) - {"runbook", "run"}
    if unknown:
        raise CorpusError(f"{origin}: unknown section(s) {sorted(unknown)}")

    out = SourceFile(path=origin, runbook=runbook, run=default_run)
    for section, source in SECTION_SOURCES.items():
        items = data.get(section)
        if items is None:
            continue
        if not isinstance(items, list):
            raise CorpusError(f"{origin}: section `{section}` must be a list")
        for i, raw in enumerate(items):
            out.examples.append(
                _parse_example(
                    raw,
                    origin=origin,
                    source=source,
                    default_expect=(runbook,),
                    default_run=default_run,
                    # A section names where a row CAME FROM; `docs:` is the label
                    # the classifier learns. They are different questions, and
                    # conflating them is the mislabelling that measured 5/120
                    # docs recall on turns with a runbook loaded. Rows in a
                    # runbook file answer with that runbook, so the docs label is
                    # off unless a row says otherwise.
                    default_docs=False,
                    default_empty_kind=None,
                    position=f"{section}[{i}]",
                )
            )
    return out


def _load_shared_file(path: Path, origin: str) -> SourceFile:
    data = _read_yaml(path, origin)
    if not isinstance(data, dict):
        raise CorpusError(f"{origin}: top level must be a mapping")

    unknown = set(data) - {"source", "run", "docs", "empty_kind", "examples"}
    if unknown:
        raise CorpusError(f"{origin}: unknown key(s) {sorted(unknown)}")

    source = data.get("source")
    if not isinstance(source, str) or not source:
        raise CorpusError(f"{origin}: `source` is required")

    default_run = data.get("run", LEGACY_RUN)
    if not isinstance(default_run, str) or not default_run:
        raise CorpusError(f"{origin}: `run` must be a non-empty string")

    default_docs = data.get("docs", False)
    if not isinstance(default_docs, bool):
        raise CorpusError(f"{origin}: `docs` must be true or false")

    default_empty_kind = data.get("empty_kind")
    if default_empty_kind is not None and not isinstance(default_empty_kind, str):
        raise CorpusError(f"{origin}: `empty_kind` must be a string or absent")

    items = data.get("examples")
    if not isinstance(items, list):
        raise CorpusError(f"{origin}: `examples` must be a list")

    out = SourceFile(
        path=origin,
        runbook=None,
        run=default_run,
        docs=default_docs,
        empty_kind=default_empty_kind,
    )
    for i, raw in enumerate(items):
        out.examples.append(
            _parse_example(
                raw,
                origin=origin,
                source=source,
                default_expect=(),
                default_run=default_run,
                default_docs=default_docs,
                default_empty_kind=default_empty_kind,
                position=f"examples[{i}]",
            )
        )
    return out


def source_files(root: Path | None = None) -> Iterator[SourceFile]:
    """Every curated file, in a deterministic order.

    Sorted by path, and within a file by section order then list order, so the
    compiled artifact is a pure function of the tree's bytes. CI compares the
    committed ``selection.jsonl`` against a fresh compile; a set iteration or a
    directory listing order anywhere in here would make that gate flap.
    """
    base = root or CORPUS
    if not base.is_dir():
        raise CorpusError(f"no curated corpus at {base}")
    for kind, loader in ((RUNBOOKS_DIR, _load_runbook_file), (SHARED_DIR, _load_shared_file)):
        for path in sorted((base / kind).glob("*.yaml")):
            yield loader(path, f"{kind}/{path.name}")


def load_examples(root: Path | None = None) -> list[Example]:
    """Flatten the curated tree into examples, still ungated."""
    return [ex for f in source_files(root) for ex in f.examples]


# --------------------------------------------------------------------------
# Writing


def _example_to_yaml(
    ex: Example,
    *,
    implicit_expect: tuple[str, ...],
    file_run: str,
    file_docs: bool,
    file_empty_kind: str | None,
) -> Any:
    """Render one example as the smallest thing that ROUND-TRIPS it.

    A bare string when the query is all there is — which is most positives, and
    the difference between a forty-line file and a hundred-and-twenty-line one.

    Every defaulted field has to be compared against the default the LOADER will
    apply, not against a plausible-looking constant. The first version of this
    omitted ``docs`` from runbook files on the assumption that a row answering a
    runbook is never a docs turn; 26 rows in the existing corpus are both, and
    they silently lost the label on the way out.
    """
    body: dict[str, Any] = {"query": ex.query}
    if ex.history:
        body["history"] = list(ex.history)
    if ex.active:
        body["active"] = list(ex.active)
    if tuple(ex.expect) != tuple(implicit_expect):
        body["expect"] = list(ex.expect)
    if ex.confusable_with:
        body["confusable_with"] = list(ex.confusable_with)
    if ex.is_docs != file_docs:
        body["docs"] = ex.is_docs
    # Only where the loader would disagree: it nulls `empty_kind` on any row that
    # selects a runbook, so writing one there would not survive a reload.
    if not ex.expect and ex.empty_kind != file_empty_kind:
        body["empty_kind"] = ex.empty_kind
    if ex.run != file_run:
        body["run"] = ex.run
    return ex.query if set(body) == {"query"} else body


class _Dumper(yaml.SafeDumper):
    """Block sequences indented under their key, which is how humans write YAML."""

    # `indentless` is PyYAML's signature and is deliberately ignored: forcing it
    # False is the entire override. Naming it anyway keeps the signature checkable
    # against the base class.
    def increase_indent(self, flow: bool = False, indentless: bool = False) -> None:  # noqa: ARG002
        super().increase_indent(flow, False)


def _represent_list(dumper: yaml.SafeDumper, data: list[Any]) -> Any:
    """Runbook-id lists inline, prose lists one per line.

    ``confusable_with: [daintree.forge.manage-issue]`` on one line rather than
    three, across the two thousand boundary rows that carry one. The
    discriminator is a SPACE: a runbook id never has one and a history line
    always does, so `history` keeps the block form where each turn is its own
    reviewable line and a reworded turn is a one-line diff.
    """
    inline = bool(data) and all(
        isinstance(item, str) and " " not in item for item in data
    ) and sum(len(item) + 2 for item in data) < 88
    return dumper.represent_sequence("tag:yaml.org,2002:seq", data, flow_style=inline)


_Dumper.add_representer(list, _represent_list)


def _write(path: Path, text: str) -> None:
    """Write a curated file atomically, UTF-8, LF.

    Atomic because ``ingest`` and ``prune`` rewrite files in a loop: a failure
    partway through used to be able to leave one file truncated and the rest
    untouched, and a half-written YAML file in the source plane is worse than no
    change at all. Explicit encoding and newline because the corpus carries emoji
    and because ``compiled_sha256`` hashes an LF-only serialisation — a platform
    that translated newlines on write would compute one hash and store another.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(text, encoding="utf-8", newline="\n")
    tmp.replace(path)


def _dump(data: dict[str, Any]) -> str:
    return yaml.dump(
        data,
        Dumper=_Dumper,
        sort_keys=False,
        allow_unicode=True,
        default_flow_style=False,
        # Never wrap. A soft-wrapped query re-flows the moment a neighbouring
        # word changes, turning a one-line edit into a twenty-line diff — the
        # exact reviewability this whole layout is for.
        width=10**9,
    )


def write_runbook_file(
    path: Path, runbook: str, examples: Iterable[Example], *, run: str = LEGACY_RUN
) -> None:
    """Write one runbook's curated file, sections in ``SECTION_SOURCES`` order."""
    grouped: dict[str, list[Any]] = {}
    for ex in examples:
        section = SOURCE_SECTIONS.get(ex.source)
        if section is None:
            raise CorpusError(f"{runbook}: source {ex.source!r} has no runbook-file section")
        grouped.setdefault(section, []).append(
            _example_to_yaml(
                ex,
                implicit_expect=(runbook,),
                file_run=run,
                file_docs=False,
                file_empty_kind=None,
            )
        )

    doc: dict[str, Any] = {"runbook": runbook, "run": run}
    for section in SECTION_SOURCES:
        if section in grouped:
            doc[section] = grouped[section]

    _write(path, _RUNBOOK_HEADER.format(runbook=runbook) + _dump(doc))


def write_shared_file(
    path: Path,
    source: str,
    examples: Iterable[Example],
    *,
    run: str = LEGACY_RUN,
    docs: bool = False,
    empty_kind: str | None = None,
) -> None:
    """Write one shared file: rows that select no runbook, or several."""
    doc: dict[str, Any] = {"source": source, "run": run}
    if docs:
        doc["docs"] = docs
    if empty_kind is not None:
        doc["empty_kind"] = empty_kind

    doc["examples"] = [
        _example_to_yaml(
            ex,
            implicit_expect=(),
            file_run=run,
            file_docs=docs,
            file_empty_kind=empty_kind,
        )
        for ex in examples
    ]

    _write(path, _SHARED_HEADER.format(source=source) + _dump(doc))


_RUNBOOK_HEADER = """\
# Training examples whose answer is `{runbook}` and nothing else.
#
# Sections name what kind of evidence a row is; see ../README.md. An example is
# either a bare query string or a mapping (query/history/active/confusable_with).
# Rows whose answer is several runbooks, or none, live in ../shared/.
"""

_SHARED_HEADER = """\
# Curated rows tagged `{source}` whose answer is not exactly one runbook.
#
# See ../README.md. `source`, `docs` and `empty_kind` at the top are defaults
# every example inherits; an example may override any of them.
"""
