# Test fixtures

## `daintree_mcp/`

The tool surfaces the Daintree app's MCP server lists to each kind of caller: an
agent pane at core and full tier, the sidebar assistant at core and full, and an
API-key client. All five come from one app commit and are the authority on which
actions a runbook may name and what arguments its examples may pass.

- `tests/unit/test_runbook_catalog_hygiene.py` checks every `requiredTools` and
  `optionalTools` name against them.
- `tests/unit/test_runbook_examples.py` validates every tagged example against the
  schema of the caller it is written for.
- `tests/unit/test_runbook_migration.py` fails any action-shaped name in a runbook
  that no caller is offered.

[`daintree_mcp/README.md`](daintree_mcp/README.md) records the app commit, what each
file holds, what the capture does and does not prove, and how to refresh it
(`daintree_mcp/capture/capture.sh`). Refresh all five together. The hygiene test
fails when they come from different builds.

These replaced the retired Daintree Assistant CLI's tool inventory
(`cli_tool_inventory.json`, `cli_tool_names.txt`) and the `cli/` submodule it was
captured from. Runbooks are now followed by Claude Code and Codex through the app's
MCP, not by that CLI.
