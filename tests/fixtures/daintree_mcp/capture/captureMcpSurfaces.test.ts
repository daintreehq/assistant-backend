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
 *      `ActionService` over the real `createActionDefinitions` registry.
 *   2. Hand that manifest to the real `createSessionServer` with a session store
 *      and deps configured as the given caller (tier, origin, workspace binding,
 *      pane approval route).
 *   3. Speak raw JSON-RPC to it over the SDK's in-memory transport: `initialize`,
 *      `notifications/initialized`, `tools/list`, then `tools/call mcp.surface`.
 *      The `tools/list` result is JSON round-tripped, which is what the wire
 *      does to it.
 *   4. Cross-check every advertised input schema against the app's independent
 *      wire-measurement helper (`measureWireSurface`), and the listed id set
 *      against the tier allowlists, and fail on any disagreement.
 *
 * Cross-host messaging (#13335, #13336, #13338): every session is given the
 * real `bindAssistantMessaging` / `bindHostMessaging` access, bound to its help
 * session exactly as `httpLifecycle` binds it, over a stand-in runtime that
 * answers only the listing questions for the machine the spec describes (a
 * Shell with a host configured, a machine listening as a host, both, or
 * neither). Nothing is enrolled: the app lists the hub and lane tools from the
 * start of any help session messaging could reach, and that is the listing
 * captured. Every other runtime call throws. The renderer's added-hosts list
 * follows the same machine, since host-gated actions read it in `isVisible`.
 * The hub and lane surfaces are written as `<surface>.delta.json` against the
 * sidebar surface of the same tier.
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
// (`host.switch`, `project.openOnHost`, ...) read in `isVisible`. Set per
// machine below; empty means this machine has added no host.
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
import {
  ASSISTANT_MESSAGING_TOOL_IDS,
  ASSISTANT_SEND_MESSAGE_TOOL,
} from "@shared/types/assistantMessaging";
import { HOST_MESSAGING_TOOL_IDS } from "@shared/types/hostMessaging";
import {
  bindAssistantMessaging,
  setAssistantMessagingRuntime,
  type AssistantMessagingRuntimeHandle,
} from "../../../../electron/services/mcp-server/assistantMessagingTools";
import {
  bindHostMessaging,
  setHostMessagingRuntime,
  type HostMessagingRuntimeHandle,
} from "../../../../electron/services/mcp-server/hostMessagingTools";

import {
  createSessionServer,
  type SessionServerDeps,
} from "../../../../electron/services/mcp-server/sessionServer";
import type { SessionStore } from "../../../../electron/services/mcp-server/sessionStore";
import { GrantCache } from "../../../../electron/services/mcp-server/grantCache";
import { ResourceOwnershipLedger } from "../../../../electron/services/mcp-server/resourceOwnership";
import {
  getReachableActionIds,
  isTierPermitted,
  isWithheldFromBoundSession,
  shouldExposeTool,
} from "../../../../electron/services/mcp-server/tierAuth";
import { buildSurfaceManifest } from "../../../../electron/services/mcp-server/surfaceManifest";
import type { McpTier } from "../../../../electron/services/mcp-server/shared";

const appVersion = (
  JSON.parse(readFileSync(path.resolve(process.cwd(), "package.json"), "utf8")) as {
    version: string;
  }
).version;

type Origin = "help" | "assistant-pane" | "external";

interface SurfaceSpec {
  /** Output file stem. */
  name: string;
  /** Human label for the fixture's `caller_origin`. */
  callerOrigin: string;
  /** The MCP session origin main records for this caller. */
  origin: Origin;
  tier: McpTier;
  /** Agent panes are bound to their launch workspace (#12486). */
  workspaceBound: boolean;
  /** Only an agent pane is handed an approval route (#12692). */
  paneApproval: boolean;
  /**
   * The machine the session runs on, as cross-host messaging sees it.
   * `hosts`: a Shell with at least one Remote Host configured, so its help
   * session could become the hub (`HubMessagingRuntime.isReachable`).
   * `listening`: the machine listens as a Remote Host, so a remote Shell could
   * enroll its lane (`AssistantMessagingRuntime.isSendReachable`). A lane's
   * inbox is reachable on either (`AssistantMessagingRuntime.isReachable`).
   */
  machine: { hosts: boolean; listening: boolean };
  /**
   * Written as a delta against this surface (captured earlier in SURFACES, same
   * tier, same commit) rather than as a whole copy: the hub and lane surfaces
   * are the sidebar assistant's plus a handful of messaging tools.
   */
  deltaOf?: string;
  /**
   * Captured and checked like any surface, but not written: what it adds to its
   * `deltaOf` base is recorded in this other surface's delta as `core_listing`.
   */
  summaryInto?: string;
  description: string;
}

// Panes and API-key clients are captured on a machine where messaging is live
// both ways, so their fixtures show that neither is ever offered the hub or
// lane tools: they are not help sessions.
const MESSAGING_LIVE = { hosts: true, listening: true } as const;
const NO_MESSAGING = { hosts: false, listening: false } as const;

const SURFACES: SurfaceSpec[] = [
  {
    name: "pane-core",
    callerOrigin: "agent-pane",
    origin: "external",
    tier: "core",
    workspaceBound: true,
    paneApproval: true,
    machine: MESSAGING_LIVE,
    description:
      "An agent CLI (Claude Code, Codex, ...) running in a Daintree terminal pane with the project's Daintree MCP set to Core. Authenticated by the per-pane bearer Daintree writes into its --mcp-config; origin 'external', workspace-bound to its launch workspace, with an approval route.",
  },
  {
    name: "pane-full",
    callerOrigin: "agent-pane",
    origin: "external",
    tier: "full",
    workspaceBound: true,
    paneApproval: true,
    machine: MESSAGING_LIVE,
    description:
      "The same agent-pane caller with the project's Daintree MCP set to Full.",
  },
  {
    name: "help-core",
    callerOrigin: "help",
    origin: "help",
    tier: "core",
    workspaceBound: false,
    paneApproval: false,
    machine: NO_MESSAGING,
    description:
      "Daintree's own sidebar assistant (help session), Tool set = Core, on a machine with no Remote Host configured that does not listen as a host. Help-token bearer, origin 'help' (renderer-owned), pinned to the WebContents that minted it; no approval route.",
  },
  {
    name: "help-full",
    callerOrigin: "help",
    origin: "help",
    tier: "full",
    workspaceBound: false,
    paneApproval: false,
    machine: NO_MESSAGING,
    description: "The same sidebar help session with Tool set = Full.",
  },
  {
    name: "hub-core",
    deltaOf: "help-core",
    summaryInto: "hub-full",
    callerOrigin: "help-hub",
    origin: "help",
    tier: "core",
    workspaceBound: false,
    paneApproval: false,
    machine: { hosts: true, listening: false },
    description: "The hub sidebar session with Tool set = Core: the reads only.",
  },
  {
    name: "hub-full",
    deltaOf: "help-full",
    callerOrigin: "help-hub",
    origin: "help",
    tier: "full",
    workspaceBound: false,
    paneApproval: false,
    machine: { hosts: true, listening: false },
    description:
      "The sidebar assistant on a Shell with a Remote Host configured (the hub), Tool set = Full, captured at session start before enrollment. The hub's host.* tools and its own lane's inbox tools are listed from the start; the calls refuse until the session is enrolled as the hub. At Core the hub is offered only the reads (help-core's allowlisted_but_not_listed shows which).",
  },
  {
    name: "lane-core",
    deltaOf: "help-core",
    summaryInto: "lane-full",
    callerOrigin: "help-lane",
    origin: "help",
    tier: "core",
    workspaceBound: false,
    paneApproval: false,
    machine: { hosts: false, listening: true },
    description: "The lane sidebar session with Tool set = Core.",
  },
  {
    name: "lane-full",
    deltaOf: "help-full",
    callerOrigin: "help-lane",
    origin: "help",
    tier: "full",
    workspaceBound: false,
    paneApproval: false,
    machine: { hosts: false, listening: true },
    description:
      "The sidebar assistant on a machine listening as a Remote Host (a lane a remote Shell can enroll), Tool set = Full, captured at session start before enrollment. The lane tools are listed from the start, at Core as at Full; the calls refuse until a remote Shell enrolls the lane.",
  },
  {
    name: "external",
    callerOrigin: "api-key",
    origin: "external",
    tier: "external",
    workspaceBound: false,
    paneApproval: false,
    machine: MESSAGING_LIVE,
    description:
      "A third-party MCP client authenticated with the Daintree API key (or with no key configured), not bound to a workspace. Origin 'external', tier 'external'.",
  },
];

/**
 * Stand-in messaging runtimes for one machine. Only the listing questions are
 * answered, the way the real runtimes answer them for a help session bound to
 * a project slot, before any enrollment (`isAvailable` is false: nothing is
 * enrolled at a session's start). Anything else would be a call, and throws.
 */
function messagingRuntimes(machine: SurfaceSpec["machine"]): {
  lane: AssistantMessagingRuntimeHandle;
  hub: HostMessagingRuntimeHandle;
} {
  const lane = {
    isAvailable: () => false,
    isSendAvailable: () => false,
    isReachable: () => machine.listening || machine.hosts,
    isSendReachable: () => machine.listening,
    getInbox: unreachable("assistant.getInbox"),
    reply: unreachable("assistant.reply"),
    sendMessage: unreachable("assistant.sendMessage"),
  } as AssistantMessagingRuntimeHandle;
  const hub = {
    isAvailable: () => false,
    isReachable: () => machine.hosts,
    list: unreachable("host.list"),
    listAssistants: unreachable("host.listAssistants"),
    sendMessage: unreachable("host.sendMessage"),
    getTask: unreachable("host.getTask"),
    listTasks: unreachable("host.listTasks"),
    cancelTask: unreachable("host.cancelTask"),
    startAssistant: unreachable("host.startAssistant"),
  } as HostMessagingRuntimeHandle;
  return { lane, hub };
}

/** What `sessionServer` offers this session, derived independently and cross-checked below. */
function expectedMessaging(spec: SurfaceSpec) {
  const helpSession = spec.origin === "help";
  const lane = helpSession && (spec.machine.listening || spec.machine.hosts);
  return {
    assistantMessaging: lane,
    assistantSending: lane && spec.machine.listening,
    hubMessaging: helpSession && spec.machine.hosts,
  };
}

const WORKSPACE_BINDING = {
  kind: "project",
  workspaceId: "ws-capture",
  workspacePath: "/capture/workspace",
} as const;

function fakeSessionStore(sessionId: string, spec: SurfaceSpec): SessionStore {
  const grantCache = new GrantCache({ sweepIntervalMs: 0 });
  const sessionOriginMap = new Map<string, string>([[sessionId, spec.origin]]);
  const sessionWorkspaceMap = new Map<string, string>();
  if (spec.workspaceBound) sessionWorkspaceMap.set(sessionId, WORKSPACE_BINDING.workspaceId);
  const noop = () => {};
  return {
    sessions: new Map([[sessionId, {}]]),
    httpSessions: new Map(),
    sessionTierMap: new Map([[sessionId, spec.tier]]),
    sessionWebContentsMap: new Map(),
    sessionOriginMap,
    // Only a help-token session is bound to a help session id; messaging reads
    // its lane through this, so a pane or API-key session has none.
    sessionHelpIdMap: new Map<string, string>(
      spec.origin === "help" ? [[sessionId, `help-${sessionId}`]] : []
    ),
    // Derived from the origin exactly as the real store derives it.
    isRendererOwnedOrigin: (id: string) => {
      const origin = sessionOriginMap.get(id) ?? "external";
      return origin === "help" || origin === "assistant-pane";
    },
    getOrigin: (id: string) => sessionOriginMap.get(id) ?? "external",
    sessionWorkspaceMap,
    resourceSubscriptions: new Map(),
    dedupInFlight: new Map(),
    dedupResultCache: new Map(),
    grantCache,
    resourceOwnership: new ResourceOwnershipLedger(),
    drain: noop,
    getTier: () => spec.tier,
    createIdleTimer: () => setTimeout(noop, 1_000_000),
    createHttpIdleTimer: () => setTimeout(noop, 1_000_000),
    resetIdleTimer: noop,
    resetHttpIdleTimer: noop,
    clearDedupState: noop,
  } as unknown as SessionStore;
}

function unreachable(what: string): () => never {
  return () => {
    throw new Error(`capture: ${what} must not be reached while listing tools`);
  };
}

function sessionDeps(
  sessionId: string,
  spec: SurfaceSpec,
  manifest: ActionManifestEntry[]
): SessionServerDeps {
  const sessionStore = fakeSessionStore(sessionId, spec);
  const helpSessionId = () => sessionStore.sessionHelpIdMap.get(sessionId);
  const deps: SessionServerDeps = {
    sessionStore,
    // Bound for every session, as httpLifecycle binds them: whether a session
    // is offered anything is the session server's and runtime's decision.
    assistantMessaging: bindAssistantMessaging(helpSessionId),
    hostMessaging: bindHostMessaging(helpSessionId),
    // Fresh structured clones per request, as the renderer IPC would deliver.
    requestManifest: async () => structuredClone(manifest),
    getCachedManifest: () => structuredClone(manifest),
    dispatchAction: unreachable("dispatchAction"),
    handleWaitUntilIdle: unreachable("handleWaitUntilIdle"),
    handleWaitUntilIdleBatch: unreachable("handleWaitUntilIdleBatch"),
    handleSkillsSearch: unreachable("handleSkillsSearch"),
    handleSkillsLoad: unreachable("handleSkillsLoad"),
    handleProjectRunCheck: unreachable("handleProjectRunCheck"),
    handleTerminalGetStatusViewless: unreachable("handleTerminalGetStatusViewless"),
    isTerminalIdInUse: () => false,
    appendAuditRecord: () => {},
  } as unknown as SessionServerDeps;
  if (spec.workspaceBound) {
    (deps as { workspaceBinding?: unknown }).workspaceBinding = { ...WORKSPACE_BINDING };
  }
  if (spec.paneApproval) {
    deps.requestApproval = unreachable("requestApproval") as SessionServerDeps["requestApproval"];
  }
  return deps;
}

/** A minimal raw JSON-RPC client over the SDK's in-memory transport. */
async function openRawSession(server: ReturnType<typeof createSessionServer>) {
  const [clientTransport, serverTransport] = InMemoryTransport.createLinkedPair();
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

async function buildManifest(machineHasHosts: boolean): Promise<{
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
  // the added-hosts list, from the machine being captured.
  hostList.hosts = machineHasHosts ? [{ descriptor: { id: "capture-host" } }] : [];
  const manifest = service.list({}, { includeSchemas: true });
  hostList.hosts = [];
  return { manifest, contextual };
}

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

    // One manifest per machine state: a machine that has added a Remote Host
    // shows the host-gated actions its `isVisible` admits.
    const manifests = new Map([
      [false, await buildManifest(false)],
      [true, await buildManifest(true)],
    ]);

    // The app's independent wire measurement: registry -> z.toJSONSchema ->
    // toWireSchema, without ActionService or the session server.
    const measured = new Map((await measureWireSurface()).map((tool) => [tool.id, tool]));

    const summaries = new Map<string, Record<string, unknown>>();
    const captured = new Map<
      string,
      { capture: Record<string, unknown>; tools: Array<Record<string, unknown>> }
    >();
    for (const spec of SURFACES) {
      const sessionId = `capture-${spec.name}`;
      const runtimes = messagingRuntimes(spec.machine);
      const uninstallLane = setAssistantMessagingRuntime(runtimes.lane);
      const uninstallHub = setHostMessagingRuntime(runtimes.hub);
      const messaging = expectedMessaging(spec);
      const { manifest, contextual } = manifests.get(spec.machine.hosts)!;
      const byId = new Map(manifest.map((entry) => [entry.id, entry]));
      const server = createSessionServer(sessionId, sessionDeps(sessionId, spec, manifest));
      const session = await openRawSession(server);

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

      // mcp.surface, called for real where the session can reach it.
      const surfacePolicy = {
        workspaceBound: spec.workspaceBound,
        paneApproval: spec.paneApproval,
        rendererOwnedOrigin: spec.origin === "help" || spec.origin === "assistant-pane",
        ...messaging,
      };
      const direct = buildSurfaceManifest(manifest, spec.tier, appVersion, surfacePolicy);
      let surfaceHash: string | null = null;
      let surfaceHashMethod: string;
      let surfaceTiers = new Map<string, string>(direct.tools.map((t) => [t.id, t.tier]));
      const call = await session.request("tools/call", { name: "mcp.surface", arguments: {} });
      const callResult = call["result"] as
        | { isError?: boolean; structuredContent?: { hash?: string; tools?: Array<{ id: string; tier: string }> } }
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
          "structuredContent.hash of a real tools/call mcp.surface on the same session (equal to buildSurfaceManifest over the same manifest)";
      } else {
        surfaceHash = direct.hash;
        surfaceHashMethod = `buildSurfaceManifest(manifest, tier, appVersion, sessionSurface) — the function the mcp.surface handler calls with the same inputs; the real tools/call was refused in the capture harness: ${JSON.stringify(call["result"] ?? call["error"]).slice(0, 300)}`;
      }
      await session.close();
      await server.close();
      uninstallHub();
      uninstallLane();

      const rendererOwned = surfacePolicy.rendererOwnedOrigin;
      const expectedIds = [...getReachableActionIds(spec.tier, rendererOwned, spec.paneApproval)]
        .filter((id) => !isWithheldFromBoundSession(byId.get(id) ?? { danger: "safe" }, spec.tier, surfacePolicy))
        .sort();
      const listedIds = listResult.tools.map((t) => String(t["name"])).sort();
      const notListed = expectedIds.filter((id) => !listedIds.includes(id));
      const unexpected = listedIds.filter((id) => !expectedIds.includes(id));
      expect(unexpected, `${spec.name}: listed ids outside the tier allowlist`).toEqual([]);
      // Anything allowlisted but not listed must be explained by the manifest,
      // never by the harness.
      const withheld = notListed.map((id) => {
        const entry = byId.get(id);
        const reason = !entry
          ? "absent from ActionService.list({}) (isVisible false under an empty context, or restricted)"
          : entry.mcpVisibility === "hidden"
            ? "mcpVisibility: hidden"
            : entry.danger === "restricted"
              ? "danger: restricted"
              : HOST_MESSAGING_TOOL_IDS.has(id) && !messaging.hubMessaging
                ? "hub messaging not offered: no Remote Host configured on this Shell"
                : id === ASSISTANT_SEND_MESSAGE_TOOL && !messaging.assistantSending
                  ? "lane send not offered: this machine does not listen as a Remote Host"
                  : ASSISTANT_MESSAGING_TOOL_IDS.has(id) && !messaging.assistantMessaging
                    ? "lane messaging not offered: no Remote Host configured and not listening as one"
                    : "unexplained";
        return { action_id: id, reason };
      });
      expect(
        withheld.filter((w) => w.reason === "unexplained"),
        `${spec.name}: allowlisted tools missing from tools/list`
      ).toEqual([]);

      const tools = listResult.tools
        .map((wire) => {
          const id = String(wire["name"]);
          const entry = byId.get(id);
          if (!entry) throw new Error(`${spec.name}: listed ${id} has no manifest entry`);
          expect(shouldExposeTool(entry, spec.tier, surfacePolicy)).toBe(true);

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

          const reach = isTierPermitted(spec.tier, id, rendererOwned) ? "tier" : "approval";
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

      // Bound api-key sessions lose confirm-gated tools (#11789); record which.
      const boundExternalWithheld =
        spec.tier === "external"
          ? tools
              .filter((t) =>
                isWithheldFromBoundSession(
                  byId.get(String(t["action_id"]))!,
                  "external",
                  { workspaceBound: true }
                )
              )
              .map((t) => t["action_id"])
          : undefined;

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
          },
          caller: spec.description,
          // Cross-host messaging: the machine the session runs on, and what
          // the listing offered from it. Nothing is enrolled at capture time.
          messaging: {
            help_session: spec.origin === "help",
            machine_has_hosts: spec.machine.hosts,
            machine_listening_as_host: spec.machine.listening,
            enrolled: false,
            hub_tools_offered: messaging.hubMessaging,
            lane_tools_offered: messaging.assistantMessaging,
            lane_send_offered: messaging.assistantSending,
          },
          captured_at: capturedAt,
          mcp_protocol_version: initResult.protocolVersion,
          method:
            "Real ActionService.list({}, {includeSchemas:true}) over the real createActionDefinitions registry, served by the real createSessionServer (electron/services/mcp-server/sessionServer.ts) configured as this caller, and read back as raw JSON-RPC tools/list over the MCP SDK in-memory transport, JSON round-tripped. Input schemas cross-checked against the app's measureWireSurface helper.",
          surface_hash: surfaceHash,
          surface_hash_method: surfaceHashMethod,
          tool_count: tools.length,
          ...(withheld.length > 0 ? { allowlisted_but_not_listed: withheld } : {}),
          ...(boundExternalWithheld ? { withheld_when_workspace_bound: boundExternalWithheld } : {}),
          ...(spec.tier === "external" ? { omitted_args: MCP_EXTERNAL_OMITTED_ARGS } : {}),
          // Tools on this surface whose presence depends on `isVisible` against
          // the live renderer context, which this capture evaluates as `{}`.
          context_gated_tools: contextual.filter(
            (id) => listedIds.includes(id) || notListed.includes(id)
          ),
          ...(spec.paneApproval
            ? {
                notes: [
                  "An agent pane's tier is where auto-approval stops, not a ceiling (#12692): tools/list also carries every tool up to the full non-renderer-owned surface, and calling one above the tier asks the user first. The wire listing is therefore the same at core and full; tools[].daintree.reach says which run without asking ('tier') and which ask ('approval').",
                ],
              }
            : {}),
        },
        tools,
      };
      captured.set(spec.name, fixture);
      if (spec.deltaOf === undefined) {
        writeFileSync(path.join(outDir, `${spec.name}.json`), JSON.stringify(fixture, null, 2) + "\n", "utf8");
        continue;
      }
      // A delta: the base surface's tools, minus `removed`, with `changed`
      // replacing their entries, plus `added`. Rebuilding it must give back
      // exactly the listing captured above, or the run fails.
      const base = captured.get(spec.deltaOf);
      if (!base || base.capture.tier !== spec.tier) {
        throw new Error(`${spec.name}: delta base ${spec.deltaOf} missing or another tier`);
      }
      const baseById = new Map(base.tools.map((t) => [String(t["action_id"]), t]));
      if (spec.summaryInto !== undefined) {
        const ownIds = tools.map((t) => String(t["action_id"]));
        summaries.set(spec.summaryInto, {
          surface: spec.name,
          tier: spec.tier,
          tool_count: tools.length,
          surface_hash: fixture.capture.surface_hash,
          delta_of: spec.deltaOf,
          delta_of_surface_hash: base.capture.surface_hash,
          added: ownIds.filter((id) => !baseById.has(id)),
          removed: [...baseById.keys()].filter((id) => !ownIds.includes(id)),
          changed: tools
            .filter((t) => {
              const prior = baseById.get(String(t["action_id"]));
              return prior !== undefined && JSON.stringify(prior) !== JSON.stringify(t);
            })
            .map((t) => String(t["action_id"])),
        });
        continue;
      }
      const ownById = new Map(tools.map((t) => [String(t["action_id"]), t]));
      const same = (a: unknown, b: unknown) => JSON.stringify(a) === JSON.stringify(b);
      const delta = {
        capture: {
          ...fixture.capture,
          delta_of: spec.deltaOf,
          delta_of_surface_hash: base.capture.surface_hash,
          ...(summaries.has(spec.name) ? { core_listing: summaries.get(spec.name) } : {}),
        },
        added: tools.filter((t) => !baseById.has(String(t["action_id"]))),
        changed: tools.filter((t) => {
          const prior = baseById.get(String(t["action_id"]));
          return prior !== undefined && !same(prior, t);
        }),
        removed: base.tools
          .map((t) => String(t["action_id"]))
          .filter((id) => !ownById.has(id)),
      };
      const rebuilt = base.tools
        .filter((t) => !delta.removed.includes(String(t["action_id"])))
        .map((t) => delta.changed.find((c) => c["action_id"] === t["action_id"]) ?? t)
        .concat(delta.added)
        .sort((a, b) => (String(a["action_id"]) < String(b["action_id"]) ? -1 : 1));
      expect(same(rebuilt, tools), `${spec.name}: delta does not rebuild the listing`).toBe(true);
      writeFileSync(
        path.join(outDir, `${spec.name}.delta.json`),
        JSON.stringify(delta, null, 2) + "\n",
        "utf8"
      );
    }

    // Sanity: the files we wrote parse.
    for (const spec of SURFACES) {
      if (spec.summaryInto !== undefined) continue;
      const file = spec.deltaOf === undefined ? `${spec.name}.json` : `${spec.name}.delta.json`;
      JSON.parse(readFileSync(path.join(outDir, file), "utf8"));
    }
  }, 120_000);
});
