"""Test doubles."""

from __future__ import annotations

import time
from typing import Any

from daintree_assistant_server.runbooks.local.runtime import InferencePool, RankTrace
from daintree_assistant_server.runbooks.local.selector import RunbookSearchHit


class ScriptedSelector:
    """A ``RunbookSelector`` whose answer is scripted, not ranked by a model.

    Lets the wire tests run without loading the ONNX artifact, so they pass on a
    clone without Git LFS. The real search is tested against the shipped
    artifact in ``tests/unit/test_runbook_search.py``.
    """

    def __init__(self, catalog: Any) -> None:
        self.catalog = catalog
        #: Scripted answer, as (runbook id, score, selected) in rank order. None
        #: ranks the selectable catalog by id with falling scores — deterministic,
        #: and enough for a test about the wire rather than the ranking.
        self.search_hits: list[tuple[str, float, bool]] | None = None
        #: Makes the model "unable to answer".
        self.search_fails = False
        self.search_requests: list[tuple[str, int]] = []
        #: What each search was told to leave out, parallel to ``search_requests``.
        self.search_excludes: list[frozenset[str]] = []
        #: A real pool to answer through, so its fail-open paths (refused, timed
        #: out) and its trace reach the wire; ``rank_seconds`` is the scripted
        #: answer's time on the worker.
        self.pool: InferencePool | None = None
        self.rank_seconds = 0.0

    async def search(
        self,
        query: str,
        *,
        limit: int,
        exclude: frozenset[str] = frozenset(),
        trace: RankTrace | None = None,
    ) -> list[RunbookSearchHit] | None:
        self.search_requests.append((query, limit))
        self.search_excludes.append(exclude)
        if self.pool is not None:
            return await self.pool.run(lambda: self._rank(limit, exclude), trace)
        if self.search_fails:
            return None
        return self._rank(limit, exclude)

    def _rank(self, limit: int, exclude: frozenset[str]) -> list[RunbookSearchHit]:
        if self.search_fails:
            raise RuntimeError("scripted inference failure")
        if self.rank_seconds:
            time.sleep(self.rank_seconds)
        hits = self.search_hits
        if hits is None:
            ids = sorted(self.catalog.selectable_ids)
            hits = [(sid, round(0.9 - i * 0.01, 4), False) for i, sid in enumerate(ids)]
        return [
            RunbookSearchHit(runbook_id=sid, score=score, selected=selected)
            for sid, score, selected in hits
            if sid not in exclude
        ][:limit]
