"""The immutable runbook catalog.

Built at startup from the packaged runbook files, plus a local overlay directory
when one is configured. Computes a deterministic catalog revision used for
prompt-cache identity and runbook-state validation. A ``Catalog`` instance is
immutable; the catalog changes at runtime only by REPLACEMENT — a
:class:`CatalogManager` holds the current instance and swaps in a freshly-loaded
one, never mutating in place. Today the one thing that asks for a swap is the
local runbook-overlay reload endpoint.
"""

from __future__ import annotations

import asyncio
import hashlib
from collections.abc import Iterable, Sequence
from pathlib import Path

from ..config import DOCS_RUNBOOK_ID
from .loader import RunbookLoadError, load_runbooks_dir
from .models import Runbook


def default_runbooks_dir() -> Path:
    return Path(__file__).resolve().parent / "files"


# Runbooks the classifier is never shown and may never name. One entry today: the
# server pins the docs runbook itself whenever a turn produces a `docs_query`
# (see ``config.DOCS_RUNBOOK_ID``), so offering it to the classifier would spend
# selector bytes on every call and add a false-positive surface for a decision
# that has already been made. Hidden here rather than filtered at the prompt, so
# `public_list` and the injected bodies are untouched — it is a real, loadable
# runbook, just not a selectable one.
_HIDDEN_FROM_SELECTOR = frozenset({DOCS_RUNBOOK_ID})


# Distinct tool inventories a catalog generation will memoise a derived subset for.
# Real callers present a handful (one per client build x profile); the bound exists
# only so a caller sending varying junk names cannot grow the map without limit.
_EXECUTABLE_CACHE_MAX = 32


def _normalize_tool(name: str) -> str:
    """The dotted form of a tool name. ``requiredTools`` is authored dotted while a
    client inventory arrives wire-encoded (``fs__read``), so both sides normalize
    here rather than at each comparison."""
    return name.replace("__", ".")


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
        # Memoised `executable_with` results, keyed by the normalized tool set. The
        # catalog is immutable, so a cached subset can never go stale — but the key
        # is CALLER-SUPPLIED, so it is bounded (see _EXECUTABLE_CACHE_MAX): a client
        # sending a different junk tool name each request would otherwise grow this
        # without limit for the life of the catalog generation. Insertion-ordered,
        # evicted oldest-first, which suits the real access pattern (a handful of
        # stable inventories, hit over and over).
        self._executable_cache: dict[frozenset[str], Catalog] = {}
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
        # Every tool ANY runbook declares, in WIRE form — the set the catalog can be
        # said to have an opinion about. Tool projection withholds on the argument
        # "no active runbook needs this", and that argument is only available for a
        # tool some runbook mentions; anything outside this set is passed through
        # rather than judged by silence. Precomputed for the same reason as
        # `_selectable_ids`: it is the same answer for every request at a given
        # revision, and the alternative is walking the whole catalog per turn.
        self._declared_tools = frozenset(
            tool.replace(".", "__")
            for runbook in self._runbooks
            for tool in (runbook.meta.required_tools or ())
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

    @property
    def declared_tools(self) -> frozenset[str]:
        """Wire names of every tool some runbook in this catalog declares.

        Note this is the NARROWED catalog's answer when a sub-agent narrowed it:
        a tool only orchestration runbooks declare reads as undeclared to a menu
        that holds none of them, and is passed through rather than withheld. That
        is the safe direction — a narrowed menu knows less, so it withholds less.
        """
        return self._declared_tools

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

    def public_list(self) -> list[dict[str, object]]:
        return [s.public_view() for s in self._runbooks]

    # ---- derived catalogs -----------------------------------------------

    def executable_with(self, tool_names: Iterable[str]) -> Catalog:
        """The sub-catalog a caller holding only ``tool_names`` could actually run.

        A runbook is a PROCEDURE, and one whose steps call tools the caller does not
        have is worse than no runbook at all: it spends context, then instructs the
        model toward calls that can only fail. That is the exact situation a
        read-only sub-agent is in — it is offered ~36 read tools, while most of the
        catalog drives spawning, worktrees and terminals — so the menu it chooses
        from has to be filtered before selection, not after.

        The gate is ``requiredTools``, which runbooks already declare and which the
        tool projection already trusts. Using it here means a new runbook is
        classified correctly the day it is written, with no second list to keep in
        step. A runbook declaring NO required tools is admitted: it makes no tool
        claim, so there is nothing to be unable to satisfy.

        Names are normalized to the dotted form on both sides — ``requiredTools``
        is authored dotted (``context.snapshot``) while a client's inventory
        arrives in the wire form (``context__snapshot``), and comparing them raw
        silently matches nothing, which would filter the catalog down to runbooks
        that merely happen to declare no tools.

        Cached per normalized tool set: the caller's inventory is fixed for a build,
        so this computes once and every later sub-agent reuses it — which also keeps
        the selector's memoised system render stable, since it keys on the catalog's
        revision.
        """
        wanted = frozenset(_normalize_tool(n) for n in tool_names)
        cached = self._executable_cache.get(wanted)
        if cached is not None:
            return cached
        subset = Catalog(
            runbook
            for runbook in self._runbooks
            if all(_normalize_tool(t) in wanted for t in (runbook.meta.required_tools or ()))
        )
        while len(self._executable_cache) >= _EXECUTABLE_CACHE_MAX:
            self._executable_cache.pop(next(iter(self._executable_cache)))
        self._executable_cache[wanted] = subset
        return subset


def load_catalog(primary_dir: Path | None = None, overlay_dir: Path | None = None) -> Catalog:
    """Load and validate the catalog from disk.

    ``primary_dir`` is the packaged runbooks directory. ``overlay_dir`` (local
    development only, from ``DAINTREE_RUNBOOKS_OVERLAY_DIR``) is a scratch directory
    whose runbooks are merged on top: an overlay runbook with the same id REPLACES the
    packaged one, and a new id is added. When ``overlay_dir`` is the packaged
    directory itself, or absent/missing, only the primary set is loaded — a
    developer who has not created the directory yet gets the packaged catalog
    rather than a startup failure.
    """
    primary = load_runbooks_dir(primary_dir or default_runbooks_dir())
    if overlay_dir is None or not overlay_dir.exists():
        return Catalog(primary)
    # An overlay path that exists but is not a directory is a typo, and the one
    # thing worse than failing on it is succeeding: the developer edits runbooks
    # into a path nothing reads, reloads, and is told nothing changed.
    if not overlay_dir.is_dir():
        raise RunbookLoadError(f"runbook overlay path is not a directory: {overlay_dir}")
    if primary_dir is not None and overlay_dir.resolve() == primary_dir.resolve():
        return Catalog(primary)
    merged: dict[str, Runbook] = {s.id: s for s in primary}
    for runbook in load_runbooks_dir(overlay_dir):
        merged[runbook.id] = runbook  # overlay wins on an id collision
    return Catalog(merged.values())


class CatalogManager:
    """Holds the current :class:`Catalog` and reloads it from disk on demand.

    The catalog itself stays immutable; a reload builds a brand-new instance and
    rebinds the reference (an atomic assignment in CPython), so concurrent readers
    always observe a fully-valid catalog — either the old one or the new one, never
    a half-built mix. That ordering is also what makes a reload fail SAFE: the
    whole new catalog is loaded and validated before the assignment, so a malformed
    overlay runbook leaves the previous catalog serving untouched.
    """

    def __init__(
        self,
        catalog: Catalog,
        *,
        primary_dir: Path,
        overlay_dir: Path | None = None,
    ) -> None:
        self._catalog = catalog
        self._primary_dir = primary_dir
        self._overlay_dir = overlay_dir
        self._reload_lock = asyncio.Lock()

    @property
    def catalog(self) -> Catalog:
        return self._catalog

    @property
    def primary_dir(self) -> Path:
        return self._primary_dir

    @property
    def overlay_dir(self) -> Path | None:
        return self._overlay_dir

    def reload_sync(self) -> tuple[str, Catalog]:
        previous = self._catalog.revision
        self._catalog = load_catalog(self._primary_dir, self._overlay_dir)
        return previous, self._catalog

    async def reload(self) -> tuple[str, Catalog]:
        """Reload off the event loop; returns (revision replaced, new catalog).

        Serialized on the manager's own lock, and reporting what it replaced
        rather than leaving the caller to read that before calling. Both exist
        for the same reason: two concurrent reloads each complete their load
        before rebinding, so neither can expose a half-built catalog, but they
        can still FINISH out of order and leave the OLDER one bound — while the
        loser cheerfully reports the revision it thought it was replacing. To an
        author that is an edit which silently reverted. Holding the lock across
        the read AND the swap is what makes the reported pair true.

        The lock is deliberately inside this method rather than something a
        caller is trusted to take: a serialization rule that has to be
        remembered at every call site is one that eventually is not.
        """
        async with self._reload_lock:
            previous = self._catalog.revision
            new = await asyncio.to_thread(load_catalog, self._primary_dir, self._overlay_dir)
            self._catalog = new
            return previous, new
