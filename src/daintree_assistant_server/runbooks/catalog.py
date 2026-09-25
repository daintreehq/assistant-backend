"""The immutable runbook catalog.

Built once at startup from the packaged runbook files. Computes a deterministic
revision over every runbook's content, which search results carry so a caller
can tell when the catalog behind an answer changed.
"""

from __future__ import annotations

import hashlib
from collections.abc import Iterable, Sequence
from pathlib import Path

from .loader import RunbookLoadError, load_runbooks_dir
from .models import Runbook


def default_runbooks_dir() -> Path:
    return Path(__file__).resolve().parent / "files"


# Runbooks the classifier is never shown and may never name. One entry: the docs
# runbook governs answering from documentation the old assistant retrieved and
# injected itself. Nothing retrieves documentation any more, so it is kept only
# because the training corpus and routing benchmark still name it, and it must
# never come back from a search.
DOCS_RUNBOOK_ID = "daintree.docs.answer-usage-question"
_HIDDEN_FROM_SELECTOR = frozenset({DOCS_RUNBOOK_ID})


class Catalog:
    """An immutable collection of validated runbooks."""

    def __init__(self, runbooks: Iterable[Runbook]) -> None:
        by_id: dict[str, Runbook] = {}
        for runbook in runbooks:
            if runbook.id in by_id:
                raise RunbookLoadError(f"duplicate runbook id {runbook.id!r}")
            by_id[runbook.id] = runbook
        self._by_id = by_id
        self._runbooks: tuple[Runbook, ...] = tuple(sorted(by_id.values(), key=lambda s: s.id))
        self.revision = self._compute_revision(self._runbooks)
        # domain -> foundation id, for auto-pairing a selected specific with its
        # nearest foundation (see ``domain_foundation``, which walks up this index).
        # A domain may declare at most ONE foundation: with two, the loser would not
        # even resolve to itself — selecting it would admit BOTH foundations and burn
        # two slots of the active-set budget on one domain's base judgment. There is
        # no sensible tie-break, so this fails closed like any other malformed runbook.
        foundation_by_domain: dict[str, str] = {}
        for runbook in self._runbooks:
            if not runbook.meta.foundation:
                continue
            domain = self._domain_of(runbook.id)
            existing = foundation_by_domain.get(domain)
            if existing is not None:
                raise RunbookLoadError(
                    f"domain {domain!r} declares two foundation runbooks "
                    f"({existing!r} and {runbook.id!r}); a domain may declare at most one"
                )
            foundation_by_domain[domain] = runbook.id
        self._foundation_by_domain = foundation_by_domain
        # Ids the classifier is allowed to name. Precomputed because the selector's
        # output parser tests membership per returned id, and because it is the same
        # answer for every call at a given revision.
        self._selectable_ids = frozenset(
            s.id for s in self._runbooks if s.id not in _HIDDEN_FROM_SELECTOR
        )

    @staticmethod
    def _domain_of(runbook_id: str) -> str:
        """The domain key for an id: its namespace minus the final segment.

        ``daintree.orchestration.competition`` and ``daintree.orchestration.debate``
        share the domain ``daintree.orchestration``; ``daintree.foundation`` keys the
        domain ``daintree``. An id with no dot is its own domain.
        """
        prefix, sep, _ = runbook_id.rpartition(".")
        return prefix if sep else runbook_id

    def domain_foundation(self, runbook_id: str) -> str | None:
        """The nearest foundation runbook id above ``runbook_id``, if any.

        Used to auto-pair a domain's foundation with a selected specific so the
        foundation loads on the SAME turn its specific is selected rather than a
        turn late. Returns the id (possibly ``runbook_id`` itself, when it is the
        foundation) or ``None`` when no ancestor namespace has a foundation.

        The lookup walks UP the dotted id namespace, NEAREST-FIRST: the runbook's
        own domain key, then each successively shorter prefix. So a top-level
        ``daintree.foundation`` (domain key ``daintree``) backs every
        ``daintree.*`` runbook, while a narrower
        ``daintree.orchestration.<something>`` foundation still wins for ids
        inside ``daintree.orchestration``. Without the walk, only runbooks sharing
        the foundation's immediate domain paired at all — every other namespace
        loaded with no base judgment layer.
        """
        domain = self._domain_of(runbook_id)
        while True:
            found = self._foundation_by_domain.get(domain)
            if found is not None:
                return found
            prefix, sep, _ = domain.rpartition(".")
            if not sep:
                return None
            domain = prefix

    @staticmethod
    def _compute_revision(runbooks: Sequence[Runbook]) -> str:
        entries = sorted(f"{s.id}:{s.content_hash}" for s in runbooks)
        digest = hashlib.sha256("\n".join(entries).encode("utf-8")).hexdigest()
        return f"sha256:{digest}"

    # ---- access ---------------------------------------------------------

    @property
    def runbooks(self) -> tuple[Runbook, ...]:
        return self._runbooks

    @property
    def known_ids(self) -> frozenset[str]:
        return frozenset(self._by_id)

    def __len__(self) -> int:
        return len(self._runbooks)

    def __contains__(self, runbook_id: object) -> bool:
        return runbook_id in self._by_id

    def get(self, runbook_id: str) -> Runbook | None:
        return self._by_id.get(runbook_id)

    def get_many(self, runbook_ids: Iterable[str]) -> list[Runbook]:
        """Return runbooks for the given ids in order, silently skipping unknown ids."""
        out: list[Runbook] = []
        for sid in runbook_ids:
            runbook = self._by_id.get(sid)
            if runbook is not None:
                out.append(runbook)
        return out

    def filter_known(self, runbook_ids: Iterable[str]) -> list[str]:
        return [sid for sid in runbook_ids if sid in self._by_id]

    # ---- projections ----------------------------------------------------

    def selector_metadata(self) -> list[dict[str, object]]:
        """Catalog entries for the selector prompt — request-INVARIANT by design.

        Nothing here varies per turn, so the rendered catalog block is byte-stable
        across every selector call for a given catalog revision. The active set is
        passed to the template separately, and everything it varies — the ids
        themselves and the reason budget they select — renders in the short tail
        after this block; when it was an inline per-runbook marker, activating an
        early-sorting runbook
        invalidated most of the prompt on the upstream's content-prefix cache (no call
        sends ``prompt_cache_key`` — with ``require_parameters`` on, an unadvertised
        parameter can empty the eligible-endpoint pool — so a shared content prefix
        is the only thing the upstream can match on).

        Byte-stability is half of what makes that prefix pay. The other half is
        that selector calls carry a shared cache-affinity key
        (``selector._affinity_key``), because a prefix cache is node-local: these
        identical bytes are worth nothing if consecutive calls land on different
        endpoints.
        """
        return [s.selector_view() for s in self._runbooks if s.id in self._selectable_ids]

    @property
    def selectable_ids(self) -> frozenset[str]:
        """Ids the classifier may name — `known_ids` minus the server-pinned ones."""
        return self._selectable_ids

    def selectable(self, runbook_ids: Sequence[str]) -> list[str]:
        """Known ids the SELECTOR may name — `filter_known` minus the pinned ones.

        The active set can legitimately contain a runbook the classifier cannot see,
        so the two filters are different questions: `filter_known` asks "does this
        exist", this asks "may the selector say it".
        """
        return [sid for sid in runbook_ids if sid in self._selectable_ids]



def load_catalog(runbooks_dir: Path | None = None) -> Catalog:
    """Load and validate the catalog from disk (the packaged runbooks by default)."""
    return Catalog(load_runbooks_dir(runbooks_dir or default_runbooks_dir()))
