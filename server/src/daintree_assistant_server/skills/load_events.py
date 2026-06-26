"""Synthetic skill-load events.

A single immutable :class:`SkillLoadExecution` is the source of truth for both
the upstream synthetic transcript (so the main model observes the load) and the
``daintree.prelude.tool_executions`` metadata returned to the CLI. Deriving
both from the same object prevents the visible prelude and the upstream prelude
from drifting apart.
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
    """One synthetic skill-load tool call/result pair."""

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

    def wire_tool_call(self) -> dict[str, object]:
        return {
            "id": self.call_id,
            "type": "function",
            "function": {"name": SKILL_LOAD_WIRE_NAME, "arguments": self.arguments_json()},
        }

    def wire_tool_result(self) -> dict[str, object]:
        return {
            "role": "tool",
            "tool_call_id": self.call_id,
            "content": self.result_content(),
        }

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


def synthetic_transcript(executions: list[SkillLoadExecution]) -> list[dict[str, object]]:
    """Build the upstream messages for a set of newly loaded skills.

    One assistant message carries all the tool calls, followed by one tool
    message per call — mirroring the CLI's batched-tool presentation.
    """
    if not executions:
        return []
    assistant: dict[str, object] = {
        "role": "assistant",
        "tool_calls": [e.wire_tool_call() for e in executions],
    }
    messages: list[dict[str, object]] = [assistant]
    messages.extend(e.wire_tool_result() for e in executions)
    return messages
