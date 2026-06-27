"""The immutable skill catalog.

Built once at startup from the packaged skill files. Computes a deterministic
catalog revision used for prompt-cache identity and skill-state validation.
Skills are immutable for the lifetime of a deployment revision.
"""

from __future__ import annotations

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


def load_catalog(directory: Path | None = None) -> Catalog:
    """Load and validate the catalog from disk."""
    return Catalog(load_skills_dir(directory or default_skills_dir()))
