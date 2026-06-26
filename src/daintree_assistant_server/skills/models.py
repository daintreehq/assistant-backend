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

SEMVER_RE = re.compile(r"^\d+\.\d+\.\d+(?:[-+][0-9A-Za-z.-]+)?$")
TOOL_NAME_RE = re.compile(r"^[A-Za-z0-9_.\-]+$")

PRIORITY_MIN = 0
PRIORITY_MAX = 1000


class SkillMeta(BaseModel):
    """Validated skill frontmatter."""

    model_config = ConfigDict(extra="forbid", populate_by_name=True, frozen=True)

    id: str = Field(min_length=1, max_length=256)
    title: str = Field(min_length=1, max_length=256)
    version: str = Field(min_length=1, max_length=64)
    summary: str = Field(min_length=1, max_length=2048)
    when_to_use: str = Field(min_length=1, max_length=4096, alias="whenToUse")
    tags: list[str] = Field(default_factory=list)
    priority: int = Field(default=0, ge=PRIORITY_MIN, le=PRIORITY_MAX)
    risk: str = "read"
    required_tools: list[str] = Field(default_factory=list, alias="requiredTools")

    @field_validator("version")
    @classmethod
    def _check_version(cls, v: str) -> str:
        if not SEMVER_RE.match(v):
            raise ValueError(f"version {v!r} is not a valid SemVer-compatible string")
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
    def version(self) -> str:
        return self.meta.version

    @property
    def summary(self) -> str:
        return self.meta.summary

    @property
    def when_to_use(self) -> str:
        return self.meta.when_to_use

    @property
    def risk(self) -> str:
        return self.meta.risk

    def selector_view(self, *, active: bool) -> dict[str, object]:
        """The bounded metadata the selector is allowed to see."""
        return {
            "id": self.meta.id,
            "title": self.meta.title,
            "summary": self.meta.summary,
            "when_to_use": self.meta.when_to_use,
            "tags": list(self.meta.tags),
            "priority": self.meta.priority,
            "risk": self.meta.risk,
            "required_tools": list(self.meta.required_tools),
            "active": active,
        }

    def public_view(self) -> dict[str, object]:
        """The metadata exposed by the public catalog endpoint (never the body)."""
        return {
            "id": self.meta.id,
            "title": self.meta.title,
            "version": self.meta.version,
            "summary": self.meta.summary,
            "when_to_use": self.meta.when_to_use,
            "tags": list(self.meta.tags),
            "priority": self.meta.priority,
            "risk": self.meta.risk,
            "required_tools": list(self.meta.required_tools),
        }

    def ref(self) -> dict[str, str]:
        """A minimal {id, version, title} reference."""
        return {"id": self.meta.id, "version": self.meta.version, "title": self.meta.title}
