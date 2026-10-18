# Daintree app MCP tool surfaces

What the Daintree desktop app's MCP server (`/mcp` on the app's loopback port)
sends each class of caller in `tools/list`: the exact wire tool entries
(`name`, `description`, `inputSchema`, `outputSchema`, `annotations`, `_meta`),
captured from the app itself rather than written by hand. Runbooks that show
Daintree MCP calls can validate their tagged JSON examples against these.

**Source:** `daintreehq/daintree` commit `f91badae47` ("Merge pull request
#13492 from daintreehq/feature/issue-13468"), app `develop` with default-on
assistant messaging merged (#13467, #13468, #13469: no enrollment, no hub
singleton, messaging on Core), app version 0.44.0, MCP SDK 1.30, protocol
`2025-11-25`. Captured 2026-10-18. Previous capture: `0436363469`
(2026-10-15).

| file | caller | tier | tools | `mcp.surface` hash |
|---|---|---|---|---|
| `pane-core.json` | agent CLI in a Daintree pane, project MCP = Core | `core` | 78 (34 run, 44 ask first) | `f35b6dd3…` |
| `pane-full.json` | agent CLI in a Daintree pane, project MCP = Full | `full` | 78 | `2aa4d74a…` |
| `help-core.json` | Daintree's sidebar assistant (help session), Tool set = Core | `core` | 36 | `d9732a8b…` |
| `help-full.json` | the same sidebar assistant, Tool set = Full | `full` | 82 | `a5ed0ff1…` |
| `hub-full.delta.json` | the sidebar assistant on a Shell with a Remote Host (the hub), Full | `full` | 94 = help-full + 12 | `75ff468d…` |
| `lane-full.delta.json` | the sidebar assistant on a machine listening as a host (a lane), Full | `full` | 85 = help-full + 3 | `efcef010…` |
| `external.json` | API-key client (or no key set), not bound to a workspace | `external` | 34 | `8746e6e1…` |

Full hashes are in each file's `capture.surface_hash`.

**The machine matters.** Each surface is captured on a described machine
(`capture.messaging`): whether it has added a Remote Host, and whether it
listens as one. Panes and the API-key client are captured on a machine that
does both, so their files show what a pane gets where hosts exist
(`host.switch` and `project.openOnHost`, ask first) and that neither is ever
offered the messaging tools. `help-*` is the sidebar on a machine with
neither, so no messaging tool is listed there.

## Cross-host messaging: the hub and lane surfaces

The `host.*` hub tools and a lane's `assistant.*` inbox are offered only to a
sidebar-assistant session (a help-session bearer, `sessionHelpIdMap`), and only
on a machine messaging can reach (`sessionServer.ts`, the `*Offered` gates):

- **Hub** (`hub-full`): any sidebar assistant in one of a Shell's own views,
  where the Shell has at least one Remote Host. Every `host.*` tool
  (`host.list`, `host.listAssistants`, `host.sendMessage`, `host.getTask`,
  `host.listTasks`, `host.cancelTask`, `host.startAssistant`,
  `host.readQuota`) is on Core, with nothing to enroll; tasks belong to the
  Shell, so any of its assistants may read or cancel one. The hub's own lane
  also gets `assistant.getInbox` and `assistant.reply` (host-initiated
  messages). It is not offered `assistant.sendMessage` here; a Shell that also
  listens as a host would list it, but those calls refuse unless that lane is
  routed to a remote Shell. The machine has a host, so `host.switch` and
  `project.openOnHost` are listed too: the only host actions that stay Full.
- **Lane** (`lane-full`): a machine listening as a host. Every running lane
  takes new messages from every authenticated Shell unless its own toggle, the
  host's setting or the pause stops them: `assistant.getInbox`,
  `assistant.reply`, `assistant.sendMessage`, all on Core. Its own
  `assistant.sendMessage` goes only to the Shell the lane is routed to: the one
  that started it, or whose view it runs in.

They are listed from the start of such a session: Codex reads the tool list
once and never acts on `tools/list_changed`, so the app offers them on
reachability alone. A pane or API-key caller is refused (`NOT_ELIGIBLE`), as is
a lane session another has replaced; a run turned off still reads and answers
what reached it. The capture is that start-of-session listing.

**Stored as deltas.** A hub or lane surface is the sidebar surface of the same
tier plus a handful of tools, so it is written as `<name>.delta.json`:
`capture` (its own full metadata, plus `delta_of` and the base's
`delta_of_surface_hash`), `added` (full wire entries), `changed` (full entries
for any shared tool that differs; none today) and `removed` (ids). The hub
and lane sessions at Tool set = Core are captured and checked too, and recorded
in the Full delta as `capture.core_listing` (what each lists beyond
`help-core`), not as files of their own.
`tests/daintree_mcp_surfaces.py` rebuilds the surface from its base, and the
capture run fails if the rebuild differs from what the session listed.

**Caller policy.** A runbook's `requiredTools` and untagged examples are
checked against its caller's surface, decided by the id namespace alone
(`CALLER_POLICY` in `tests/daintree_mcp_surfaces.py`): `pane-core` by default,
and for `daintree.hosts.*` one of `hub-full` or `lane-full`, the whole
baseline on one of them, and so must each runbook's bundle with its supporting
runbooks. Outside `daintree.hosts` a messaging tool can only be `optionalTools`,
the sidebar hub variant of a pane runbook, and its tagged example must say
`surface=hub-full` (or `lane-full`): untagged examples resolve only on the
runbook's own callers (`untagged_example_surfaces`).

## Shape

```jsonc
{
  "capture": {
    "app_repo", "app_branch", "app_commit", "app_version",
    "caller_origin",        // agent-pane | help | help-hub | help-lane | api-key
    "session_origin",       // what main records: external | help
    "tier",                 // core | full | external
    "session": { "workspace_bound", "pane_approval", "renderer_owned_origin" },
    "captured_at", "mcp_protocol_version", "method",
    "surface_hash",         // what a real tools/call mcp.surface returned for this session
    "surface_hash_method",
    "messaging",            // the machine (has hosts, listens as one) and what was offered
    "tool_count",
    "context_gated_tools",  // tools whose presence depends on renderer state (isVisible)
    "omitted_args",         // external only: args removed from the listing
    "withheld_when_workspace_bound", // external only
    "notes"                 // pane only
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
        "reach"             // "tier": runs without asking; "approval": pane asks the user first
      }
    }
  ]
}
```

## Reading the surfaces

**Wire name = action id, verbatim.** The session server lists `name: entry.id`:
dots kept, no prefix, no escaping (`terminal.sendCommandOwned`). Any renaming
happens in the client. Daintree registers itself under the server key `daintree`
in the pane's `--mcp-config` and in the help session's settings, so Claude Code
shows these as `mcp__daintree__<name>`; whatever further character mapping a
client applies to the dots is the client's and is not captured here.

**Agent panes list the same 78 tools at core and full.** A pane's tier is where
auto-approval stops, not a ceiling (#12692): `tools/list` carries everything up to
the full surface a non-assistant origin may hold, and a call above the tier asks
the user instead of being refused. The two pane files are identical on the wire;
they differ only in `daintree.reach`, `daintree.surface_tier` and the surface
hash.

**Owned twins.** Only Daintree's own assistant (origin `help` or
`assistant-pane`) gets the unscoped panel tools. Every other session gets the
owned form in their place, and loses the tools that have none:

| help (assistant) | pane / external |
|---|---|
| `terminal.sendCommand` | `terminal.sendCommandOwned` |
| `terminal.inject` (full) | `terminal.injectOwned` |
| `terminal.close` | `terminal.closeOwned` |
| `terminal.sendKeys` | `terminal.sendKeysOwned` |
| `worktree.delete` | only `worktree.deleteOwned`, which help also has |
| `terminal.kill`, `terminal.restart`, `terminal.closeAll`, `copyTree.injectToTerminal` (full) | none |

**`notify`.** `terminal.sendCommand`, `terminal.sendCommandOwned` and
`agent.launch` take `notify: boolean` for panes and the assistant, as do
`agent.launchMany`, `terminal.sendCommandMany` and `terminal.sendKeys(Owned)`,
none of which the external listing carries. The external
listing drops it from `terminal.sendCommandOwned` and `agent.launch`
(`MCP_EXTERNAL_OMITTED_ARGS`); main refuses it from an API-key client if sent
anyway. That is the only property-level difference between surfaces for the same
action id. `terminal.notifyWhenIdle` is on core (panes and assistant) and not on
external.

**External** is a strict subset of the pane surface, without the batch tools or
`terminal.sendKeysOwned`. Bound to a workspace (`Daintree-Workspace-Id`), it
also withholds the confirm-gated `recipe.run` and `worktree.deleteOwned`.

## Regenerating

Needs the app checked out with `node_modules` installed (`npm ci`).

```bash
tests/fixtures/daintree_mcp/capture/capture.sh <commit-ish> [branch-label]
# e.g. tests/fixtures/daintree_mcp/capture/capture.sh f91badae47 develop
```

`DAINTREE_APP_REPO` points it at the app checkout (default
`~/Projects/Daintree/daintree`). The script makes a throwaway detached worktree
at that commit, links `node_modules` into it (workspace packages re-pointed at
the worktree's own `packages/`), copies `capture/captureMcpSurfaces.test.ts`
into `src/services/actions/__tests__/` there, runs that one vitest file, writes
the seven files here (two as deltas) and removes the worktree. It never writes
into the app checkout. Refresh them all together: the hygiene test fails when
they come from different builds.

How the test captures a surface: the real `ActionService` over the real action
registry builds the manifest the renderer hands main (`actionService.list()`);
the real `createSessionServer` serves it to a session configured as the caller
(tier, origin, workspace binding, pane approval route, and the real
`bindHostMessaging` / `bindAssistantMessaging` bound to its help session over a
stand-in runtime that answers only the listing questions for its machine); a raw JSON-RPC client
does `initialize` then `tools/list` then `tools/call mcp.surface` over the SDK's
in-memory transport, and the result is JSON round-tripped. The run fails if any
listed input schema or description differs from the app's own independent wire
measurement (`measureWireSurface`), if a listed id falls outside the tier
allowlists, if an allowlisted tool is missing without a manifest reason, if the
real `mcp.surface` hash differs from `buildSurfaceManifest`, or if the wire
carries a key the fixture does not record.

If a newer app commit changes how callers are classified, update `SURFACES` in
the test: it is the one place the harness says what each caller is.

## What this does not prove

- **Only the listing.** Nothing is dispatched. Ownership checks, the confirm
  dialog, the pane approval ask, refusing `notify` from an API-key client, and
  whether a notice is actually typed into the caller's prompt are not exercised.
  Messaging past the listing (start consent, the assistant limit, one CLI per
  project, consent withdrawn mid-start, send outcomes, the hub's update line) is
  proven offline only by the app's own tests at the same commit, which
  `capture.sh` runs after the capture (`HostAssistantStarter`,
  `HubMessagingRuntime`, `install`, `hostMessagingTools`,
  `assistantMessagingTools`; 126 passed at `f91badae47`), and fully only live.
- **The wire schema is looser than what the app accepts.** The listing strips
  `minimum`, `maximum`, `minLength`, `maxLength`, `pattern`, `minItems`,
  `maxItems` and similar bounds (`shared/utils/mcpWireSchema.ts`); the app
  validates calls against the full zod `argsSchema`, bounds and refinements
  included. An example that validates here can still be refused there, e.g.
  `terminal.notifyWhenIdle`'s `note` over 160 characters. Output schemas are not
  stripped: they are listed verbatim, `$schema` key included.
- **Static tier only.** A help session's tier can change mid-session
  (`notifications/tools/list_changed`). Live per-tool and native grants widen
  what may be *called* but never what is *listed*.
- **Harness, not a running app.** The session store and transport are stand-ins;
  the tool-listing and `mcp.surface` handlers are the app's own. No bearer auth
  or HTTP transport is involved, and those do not alter the listed payload.
- **Empty UI context, one exception.** The manifest is built with an empty
  `ActionContext`. The one piece of renderer state set is the list of added
  Remote Hosts, from the machine described: `host.switch` and
  `project.openOnHost` declare `isVisible` on it, so they are listed where the
  machine has a host and named in `context_gated_tools`. `isEnabled` only sets
  the manifest's `enabled` flag, which is not listed.
- **Not captured separately:** the `assistant-pane` origin (Daintree's own
  assistant running in a pane). By the code it is renderer-owned with no approval
  route, so it should match `help-*` at the same tier. Not measured. Plugin agent
  MCP servers (`/mcp/plugin/…`, server keys `daintree-<plugin>`) are separate
  servers and out of scope; plugin actions are on no tier and never appear here.
- **Where schemas come from.** Every schema comes from the action registry. Tools
  that main executes itself (`mcp.surface`, `terminal.notifyWhenIdle`,
  `terminal.waitUntilIdle`, `terminal.waitUntilIdleBatch`,
  `terminal.readLastMessageOwned`, `skills.*`, `project.runCheck`) or checks
  against its ownership ledger before delegating (the `*Owned` twins) are still
  registered in the renderer registry for their schema and description, and that
  registry entry is what gets listed. The two
  wait tools' output schemas are hand-written JSON Schema constants in the app
  (`shared/types/terminalWaitUntilIdle.ts`) rather than zod. They are still
  listed as registered, not re-derived here.
- **`daintree.*` fields** are not on the wire. `reach` is the app's own
  `isTierPermitted` for the session's tier and origin. `surface_tier` is what
  `mcp.surface` reported.
