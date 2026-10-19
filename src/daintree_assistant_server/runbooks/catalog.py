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

from .host_state import HOSTS_NAMESPACE, CallerState, HostModel, HostState, fits
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


def load_catalog(runbooks_dir: Path | None = None) -> Catalog:
    """Load and validate the catalog from disk (the packaged runbooks by default).

    The packaged runbooks are the CONTROL catalog, which every tool (benchmark,
    corpus, training) reads unless it asks for the inbox one (``load_catalogs``).
    """
    return Catalog(load_runbooks_dir(runbooks_dir or default_runbooks_dir()))


# ---- one catalog per host model ------------------------------------------
#
# Daintree drives its Remote Hosts in one of two ways (``host_state.HostModel``):
# v0.44 messages a host's assistant through an inbox, and later builds control the
# host directly through `host.call`. Both are live while v0.44 is installed, so the
# server holds a catalog for each and serves a caller from its own, before search,
# exact retrieval, supporting delivery and the revision: a v0.44 client never
# sees `host.call`, and a later one never sees an inbox tool.
#
# The control catalog is `files/`. The inbox catalog is that, minus the hosts
# namespace and every runbook `inbox/` overrides, plus `inbox/`: the v0.44 copies,
# frozen, of every hosts runbook and of each shared runbook whose text differs
# between the models. Every other runbook is shared, and keeps improving for both.

#: The control catalog's hosts foundation: how to work on another machine. A
#: caller with a host connected gets it with every runbook it is served in full,
#: whatever the domain, since any task may be one whose work is on a host.
HOSTS_FOUNDATION_ID = "daintree.hosts.foundation"


def default_inbox_dir() -> Path:
    return Path(__file__).resolve().parent / "inbox"


@dataclass(frozen=True)
class CatalogView:
    """What one caller is served from: its catalog, what its search leaves out, and
    what it gets with every runbook delivered in full (``Catalog.supporting_for``)."""

    catalog: Catalog
    exclude: frozenset[str]
    attach: tuple[str, ...]


@dataclass(frozen=True)
class Catalogs:
    """The two models' catalogs, each validated on its own."""

    control: Catalog
    inbox: Catalog

    def for_model(self, model: HostModel) -> Catalog:
        return self.control if model == "control" else self.inbox

    def view_for(self, state: CallerState) -> CatalogView:
        catalog = self.for_model(state.model)
        attach: tuple[str, ...] = ()
        if (
            state.model == "control"
            and state.hosts is not None
            and state.hosts.hosts == "connected"
            and HOSTS_FOUNDATION_ID in catalog
        ):
            attach = (HOSTS_FOUNDATION_ID,)
        return CatalogView(
            catalog=catalog, exclude=catalog.excluded_for(state.hosts), attach=attach
        )


def compose_inbox(shared: Sequence[Runbook], frozen: Sequence[Runbook]) -> list[Runbook]:
    """The inbox catalog's runbooks: ``frozen`` in place of the shared hosts
    namespace and of every shared runbook it names.

    A frozen runbook must stand for something: a hosts runbook, or a shared one it
    overrides. One that overrides nothing would be a v0.44-only runbook outside the
    hosts namespace, which nothing keeps out of the control catalog's way.
    """
    shared_ids = {r.id for r in shared}
    for runbook in frozen:
        if not runbook.id.startswith(HOSTS_NAMESPACE) and runbook.id not in shared_ids:
            raise RunbookLoadError(
                f"inbox runbook {runbook.id!r} overrides no shared runbook and is not a "
                "hosts runbook"
            )
    overridden = {r.id for r in frozen}
    kept = [
        r for r in shared if r.id not in overridden and not r.id.startswith(HOSTS_NAMESPACE)
    ]
    return [*kept, *frozen]


def load_catalogs(runbooks_dir: Path | None = None, inbox_dir: Path | None = None) -> Catalogs:
    """Load and validate both models' catalogs (the packaged runbooks by default).

    Fails closed like any malformed runbook: a lane runbook in the control catalog
    (it has no lanes), or one whose supporting runbooks pass the bound once the
    hosts foundation is attached.
    """
    shared = load_runbooks_dir(runbooks_dir or default_runbooks_dir())
    frozen = load_runbooks_dir(inbox_dir or default_inbox_dir())
    control = Catalog(shared)
    for runbook in control.runbooks:
        if runbook.meta.host_eligibility == "lane":
            raise RunbookLoadError(
                f"{runbook.id!r} is a lane runbook; the control catalog has no lanes"
            )
        attached = control.supporting_for([runbook.id], attach=(HOSTS_FOUNDATION_ID,))
        if len(attached) > MAX_SUPPORTING_RUNBOOKS:
            raise RunbookLoadError(
                f"{runbook.id!r} pulls in {len(attached)} supporting runbooks with the hosts "
                f"foundation attached, over the bound of {MAX_SUPPORTING_RUNBOOKS}"
            )
    return Catalogs(control=control, inbox=Catalog(compose_inbox(shared, frozen)))
