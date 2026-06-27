"""Skill-load events surfaced to the CLI.

A single immutable :class:`SkillLoadExecution` describes one skill that was just
activated this turn. It feeds the ``daintree.prelude.tool_executions`` metadata
the server returns to the CLI, which renders a visible "skill.load" prelude for
the user. The main model is NOT shown this as a tool call — active skill bodies
reach it as plain context in the "Loaded skills" system block — so there is no
synthetic self-call for it to imitate or narrate.
"""

from __future__ import annotations

import json
from dataclasses import dataclass

from ..contracts.extensions import (
    PreludeExecution,
    PreludeToolCall,
    PreludeToolCallFunction,
    PreludeToolResult,
)
from .models import Skill

# Wire-safe name for the (server-only, never client-executable) load tool.
SKILL_LOAD_WIRE_NAME = "skill__load"
SKILL_LOAD_DISPLAY_NAME = "skill.load"


@dataclass(frozen=True)
class SkillLoadExecution:
    """One skill-load event, surfaced to the CLI as a visible prelude."""

    call_id: str
    skill_id: str
    version: str
    title: str
    summary: str

    @classmethod
    def from_skill(cls, call_id: str, skill: Skill) -> SkillLoadExecution:
        return cls(
            call_id=call_id,
            skill_id=skill.id,
            version=skill.version,
            title=skill.title,
            summary=skill.summary,
        )

    def arguments_json(self) -> str:
        return json.dumps({"skillId": self.skill_id}, separators=(",", ":"))

    def result_content(self) -> str:
        return json.dumps(
            {
                "ok": True,
                "summary": self.summary,
                "data": {"id": self.skill_id, "version": self.version},
            },
            separators=(",", ":"),
        )

    def prelude_execution(self) -> PreludeExecution:
        return PreludeExecution(
            call=PreludeToolCall(
                id=self.call_id,
                function=PreludeToolCallFunction(
                    name=SKILL_LOAD_WIRE_NAME, arguments=self.arguments_json()
                ),
            ),
            result=PreludeToolResult(tool_call_id=self.call_id, content=self.result_content()),
            display_name=SKILL_LOAD_DISPLAY_NAME,
        )
