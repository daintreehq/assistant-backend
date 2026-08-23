"""Runbook-load events surfaced to the CLI.

A single immutable :class:`RunbookLoadExecution` describes one runbook that was just
activated this turn. It feeds the ``daintree.prelude.tool_executions`` metadata
the server returns to the CLI, which renders a visible "runbook.load" prelude for
the user. The main model is NOT shown this as a tool call — active runbook bodies
reach it as plain context in the "Loaded runbooks" block — so there is no
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
from .models import Runbook

# Wire-safe name for the (server-only, never client-executable) load tool.
RUNBOOK_LOAD_WIRE_NAME = "runbook__load"
RUNBOOK_LOAD_DISPLAY_NAME = "runbook.load"


@dataclass(frozen=True)
class RunbookLoadExecution:
    """One runbook-load event, surfaced to the CLI as a visible prelude."""

    call_id: str
    runbook_id: str
    title: str
    summary: str

    @classmethod
    def from_runbook(cls, call_id: str, runbook: Runbook) -> RunbookLoadExecution:
        return cls(
            call_id=call_id,
            runbook_id=runbook.id,
            title=runbook.title,
            summary=runbook.summary,
        )

    def arguments_json(self) -> str:
        return json.dumps({"runbookId": self.runbook_id}, separators=(",", ":"))

    def result_content(self) -> str:
        return json.dumps(
            {
                "ok": True,
                "summary": self.summary,
                "data": {"id": self.runbook_id},
            },
            separators=(",", ":"),
        )

    def prelude_execution(self) -> PreludeExecution:
        return PreludeExecution(
            call=PreludeToolCall(
                id=self.call_id,
                function=PreludeToolCallFunction(
                    name=RUNBOOK_LOAD_WIRE_NAME, arguments=self.arguments_json()
                ),
            ),
            result=PreludeToolResult(tool_call_id=self.call_id, content=self.result_content()),
            display_name=RUNBOOK_LOAD_DISPLAY_NAME,
        )
