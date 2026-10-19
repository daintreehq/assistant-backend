# Daintree app MCP tool surfaces

What the Daintree desktop app's MCP server (`/mcp` on the app's loopback port)
sends each class of caller in `tools/list`: the exact wire tool entries
(`name`, `description`, `inputSchema`, `outputSchema`, `annotations`, `_meta`),
captured from the app itself rather than written by hand. The control catalog's
runbooks (`runbooks/files/`) are checked against these. The inbox catalog served
to v0.44 clients is checked against its own frozen capture in `inbox/`.

**Source:** `daintreehq/daintree` commit `f45fa04b3b` ("Merge pull request
#13514 from daintreehq/feature/issue-13496-message-assistants-like-agent-terminals"),
app `develop` with direct host control (#13493: `host.list`, `host.call`), the
window-less host loop and its command runner (#13495: `project.runCommand`),
relayed idle notices (#13500) and assistant messaging as agent messaging with the
inbox retired (#13496: `assistant.list`, `assistant.start`, `agent.readQuota`).
App version 0.44.0 (unreleased build), MCP SDK 1.30, protocol `2025-11-25`.
Captured 2026-10-18. Previous capture: `f91badae47` (2026-10-18, the inbox
model; frozen in `inbox/`).

| file | caller | tier | tools | `mcp.surface` hash |
|---|---|---|---|---|
| `pane-core.json` | agent CLI in a Daintree pane, project MCP = Core | `core` | 78 (34 run, 44 ask first) | `6440e552…` |
| `pane-full.json` | agent CLI in a Daintree pane, project MCP = Full | `full` | 78 | `5ce81f4c…` |
| `help-core.json` | Daintree's sidebar assistant (help session) on a Shell with a Remote Host, Tool set = Core | `core` | 41 | `c9ba0b84…` |
| `help-full.json` | the same sidebar assistant, Tool set = Full | `full` | 90 | `ce7bfbc7…` |
| `relayed-core.json` | what a Remote Host offers a Shell's assistant inside `host.call`, Core | `core` | 36 | `8419db95…` |
| `relayed-full.json` | the same relayed session, Full | `full` | 79 | `2a33fb16…` |
| `external.json` | API-key client (or no key set), not bound to a workspace | `external` | 34 | `3ceda0e9…` |

Full hashes are in each file's `capture.surface_hash`.

## Host control: the help and relayed surfaces

`host.list` and `host.call` are offered to this machine's own assistant sessions
(a help-session bearer in one of this machine's own views,
`hostControl.isCallReachable()`), hosts or not, and to nobody else. Each help
file records two narrower variants in `capture.variants`: a help session this
machine runs for a remote Shell's window (no `host.*`), and this machine's
assistant running with no window (#13511), which is what a host's assistant a
Shell started through `host.call` `assistant.start` works with (30 tools at Core).

`host.call {hostId, projectId, tool, args?, callId?}` runs `tool` with `args`
through a session the host opens for the calling assistant: the relayed surface.
It is origin `external`, bound to the one project, at the caller's own Tool set,
with no approval route, so the tier is a ceiling. It carries the owned terminal
twins (`terminal.sendCommandOwned` with `generation`, `handback` and `notify`,
`terminal.readLastMessageOwned`, `terminal.interruptOwned`, ...), `assistant.list`,
`assistant.start` and `agent.readQuota` at both tiers, and `project.runCheck`,
`project.runCommand` and `terminal.new` at Full only. It never carries a `host.*`
tool (a call does not chain) or `help.displayImage`. A host with no window on the
project lists less to a relay (`capture.variants.no_view_attached`), but calls
name a tool by id and run regardless.

**Caller policy.** A runbook's `requiredTools` and untagged examples are checked
against its caller's surface, decided by the id namespace alone (`CALLER_POLICY`
in `tests/daintree_mcp_surfaces.py`): `pane-core` by default, and for
`daintree.hosts.*` the sidebar assistant (`help-core` or `help-full`), the whole
baseline on one of them. A tagged `host.call` example is validated twice: the
call against the help surface, and its `tool` and `args` against the relayed
surface of the same tier.

## Shape

```jsonc
{
  "capture": {
    "app_repo", "app_branch", "app_commit", "app_version",
    "caller_origin",        // agent-pane | help | relayed | api-key
    "session_origin",       // what main records: external | help
    "tier",                 // core | full | external
    "session": { "workspace_bound", "pane_approval", "renderer_owned_origin", "relay" },
    "caller",               // what this caller is, in words
    "host_control",         // whether host.* and assistant.* are offered, and why
    "machine",              // whether a Remote Host is added, and where the manifest came from
    "captured_at", "mcp_protocol_version", "method",
    "surface_hash",         // what a real tools/call mcp.surface returned for this session
    "surface_hash_method",
    "tool_count",
    "context_gated_tools",  // tools whose presence depends on renderer state (isVisible)
    "variants",             // narrower listings of the same caller, as added/removed/changed
    "notes"
  },
  "tools": [                // sorted by action_id
    {
      "action_id", "wire_name", "description",
      "inputSchema",        // exactly as listed
      "outputSchema",       // exactly as listed, or null when none is advertised
      "annotations",        // title, readOnlyHint, idempotentHint, destructiveHint, openWorldHint
      "_meta",              // present only when the wire carries it ({ "examples": [...] })
      "daintree": {         // NOT on the wire; the manifest facts the listing is decided from
        "kind", "category", "danger", "band",
        "surface_tier",     // the per-tool tier mcp.surface reports
        "reach"             // "tier": runs without asking; "approval": pane asks the user
                            // first; "caller": admitted by its caller rule (assistant.*,
                            // agent.readQuota, project.runCommand)
      }
    }
  ]
}
```

## Reading the surfaces

**Wire name = action id, verbatim.** The session server lists `name: entry.id`:
dots kept, no prefix, no escaping (`terminal.sendCommandOwned`). Daintree
registers itself under the server key `daintree`, so Claude Code shows these as
`mcp__daintree__<name>`.

**Agent panes list the same 78 tools at core and full.** A pane's tier is where
auto-approval stops, not a ceiling (#12692). The two pane files are identical on
the wire; they differ only in `daintree.reach`, `daintree.surface_tier` and the
surface hash. A pane is never offered `host.call`, `assistant.*`,
`agent.readQuota` or `project.runCommand`.

**Owned twins.** Only Daintree's own assistant gets the unscoped panel tools
(`terminal.sendCommand`, `terminal.sendKeys`, `terminal.close`, `worktree.delete`,
and at Full `terminal.inject`, `kill`, `restart`, `closeAll`). Every other
session, the relayed one included, gets the owned form and loses the tools that
have none. There is no unscoped `terminal.interrupt` on any surface:
`terminal.interruptOwned` is it.

**`notify`.** `terminal.sendCommand(Owned)`, `agent.launch` and the batch tools
take `notify: boolean` for panes, the assistant and a relay. The external listing
drops it (`MCP_EXTERNAL_OMITTED_ARGS`), and `terminal.notifyWhenIdle` is not on
external.

## Regenerating

Needs the app checked out with `node_modules` installed (`npm ci`).

```bash
tests/fixtures/daintree_mcp/capture/capture.sh <commit-ish> [branch-label]
# e.g. tests/fixtures/daintree_mcp/capture/capture.sh f45fa04b3b develop
```

`DAINTREE_APP_REPO` points it at the app checkout (default
`~/Projects/Daintree/daintree`). The script makes a throwaway detached worktree at
that commit, links `node_modules` into it, copies
`capture/captureMcpSurfaces.test.ts` into `src/services/actions/__tests__/` there,
runs it and then the app's own host-control contract tests at the same commit,
writes the seven files here and removes the worktree. It never writes into the app
checkout, and it does not remove a stale file: refresh them all together, since
the hygiene test fails when they come from different builds.

How a surface is captured: the real `ActionService` over the real action registry
builds the manifest the renderer hands main; the real `createSessionServer` serves
it to a session configured as the caller (tier, origin, workspace binding, pane
approval route, `deps.relay` for a relayed session, and the real `bindHostControl`
over a stand-in runtime that answers only `isCallReachable`); a raw JSON-RPC client
does `initialize`, `tools/list` and `tools/call mcp.surface` over the SDK's
in-memory transport, and the result is JSON round-tripped. The run fails if a
listed input schema or description differs from the app's own wire measurement
(`measureWireSurface`), if a listed id falls outside the tier allowlists or its
caller rule, if an allowlisted tool is missing without a manifest reason, or if
the real `mcp.surface` hash differs from `buildSurfaceManifest`.
