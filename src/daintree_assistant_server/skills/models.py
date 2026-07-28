"""Skill data models and validation rules.

The on-disk frontmatter uses camelCase keys (``whenToUse``, ``requiredTools``);
those are mapped to snake_case fields. ``extra="forbid"`` keeps the catalog
free of stray hidden metadata.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from pydantic import BaseModel, ConfigDict, Field, field_validator

# Allowed values for the ``risk`` enum (mirrors the Daintree CLI).
RISK_VALUES: frozenset[str] = frozenset(
    {"read", "local", "ui", "terminal", "project", "external", "git", "system"}
)

TOOL_NAME_RE = re.compile(r"^[A-Za-z0-9_.\-]+$")


class SkillMeta(BaseModel):
    """Validated skill frontmatter."""

    model_config = ConfigDict(extra="forbid", populate_by_name=True, frozen=True)

    id: str = Field(min_length=1, max_length=256)
    title: str = Field(min_length=1, max_length=256)
    summary: str = Field(min_length=1, max_length=2048)
    when_to_use: str = Field(min_length=1, max_length=4096, alias="whenToUse")
    # Layer marker: a foundation skill carries the base judgment for a whole
    # domain (how to read state, choose safe actions) rather than one concrete
    # workflow. The selector is told to pair a domain's foundation with the
    # specific workflow skill(s) it selects — see selector/system.md.j2.
    foundation: bool = Field(default=False)
    tags: list[str] = Field(default_factory=list)
    risk: str = "read"
    required_tools: list[str] = Field(default_factory=list, alias="requiredTools")

    @field_validator("id")
    @classmethod
    def _check_id_segments(cls, v: str) -> str:
        """Reject empty dotted segments — they silently break foundation pairing.

        ``Catalog._domain_of`` splits an id on its final dot and ``domain_foundation``
        walks the remaining prefixes, so ``.x`` collapses into the EMPTY domain and
        ``x.`` makes an id its own domain key. Both load fine and then pair with the
        wrong foundation (or none) with no error anywhere. This is deliberately only a
        well-formedness check, not the learner's stricter ``daintree.<domain>.<workflow>``
        shape: hand-authored ids and test fixtures legitimately use other namespaces.
        """
        if any(seg == "" for seg in v.split(".")):
            raise ValueError(f"skill id {v!r} has an empty dotted segment")
        return v

    @field_validator("risk")
    @classmethod
    def _check_risk(cls, v: str) -> str:
        if v not in RISK_VALUES:
            raise ValueError(f"risk {v!r} not in {sorted(RISK_VALUES)}")
        return v

    @field_validator("tags")
    @classmethod
    def _check_tags(cls, v: list[str]) -> list[str]:
        seen: set[str] = set()
        out: list[str] = []
        for raw in v:
            tag = raw.strip().lower()
            if not tag:
                raise ValueError("tags must be non-empty strings")
            if tag in seen:
                raise ValueError(f"duplicate tag {tag!r}")
            seen.add(tag)
            out.append(tag)
        return out

    @field_validator("required_tools")
    @classmethod
    def _check_required_tools(cls, v: list[str]) -> list[str]:
        out: list[str] = []
        for raw in v:
            name = raw.strip()
            if not name or not TOOL_NAME_RE.match(name):
                raise ValueError(f"invalid required tool name {raw!r}")
            out.append(name)
        return out


@dataclass(frozen=True)
class Skill:
    """A fully-loaded skill: validated metadata, normalized body, content hash."""

    meta: SkillMeta
    body: str
    content_hash: str

    @property
    def id(self) -> str:
        return self.meta.id

    @property
    def title(self) -> str:
        return self.meta.title

    @property
    def summary(self) -> str:
        return self.meta.summary

    @property
    def when_to_use(self) -> str:
        return self.meta.when_to_use

    @property
    def risk(self) -> str:
        return self.meta.risk

    def selector_view(self) -> dict[str, object]:
        """The bounded metadata the selector is allowed to see.

        Deliberately narrower than the frontmatter: only the fields the selector
        prompt actually renders AND instructs on. ``title`` restates the summary,
        and ``tags``/``risk``/``required_tools`` are near-non-discriminative
        within a single-product catalog with no instruction referencing them —
        they were pure prompt weight, so they are not shown to the classifier.
        (They remain in :meth:`public_view` and are still read server-side, e.g.
        ``required_tools`` for workflow-plan skill outlines.)

        Request-varying state is NOT here: which skills are currently active is
        rendered once after the catalog block, so the per-skill lines stay
        byte-identical across turns for DeepSeek's content-prefix cache.
        """
        return {
            "id": self.meta.id,
            "summary": self.meta.summary,
            "when_to_use": self.meta.when_to_use,
            "foundation": self.meta.foundation,
        }

    def public_view(self) -> dict[str, object]:
        """The metadata exposed by the public catalog endpoint (never the body)."""
        return {
            "id": self.meta.id,
            "title": self.meta.title,
            "summary": self.meta.summary,
            "when_to_use": self.meta.when_to_use,
            "tags": list(self.meta.tags),
            "risk": self.meta.risk,
            "required_tools": list(self.meta.required_tools),
        }

    def ref(self) -> dict[str, str]:
        """A minimal {id, title} reference."""
        return {"id": self.meta.id, "title": self.meta.title}
