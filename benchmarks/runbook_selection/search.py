"""Queries through the search ``search_runbooks`` serves — offline and free.

    python -m benchmarks.runbook_selection.bench search "rename my terminals"
    python -m benchmarks.runbook_selection.bench search --file queries.txt --max-results 10

It is ``LocalSelector.search`` itself, over the working tree's catalog and the
shipped artifact, so the ranking printed here is what a caller gets from this
tree once it is released. Measuring a phrasing costs one forward pass, which is
why a routing claim about a real request should always come with one.
"""

from __future__ import annotations

import asyncio
from collections.abc import Sequence
from pathlib import Path

from daintree_assistant_server.contracts.openai import ChatMessage
from daintree_assistant_server.runbook_mcp import MAX_QUERY_CHARS, MAX_RESULTS_CAP
from daintree_assistant_server.runbooks.catalog import Catalog, load_catalog
from daintree_assistant_server.runbooks.host_state import HostState
from daintree_assistant_server.runbooks.local.documents import fit_query, query_document
from daintree_assistant_server.runbooks.local.paths_local import selector_artifact_dir
from daintree_assistant_server.runbooks.local.runtime import InferencePool
from daintree_assistant_server.runbooks.local.selector import LocalSelector, RunbookSearchHit
from daintree_assistant_server.runbooks.local.setwise import SetwiseClassifier

SearchResult = list[RunbookSearchHit] | None


def request_errors(queries: Sequence[str], limit: int) -> list[str]:
    """What the tool would refuse, in its own terms (`runbook_mcp.py`).

    A ranking for a request the MCP rejects measures nothing a caller can get.
    """
    errors = []
    if not 1 <= limit <= MAX_RESULTS_CAP:
        errors.append(f"--max-results must be from 1 to {MAX_RESULTS_CAP}, as the tool's is")
    for query in queries:
        if not query.strip():
            errors.append("a query is blank; the tool refuses a blank query")
        elif len(query) > MAX_QUERY_CHARS:
            errors.append(f"a query is over {MAX_QUERY_CHARS} characters; the tool refuses it")
    return errors


def read_queries(path: Path) -> list[str]:
    """One query per line; blank lines and ``#`` comments are skipped."""
    lines = (line.strip() for line in path.read_text(encoding="utf-8").splitlines())
    return [line for line in lines if line and not line.startswith("#")]


def search(
    queries: Sequence[str], *, limit: int, host_state: HostState | None = None
) -> tuple[Catalog, list[SearchResult], list[bool]]:
    """Rankings, plus whether each query had to be shrunk to fit the encoder.

    ``host_state`` ranks for a caller reporting it, as the transport does from
    its headers; ``None`` is an unreported caller, offered the whole catalog.

    A shrunk query is where serving and ``bench local`` part ways: search renders
    it with ``fit_query``, the benchmark with ``query_document``, and they agree
    byte for byte only when the query fits.
    """
    catalog = load_catalog()
    classifier = SetwiseClassifier(selector_artifact_dir())
    classifier.sync(catalog)
    shrunk = []
    for query in queries:
        message = [ChatMessage(role="user", content=query.strip())]
        fitted = fit_query(message, fits=classifier.query_fits)
        shrunk.append(fitted != query_document(message))
    pool = InferencePool()

    async def run_all() -> list[SearchResult]:
        selector = LocalSelector(classifier, pool, catalog)
        exclude = catalog.excluded_for(host_state)
        # Stripped as the tool strips it (`runbook_mcp.py`), so a padded query
        # pasted from a transcript ranks exactly as it did for the caller.
        return [
            await selector.search(query.strip(), limit=limit, exclude=exclude)
            for query in queries
        ]

    try:
        return catalog, asyncio.run(run_all()), shrunk
    finally:
        pool.close()


def print_search(
    catalog: Catalog,
    queries: Sequence[str],
    results: Sequence[SearchResult],
    shrunk: Sequence[bool],
) -> None:
    print(f"catalog_revision: {catalog.revision}")
    for query, hits, was_shrunk in zip(queries, results, shrunk, strict=True):
        print(f"\n> {query}")
        if was_shrunk:
            print("  longer than the encoder reads: search shrank it, and bench local would not")
        if hits is None:
            print("  search failed: the pool refused the work or the graph raised")
            continue
        if not any(hit.selected for hit in hits):
            print("  nothing selected: no runbook really fits")
        for rank, hit in enumerate(hits, 1):
            mark = "selected" if hit.selected else ""
            print(f"  {rank:>2}. {hit.score:.4f}  {mark:<8}  {hit.runbook_id}")
