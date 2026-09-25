"""Daintree runbooks.

A stateless MCP server with two read-only tools: ``search_runbooks`` runs the
local two-stage runbook selector over a task description, and ``get_runbook``
returns one runbook by id. Both return full procedures plus the foundation each
one builds on. Instructions only: the actions they name belong to the Daintree
app's own MCP.
"""

__version__ = "1.0.0"
