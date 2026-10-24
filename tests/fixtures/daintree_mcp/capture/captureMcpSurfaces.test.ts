/**
 * Capture the Daintree MCP tool surfaces as each caller class receives them.
 *
 * NOT a test of the Daintree app. It is a vitest file only because the action
 * registry and the MCP session server need the app's own module graph, aliases
 * and setup to load. `capture.sh` copies it into a throwaway detached worktree
 * of daintreehq/daintree at
 * `src/services/actions/__tests__/captureMcpSurfaces.test.ts`, runs it there,
 * and the JSON it writes becomes the fixtures under `tests/fixtures/daintree_mcp/`
 * in the runbook backend.
 *
 * Method, per surface:
 *   1. Build the action manifest exactly as the renderer answers main's manifest
 *      request (`useMcpBridge` -> `actionService.list()`): the real
 *      `ActionService` over the real `createActionDefinitions` registry. Where a
 *      session has no view to ask, the app's own generated base surface instead
 *      (`getExternalBaseManifest`, `getViewlessHelpBaseManifest`), reached the way
 *      main reaches it.
 *   2. Hand that manifest to the real `createSessionServer`, over the real
 *      `SessionStore`, with the store rows and deps `httpLifecycle` writes for the
 *      given caller (tier, origin, workspace binding, help-session binding, pane
 *      approval route, relay).
 *   3. Speak raw JSON-RPC to it over the SDK's in-memory transport: `initialize`,
 *      `notifications/initialized`, `tools/list`, then `tools/call mcp.surface`.
 *      The `tools/list` result is JSON round-tripped, which is what the wire
 *      does to it.
 *   4. Cross-check every advertised input schema and description against the
 *      app's independent wire-measurement helper (`measureWireSurface`), and the
 *      listed id set against the tier allowlists and the caller gates this file
 *      states for itself, and fail on any disagreement.
 *
 * Direct host control (#13493, #13496, #13498): a Shell's own assistants reach
 * its Remote Hosts through `host.list` and `host.call`, and one assistant finds
 * and starts another with `assistant.list` / `assistant.start` (plus
 * `agent.readQuota`). Every non-relayed session is given the real
 * `bindHostControl` access, bound to its help session exactly as
 * `httpLifecycle` binds it, over a stand-in runtime that answers only the
 * listing question (`isCallReachable`) the way the spec describes; its calls
 * throw. Every session is given a stand-in `assistantControl` and the
 * `assistantCaller` `httpLifecycle` derives for it: a help session in its
 * project, a relay principal in its bound project, or nobody. A relayed
 * session (the one a host opens for a Shell's assistant inside `host.call`)
 * gets `deps.relay`, as `openRelaySession` builds it.
 *
 * A spec with `variantOf` is captured and checked like any surface, but not
 * written: what its listing adds to or drops from that surface is recorded
 * there, under `capture.variants`.
 *
 * Environment:
 *   CAPTURE_OUT_DIR     directory to write <surface>.json into (required)
 *   CAPTURE_APP_COMMIT  full sha of the app commit being captured (required)
 *   CAPTURE_APP_BRANCH  branch name to record (optional)
 *   CAPTURE_AT          ISO date to record (optional; defaults to today, UTC)
 */
import { describe, it, expect, vi } from "vitest";
import { mkdirSync, readFileSync, writeFileSync } from "node:fs";
import path from "node:path";

const hintMocks = vi.hoisted(() => ({
  getState: vi.fn(() => ({ hydrated: true, counts: {}, show: vi.fn(), incrementCount: vi.fn() })),
  getEffectiveCombo: vi.fn((_actionId: string): string | null => null),
  getDisplayCombo: vi.fn((_actionId: string): string => ""),
}));

// The same renderer-singleton mocks the app's own manifest generator uses
// (`mcpExternalBaseManifest.test.ts`); none of them touches a schema.
vi.mock("@/store/shortcutHintStore", () => ({
  shortcutHintStore: { getState: hintMocks.getState },
}));
vi.mock("../../../store/shortcutHintStore", () => ({
  shortcutHintStore: { getState: hintMocks.getState },
}));
vi.mock("@/services/KeybindingService", () => ({
  keybindingService: {
    getEffectiveCombo: hintMocks.getEffectiveCombo,
    getDisplayCombo: hintMocks.getDisplayCombo,
  },
}));
vi.mock("@/lib/notify", () => ({ notify: vi.fn() }));

// The renderer's list of added Remote Hosts, which host-gated actions
// (`host.switch`, `project.openOnHost`, `host.forwardPort`) read in
// `isVisible`. Set per view below; empty means that view's machine has added
// no host.
const hostList = vi.hoisted(() => ({ hosts: [] as unknown[] }));
vi.mock("@/components/Hosts/hostList", async (importOriginal) => ({
  ...(await importOriginal<Record<string, unknown>>()),
  getHostListSnapshot: () => ({ hosts: hostList.hosts, localSummary: null }),
}));

// Main reads only `app.getVersion()` at session construction. The version is
// the app's package.json version, so `serverInfo` and `mcp.surface` report what
// a build of this commit would.
vi.mock("electron", async () => {
  const fs = await import("node:fs");
  const p = await import("node:path");
  const pkg = JSON.parse(fs.readFileSync(p.resolve(process.cwd(), "package.json"), "utf8")) as {
    version: string;
  };
  return { app: { getVersion: () => pkg.version } };
});

import { InMemoryTransport } from "@modelcontextprotocol/sdk/inMemory.js";
import { LATEST_PROTOCOL_VERSION } from "@modelcontextprotocol/sdk/types.js";
import type { JSONRPCMessage } from "@modelcontextprotocol/sdk/types.js";
import type { ActionManifestEntry } from "@shared/types/actions";
import { ActionService } from "@/services/ActionService";
import { createActionRegistry, measureWireSurface } from "./helpers/wireSurface";
import { MCP_EXTERNAL_OMITTED_ARGS } from "@shared/config/mcpExternalTierAllowlist";
import { omitWireSchemaArgs } from "@shared/utils/mcpWireSchema";
import { HOST_CONTROL_TOOL_IDS } from "@shared/types/hostControl";
import { GLOBAL_ASSISTANT_ID, GLOBAL_ASSISTANT_TOOL_IDS } from "@shared/config/globalAssistant";
import {
  GLOBAL_ALLOWED_TOOLS,
  GLOBAL_WITHHELD_TOOLS,
} from "../../../../electron/remote/assistant/HostCallRuntime";
import {
  RUNBOOKS_MCP_SERVER_NAME,
  buildRunbooksAddendum,
  buildRunbooksHostHeaders,
} from "../../../../electron/services/helpSessionRunbooks";
import {
  ASSISTANT_GLOBAL_CHECKPOINT_BUCKET,
  ASSISTANT_RUN_CHECKPOINT_ENV_VAR,
  ASSISTANT_RUN_CHECKPOINT_WRITER_ENV_VAR,
  buildRunCheckpointAddendum,
} from "../../../../electron/services/AssistantRunCheckpointService";
import { ASSISTANT_SCRATCH_ENV_VAR } from "../../../../electron/services/AssistantScratchService";
import { ASSISTANT_CONTROL_TOOL_IDS } from "@shared/types/assistantControl";
import {
  bindHostControl,
  setHostControlRuntime,
  type HostControlRuntimeHandle,
} from "../../../../electron/services/mcp-server/hostControlTools";
import {
  createSessionServer,
  type SessionServerDeps,
} from "../../../../electron/services/mcp-server/sessionServer";
import { SessionStore } from "../../../../electron/services/mcp-server/sessionStore";
import { NoFrontendAttachedError } from "../../../../electron/services/mcp-server/rendererBridge";
import {
  getExternalBaseManifest,
  getViewlessHelpBaseManifest,
} from "../../../../electron/services/mcp-server/baseManifest";
import {
  getReachableActionIds,
  isTierPermitted,
  isWithheldFromBoundSession,
  isWithheldFromRelaySession,
  shouldExposeTool,
  type SessionSurfacePolicy,
} from "../../../../electron/services/mcp-server/tierAuth";
import { buildSurfaceManifest } from "../../../../electron/services/mcp-server/surfaceManifest";
import type { McpTier } from "../../../../electron/services/mcp-server/shared";

const appVersion = (
  JSON.parse(readFileSync(path.resolve(process.cwd(), "package.json"), "utf8")) as {
    version: string;
  }
).version;

const PROJECT_RUN_COMMAND = "project.runCommand";

/**
 * Where `tools/list` gets the session's manifest.
 * - `view`: a live view of the session's workspace answers (`ActionService.list({})`).
 * - `no-view`: a workspace-bound session whose workspace has no view attached;
 *   the route refuses with `NoFrontendAttachedError` and main lists the host's
 *   external base surface (`getExternalBaseManifest`).
 * - `viewless-help`: this machine's own assistant running with no window
 *   (#13511); main lists `getViewlessHelpBaseManifest` without asking anyone.
 */
type ManifestSource = "view" | "no-view" | "viewless-help";

interface SurfaceSpec {
  /** Output file stem, or for a variant the name it is checked under. */
  name: string;
  /** Human label for the fixture's `caller_origin`. */
  callerOrigin: "agent-pane" | "help" | "global" | "api-key" | "relayed";
  /** The MCP session origin main records for this caller (`McpSessionOrigin`). */
  origin: "help" | "external";
  tier: McpTier;
  /** Agent panes (#12486) and relayed sessions (#13493) are bound to one workspace. */
  workspaceBound: boolean;
  /** Only an agent pane is handed an approval route (#12692). */
  paneApproval: boolean;
  /** A help-token bearer, bound to one of this machine's help sessions (`sessionHelpIdMap`). */
  helpSession: boolean;
  /**
   * What the host-control runtime answers `isCallReachable` for that help
   * session: true for a live assistant in one of this machine's own views
   * (`HostCallRuntime.isCallReachable`), false for one a remote Shell's view
   * runs here or one with no view.
   */
  hostControlReachable: boolean;
  /** Opened by `openRelaySession` for another machine's assistant: `deps.relay` is set. */
  relay: boolean;
  /**
   * The installation's one projectless assistant (#14159): a viewless help
   * session whose row names `GLOBAL_ASSISTANT_ID`, offered only its root tools
   * (#14161) and reaching every project through `host.call`.
   */
  global?: boolean;
  /** What `assistantCaller()` resolves to: a help session in its project, a relay principal, or nobody. */
  assistantCaller: "local" | "relay" | null;
  /** The renderer added-hosts list of the view answering the manifest. */
  addedRemoteHosts: boolean;
  manifestSource: ManifestSource;
  /** Captured and checked, not written: recorded in that surface's `capture.variants`. */
  variantOf?: string;
  /** The key it is recorded under there. */
  variantKey?: string;
  description: string;
}

const PANE = {
  callerOrigin: "agent-pane",
  origin: "external",
  workspaceBound: true,
  paneApproval: true,
  helpSession: false,
  hostControlReachable: true,
  relay: false,
  assistantCaller: null,
  addedRemoteHosts: true,
  manifestSource: "view",
} as const;

const HELP = {
  callerOrigin: "help",
  origin: "help",
  workspaceBound: false,
  paneApproval: false,
  helpSession: true,
  hostControlReachable: true,
  relay: false,
  assistantCaller: "local",
  addedRemoteHosts: true,
  manifestSource: "view",
} as const;

const RELAYED = {
  callerOrigin: "relayed",
  origin: "external",
  workspaceBound: true,
  paneApproval: false,
  helpSession: false,
  hostControlReachable: false,
  relay: true,
  assistantCaller: "relay",
  addedRemoteHosts: false,
  manifestSource: "view",
} as const;

// Panes, the sidebar assistant and API-key clients are captured on a Shell
// that has added a Remote Host, so their files show the host-gated actions
// where hosts exist, and that only the sidebar assistant is offered host
// control. The stand-in runtime would count any help session it is asked about
// as reachable; a pane or API-key session is never asked, having no help
// session. A relayed session runs on the host, listed from a view of the bound
// project on a machine that has added no host of its own.
const SURFACES: SurfaceSpec[] = [
  {
    ...PANE,
    name: "pane-core",
    tier: "core",
    description:
      "An agent CLI (Claude Code, Codex, ...) running in a Daintree terminal pane with the project's Daintree MCP set to Core, on a Shell that has added a Remote Host. Authenticated by the per-pane bearer Daintree writes into its --mcp-config; origin 'external', workspace-bound to its launch workspace, with an approval route. It is no help session, so it is never offered host.list or host.call, and no assistant caller, so never assistant.list, assistant.start or agent.readQuota.",
  },
  {
    ...PANE,
    name: "pane-full",
    tier: "full",
    description: "The same agent-pane caller with the project's Daintree MCP set to Full.",
  },
  {
    ...HELP,
    name: "help-core",
    tier: "core",
    description:
      "Daintree's own sidebar assistant (a help session) in one of this machine's own windows with a project open, Tool set = Core, on a Shell that has added a Remote Host. Help-token bearer, origin 'help' (renderer-owned), pinned to the WebContents that minted it; no approval route. The host-control runtime counts it as one of this machine's assistants in its own view, so host.list and host.call are listed from the session's start, whether or not a host exists yet; it is an assistant caller in its project, so assistant.list, assistant.start and agent.readQuota are listed, at Core as at Full; as this machine's own assistant it is offered project.runCommand at Core as at Full.",
  },
  {
    ...HELP,
    name: "help-full",
    tier: "full",
    description:
      "The same sidebar help session with Tool set = Full: the Full tier allowlist adds project.runCheck, terminal.new and the rest, and the Full-only window moves host.switch and project.openOnHost, listed because the Shell has added a host.",
  },
  {
    ...HELP,
    name: "help-core-remote-view",
    variantOf: "help-core",
    variantKey: "in_a_remote_shells_view",
    tier: "core",
    hostControlReachable: false,
    description:
      "A help session this machine runs for a remote Shell's window (its view is a remote endpoint), Tool set = Core. The host-control runtime does not count it (isCallReachable false: a host relaying through here could reach every other), so host.list and host.call are not listed; it is still an assistant caller in its project.",
  },
  {
    ...HELP,
    name: "help-full-remote-view",
    variantOf: "help-full",
    variantKey: "in_a_remote_shells_view",
    tier: "full",
    hostControlReachable: false,
    description:
      "A help session this machine runs for a remote Shell's window, Tool set = Full: host.list and host.call not listed.",
  },
  {
    ...HELP,
    name: "help-core-viewless",
    variantOf: "help-core",
    variantKey: "with_no_view",
    tier: "core",
    hostControlReachable: false,
    manifestSource: "viewless-help",
    description:
      "This machine's own assistant running with no window (#13511), such as a host's assistant a Shell started through host.call assistant.start, Tool set = Core: help origin, no pinned view, listed from the viewless help base surface (getViewlessHelpBaseManifest). Not in a local view, so no host control; an assistant caller in its project.",
  },
  {
    ...HELP,
    name: "help-full-viewless",
    variantOf: "help-full",
    variantKey: "with_no_view",
    tier: "full",
    hostControlReachable: false,
    manifestSource: "viewless-help",
    description: "The same assistant with no window, Tool set = Full.",
  },
  {
    ...HELP,
    name: "global-core",
    callerOrigin: "global",
    tier: "core",
    manifestSource: "viewless-help",
    global: true,
    description:
      "This installation's one global Daintree Assistant (#14159, #14161), Tool set = Core: a help session main runs with no project and no view, its row naming GLOBAL_ASSISTANT_ID, listed from the viewless help base surface. Offered only its root tools (GLOBAL_ASSISTANT_TOOL_IDS); every project, this machine's under hostId local, is reached through host.call, where the host admits only GLOBAL_ALLOWED_TOOLS (recorded in capture.host_call), the owned tools acting on what it launched there or the person handed it.",
  },
  {
    ...HELP,
    name: "global-full",
    callerOrigin: "global",
    tier: "full",
    manifestSource: "viewless-help",
    global: true,
    description: "The same global assistant with Tool set = Full.",
  },
  {
    name: "external",
    callerOrigin: "api-key",
    origin: "external",
    tier: "external",
    workspaceBound: false,
    paneApproval: false,
    helpSession: false,
    hostControlReachable: true,
    relay: false,
    assistantCaller: null,
    addedRemoteHosts: true,
    manifestSource: "view",
    description:
      "A third-party MCP client authenticated with the Daintree API key (or with no key configured), not bound to a workspace, on a Shell that has added a Remote Host. Origin 'external', tier 'external'.",
  },
  {
    ...RELAYED,
    name: "relayed-core",
    tier: "core",
    description:
      "The session a Remote Host opens for a Shell's assistant reaching it through host.call (openRelaySession, #13493), at the calling assistant's Tool set = Core: origin 'external', bound to the one host project the call names, deps.relay set, no approval route, and the relay principal an assistant caller in that project. Listed from a live view of the bound project in which no Remote Host has been added. Never offered a host.* tool or help.displayImage, whatever its tier; gets the owned terminal twins; assistant.list, assistant.start and agent.readQuota by the assistant-caller rule, and project.runCommand by the command-runner rule (#13501), held to the drive rule at the call. What host.call can name on a host is this listing.",
  },
  {
    ...RELAYED,
    name: "relayed-full",
    tier: "full",
    description:
      "The same relayed session at the calling assistant's Tool set = Full: the Full tier allowlist adds project.runCheck, terminal.new and the rest.",
  },
  {
    ...RELAYED,
    name: "relayed-core-no-view",
    variantOf: "relayed-core",
    variantKey: "no_view_attached",
    tier: "core",
    manifestSource: "no-view",
    description:
      "The same relayed session, Core, on a host with no view of the bound project attached: the route refuses with NoFrontendAttachedError and tools/list falls back to the host's external base surface (getExternalBaseManifest), filtered by the session's gates. Calls are still admitted by tier and caller, not by this listing.",
  },
  {
    ...RELAYED,
    name: "relayed-full-no-view",
    variantOf: "relayed-full",
    variantKey: "no_view_attached",
    tier: "full",
    manifestSource: "no-view",
    description: "The same relayed session with no view attached, Full.",
  },
];

/**
 * What the session server should offer this caller beyond its tier allowlist,
 * stated here from the spec rather than read from the app, and cross-checked
 * against what it lists.
 */
function expectedGates(spec: SurfaceSpec) {
  const inAppTier = spec.tier === "core" || spec.tier === "full";
  return {
    // `hostCallOffered`: a session bound to a help session, with host-control
    // access (every session but a relayed one), that the runtime counts.
    hostCall: spec.helpSession && !spec.relay && spec.hostControlReachable,
    // `mayControlAssistants`: an assistant caller at an in-app tier.
    assistantCaller: spec.assistantCaller !== null && inAppTier,
    // `mayRunProjectCommand`: an in-app tier, and this machine's assistant or a relay.
    commandRunner: inAppTier && (spec.origin === "help" || spec.relay),
  };
}

function checkSpec(spec: SurfaceSpec): void {
  const bad = (why: string) => new Error(`${spec.name}: inconsistent spec: ${why}`);
  if (spec.helpSession !== (spec.origin === "help")) throw bad("help sessions have origin help");
  if (spec.relay && (spec.origin !== "external" || !spec.workspaceBound)) {
    throw bad("a relayed session is external and bound");
  }
  if ((spec.assistantCaller === "relay") !== spec.relay) throw bad("relay caller iff relayed");
  if (spec.assistantCaller === "local" && !spec.helpSession) throw bad("local caller is a help session");
  if (spec.paneApproval && (!spec.workspaceBound || spec.origin !== "external")) {
    throw bad("only a bound external pane has an approval route");
  }
  if (spec.manifestSource === "no-view" && !spec.workspaceBound) throw bad("no-view is bound");
  if (spec.manifestSource === "viewless-help" && !spec.helpSession) throw bad("viewless is help");
  if (spec.global && (spec.manifestSource !== "viewless-help" || spec.workspaceBound)) {
    throw bad("the global assistant is a viewless, unbound help session");
  }
}

const WORKSPACE_BINDING = {
  kind: "project",
  workspaceId: "ws-capture",
  workspacePath: "/capture/workspace",
} as const;
const CAPTURE_WEB_CONTENTS_ID = 7;
const SHELL = {
  linkSessionId: "capture-link-session",
  senderId: "capture-sender",
  helpSessionId: "capture-shell-help-session",
  shellName: "capture-shell",
} as const;

const helpSessionIdOf = (sessionId: string) => `help-${sessionId}`;

function unreachable(what: string): () => never {
  return () => {
    throw new Error(`capture: ${what} must not be reached while listing tools`);
  };
}

/** The store rows `httpLifecycle` writes for this caller at handshake (or `openRelaySession`). */
function sessionStoreFor(sessionId: string, spec: SurfaceSpec): SessionStore {
  const store = new SessionStore(() => {});
  store.sessionTierMap.set(sessionId, spec.tier);
  store.sessionOriginMap.set(sessionId, spec.origin);
  if (spec.workspaceBound) store.sessionWorkspaceMap.set(sessionId, WORKSPACE_BINDING.workspaceId);
  if (spec.helpSession) {
    store.sessionHelpIdMap.set(sessionId, helpSessionIdOf(sessionId));
    if (spec.manifestSource === "viewless-help") {
      store.sessionViewlessHelpMap.set(
        sessionId,
        spec.global ? GLOBAL_ASSISTANT_ID : WORKSPACE_BINDING.workspaceId
      );
    } else {
      store.sessionWebContentsMap.set(sessionId, CAPTURE_WEB_CONTENTS_ID);
    }
  }
  if (spec.relay) store.sessionRelayMap.set(sessionId, { ...SHELL });
  return store;
}

/** The manifest this session's `tools/list` resolves, as main would reach it. */
function manifestFor(
  spec: SurfaceSpec,
  viewManifests: Map<boolean, ActionManifestEntry[]>
): ActionManifestEntry[] {
  switch (spec.manifestSource) {
    case "view":
      return viewManifests.get(spec.addedRemoteHosts)!;
    case "no-view":
      return getExternalBaseManifest();
    case "viewless-help":
      return getViewlessHelpBaseManifest();
  }
}

function sessionDeps(
  sessionId: string,
  spec: SurfaceSpec,
  store: SessionStore,
  manifest: ActionManifestEntry[]
): SessionServerDeps {
  const caller =
    spec.assistantCaller === "local"
      ? {
          kind: "local" as const,
          helpSessionId: helpSessionIdOf(sessionId),
          projectId: spec.global ? GLOBAL_ASSISTANT_ID : WORKSPACE_BINDING.workspaceId,
          machineName: "capture-machine",
        }
      : spec.assistantCaller === "relay"
        ? {
            kind: "relay" as const,
            senderId: SHELL.senderId,
            linkSessionId: SHELL.linkSessionId,
            helpSessionId: SHELL.helpSessionId,
            projectId: WORKSPACE_BINDING.workspaceId,
            machineName: SHELL.shellName,
          }
        : null;
  const noView = spec.manifestSource === "no-view";
  const deps: Record<string, unknown> = {
    sessionStore: store,
    // Fresh structured clones per request, as the renderer IPC would deliver.
    // A bound workspace with nothing attached refuses as the bridge does.
    requestManifest: noView
      ? async () => {
          throw new NoFrontendAttachedError(WORKSPACE_BINDING.workspaceId);
        }
      : async () => structuredClone(manifest),
    getCachedManifest: noView ? () => null : () => structuredClone(manifest),
    dispatchAction: unreachable("dispatchAction"),
    handleWaitUntilIdle: unreachable("handleWaitUntilIdle"),
    handleWaitUntilIdleBatch: unreachable("handleWaitUntilIdleBatch"),
    handleSkillsSearch: unreachable("handleSkillsSearch"),
    handleSkillsLoad: unreachable("handleSkillsLoad"),
    handleProjectRunCheck: unreachable("handleProjectRunCheck"),
    handleProjectRunCommand: unreachable("handleProjectRunCommand"),
    handleTerminalGetStatusViewless: unreachable("handleTerminalGetStatusViewless"),
    handleTerminalReadLastMessageOwned: unreachable("handleTerminalReadLastMessageOwned"),
    isTerminalIdInUse: () => false,
    appendAuditRecord: () => {},
    // Always wired by httpLifecycle; whether a session is an assistant caller
    // is `assistantCaller`'s answer alone.
    assistantControl: {
      isAssistantTerminal: () => false,
      resolve: () => null,
      list: unreachable("assistantControl.list"),
      start: unreachable("assistantControl.start"),
      submit: unreachable("assistantControl.submit"),
      interrupt: unreachable("assistantControl.interrupt"),
      close: unreachable("assistantControl.close"),
      readReply: unreachable("assistantControl.readReply"),
    },
    assistantCaller: () => caller,
    readCodexQuota: unreachable("readCodexQuota"),
  };
  if (spec.relay) {
    // A relayed session has no host control of its own: a call never chains.
    deps["relay"] = { shellName: SHELL.shellName, refuseWrite: unreachable("relay.refuseWrite") };
  } else {
    // Bound for every other session, as httpLifecycle binds it: whether it is
    // offered anything is the session server's and the runtime's decision.
    deps["hostControl"] = bindHostControl(() => store.sessionHelpIdMap.get(sessionId));
    if (spec.manifestSource === "viewless-help") deps["refuseWrite"] = unreachable("refuseWrite");
  }
  if (spec.workspaceBound) deps["workspaceBinding"] = { ...WORKSPACE_BINDING };
  if (spec.paneApproval) deps["requestApproval"] = unreachable("requestApproval");
  return deps as unknown as SessionServerDeps;
}

/**
 * A minimal raw JSON-RPC client over the SDK's in-memory transport. The server
 * side is registered in the store first, the way the real transports are, so
 * the session is live (`getTier`) before the first request.
 */
async function openRawSession(
  server: ReturnType<typeof createSessionServer>,
  register: (transport: InMemoryTransport) => void
) {
  const [clientTransport, serverTransport] = InMemoryTransport.createLinkedPair();
  register(serverTransport);
  const pending = new Map<number, (msg: Record<string, unknown>) => void>();
  clientTransport.onmessage = (message: JSONRPCMessage) => {
    const msg = message as Record<string, unknown>;
    if (typeof msg["id"] === "number" && ("result" in msg || "error" in msg)) {
      pending.get(msg["id"])?.(msg);
      pending.delete(msg["id"]);
    }
  };
  await server.connect(serverTransport);
  await clientTransport.start();
  let nextId = 1;
  const request = (method: string, params: Record<string, unknown>) =>
    new Promise<Record<string, unknown>>((resolve, reject) => {
      const id = nextId++;
      const timer = setTimeout(() => reject(new Error(`${method} timed out`)), 10_000);
      pending.set(id, (msg) => {
        clearTimeout(timer);
        // What a client parses off the wire: a JSON round trip drops undefined
        // members and anything else JSON cannot carry.
        resolve(JSON.parse(JSON.stringify(msg)) as Record<string, unknown>);
      });
      void clientTransport.send({ jsonrpc: "2.0", id, method, params } as JSONRPCMessage);
    });
  const notify = (method: string) =>
    clientTransport.send({ jsonrpc: "2.0", method } as JSONRPCMessage);
  return { request, notify, close: () => clientTransport.close() };
}

async function buildViewManifest(addedRemoteHosts: boolean): Promise<{
  manifest: ActionManifestEntry[];
  contextual: string[];
}> {
  const registry = await createActionRegistry();
  const service = new ActionService();
  const contextual: string[] = [];
  for (const factory of registry.values()) {
    const definition = factory();
    if (service.has(definition.id)) continue;
    service.register(definition);
    if (definition.isVisible !== undefined) contextual.push(definition.id);
  }
  // What `useMcpBridge` answers a manifest request with, minus the live
  // context: `list()` evaluates `isVisible` against it. Any MCP-reachable action
  // that declares `isVisible` is reported in the capture so the dependence is
  // visible rather than silent. The one piece of renderer state set here is
  // the added-hosts list of the view being captured.
  hostList.hosts = addedRemoteHosts ? [{ descriptor: { id: "capture-host" } }] : [];
  const manifest = service.list({}, { includeSchemas: true });
  hostList.hosts = [];
  return { manifest, contextual };
}

const sortIds = (ids: Iterable<string>) => [...ids].sort();

describe("capture Daintree MCP surfaces", () => {
  it("writes one fixture per caller surface", async () => {
    const outDir = process.env.CAPTURE_OUT_DIR;
    const commit = process.env.CAPTURE_APP_COMMIT;
    if (!outDir || !commit) {
      throw new Error("CAPTURE_OUT_DIR and CAPTURE_APP_COMMIT are required");
    }
    const branch = process.env.CAPTURE_APP_BRANCH || null;
    const capturedAt = process.env.CAPTURE_AT || new Date().toISOString().slice(0, 10);
    mkdirSync(outDir, { recursive: true });

    // One view manifest per added-hosts state: a view whose machine has added
    // a Remote Host shows the host-gated actions its `isVisible` admits.
    const withHosts = await buildViewManifest(true);
    const withoutHosts = await buildViewManifest(false);
    const viewManifests = new Map([
      [true, withHosts.manifest],
      [false, withoutHosts.manifest],
    ]);
    const contextual = withHosts.contextual;

    // The app's independent wire measurement: registry -> z.toJSONSchema ->
    // toWireSchema, without ActionService or the session server.
    const measured = new Map((await measureWireSurface()).map((tool) => [tool.id, tool]));

    const captured = new Map<
      string,
      { capture: Record<string, unknown>; tools: Array<Record<string, unknown>> }
    >();
    for (const spec of SURFACES) {
      checkSpec(spec);
      const sessionId = `capture-${spec.name}`;
      const gates = expectedGates(spec);
      const rendererOwned = spec.origin === "help";
      const manifest = manifestFor(spec, viewManifests);
      const byId = new Map(manifest.map((entry) => [entry.id, entry]));

      // The stand-in host-control runtime: it answers only the listing
      // question, and records who asked.
      const askedReachable: string[] = [];
      const runtime: HostControlRuntimeHandle = {
        isCallReachable: (helpSessionId: string) => {
          askedReachable.push(helpSessionId);
          return spec.hostControlReachable;
        },
        list: unreachable("host.list runtime"),
        call: unreachable("host.call runtime"),
      };
      const uninstallRuntime = setHostControlRuntime(runtime);

      const store = sessionStoreFor(sessionId, spec);
      expect(store.isRendererOwnedOrigin(sessionId), `${spec.name}: origin ownership`).toBe(
        rendererOwned
      );
      const server = createSessionServer(sessionId, sessionDeps(sessionId, spec, store, manifest));
      const idleTimer = setTimeout(() => {}, 1_000_000);
      const session = await openRawSession(server, (transport) => {
        if (spec.relay) store.relaySessions.set(sessionId, { transport, server });
        else store.httpSessions.set(sessionId, { transport, server, idleTimer } as never);
      });

      const init = await session.request("initialize", {
        protocolVersion: LATEST_PROTOCOL_VERSION,
        capabilities: {},
        clientInfo: { name: "daintree-surface-capture", version: "1.0.0" },
      });
      if (!init["result"]) throw new Error(`${spec.name}: initialize failed ${JSON.stringify(init)}`);
      const initResult = init["result"] as { protocolVersion: string };
      await session.notify("notifications/initialized");

      const listed = await session.request("tools/list", {});
      if (!listed["result"]) throw new Error(`${spec.name}: tools/list failed ${JSON.stringify(listed)}`);
      const listResult = listed["result"] as {
        tools: Array<Record<string, unknown>>;
        nextCursor?: string;
      };
      expect(listResult.nextCursor, `${spec.name}: tools/list paginated`).toBeUndefined();

      // mcp.surface, called for real; it must agree with buildSurfaceManifest
      // over the same manifest and the policy this file states.
      const policy: SessionSurfacePolicy = {
        workspaceBound: spec.workspaceBound,
        paneApproval: spec.paneApproval,
        paneSkipConfirmations: false,
        relay: spec.relay,
        rendererOwnedOrigin: rendererOwned,
        hostCall: gates.hostCall,
        assistantCaller: spec.assistantCaller !== null,
      };
      const direct = buildSurfaceManifest(manifest, spec.tier, appVersion, policy);
      let surfaceHash: string;
      let surfaceHashMethod: string;
      let surfaceTiers = new Map<string, string>(direct.tools.map((t) => [t.id, t.tier]));
      const call = await session.request("tools/call", { name: "mcp.surface", arguments: {} });
      const callResult = call["result"] as
        | {
            isError?: boolean;
            structuredContent?: { hash?: string; tools?: Array<{ id: string; tier: string }> };
          }
        | undefined;
      if (callResult && !callResult.isError && callResult.structuredContent?.hash) {
        surfaceHash = callResult.structuredContent.hash;
        surfaceTiers = new Map(
          (callResult.structuredContent.tools ?? []).map((t) => [t.id, t.tier])
        );
        expect(surfaceHash, `${spec.name}: mcp.surface disagrees with buildSurfaceManifest`).toBe(
          direct.hash
        );
        surfaceHashMethod =
          "structuredContent.hash of a real tools/call mcp.surface on the same session (equal to buildSurfaceManifest over the same manifest and session policy)";
      } else {
        surfaceHash = direct.hash;
        surfaceHashMethod = `buildSurfaceManifest(manifest, tier, appVersion, sessionPolicy) — the function the mcp.surface handler calls with the same inputs; the real tools/call was refused in the capture harness: ${JSON.stringify(call["result"] ?? call["error"]).slice(0, 300)}`;
      }
      await session.close();
      await server.close();
      clearTimeout(idleTimer);
      store.drain();
      store.grantCache.dispose();
      uninstallRuntime();

      // The runtime is asked about a session's own help session, and only
      // when it has one: a pane or API-key session never gets that far, and a
      // relayed session has no host-control access at all.
      if (spec.helpSession && !spec.relay) {
        expect(askedReachable.length, `${spec.name}: runtime never asked`).toBeGreaterThan(0);
        expect(new Set(askedReachable), `${spec.name}: runtime asked about another session`).toEqual(
          new Set([helpSessionIdOf(sessionId)])
        );
      } else {
        expect(askedReachable, `${spec.name}: runtime asked for a non-help session`).toEqual([]);
      }

      // Everything the tier lets this session reach, plus what the caller
      // rules admit past the allowlist.
      const allowlisted = new Set(getReachableActionIds(spec.tier, rendererOwned, spec.paneApproval));
      if (gates.commandRunner) allowlisted.add(PROJECT_RUN_COMMAND);
      if (gates.assistantCaller) for (const id of ASSISTANT_CONTROL_TOOL_IDS) allowlisted.add(id);
      const globalRootOnly = (id: string) => !spec.global || GLOBAL_ASSISTANT_TOOL_IDS.has(id);

      // Why the caller rules withhold an allowlisted id from this session
      // whatever its view shows, or null when they don't.
      const gateReason = (id: string): string | null => {
        if (!globalRootOnly(id)) {
          return "not a root tool of the global assistant: it reaches every project through host.call";
        }
        if (spec.relay && isWithheldFromRelaySession(id)) {
          return "withheld from a relayed session: no host.* tool (a call never chains), none of this machine's assistant-panel tools";
        }
        if (HOST_CONTROL_TOOL_IDS.has(id) && !gates.hostCall) {
          return spec.helpSession
            ? "host control not offered: the runtime does not count this help session as one of this machine's assistants in its own view"
            : "host control not offered: not one of this machine's help sessions";
        }
        if (id === PROJECT_RUN_COMMAND && !gates.commandRunner) {
          return "command runner not offered: only this machine's assistant or a relay, at an in-app tier";
        }
        if (ASSISTANT_CONTROL_TOOL_IDS.has(id) && !gates.assistantCaller) {
          return "assistant tools not offered: not an assistant caller";
        }
        const entry = byId.get(id);
        if (entry && isWithheldFromBoundSession(entry, spec.tier, policy)) {
          return "confirm-gated: withheld from a workspace-bound external session";
        }
        return null;
      };
      // Why an allowlisted id is not listed, or null when it should be.
      const notListedReason = (id: string): string | null => {
        const gated = gateReason(id);
        if (gated !== null) return gated;
        const entry = byId.get(id);
        if (!entry) {
          return spec.manifestSource === "view"
            ? "absent from ActionService.list({}) (isVisible false under an empty context, or restricted)"
            : `absent from the ${spec.manifestSource === "no-view" ? "external" : "viewless help"} base manifest this session is listed from`;
        }
        if (entry.mcpVisibility === "hidden") return "mcpVisibility: hidden";
        if (entry.danger === "restricted") return "danger: restricted";
        return null;
      };

      const listedIds = sortIds(listResult.tools.map((t) => String(t["name"])));
      const unexpected = listedIds.filter((id) => !allowlisted.has(id) || notListedReason(id) !== null);
      expect(
        unexpected.map((id) => `${id}: ${notListedReason(id) ?? "outside the allowlist"}`),
        `${spec.name}: listed ids this caller should not be offered`
      ).toEqual([]);
      const withheld = sortIds([...allowlisted].filter((id) => !listedIds.includes(id))).map(
        (id) => ({ action_id: id, reason: notListedReason(id) ?? "unexplained" })
      );
      expect(
        withheld.filter((w) => w.reason === "unexplained"),
        `${spec.name}: allowlisted tools missing from tools/list`
      ).toEqual([]);

      // The host-control contract, stated outright as well.
      const ids = new Set(listedIds);
      for (const id of HOST_CONTROL_TOOL_IDS) {
        // The global assistant is listed from the viewless base manifest, so a
        // root tool appears only where that manifest carries it.
        const inManifest = !spec.global || byId.has(id);
        expect(ids.has(id), `${spec.name}: ${id} listing`).toBe(
          gates.hostCall && globalRootOnly(id) && inManifest
        );
      }
      if (spec.global) {
        expect(listedIds.filter((id) => !GLOBAL_ASSISTANT_TOOL_IDS.has(id)), `${spec.name}: beyond its root tools`).toEqual([]);
      }
      if (spec.relay) {
        expect(listedIds.filter((id) => id.startsWith("host.")), `${spec.name}: host.* on a relay`).toEqual([]);
        expect(ids.has("help.displayImage"), `${spec.name}: help.displayImage on a relay`).toBe(false);
      }
      if (spec.manifestSource === "view" && !spec.global) {
        for (const id of ASSISTANT_CONTROL_TOOL_IDS) {
          expect(ids.has(id), `${spec.name}: ${id} listing`).toBe(gates.assistantCaller);
        }
        expect(ids.has(PROJECT_RUN_COMMAND), `${spec.name}: project.runCommand listing`).toBe(
          gates.commandRunner
        );
      }

      const tools = listResult.tools
        .map((wire) => {
          const id = String(wire["name"]);
          const entry = byId.get(id);
          if (!entry) throw new Error(`${spec.name}: listed ${id} has no manifest entry`);
          expect(shouldExposeTool(entry, spec.tier, policy), `${spec.name}: ${id} exposure`).toBe(true);

          // Independent cross-check of the advertised input schema.
          const m = measured.get(id);
          if (!m) throw new Error(`${spec.name}: ${id} not in measureWireSurface cohort`);
          const omitted = spec.tier === "external" ? MCP_EXTERNAL_OMITTED_ARGS[id] : undefined;
          const expectedInput =
            omitted !== undefined
              ? omitWireSchemaArgs(m.inputSchema as Record<string, unknown>, omitted)
              : m.inputSchema;
          expect(wire["inputSchema"], `${spec.name}: ${id} inputSchema`).toEqual(
            JSON.parse(JSON.stringify(expectedInput))
          );
          expect(wire["description"], `${spec.name}: ${id} description`).toBe(m.description);

          // How a call is admitted, as the dispatch gate decides it: the
          // command runner and the assistant tools by their caller rule, the
          // rest by the tier, and for a pane above its tier by asking.
          const reach =
            id === PROJECT_RUN_COMMAND || ASSISTANT_CONTROL_TOOL_IDS.has(id)
              ? "caller"
              : isTierPermitted(spec.tier, id, rendererOwned)
                ? "tier"
                : "approval";
          if (reach === "approval") {
            expect(spec.paneApproval, `${spec.name}: ${id} listed above the tier`).toBe(true);
          }
          const tool: Record<string, unknown> = {
            action_id: id,
            wire_name: wire["name"],
            description: wire["description"],
            inputSchema: wire["inputSchema"],
            outputSchema: wire["outputSchema"] ?? null,
            annotations: wire["annotations"] ?? null,
          };
          if (wire["_meta"] !== undefined) tool["_meta"] = wire["_meta"];
          // Not on the wire: the manifest facts main decides this listing
          // from, recorded so a reader can tell a confirm-gated tool or an
          // ask-first tool apart without the app.
          tool["daintree"] = {
            kind: entry.kind,
            category: entry.category,
            danger: entry.danger,
            band: entry.band,
            surface_tier: surfaceTiers.get(id) ?? null,
            reach,
          };
          // Guard against the wire carrying anything this fixture drops.
          const known = new Set(["name", "description", "inputSchema", "outputSchema", "annotations", "_meta"]);
          const extra = Object.keys(wire).filter((k) => !known.has(k));
          expect(extra, `${spec.name}: ${id} unrecorded wire keys`).toEqual([]);
          return tool;
        })
        .sort((a, b) => (String(a["action_id"]) < String(b["action_id"]) ? -1 : 1));
      // The global assistant may not call mcp.surface, and its root-tool filter
      // is the session server's, not buildSurfaceManifest's: compare its share.
      expect(
        new Set(direct.tools.map((t) => t.id).filter(globalRootOnly)),
        `${spec.name}: mcp.surface tools differ from tools/list`
      ).toEqual(ids);

      // Bound api-key sessions lose confirm-gated tools (#11789); record which.
      const boundExternalWithheld =
        spec.tier === "external"
          ? tools
              .filter((t) =>
                isWithheldFromBoundSession(byId.get(String(t["action_id"]))!, "external", {
                  workspaceBound: true,
                })
              )
              .map((t) => t["action_id"])
          : undefined;

      const notes: string[] = [];
      if (spec.paneApproval) {
        notes.push(
          "An agent pane's tier is where auto-approval stops, not a ceiling (#12692): tools/list also carries every tool up to the full non-renderer-owned surface, and calling one above the tier asks the user first. The wire listing is therefore the same at core and full; tools[].daintree.reach says which run without asking ('tier') and which ask ('approval')."
        );
      }
      if ((gates.assistantCaller || gates.commandRunner) && !spec.global) {
        notes.push(
          "tools[].daintree.reach 'caller': admitted by its caller rule rather than the tier allowlist (mayControlAssistants for assistant.list, assistant.start and agent.readQuota; mayRunProjectCommand for project.runCommand). No grant widens either."
        );
      }
      if (spec.global) {
        const unlisted = sortIds([...GLOBAL_ASSISTANT_TOOL_IDS].filter((id) => !ids.has(id)));
        if (unlisted.length > 0) {
          notes.push(
            `Root tools the dispatch guard admits but tools/list does not carry: ${unlisted.join(", ")}. They are absent from the viewless help base manifest (generated mcpExternalBaseManifest + mcpViewlessBaseManifest) this session is listed from, so a client that calls only listed tools cannot reach them at this commit.`
          );
        }
      }
      if (spec.relay) {
        notes.push(
          "What a Shell's assistant can name in host.call's `tool` on this host: the host's own gates decide each call (its tier, the relay principal's ownership, the drive rule for changes), and notices armed here are typed into the calling run's pane on the Shell (#13500). Confirm-gated calls are put to the person at the Shell (#13501)."
        );
      }

      const fixture = {
        capture: {
          app_repo: "daintreehq/daintree",
          app_branch: branch,
          app_commit: commit,
          app_version: appVersion,
          caller_origin: spec.callerOrigin,
          session_origin: spec.origin,
          tier: spec.tier,
          session: {
            workspace_bound: spec.workspaceBound,
            pane_approval: spec.paneApproval,
            renderer_owned_origin: rendererOwned,
            relay: spec.relay,
          },
          caller: spec.description,
          // Direct host control: what the session is, and what the listing
          // offered it beyond its tier.
          host_control: {
            help_session: spec.helpSession,
            call_reachable: spec.helpSession ? spec.hostControlReachable : null,
            host_tools_offered: gates.hostCall,
            assistant_caller: spec.assistantCaller,
            // The global assistant passes the caller rules, then lists and runs
            // only its root tools: what it is effectively offered.
            assistant_tools_offered: gates.assistantCaller && !spec.global,
            command_runner_offered: gates.commandRunner && !spec.global,
          },
          // The view that answered the manifest.
          machine: {
            added_remote_hosts: spec.addedRemoteHosts,
            manifest_source: spec.manifestSource,
          },
          captured_at: capturedAt,
          mcp_protocol_version: initResult.protocolVersion,
          method:
            "Real ActionService.list({}, {includeSchemas:true}) over the real createActionDefinitions registry (or the app's generated base surface where the session has no view), served by the real createSessionServer (electron/services/mcp-server/sessionServer.ts) over the real SessionStore configured as this caller, with the real bindHostControl over a stand-in runtime that answers only isCallReachable, and read back as raw JSON-RPC tools/list over the MCP SDK in-memory transport, JSON round-tripped. Input schemas and descriptions cross-checked against the app's measureWireSurface helper.",
          surface_hash: surfaceHash,
          surface_hash_method: surfaceHashMethod,
          tool_count: tools.length,
          ...(withheld.length > 0 ? { allowlisted_but_not_listed: withheld } : {}),
          ...(boundExternalWithheld ? { withheld_when_workspace_bound: boundExternalWithheld } : {}),
          ...(spec.tier === "external" ? { omitted_args: MCP_EXTERNAL_OMITTED_ARGS } : {}),
          // Tools this session could be offered whose presence depends on
          // `isVisible` against the live renderer context, which this capture
          // evaluates as `{}` (plus the added-hosts list in `machine`).
          ...(spec.manifestSource === "view"
            ? {
                context_gated_tools: contextual.filter(
                  (id) => allowlisted.has(id) && gateReason(id) === null
                ),
              }
            : {}),
          // What `host.call` admits from this caller in any project, on any
          // host (`HostCallRuntime.call`): an allow-list, refused otherwise
          // as `policy_rejected` before the call leaves this machine.
          ...(spec.global
            ? {
                // What the dispatch guard runs for it directly
                // (`sessionServer.ts`), whether or not the listing shows it.
                root_tools: sortIds(GLOBAL_ASSISTANT_TOOL_IDS),
                root_tools_listed: sortIds([...GLOBAL_ASSISTANT_TOOL_IDS].filter((id) => ids.has(id))),
                host_call: {
                  admits: sortIds(GLOBAL_ALLOWED_TOOLS),
                  withheld: sortIds(GLOBAL_WITHHELD_TOOLS),
                  method:
                    "GLOBAL_ALLOWED_TOOLS and GLOBAL_WITHHELD_TOOLS imported from electron/remote/assistant/HostCallRuntime.ts at this commit. What a call can name is the relayed surface of the same Tool set narrowed to `admits`: the host still applies its tier, ownership and drive rule.",
                },
              }
            : {}),
          // What Daintree provisions this assistant's CLI with besides the MCP
          // (HelpSessionService): the runbook server, its headers, the
          // runbook and checkpoint instructions, and the env its PTY gets.
          ...(spec.global
            ? {
                native_setup: {
                  runbooks_mcp_server: RUNBOOKS_MCP_SERVER_NAME,
                  runbooks_headers_connected: buildRunbooksHostHeaders("connected"),
                  runbooks_headers_unknown: buildRunbooksHostHeaders(null),
                  runbooks_addendum: buildRunbooksAddendum({ global: true }),
                  checkpoint_env: ASSISTANT_RUN_CHECKPOINT_ENV_VAR,
                  checkpoint_writer_env: ASSISTANT_RUN_CHECKPOINT_WRITER_ENV_VAR,
                  checkpoint_bucket: ASSISTANT_GLOBAL_CHECKPOINT_BUCKET,
                  checkpoint_addendum: buildRunCheckpointAddendum(),
                  scratch_env: ASSISTANT_SCRATCH_ENV_VAR,
                },
              }
            : {}),
          ...(notes.length > 0 ? { notes } : {}),
        } as Record<string, unknown>,
        tools,
      };
      captured.set(spec.name, fixture);
    }

    // Record each variant in the surface it varies, as the ids its listing
    // adds, drops or lists differently.
    for (const spec of SURFACES) {
      if (spec.variantOf === undefined) continue;
      const base = captured.get(spec.variantOf);
      const own = captured.get(spec.name)!;
      if (!base || base.capture["tier"] !== spec.tier || spec.variantKey === undefined) {
        throw new Error(`${spec.name}: variant base ${spec.variantOf} missing, another tier, or no key`);
      }
      const baseById = new Map(base.tools.map((t) => [String(t["action_id"]), t]));
      const ownIds = own.tools.map((t) => String(t["action_id"]));
      const variants = (base.capture["variants"] ??= {}) as Record<string, unknown>;
      if (variants[spec.variantKey] !== undefined) throw new Error(`${spec.name}: duplicate variant`);
      variants[spec.variantKey] = {
        caller: spec.description,
        host_control: own.capture["host_control"],
        machine: own.capture["machine"],
        tool_count: own.tools.length,
        surface_hash: own.capture["surface_hash"],
        added: ownIds.filter((id) => !baseById.has(id)),
        removed: [...baseById.keys()].filter((id) => !ownIds.includes(id)),
        changed: own.tools
          .filter((t) => {
            const prior = baseById.get(String(t["action_id"]));
            return prior !== undefined && JSON.stringify(prior) !== JSON.stringify(t);
          })
          .map((t) => String(t["action_id"])),
      };
    }

    for (const spec of SURFACES) {
      if (spec.variantOf !== undefined) continue;
      const file = path.join(outDir, `${spec.name}.json`);
      writeFileSync(file, JSON.stringify(captured.get(spec.name), null, 2) + "\n", "utf8");
      // Sanity: the file we wrote parses back to what was captured.
      expect(JSON.parse(readFileSync(file, "utf8"))).toEqual(
        JSON.parse(JSON.stringify(captured.get(spec.name)))
      );
    }
  }, 180_000);
});
