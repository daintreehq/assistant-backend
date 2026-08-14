"""The docs lookup, surfaced to the CLI as a visible prelude.

The server ran a documentation search on the user's behalf, before the model
said a word. From the CLI's side that is indistinguishable from a tool call it
would have executed itself — so it is rendered as one, using the same
``daintree.prelude.tool_executions`` channel a skill load already rides.

This is presentation, not protocol: the CLI never executes the call, and the
main model is never shown it as a tool call either (the retrieved pages reach it
as plain context in the injected tail, the same way skill bodies reach it as a
system block). What the prelude buys is honesty in the transcript — the user
sees that their question was answered from the docs, and which query found them,
rather than watching the assistant produce cited pages from nowhere.
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

# Wire-safe name for the (server-only, never client-executable) docs search.
# Deliberately the same spelling the CLI's own docs tool used, so a transcript
# reads identically whether the lookup happened here or there.
DOCS_SEARCH_WIRE_NAME = "docs__search"
DOCS_SEARCH_DISPLAY_NAME = "docs.search"


@dataclass(frozen=True)
class DocsSearchExecution:
    """One server-side documentation search, as the CLI renders it."""

    call_id: str
    query: str
    paths: list[str]

    def arguments_json(self) -> str:
        return json.dumps({"query": self.query}, separators=(",", ":"))

    def result_content(self) -> str:
        return json.dumps(
            {
                "ok": True,
                "summary": f"Searched Daintree documentation for: {self.query}",
                # Paths only. The CLI renders a one-line prelude, and the page
                # text is already going to the model through the prompt — sending
                # it twice would put kilobytes of documentation on the wire for a
                # line of UI.
                "data": {"paths": self.paths},
            },
            separators=(",", ":"),
        )

    def prelude_execution(self) -> PreludeExecution:
        return PreludeExecution(
            call=PreludeToolCall(
                id=self.call_id,
                function=PreludeToolCallFunction(
                    name=DOCS_SEARCH_WIRE_NAME, arguments=self.arguments_json()
                ),
            ),
            result=PreludeToolResult(tool_call_id=self.call_id, content=self.result_content()),
            display_name=DOCS_SEARCH_DISPLAY_NAME,
        )
