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
    # The profile that RESOLVED the active set this token carries. A sub-agent
    # chooses from a menu narrowed to what it can execute, so the two profiles do
    # not agree about which sets are legal: a sub-agent's set can hold a specific
    # whose domain foundation was filtered out of its menu, and reuse deliberately
    # does not re-pair foundations, so an assistant round reusing that token would
    # run a workflow skill with its base judgment layer silently missing.
    #
    # Distinct session ids make the crossover unreachable for today's client, but
    # that is the CLIENT's property, not this server's. Everything else about the
    # skill boundary is enforced here at the request boundary; this makes the
    # profile match the rest. Absent on assistant tokens so their bytes are
    # unchanged.
    profile: str = "assistant"
    # The documentation search this TURN ran, carried so its pages survive the
    # turn's tool-continuation rounds.
    #
    # Those rounds deliberately skip the selector, so nothing would re-derive the
    # query — and the pages live in the volatile tail, which is rebuilt every
    # round. Without this, a usage question that takes one tool call to answer
    # loses the documentation it was about to cite, between asking and answering.
    # The query is re-run rather than the results being stored: a signed token is
    # not a place to put kilobytes of prose, and it travels through the client.
    #
    # Scoped to `last_turn_id` by the reader: a NEW instruction gets its own
    # classification, so a stale query must never search on its behalf.
    docs_query: str | None = None

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
