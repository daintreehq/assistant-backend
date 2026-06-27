"""Skill-state token payload models."""

from __future__ import annotations

from dataclasses import dataclass

from pydantic import BaseModel, ConfigDict, Field


class ActiveSkillRef(BaseModel):
    model_config = ConfigDict(extra="ignore")

    id: str


class StatePayload(BaseModel):
    """The logical payload carried by a signed state token."""

    model_config = ConfigDict(extra="ignore")

    v: int = 1
    kid: str
    session_hash: str = ""
    active_skills: list[ActiveSkillRef] = Field(default_factory=list)
    catalog_revision: str = ""
    last_turn_id: str | None = None
    last_instruction_revision: int = 0
    issued_at: int = 0
    expires_at: int = 0

    def active_ids(self) -> list[str]:
        return [s.id for s in self.active_skills]


@dataclass(frozen=True)
class StateVerification:
    """The outcome of verifying a state token.

    ``valid`` means the token is well-formed, correctly signed, bound to this
    session, and unexpired. ``reason`` carries a machine-readable warning code
    when a token was supplied but is not usable.
    """

    valid: bool
    payload: StatePayload | None = None
    reason: str | None = None
