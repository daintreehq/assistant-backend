"""Typed models for the backend-internal skill-learning pipeline.

These are NOT part of the public wire contract — no client ever sends or receives
them. They are the structured shapes the two backend-only model calls return: the
cheap gate's verdict, and the builder/improver's skill proposal. Both parse
model output, so they are tolerant (``extra="ignore"``); the real validation of a
proposal happens when it is rendered to markdown and run through the skill loader.
"""

from __future__ import annotations

from typing import Literal

from pydantic import AliasChoices, BaseModel, ConfigDict, Field

GateAction = Literal["none", "improve_existing_skill", "create_new_skill"]
LessonType = Literal[
    "general_daintree_workflow",
    "project_specific",
    "user_preference",
    "bug_report",
    "none",
]


class GateVerdict(BaseModel):
    """The cheap gate's decision about whether a completed workflow is learnable.

    The gate never writes anything; it only decides whether spending a
    build/improve call is worthwhile, and which kind.
    """

    model_config = ConfigDict(extra="ignore")

    workflow_complete: bool = False
    learnable: bool = False
    action: GateAction = "none"
    target_skill_id: str | None = None
    lesson_type: LessonType = "none"
    confidence: float = Field(default=0.0, ge=0.0, le=1.0)
    reason: str = ""
    evidence: list[str] = Field(default_factory=list)
    # Catalog ids the gate judged closest to this workflow's domain. The cheap
    # small model picks them; the builder/improver then sees those skills as
    # its house-style/scope exemplars instead of arbitrary ones. Unknown ids are
    # filtered against the catalog before use.
    related_skill_ids: list[str] = Field(default_factory=list)


class SkillProposal(BaseModel):
    """The builder/improver's proposed skill, before validation.

    Field names mirror the on-disk frontmatter (camelCase accepted via aliases).
    A ``no_change`` proposal is a valid, expected outcome — the improver returns it
    when the current skill already covers the lesson.
    """

    model_config = ConfigDict(extra="ignore", populate_by_name=True)

    action: Literal["create_skill", "update_skill", "no_change"] = "no_change"
    skill_id: str = Field(
        default="",
        max_length=256,
        validation_alias=AliasChoices("skill_id", "id"),
    )
    title: str = ""
    summary: str = ""
    when_to_use: str = Field(
        default="", validation_alias=AliasChoices("when_to_use", "whenToUse")
    )
    priority: int = 100
    risk: str = "read"
    # The builder/improver's requested tier. Advisory only: `_validate` never lets this
    # DEMOTE an existing foundation, and only lets it PROMOTE (turn on where off) when
    # SKILL_LEARNING_ALLOW_FOUNDATION_CREATE is set — otherwise it is dropped and logged.
    foundation: bool = False
    tags: list[str] = Field(default_factory=list)
    required_tools: list[str] = Field(
        default_factory=list,
        validation_alias=AliasChoices("required_tools", "requiredTools"),
    )
    body: str = ""
    change_summary: list[str] = Field(default_factory=list)
    confidence: float = Field(default=0.0, ge=0.0, le=1.0)
    # The builder/improver's diagnosis of the workflow: which parts of the (old)
    # skill/procedure worked and which failed. Audit-trail only — never rendered
    # into the skill file — so the dogfooding loop can see WHY each change landed.
    what_worked: list[str] = Field(
        default_factory=list, validation_alias=AliasChoices("what_worked", "whatWorked")
    )
    what_failed: list[str] = Field(
        default_factory=list, validation_alias=AliasChoices("what_failed", "whatFailed")
    )
