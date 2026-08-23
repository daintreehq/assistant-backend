"""The docs lookup, surfaced to the CLI as a visible prelude.

The server ran a documentation search on the user's behalf, before the model
said a word. From the CLI's side that is indistinguishable from a tool call it
would have executed itself — so it is rendered as one, using the same
``daintree.prelude.tool_executions`` channel a runbook load already rides.

This is presentation, not protocol: the CLI never executes the call, and the
main model is never shown it as a tool call either (the retrieved pages reach it
as plain context in the injected tail, the same way runbook bodies reach it as a
framed user-role block).

**And the presentation does not currently happen.** The intent was honesty in the
transcript — the user seeing that their question was answered from the docs, and
which query found them, rather than watching the assistant produce cited pages
from nowhere. The CLI decodes `runbooks.prelude` and renders nothing:
`internal/backend/contracts.go` calls the whole block "vestigial metadata the
client neither replays nor renders", left in place pending a coordinated
server-side drop. So today this costs a few hundred bytes on the wire and buys
the user nothing.

Kept rather than deleted because the drop has to be coordinated and the argument
for showing a server-side retrieval has not changed — but do not read the
paragraph above as describing what a user sees. Either the CLI starts rendering
it or this goes; it should not stay a third thing indefinitely.
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
#
# It is the spelling the CLI's own docs tool used, chosen when both existed so a
# transcript read identically whichever side performed the lookup. The CLI has
# since dropped that tool family entirely — documentation retrieval is server-side
# now — so the name no longer collides with anything, and it is kept because it is
# what a client already knows how to render, not for the original reason.
#
# NOTE it is deliberately NOT in `validation.RESERVED_TOOL_NAMES`. A client
# offering a real tool by this name would give the model two routes to the same
# answer, which `/capabilities` tells clients not to do — but rejecting the
# request outright is a stricter contract than the runbook tools need, and no
# current client sends it.
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
