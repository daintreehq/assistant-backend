"""The immutable runbook catalog.

Built once at startup from the packaged runbook files. Computes a deterministic
revision over every runbook's content, which search results carry so a caller
can tell when the catalog behind an answer changed.
"""

from __future__ import annotations

import hashlib
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from pathlib import Path

from .host_state import HostState, fits
from .loader import RunbookLoadError, load_runbooks_dir
from .models import Runbook


def default_runbooks_dir() -> Path:
    return Path(__file__).resolve().parent / "catalog"


# A reserved tombstone for the old documentation procedure, now archived. Keep
# refusing it if a synthetic or accidentally reintroduced runbook names that id.
DOCS_RUNBOOK_ID = "daintree.docs.answer-usage-question"
_HIDDEN_FROM_SELECTOR = frozenset({DOCS_RUNBOOK_ID})

#: The most supporting runbooks one runbook's instructions may pull in (its
#: foundation plus everything `requires` reaches). Checked at load, so a runaway
#: dependency chain fails readiness instead of a response silently dropping part
#: of a procedure's prerequisites at serving time.
MAX_SUPPORTING_RUNBOOKS = 6


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
        self._supporting = self._resolve_dependencies()

    # ---- instruction dependencies -----------------------------------------

    def _direct_dependencies(self, runbook_id: str) -> list[str]:
        """What must be read before ``runbook_id``: its nearest foundation, then its
        declared ``requires`` in order.

        The foundation edge is implicit because every specific relies on its domain's
        base judgment; a foundation has no implicit edge to an ANCESTOR foundation,
        so a nested foundation that builds on ``daintree.foundation`` must say so in
        ``requires``.
        """
        deps: list[str] = []
        foundation = self.domain_foundation(runbook_id)
        if foundation is not None and foundation != runbook_id:
            deps.append(foundation)
        for ref in self._by_id[runbook_id].meta.requires:
            if ref not in deps:
                deps.append(ref)
        return deps

    def _resolve_dependencies(self) -> dict[str, tuple[str, ...]]:
        """Validate the dependency graph and precompute each runbook's closure.

        Fails closed, like any other malformed runbook: an unknown or hidden
        ``requires`` target, a cycle through explicit OR implicit foundation edges
        (a foundation requiring one of its own specifics is the easy one to write),
        or a closure over the bound. Closure order is dependencies first, each
        exactly once, so the general foundation leads and the specific procedures a
        runbook builds on follow.
        """
        for runbook in self._runbooks:
            for ref in runbook.meta.requires:
                if ref not in self._by_id:
                    raise RunbookLoadError(f"{runbook.id!r} requires unknown runbook {ref!r}")
                if ref in _HIDDEN_FROM_SELECTOR:
                    raise RunbookLoadError(
                        f"{runbook.id!r} requires {ref!r}, which is never served"
                    )

        closures: dict[str, tuple[str, ...]] = {}
        visiting: list[str] = []

        def visit(runbook_id: str) -> tuple[str, ...]:
            done = closures.get(runbook_id)
            if done is not None:
                return done
            if runbook_id in visiting:
                cycle = visiting[visiting.index(runbook_id) :] + [runbook_id]
                raise RunbookLoadError(
                    "runbook instruction dependencies form a cycle: " + " -> ".join(cycle)
                )
            visiting.append(runbook_id)
            ordered: list[str] = []
            for dep in self._direct_dependencies(runbook_id):
                for inherited in (*visit(dep), dep):
                    if inherited not in ordered:
                        ordered.append(inherited)
            visiting.pop()
            closures[runbook_id] = tuple(ordered)
            return closures[runbook_id]

        for runbook in self._runbooks:
            closure = visit(runbook.id)
            if len(closure) > MAX_SUPPORTING_RUNBOOKS:
                raise RunbookLoadError(
                    f"{runbook.id!r} pulls in {len(closure)} supporting runbooks, over the "
                    f"bound of {MAX_SUPPORTING_RUNBOOKS}: {list(closure)}"
                )
        return closures

    def supporting_for(
        self, runbook_ids: Sequence[str], *, attach: Sequence[str] = ()
    ) -> list[Runbook]:
        """The instruction dependencies of ``runbook_ids``, each once, in order.

        ``attach`` names runbooks this caller gets with every runbook it is served
        in full, as if each depended on them (``CatalogView.attach``): they and
        their own dependencies come first, so a general foundation still leads.
        Nothing is attached to an empty ``runbook_ids``.

        Anything already among ``runbook_ids`` is left out: a foundation that is
        itself a search hit is not sent twice. Unknown ids contribute nothing.
        """
        primary = set(runbook_ids)
        attached = [
            dep
            for extra in attach
            if extra in self._by_id
            for dep in (*self._supporting[extra], extra)
        ]
        ordered: list[str] = []
        for runbook_id in runbook_ids:
            for dep in (*attached, *self._supporting.get(runbook_id, ())):
                if dep not in primary and dep not in ordered:
                    ordered.append(dep)
        return [self._by_id[dep] for dep in ordered]

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

        Serving uses it to deliver a specific's foundation alongside it (see
        ``supporting_for``), and the classifier's training and benchmark replay use
        it to pair the two in an active set. Returns the id (possibly
        ``runbook_id`` itself, when it is the foundation) or ``None`` when no
        ancestor namespace has a foundation.

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

    def get_public(self, runbook_id: str) -> Runbook | None:
        """A runbook a caller may be served by id — never a hidden one.

        ``get`` answers "is it loaded"; this answers "may it leave the server". The
        hidden docs runbook is loaded for the corpus and benchmark, and exact
        retrieval must not become a second road to it.
        """
        return self._by_id.get(runbook_id) if runbook_id in self._selectable_ids else None

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
        """Selectable metadata for offline coverage and catalog inspection."""
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

    def excluded_for(self, state: HostState | None) -> frozenset[str]:
        """Runbooks a search must leave out for a caller in host ``state``.

        Empty when the state is unreported, so such a caller searches the whole
        catalog. Only runbooks declaring ``hostEligibility`` can appear: the rest of
        the catalog fits every caller. Exact retrieval and supporting delivery
        ignore it; it narrows what a search offers, nothing else.
        """
        if state is None:
            return frozenset()
        return frozenset(
            s.id
            for s in self._runbooks
            if s.meta.host_eligibility is not None and not fits(s.meta.host_eligibility, state)
        )


HOSTS_FOUNDATION_ID = "daintree.hosts.foundation"


@dataclass(frozen=True)
class CatalogView:
    """The catalog, exclusions and instruction attachments for one request."""

    catalog: Catalog
    exclude: frozenset[str]
    attach: tuple[str, ...]


def view_for(catalog: Catalog, state: HostState | None) -> CatalogView:
    attach = (
        (HOSTS_FOUNDATION_ID,)
        if state is not None and state.hosts == "connected" and HOSTS_FOUNDATION_ID in catalog
        else ()
    )
    return CatalogView(catalog=catalog, exclude=catalog.excluded_for(state), attach=attach)


def load_catalog(runbooks_dir: Path | None = None) -> Catalog:
    """Load the current catalog, including domain folders, and validate its bundles.

    Retired generations live outside the package in ``archive/`` and are never
    loaded at startup or offered to callers.
    """
    catalog = Catalog(load_runbooks_dir(runbooks_dir or default_runbooks_dir()))
    for runbook in catalog.runbooks:
        attached = catalog.supporting_for([runbook.id], attach=(HOSTS_FOUNDATION_ID,))
        if len(attached) > MAX_SUPPORTING_RUNBOOKS:
            raise RunbookLoadError(
                f"{runbook.id!r} pulls in {len(attached)} supporting runbooks with the hosts "
                f"foundation attached, over the bound of {MAX_SUPPORTING_RUNBOOKS}"
            )
    return catalog
