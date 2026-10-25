# Daintree app MCP tool surfaces

What the Daintree desktop app's MCP server (`/mcp` on the app's loopback port)
sends each class of caller in `tools/list`: the exact wire tool entries
(`name`, `description`, `inputSchema`, `outputSchema`, `annotations`, `_meta`),
captured from the app itself rather than written by hand. All current runbooks
(`runbooks/catalog/`) are checked against these captures. Retired v0.44 surfaces
are historical material in `archive/inbox-v0.44/surfaces/`.

**Source:** `daintreehq/daintree` commit `2c6d1fb3d46061ceb8526a88efdb60f04d6f6f57`,
`develop`, app 0.46.0. Captured 2026-10-25. This includes direct host control,
assistant messaging through agent terminals, background agent placement,
`terminal.placeOwned`, `host.ensureProject`, `host.settings`, and
`project.runCommand` admitted to Daintree's own assistant at Core as at Full,
the `host.call` throwaway target (app #14444-#14447), and the owned
`terminal.restartOnPresetOwned`. The capture also ran 254 app host-control
contract tests.

| file | tools | `mcp.surface` hash |
|---|---|---|
| `external.json` | 36 | `4fc679b0…` |
| `global-core.json` | 0 (3 root tools) | `9a046ac9…` |
| `global-full.json` | 0 (3 root tools) | `d75e9c00…` |
| `help-core.json` | 46 | `909aebe4…` |
| `help-full.json` | 94 | `2c9a0fd9…` |
| `pane-core.json` | 80 | `37899dc6…` |
| `pane-full.json` | 80 | `cd5a2894…` |
| `relayed-core.json` | 39 | `3ebdfa2c…` |
| `relayed-full.json` | 81 | `932403df…` |

Full hashes and caller/tier details are in each file's `capture`.

## Host control: the help and relayed surfaces

`host.list` and `host.call` are offered to this machine's own assistant sessions
(a help-session bearer in one of this machine's own views,
`hostControl.isCallReachable()`), hosts or not, and to nobody else. Each help
file records two narrower variants in `capture.variants`: a help session this
machine runs for a remote Shell's window (no `host.*`), and this machine's
assistant running with no window (#13511), which is what a host's assistant a
Shell started through `host.call` `assistant.start` works with (35 tools at Core).

`host.call {hostId, projectId, tool, args?, callId?}` runs `tool` with `args`
through a session the host opens for the calling assistant: the relayed surface.
It is origin `external`, bound to the one project, at the caller's own Tool set,
with no approval route, so the tier is a ceiling. It carries the owned terminal
twins (`terminal.sendCommandOwned` with `generation`, `handback` and `notify`,
`terminal.readLastMessageOwned`, `terminal.interruptOwned`, ...), `assistant.list`,
`assistant.start`, `agent.readQuota` and `project.runCommand` at both tiers, and
`project.runCheck` and `terminal.new` at Full only. It never carries a `host.*`
tool (a call does not chain) or `help.displayImage`. A host with no window on the
project lists less to a relay (`capture.variants.no_view_attached`), but calls
name a tool by id and run regardless.

**The throwaway target.** `host.call` names exactly one target: `projectId`, or
`throwaway: true` for a short-lived agent the calling session owns in one
host-owned folder, outside every project, view and worktree. The listed schema
requires only `hostId` and `tool`; the one-target rule is a refinement the app
checks at the parse boundary (`INVALID_ARGS`), not part of the JSON schema. A
throwaway target takes six tools that are not actions and appear on no surface:
`throwaway.launch`, `throwaway.status`, `throwaway.output`,
`throwaway.sendCommand`, `throwaway.sendKeys`, `throwaway.close`. Their arguments
are in the app's `shared/types/hostThrowaway.ts`, not in these captures, so a
runbook example of one is grounded by hand rather than validated here.

## The global Assistant

`global-core.json` and `global-full.json` are the installation's one projectless
Daintree Assistant (app #14159, #14161): a help session main runs with no project and
no view, under `GLOBAL_ASSISTANT_ID`. It may call only its root tools
(`capture.root_tools`: `host.list`, `host.call`, `host.settings`); every project,
this machine's under `hostId` `local`, is reached through `host.call`, where the host
admits only `capture.host_call.admits` (`GLOBAL_ALLOWED_TOOLS`) and refuses the rest
`policy_rejected`: no `project.runCommand` or `project.runCheck`, no
`agentSessionHistory.resume`, no unscoped terminal tool. The owned tools act on what
it launched there or the person handed it (Hand to orchestrator, Global Assistant);
it closes only what it launched.

At `2c6d1fb3` its `tools/list` is **empty**: it is listed from the viewless help base
manifest, which carries no `host.*` entry, although dispatch admits the three root
tools (`capture.notes`). The tests read the root tools' schemas from the help surface
of the same Tool set and record the gap rather than hide it. `capture.native_setup`
records the configuration contract the app's own helpers generate for its CLI besides the
MCP (not observed launch wiring: the integration smoke still owes that): the `daintree-runbooks`
server and its headers, the runbook instructions (owned tool names as written, through
`host.call`), the checkpoint instructions, `DAINTREE_ASSISTANT_RUN_CHECKPOINT_DIR` (bucket
`global`, kept across launches) and the scratch variable. These two files are
callers, not listings, so they sit outside `SURFACES` in
`tests/daintree_mcp_surfaces.py` (`GLOBAL_SURFACES`); what a global `host.call` can
name is the relayed surface of its Tool set narrowed to `admits`
(`global-relayed-core`, `global-relayed-full`).

**Caller policy.** A runbook's `requiredTools` and untagged examples are checked
against its caller's surface, decided by the id namespace alone (`CALLER_POLICY`
in `tests/daintree_mcp_surfaces.py`): `pane-core` by default, for
`daintree.hosts.*` the sidebar assistant (`help-core` or `help-full`), and for
`daintree.assistants.*` Daintree's own assistant, global first (`global-core`,
`global-full`, `help-core`, `help-full`), the whole baseline on one of them; a global
caller's baseline counts what its `host.call` reaches. A tagged `host.call` example is validated twice: the
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

**Agent panes list the same 80 tools at core and full.** A pane's tier is where
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

**The command runner answers to its caller.** `project.runCommand` is admitted
by `mayRunProjectCommand` (`electron/services/mcp-server/tierAuth.ts`), not the tier
allowlist: Daintree's own assistant (origin `help`) or a relay, at Core or Full.
Never a pane or an API-key client, and no grant widens it, so its `daintree.reach`
is `caller`. `project.runCheck` is a tier tool and stays Full only, here and on a
relay.

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
writes the nine files here and removes the worktree. It never writes into the app
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
