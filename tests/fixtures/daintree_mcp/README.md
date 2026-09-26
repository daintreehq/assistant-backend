# Daintree app MCP tool surfaces

What the Daintree desktop app's MCP server (`/mcp` on the app's loopback port)
sends each class of caller in `tools/list`: the exact wire tool entries
(`name`, `description`, `inputSchema`, `outputSchema`, `annotations`, `_meta`),
captured from the app itself rather than written by hand. Runbooks that show
Daintree MCP calls can validate their tagged JSON examples against these.

**Source:** `daintreehq/daintree`, branch `feature/mcp-cleanup`, commit
`c5f42a0935` ("feat(mcp): let a send or launch wait for the agent's reply"),
app version 0.38.0, MCP SDK 1.30.0, protocol `2025-11-25`. Captured
2026-09-26. It has the batch tools (`agent.launchMany`,
`terminal.sendCommandMany`, `terminal.closeMany`) and `waitForReply` /
`waitSeconds` on `agent.launch`, the sends and both batches, external included.

| file | caller | tier | tools | `mcp.surface` hash |
|---|---|---|---|---|
| `pane-core.json` | agent CLI in a Daintree pane, project MCP = Core | `core` | 72 (32 run, 40 ask first) | `afa2fd53…` |
| `pane-full.json` | agent CLI in a Daintree pane, project MCP = Full | `full` | 72 | `9b5e257e…` |
| `help-core.json` | Daintree's sidebar assistant (help session), Tool set = Core | `core` | 32 | `09ddea22…` |
| `help-full.json` | the same sidebar assistant, Tool set = Full | `full` | 77 | `cf208e7a…` |
| `external.json` | API-key client (or no key set), not bound to a workspace | `external` | 34 | `fd13e204…` |

Full hashes are in each file's `capture.surface_hash`.

## Shape

```jsonc
{
  "capture": {
    "app_repo", "app_branch", "app_commit", "app_version",
    "caller_origin",        // agent-pane | help | api-key
    "session_origin",       // what main records: external | help
    "tier",                 // core | full | external
    "session": { "workspace_bound", "pane_approval", "renderer_owned_origin" },
    "captured_at", "mcp_protocol_version", "method",
    "surface_hash",         // what a real tools/call mcp.surface returned for this session
    "surface_hash_method",
    "tool_count",
    "context_gated_tools",  // listed tools whose presence depends on live UI context (none today)
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

**Agent panes list the same 72 tools at core and full.** A pane's tier is where
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
| `worktree.delete` (full) | only `worktree.deleteOwned`, which help also has |
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
# e.g. tests/fixtures/daintree_mcp/capture/capture.sh cab0d52531 feature/mcp-cleanup
```

`DAINTREE_APP_REPO` points it at the app checkout (default
`~/Projects/Daintree/daintree`). The script makes a throwaway detached worktree
at that commit, links `node_modules` into it (workspace packages re-pointed at
the worktree's own `packages/`), copies `capture/captureMcpSurfaces.test.ts`
into `src/services/actions/__tests__/` there, runs that one vitest file, writes
the five JSON files here and removes the worktree. It never writes into the app
checkout.

How the test captures a surface: the real `ActionService` over the real action
registry builds the manifest the renderer hands main (`actionService.list()`);
the real `createSessionServer` serves it to a session configured as the caller
(tier, origin, workspace binding, pane approval route); a raw JSON-RPC client
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
- **Empty UI context.** The manifest is built with an empty `ActionContext`.
  No tool on any of these surfaces declares `isVisible` (`context_gated_tools` is
  empty everywhere), and `isEnabled` only sets the manifest's `enabled` flag,
  which is not listed. If a future tool gates on context, its presence here will
  reflect an empty context and it will be named in `context_gated_tools`.
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
