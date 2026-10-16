"""Runbook search over the local two-stage selector.

The search is ``setwise.SetwiseClassifier`` run over a query: a cosine shortlist
over the embedded catalog, then a set-wise pass that scores the shortlist
together and predicts how many runbooks the query needs (zero included). The
head has no per-runbook parameters — it reads runbook EMBEDDINGS — so a runbook
added to the catalog is embedded on load and searchable with no retrain. A
retrain is what makes it competitive against trained neighbours, not what makes
it reachable (``training/EXPERIMENTS.md``).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

from ...contracts.openai import ChatMessage
from ..catalog import Catalog
from .documents import fit_query
from .runtime import InferencePool
from .setwise import Selection, SetwiseClassifier

#: The cardinality head's cap on what it would admit. It decides ``selected``,
#: never how many results a search returns — that is the caller's ``limit``.
MAX_SELECTED = 3


@dataclass(frozen=True)
class RunbookSearchHit:
    """One ranked answer to a runbook search.

    ``score`` is the set-wise head's probability for this runbook against the
    query, not a cosine. ``selected`` says whether the classifier would load it
    for this query: the cardinality answer and admission floors applied. A
    caller wanting "the runbooks Daintree would use" reads ``selected``; one
    browsing for neighbours reads the ranking.
    """

    runbook_id: str
    score: float
    selected: bool


class RunbookSelector(Protocol):
    """The search seam. ``LocalSelector`` in production, ``ScriptedSelector`` in
    ``tests/fakes.py``; ``tests/unit/test_runbook_search.py`` pins the two
    signatures against each other."""

    async def search(
        self, query: str, *, limit: int, exclude: frozenset[str] = frozenset()
    ) -> list[RunbookSearchHit] | None: ...


class LocalSelector:
    """Runbook search by a local model — no upstream call, no credential."""

    def __init__(
        self,
        classifier: SetwiseClassifier,
        pool: InferencePool,
        catalog: Catalog,
    ) -> None:
        self._classifier = classifier
        self._pool = pool
        self._catalog = catalog

    async def search(
        self, query: str, *, limit: int, exclude: frozenset[str] = frozenset()
    ) -> list[RunbookSearchHit] | None:
        """Rank the selectable catalog against ``query``; the top ``limit`` hits.

        The query is a lone user message with nothing loaded. The classifier's
        admitted set becomes ``selected``, but the ranking is what is returned,
        so a caller asking for five runbooks gets five even where the classifier
        would have loaded one or none.

        ``exclude`` is what this caller may not be offered (``Catalog.excluded_for``
        its host state). It is applied before the shortlist, so the rest of the
        catalog ranks as if those runbooks were not there.

        ``None`` when the model could not answer (pool refused, graph failed),
        distinct from ``[]``, which is an honest empty catalog.
        """
        catalog = self._catalog
        classifier = self._classifier

        def rank() -> Selection:
            # Fitted on the worker, not the event loop: fitting tokenizes, a
            # long query can take several passes, and doing it here keeps that
            # work inside pool admission, the timeout and the failure boundary.
            # Rendered against the encoder's 256-token cap rather than blind to
            # it — the encoder truncates from the right, and a query that
            # already fits comes back byte-identical (``documents.fit_query``).
            text = fit_query(
                [ChatMessage(role="user", content=query)], fits=classifier.query_fits
            )
            return classifier.select(
                text, [], catalog=catalog, max_active=MAX_SELECTED, exclude=exclude
            )

        result = await self._pool.run(rank)
        if result is None:
            return None
        selected = set(result.runbook_ids)
        return [
            RunbookSearchHit(runbook_id=sid, score=float(p), selected=sid in selected)
            for sid, p in result.ranked
            if sid in catalog.selectable_ids and sid not in exclude
        ][:limit]
