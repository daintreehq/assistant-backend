"""The immutable skill catalog.

Built once at startup from the packaged skill files. Computes a deterministic
catalog revision used for prompt-cache identity and skill-state validation. A
``Catalog`` instance is immutable; where the catalog can change at runtime (the
local skill-learning harness), a :class:`CatalogManager` holds the current
instance and swaps in a freshly-loaded one — never mutating in place.
"""

from __future__ import annotations

import asyncio
import hashlib
from collections.abc import Iterable, Sequence
from pathlib import Path

from .loader import SkillLoadError, load_skills_dir
from .models import Skill


def default_skills_dir() -> Path:
    return Path(__file__).resolve().parent / "files"


class Catalog:
    """An immutable collection of validated skills."""

    def __init__(self, skills: Iterable[Skill]) -> None:
        by_id: dict[str, Skill] = {}
        for skill in skills:
            if skill.id in by_id:
                raise SkillLoadError(f"duplicate skill id {skill.id!r}")
            by_id[skill.id] = skill
        self._by_id = by_id
        self._skills: tuple[Skill, ...] = tuple(sorted(by_id.values(), key=lambda s: s.id))
        self.revision = self._compute_revision(self._skills)
        # domain -> foundation id, for auto-pairing a selected specific with its
        # domain foundation (see ``domain_foundation``). Built over the id-sorted
        # skills so the tie-break (a domain with two foundations) is deterministic.
        foundation_by_domain: dict[str, str] = {}
        for skill in self._skills:
            if skill.meta.foundation:
                foundation_by_domain.setdefault(self._domain_of(skill.id), skill.id)
        self._foundation_by_domain = foundation_by_domain

    @staticmethod
    def _domain_of(skill_id: str) -> str:
        """The domain key for an id: its namespace minus the final segment.

        ``daintree.orchestration.competition`` and ``daintree.orchestration.basic``
        share the domain ``daintree.orchestration``. An id with no dot is its own
        domain (it cannot pair).
        """
        prefix, sep, _ = skill_id.rpartition(".")
        return prefix if sep else skill_id

    def domain_foundation(self, skill_id: str) -> str | None:
        """The foundation skill id for ``skill_id``'s domain, if any.

        Used to auto-pair a domain's foundation with a selected specific so the
        foundation loads on the SAME turn its specific is selected rather than a
        turn late. Returns the id (possibly ``skill_id`` itself, when it is the
        foundation) or ``None`` when the domain has no foundation.
        """
        return self._foundation_by_domain.get(self._domain_of(skill_id))

    @staticmethod
    def _compute_revision(skills: Sequence[Skill]) -> str:
        entries = sorted(f"{s.id}:{s.content_hash}" for s in skills)
        digest = hashlib.sha256("\n".join(entries).encode("utf-8")).hexdigest()
        return f"sha256:{digest}"

    # ---- access ---------------------------------------------------------

    @property
    def skills(self) -> tuple[Skill, ...]:
        return self._skills

    @property
    def known_ids(self) -> frozenset[str]:
        return frozenset(self._by_id)

    def __len__(self) -> int:
        return len(self._skills)

    def __contains__(self, skill_id: object) -> bool:
        return skill_id in self._by_id

    def get(self, skill_id: str) -> Skill | None:
        return self._by_id.get(skill_id)

    def get_many(self, skill_ids: Iterable[str]) -> list[Skill]:
        """Return skills for the given ids in order, silently skipping unknown ids."""
        out: list[Skill] = []
        for sid in skill_ids:
            skill = self._by_id.get(sid)
            if skill is not None:
                out.append(skill)
        return out

    def filter_known(self, skill_ids: Iterable[str]) -> list[str]:
        return [sid for sid in skill_ids if sid in self._by_id]

    # ---- projections ----------------------------------------------------

    def selector_metadata(self, active_ids: Iterable[str] = ()) -> list[dict[str, object]]:
        active = set(active_ids)
        return [s.selector_view(active=s.id in active) for s in self._skills]

    def public_list(self) -> list[dict[str, object]]:
        return [s.public_view() for s in self._skills]


def load_catalog(
    primary_dir: Path | None = None, overlay_dir: Path | None = None
) -> Catalog:
    """Load and validate the catalog from disk.

    ``primary_dir`` is the packaged skills directory. ``overlay_dir`` (used only by
    the local skill-learning harness when it writes to a separate directory) is a
    second directory whose skills are merged on top: an overlay skill with the same
    id REPLACES the packaged one, and a new id is added. When ``overlay_dir`` is the
    packaged directory itself, or absent/missing, only the primary set is loaded.
    """
    primary = load_skills_dir(primary_dir or default_skills_dir())
    if overlay_dir is None or not overlay_dir.is_dir():
        return Catalog(primary)
    if primary_dir is not None and overlay_dir.resolve() == primary_dir.resolve():
        return Catalog(primary)
    merged: dict[str, Skill] = {s.id: s for s in primary}
    for skill in load_skills_dir(overlay_dir):
        merged[skill.id] = skill  # overlay wins on an id collision
    return Catalog(merged.values())


class CatalogManager:
    """Holds the current :class:`Catalog` and reloads it from disk on demand.

    The catalog itself stays immutable; a reload builds a brand-new instance and
    rebinds the reference (an atomic assignment in CPython), so concurrent readers
    always observe a fully-valid catalog — either the old one or the new one, never
    a half-built mix. Writers (the skill-learning harness) are expected to
    serialize their own write+reload critical section.
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

    @property
    def catalog(self) -> Catalog:
        return self._catalog

    @property
    def primary_dir(self) -> Path:
        return self._primary_dir

    @property
    def overlay_dir(self) -> Path | None:
        return self._overlay_dir

    def reload_sync(self) -> Catalog:
        self._catalog = load_catalog(self._primary_dir, self._overlay_dir)
        return self._catalog

    async def reload(self) -> Catalog:
        """Reload off the event loop (skill files are read with blocking IO)."""
        new = await asyncio.to_thread(load_catalog, self._primary_dir, self._overlay_dir)
        self._catalog = new
        return new

