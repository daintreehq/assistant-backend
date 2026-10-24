/* eslint-disable @typescript-eslint/no-explicit-any -- window globals are untyped */
// Runbook trial holder: one ISOLATED Daintree (its own user-data dir, a
// throwaway project, its own MCP port) held open while trials are fed to it.
//
// Staged into <E2E_ROOT>/app/e2e/dtrb/ by dtrb.sh, because it imports the app's
// own e2e helpers and needs its node_modules. Never run against the user's app.
//
// Protocol (all under DTRB_RUN_DIR):
//   state.json               written once the app is up (port, project id, ...)
//   mcp-api-key              the isolated instance's API key (0600)
//   inbox/<trial>.json       a request: {agentId, prompt, model?, flags?, name?, timeoutMin?,
//                            background?, adopt?: [terminalId], assistant?: true}
//   trials/<trial>/...       events.jsonl, screenshots, terminal-*.txt, done.json
//   send.json                mid-trial: type {text, terminalId?} into a pane as the user
//   adopt.json               mid-trial: hand {terminalIds} to {orchestratorPaneId}
//   hold                     a file: the running trial does not settle while it exists
//   close-all                a file: close every agent pane (between trials)
//   stop                     a file: close the app and exit
//   host<N>/mcp-api-key      with DTRB_HOSTS=N: each Remote Host's own MCP API key (0600)
//   help-session.json        the Shell's own assistant: its bearer and terminal id, 0600
//   provision-help           a file: (re)start that assistant and rewrite help-session.json
//   snap                     a file: screenshot and dump the terminals of every instance
//
// DTRB_HOSTS=1|2 also starts that many Remote Hosts (hosts.ts), seeded with the projects
// in DTRB_HOST_PROJECTS (colon-separated), and adds them to this Shell before state.json.
import { test } from "@playwright/test";
import fs from "node:fs";
import path from "node:path";
import { launchApp, closeApp, type AppContext } from "../helpers/launch";
import { openAndOnboardProject } from "../helpers/project";
import { getTerminalTextById } from "../helpers/terminal";
import { planChoice } from "../../shared/utils/terminalChoice";
import { describeHost, pairHosts, startHost, stopHosts, writeRigSsh, type RigHost } from "./hosts";

const RUN_DIR = process.env.DTRB_RUN_DIR as string;
const PROJECT = process.env.DTRB_PROJECT as string;
const MCP_PORT = Number(process.env.DTRB_MCP_PORT ?? "45600");
const TIER = (process.env.DTRB_TIER ?? "full") as "off" | "core" | "full";
const SKIP_PERMS = (process.env.DTRB_SKIP_PERMISSIONS ?? "1") === "1";
const SKIP_CONFIRMS = (process.env.DTRB_SKIP_CONFIRMATIONS ?? "1") === "1";
const APPROVE_DIALOGS = (process.env.DTRB_APPROVE_DIALOGS ?? "0") === "1";
const HOLD_MS = Number(process.env.DTRB_HOLD_HOURS ?? "8") * 3600_000;
const QUIET_MS = Number(process.env.DTRB_QUIET_MS ?? "45000");
const HOST_PROJECTS = (process.env.DTRB_HOST_PROJECTS ?? "").split(":").filter(Boolean);
const HOST_NAMES = (process.env.DTRB_HOST_NAMES ?? "devbox-1:devbox-2:devbox-3").split(":");
// The Shell's own assistant tier, which is what a host.call relays at (a ceiling).
const HELP_TIER = (process.env.DTRB_HELP_TIER ?? "core") as "core" | "full";
// The sidebar's own lane, where a person's assistant runs.
const HELP_SLOT = 0;
const HELP_AGENT = process.env.DTRB_HELP_AGENT ?? "claude";
const POLL_MS = 3_000;
const FIRST_WORK_MS = 3 * 60_000;

const TRUST_LABELS = ["Yes, I trust this folder", "Trust and continue", "Yes, proceed", "Yes, continue"];
const TRUST_DIALOG =
  /Trust this folder\?|Trust and continue|do you trust|trust the files in this folder|Yes, I trust this folder/i;
const PERMISSION_PROMPT =
  /Do you want to proceed\?|don't ask again|Would you like to run the following command|Allow Codex to|Do you want to make this edit/i;

interface TerminalInfo {
  id: string;
  launchAgentId?: string;
  detectedAgentId?: string;
  title?: string;
  cwd: string;
  agentState?: string;
  agentSessionId?: string;
  hasPty?: boolean;
  isTrashed?: boolean;
  createdAt?: number;
}

const stamp = () => new Date().toISOString();
const holderLog = (line: string) => {
  fs.appendFileSync(path.join(RUN_DIR, "holder.log"), `[${stamp()}] ${line}\n`);
  console.log(`[dtrb] ${line}`);
};

async function allTerminals(page: any): Promise<TerminalInfo[]> {
  return page.evaluate(() => (window as any).electron.terminal.getAllTerminals());
}

async function dispatch(page: any, actionId: string, args?: unknown): Promise<any> {
  return page.evaluate(
    async ([id, payload]: [string, unknown]) => {
      const run = (window as any).__daintreeDispatchAction;
      if (typeof run !== "function") throw new Error("Action dispatch hook not available");
      return run(id, payload, { source: "user", confirmed: true });
    },
    [actionId, args] as [string, unknown]
  );
}

/** The bearer of the newest lane config the app wrote for `slot` since `since`. */
function newestLaneBearer(root: string, slot: number, since: number): { token: string; sessionId: string } | null {
  let best: { token: string; sessionId: string; mtime: number } | null = null;
  const walk = (dir: string) => {
    for (const e of fs.readdirSync(dir, { withFileTypes: true })) {
      const p = path.join(dir, e.name);
      if (e.isDirectory()) walk(p);
      const m = /^slot-(\d+)-(.+)\.mcp\.json$/.exec(e.name);
      if (!e.isFile() || !m || Number(m[1]) !== slot) continue;
      const mtime = fs.statSync(p).mtimeMs;
      if (mtime < since - 1000 || (best && best.mtime >= mtime)) continue;
      const auth = JSON.parse(fs.readFileSync(p, "utf8"))?.mcpServers?.daintree?.headers?.Authorization;
      const token = /^Bearer\s+(.+)$/.exec(String(auth ?? ""))?.[1];
      if (token) best = { token, sessionId: m[2]!, mtime };
    }
  };
  if (fs.existsSync(root)) walk(root);
  // Assigned inside walk(), which TypeScript's narrowing doesn't follow.
  const found = best as { token: string; sessionId: string } | null;
  return found ? { token: found.token, sessionId: found.sessionId } : null;
}

const isAgent = (t: TerminalInfo) =>
  Boolean(t.launchAgentId ?? t.detectedAgentId) && t.launchAgentId !== "terminal";

let ctx: AppContext | undefined;
const hosts: RigHost[] = [];

test.afterAll(async () => {
  // The Shell first: it closes every ssh master it opened to the hosts on quit.
  if (ctx?.app) await closeApp(ctx.app).catch(() => {});
  holderLog("app closed");
  await stopHosts(hosts, holderLog);
});

/** Project tier and confirmations, then this instance's own MCP server on its own port. */
async function configureInstance(page: any, port: number): Promise<{ projectId: string; status: any; saved: any }> {
  const projectId: string | null = await page.evaluate(
    () => (window as any).__DAINTREE_INITIAL_PROJECT__?.id ?? null
  );
  if (!projectId) throw new Error("no project id after onboarding");
  const settings = await page.evaluate((id: string) => (window as any).electron.project.getSettings(id), projectId);
  await page.evaluate(
    ([id, s]: [string, unknown]) => (window as any).electron.project.saveSettings(id, s),
    [projectId, { ...(settings ?? {}), daintreeMcpTier: TIER, daintreeMcpSkipConfirmations: SKIP_CONFIRMS }] as [string, unknown]
  );
  await page.evaluate((p: number) => (window as any).electron.mcpServer.setPort(p), port).catch((e: unknown) => holderLog(`setPort: ${e}`));
  await page.evaluate(() => (window as any).electron.mcpServer.setEnabled(true));
  let status: any = null;
  for (let i = 0; i < 60; i++) {
    status = await page.evaluate(() => (window as any).electron.mcpServer.getStatus());
    if (status?.port) break;
    await page.waitForTimeout(500);
  }
  if (!status?.port) throw new Error("MCP server never reported a port");
  const saved = await page.evaluate((id: string) => (window as any).electron.project.getSettings(id), projectId);
  return { projectId, status, saved };
}

test("hold an isolated Daintree for runbook trials", async () => {
  test.setTimeout(HOLD_MS + 10 * 60_000);
  for (const d of ["inbox", "trials", "inbox/.taken"]) fs.mkdirSync(path.join(RUN_DIR, d), { recursive: true });

  // Launched as if from the Dock: variables from an enclosing agent session
  // (Claude Code's own markers) must not leak into every pane's agent.
  for (const key of Object.keys(process.env)) {
    if (key === "CLAUDECODE" || key.startsWith("CLAUDE_CODE_") || key === "CLAUDE_PID" || key.startsWith("CODEX_")) {
      delete process.env[key];
    }
  }

  // Remote Hosts first: the Shell's ssh wrapper names them all, and each must be
  // listening before the Shell probes it.
  if (HOST_PROJECTS.length > HOST_NAMES.length) throw new Error("more host projects than DTRB_HOST_NAMES");
  for (const [i, project] of HOST_PROJECTS.entries()) {
    hosts.push(
      await startHost({
        index: i + 1,
        name: HOST_NAMES[i]!,
        project,
        runDir: RUN_DIR,
        mcpPort: MCP_PORT + 50 + i + 1,
        log: holderLog,
        configure: async (hostPage, hostCtx) => {
          await hostCtx.window.evaluate((v: boolean) => (window as any).electron.agentSettings.setGlobal(v), SKIP_PERMS);
          const { projectId: hostProjectId, status: hostStatus } = await configureInstance(hostPage, MCP_PORT + 50 + i + 1);
          return { apiKey: String(hostStatus.apiKey ?? ""), projectId: hostProjectId };
        },
      })
    );
  }
  const rigSsh = hosts.length ? writeRigSsh(RUN_DIR, hosts) : null;

  // isolateHome: false. The app's launch helper gives every launch a throwaway HOME
  // (CLAUDE_CONFIG_DIR and CODEX_HOME inside it) unless told otherwise, which signs
  // every agent CLI out; trials run on the user's own sign-ins, as the app's own
  // assistant workflows do. A build whose helper predates the option ignores it.
  // DAINTREE_RUNBOOKS_MCP_URL points Daintree's own assistant at this checkout's server.
  const shellEnv: Record<string, string> = {};
  if (process.env.DTRB_RUNBOOKS_URL) shellEnv.DAINTREE_RUNBOOKS_MCP_URL = process.env.DTRB_RUNBOOKS_URL;
  if (rigSsh) {
    shellEnv.DAINTREE_SSH = rigSsh.wrapper;
    shellEnv.PATH = `${rigSsh.binDir}:${process.env.PATH ?? ""}`;
  }
  ctx = await launchApp({
    userDataDir: path.join(RUN_DIR, "userdata"),
    isolateHome: false,
    env: shellEnv,
    // Beside the hosts: a plain launch reaps every app this worker started.
    alongside: hosts.length > 0,
  });
  // Daintree's global "Skip permission prompts for agents" (adds each CLI's
  // dangerous args). Set BEFORE the project opens: the project's renderer caches
  // agent settings when it loads, and a launch reads that cache, so setting it
  // afterwards (pilot 1) left every pane in Claude Code's manual mode.
  await ctx.window.evaluate((v: boolean) => (window as any).electron.agentSettings.setGlobal(v), SKIP_PERMS);
  // The Shell's own assistant tier, before any help session is provisioned.
  await ctx.window.evaluate((t: string) => (window as any).electron.helpAssistant.setSettings({ tier: t }), HELP_TIER);
  const page = await openAndOnboardProject(ctx.app, ctx.window, PROJECT);
  ctx.window = page;
  holderLog(`app up, project ${PROJECT} open`);

  // Project MCP tier (what an agent pane's --mcp-config carries) and confirmations,
  // then its own MCP port, well away from the user's instance (45454).
  const { projectId, status, saved } = await configureInstance(page, MCP_PORT);
  const apiKey = String(status.apiKey ?? "");
  fs.writeFileSync(path.join(RUN_DIR, "mcp-api-key"), apiKey, { mode: 0o600 });
  if (hosts.length) await pairHosts(page, hosts, holderLog);

  // Daintree's own assistant in this view (the sidebar's lane 0): the only caller
  // host.list and host.call serve, and only while its PTY runs (a provisioned bearer
  // no terminal took is refused NOT_ELIGIBLE). Started for real with help.launchAgent,
  // so it is also a caller trials can type into; its bearer, read from the lane config
  // the app writes for it, goes to help-session.json for dtrb.sh mcp --as help.
  // DTRB_ASSISTANT=0 skips it.
  let assistantTerminalId = "";
  const startAssistant = async () => {
    const lanes = path.join(RUN_DIR, "userdata", "help-sessions");
    const since = Date.now();
    const before = new Set((await allTerminals(page)).map((t) => t.id));
    const launched = await dispatch(page, "help.launchAgent", { agentId: HELP_AGENT, slot: HELP_SLOT }).catch(
      (e: unknown) => ({ error: String(e) })
    );
    let found: { token: string; sessionId: string } | null = null;
    for (let i = 0; i < 60 && !(found && assistantTerminalId); i++) {
      await page.waitForTimeout(1000);
      found = newestLaneBearer(lanes, HELP_SLOT, since) ?? found;
      const fresh = (await allTerminals(page)).filter((t) => !before.has(t.id) && !t.isTrashed);
      assistantTerminalId = fresh.find(isAgent)?.id ?? assistantTerminalId;
    }
    if (!found || !assistantTerminalId) {
      holderLog(`assistant not started: ${JSON.stringify(launched).slice(0, 300)}`);
      return;
    }
    const file = path.join(RUN_DIR, "help-session.json");
    fs.writeFileSync(
      file + ".tmp",
      JSON.stringify({ ...found, terminalId: assistantTerminalId, agent: HELP_AGENT, tier: HELP_TIER, slot: HELP_SLOT, at: stamp() }, null, 2),
      { mode: 0o600 }
    );
    fs.renameSync(file + ".tmp", file);
    holderLog(`assistant ${HELP_AGENT} ${assistantTerminalId} running, help session ${found.sessionId} (${HELP_TIER})`);
  };
  if ((process.env.DTRB_ASSISTANT ?? "1") === "1") await startAssistant();

  const state = {
    startedAt: stamp(),
    appPid: ctx.app.process().pid,
    userDataDir: path.join(RUN_DIR, "userdata"),
    project: PROJECT,
    projectId,
    mcpPort: status.port,
    mcpUrl: `http://127.0.0.1:${status.port}/mcp`,
    tier: saved?.daintreeMcpTier,
    skipConfirmations: saved?.daintreeMcpSkipConfirmations,
    globalSkipPermissions: SKIP_PERMS,
    helpTier: HELP_TIER,
    hosts: hosts.map((h) => describeHost(h, path.join(h.dir, "mcp-api-key"))),
  };
  fs.writeFileSync(path.join(RUN_DIR, "state.json"), JSON.stringify(state, null, 2));
  await page.screenshot({ path: path.join(RUN_DIR, "ready.png") });
  for (const h of hosts) await h.ctx.window.screenshot({ path: path.join(h.dir, "ready.png") }).catch(() => {});
  holderLog(`ready: ${JSON.stringify(state)}`);

  const trusted = new Set<string>();
  const answerTrustDialogs = async (terminals: TerminalInfo[], log: (l: string) => void, on: any = page) => {
    for (const t of terminals) {
      if (t.isTrashed || trusted.has(t.id) || !isAgent(t)) continue;
      const text = (await getTerminalTextById(on, t.id).catch(() => "")).trimEnd();
      if (!TRUST_DIALOG.test(text.split("\n").slice(-30).join("\n"))) continue;
      const plan = TRUST_LABELS.map((label) => planChoice(text, label)).find((p) => p.ok);
      if (plan === undefined || !plan.ok) {
        log(`trust dialog in ${t.id} but no option to pick`);
        continue;
      }
      trusted.add(t.id);
      log(`trust dialog in ${t.launchAgentId ?? t.detectedAgentId} ${t.id}; accepting`);
      for (const key of plan.keys) {
        await on.evaluate(
          ([id, data]: [string, string]) => (window as any).electron.terminal.write(id, data),
          [t.id, key === "Enter" ? "\r" : key === "Down" ? "\x1b[B" : "\x1b[A"] as [string, string]
        );
        await on.waitForTimeout(150);
      }
    }
  };

  // Between and during trials: a fresh help bearer on request, and the trust dialog
  // of any agent a host.call launched on a host (nobody sits at a host's window).
  const serviceRig = async () => {
    const req = path.join(RUN_DIR, "provision-help");
    if (fs.existsSync(req)) {
      await startAssistant().catch((e: unknown) => holderLog(`provision-help: ${e}`));
      fs.rmSync(req, { force: true });
    }
    if (assistantTerminalId) {
      const t = (await allTerminals(page)).filter((x) => x.id === assistantTerminalId);
      await answerTrustDialogs(t, (l) => holderLog(`assistant: ${l}`));
    }
    for (const h of hosts) {
      const terminals = await allTerminals(h.ctx.window).catch(() => [] as TerminalInfo[]);
      await answerTrustDialogs(terminals, (l) => holderLog(`${h.name}: ${l}`), h.ctx.window);
    }
    // dtrb.sh snap: what each instance's own window holds, read in that window.
    const snapReq = path.join(RUN_DIR, "snap");
    if (fs.existsSync(snapReq)) {
      const out = path.join(RUN_DIR, "snaps", stamp().replace(/[:.]/g, "-"));
      fs.mkdirSync(out, { recursive: true });
      for (const [name, on] of [["shell", page], ...hosts.map((h) => [h.name, h.ctx.window])] as Array<[string, any]>) {
        await on.screenshot({ path: path.join(out, `${name}.png`) }).catch(() => {});
        const terminals = await allTerminals(on).catch((e: unknown) => [{ error: String(e) }] as any);
        for (const t of terminals) {
          if (t?.id) t.tail = (await getTerminalTextById(on, t.id).catch(() => "")).trimEnd().split("\n").slice(-25).join("\n");
        }
        fs.writeFileSync(path.join(out, `${name}-terminals.json`), JSON.stringify(terminals, null, 2));
      }
      fs.rmSync(snapReq, { force: true });
      holderLog(`snap: ${out}`);
    }
  };

  // Agents launched with {background:true} keep running across trials, are
  // tracked by every later trial, and can be handed to a trial pane ({adopt}).
  const bgIds = new Set<string>();
  const deadline = Date.now() + HOLD_MS;
  while (Date.now() < deadline) {
    if (fs.existsSync(path.join(RUN_DIR, "stop"))) break;

    if (fs.existsSync(path.join(RUN_DIR, "close-all"))) {
      // The Shell's own assistant stays: it is the rig's caller, not a trial pane.
      const agents = (await allTerminals(page)).filter((t) => !t.isTrashed && isAgent(t) && t.id !== assistantTerminalId);
      for (const t of agents) {
        // Close through the renderer's own action, so the panel leaves the layout
        // and every listing (a main-side kill or trash leaves an idle row behind).
        const closed = await dispatch(page, "terminal.close", { terminalId: t.id }).catch((e: unknown) => ({ error: String(e) }));
        if (!closed?.ok) {
          holderLog(`terminal.close ${t.id}: ${JSON.stringify(closed).slice(0, 200)}; killing`);
          await page
            .evaluate((id: string) => (window as any).electron.terminal.kill(id), t.id)
            .catch((e: unknown) => holderLog(`terminal.kill ${t.id} failed: ${e}`));
        }
      }
      // A close can be deferred: wait (bounded) until none of them is still working, so
      // a reset never rewrites the repo under an agent that is mid-edit.
      const ids = new Set(agents.map((t) => t.id));
      for (let i = 0; i < 20; i++) {
        const live = (await allTerminals(page)).filter((t) => ids.has(t.id) && !t.isTrashed && t.agentState === "working");
        if (!live.length) break;
        await page.waitForTimeout(1000);
      }
      bgIds.clear();
      holderLog(`close-all: closed ${agents.length} agent panes`);
      // Removed only now: dtrb.sh reset waits for this before touching the repo.
      fs.rmSync(path.join(RUN_DIR, "close-all"), { force: true });
    }

    const next = fs.readdirSync(path.join(RUN_DIR, "inbox")).filter((f) => f.endsWith(".json")).sort()[0];
    if (!next) {
      await serviceRig();
      await page.waitForTimeout(POLL_MS);
      continue;
    }
    const trialId = next.replace(/\.json$/, "");
    const reqPath = path.join(RUN_DIR, "inbox", next);
    const req = JSON.parse(fs.readFileSync(reqPath, "utf8"));
    fs.renameSync(reqPath, path.join(RUN_DIR, "inbox", ".taken", next));
    const dir = path.join(RUN_DIR, "trials", trialId);
    fs.mkdirSync(dir, { recursive: true });
    const events = path.join(dir, "events.jsonl");
    const log = (line: string, extra: Record<string, unknown> = {}) => {
      fs.appendFileSync(events, JSON.stringify({ at: stamp(), msg: line, ...extra }) + "\n");
      holderLog(`${trialId}: ${line}`);
    };

    const before = new Set((await allTerminals(page)).map((t) => t.id));
    const startedAt = Date.now();
    const args: Record<string, unknown> = {
      agentId: req.agentId ?? "claude",
      prompt: req.prompt,
      focusPolicy: "take",
    };
    // No pane name by default: a title like "Trial ..." is context the agent reads.
    if (req.name) args.name = req.name;
    if (req.model) args.model = req.model;
    if (Array.isArray(req.flags) && req.flags.length) args.agentLaunchFlags = req.flags;
    if (req.env) args.env = req.env;
    let launchResult: any;
    if (req.assistant) {
      // The caller is the Shell's own Daintree Assistant (help-* surface, host.*): the
      // prompt is typed into it as the person would, and it is the trial's pane.
      if (!assistantTerminalId) {
        launchResult = { error: "no assistant running (DTRB_ASSISTANT=0, or it failed to start)" };
      } else {
        await page.evaluate(
          ([id, text]: [string, string]) => (window as any).electron.terminal.write(id, text),
          [assistantTerminalId, String(req.prompt)] as [string, string]
        );
        await page.waitForTimeout(400);
        await page.evaluate((id: string) => (window as any).electron.terminal.write(id, "\r"), assistantTerminalId);
        bgIds.add(assistantTerminalId);
        launchResult = { result: { terminalId: assistantTerminalId } };
      }
    } else {
      try {
        launchResult = await dispatch(page, "agent.launch", args);
      } catch (e) {
        launchResult = { error: String(e) };
      }
    }
    log(req.assistant ? "typed into the assistant" : "launched", { args: { ...args, prompt: `${String(req.prompt).slice(0, 200)}…` }, launchResult });

    // The trial's own pane: the id the launch returned, else the first new agent pane.
    let trialTerminalId: string =
      launchResult?.result?.terminalId ?? launchResult?.result?.id ?? launchResult?.terminalId ?? "";

    if (req.background) {
      // Launch only: answer its trust dialog, record the id, and go back to the inbox.
      const bgStart = Date.now();
      while (Date.now() - bgStart < 30_000) {
        await page.waitForTimeout(POLL_MS);
        const fresh = (await allTerminals(page)).filter((t) => !t.isTrashed && !before.has(t.id));
        if (!trialTerminalId) trialTerminalId = fresh.find(isAgent)?.id ?? "";
        await answerTrustDialogs(fresh, log);
      }
      if (trialTerminalId) bgIds.add(trialTerminalId);
      const t = (await allTerminals(page)).find((x) => x.id === trialTerminalId);
      const bgOutcome = trialTerminalId ? "background" : "failed";
      fs.writeFileSync(path.join(dir, "done.json"), JSON.stringify({ trialId, outcome: bgOutcome, startedAt: new Date(startedAt).toISOString(), endedAt: stamp(), seconds: 0, trialTerminalId, request: req, terminals: t ? [{ id: t.id, agent: t.launchAgentId ?? t.detectedAgentId, title: t.title, cwd: t.cwd, agentState: t.agentState, agentSessionId: t.agentSessionId }] : [], everSeen: [trialTerminalId] }, null, 2));
      log(`background: ${trialTerminalId}`);
      continue;
    }

    // Terminals a hand-over did not reach: recorded in done.json, so a trial that
    // depended on one is read as a harness failure rather than a runbook result.
    const adoptFailed: string[] = [];
    if (Array.isArray(req.adopt) && req.adopt.length) {
      // The user's "hand over" gesture: give each named terminal to this trial's pane.
      for (let i = 0; i < 20 && !trialTerminalId; i++) {
        await page.waitForTimeout(1000);
        const fresh = (await allTerminals(page)).filter((t) => !t.isTrashed && !before.has(t.id));
        trialTerminalId = fresh.find(isAgent)?.id ?? "";
      }
      for (const target of req.adopt as string[]) {
        let res: any = null;
        for (let i = 0; i < 30; i++) {
          res = await page
            .evaluate(
              ([terminalId, orchestratorPaneId]: [string, string]) =>
                (window as any).electron.mcpServer.adoptTerminal({ terminalId, orchestratorPaneId }),
              [target, trialTerminalId] as [string, string]
            )
            .catch((e: unknown) => ({ error: String(e) }));
          if (res?.status === "handed-over") break;
          await page.waitForTimeout(1000);
        }
        log(`adopt ${target} -> ${trialTerminalId}`, { result: res });
        // An adopted terminal is part of this trial: tracked for busy detection and captured.
        if (res?.status === "handed-over") bgIds.add(target);
        else adoptFailed.push(target);
      }
    }
    const timeoutMs = Number(req.timeoutMin ?? 20) * 60_000;
    let lastChange = Date.now();
    let fingerprint = "";
    let sawWork = false;
    let shot = 1;
    let outcome = "timeout";
    const seen = new Map<string, TerminalInfo>();
    const promptsSeen = new Set<string>();
    while (Date.now() - startedAt < timeoutMs) {
      await page.waitForTimeout(POLL_MS);
      await serviceRig();
      if (fs.existsSync(path.join(RUN_DIR, "stop"))) {
        outcome = "stopped";
        break;
      }
      // A user follow-up typed into a pane mid-trial (send.json: {text, terminalId?}).
      const sendPath = path.join(RUN_DIR, "send.json");
      if (fs.existsSync(sendPath)) {
        const sreq = JSON.parse(fs.readFileSync(sendPath, "utf8"));
        fs.rmSync(sendPath, { force: true });
        const target = String(sreq.terminalId ?? trialTerminalId);
        await page.evaluate(
          ([id, data]: [string, string]) => (window as any).electron.terminal.write(id, data),
          [target, String(sreq.text)] as [string, string]
        );
        await page.waitForTimeout(800);
        await page.evaluate(([id]: [string]) => (window as any).electron.terminal.write(id, "\r"), [target] as [string]);
        log(`user follow-up typed into ${target}`, { text: String(sreq.text).slice(0, 300) });
        lastChange = Date.now();
      }
      // The user's mid-trial "Hand to orchestrator" gesture (adopt.json: {terminalIds, orchestratorPaneId}).
      const adoptPath = path.join(RUN_DIR, "adopt.json");
      if (fs.existsSync(adoptPath)) {
        const areq = JSON.parse(fs.readFileSync(adoptPath, "utf8"));
        fs.rmSync(adoptPath, { force: true });
        for (const target of areq.terminalIds as string[]) {
          const res = await page
            .evaluate(
              ([terminalId, orchestratorPaneId]: [string, string]) =>
                (window as any).electron.mcpServer.adoptTerminal({ terminalId, orchestratorPaneId }),
              [target, String(areq.orchestratorPaneId)] as [string, string]
            )
            .catch((e: unknown) => ({ error: String(e) }));
          log(`mid-trial adopt ${target} -> ${areq.orchestratorPaneId}`, { result: res });
          if (res?.status === "handed-over") bgIds.add(target);
          else adoptFailed.push(target);
        }
        lastChange = Date.now();
      }
      // A "hold" file keeps the trial from settling while the user acts by hand.
      if (fs.existsSync(path.join(RUN_DIR, "hold"))) lastChange = Date.now();
      const terminals = (await allTerminals(page)).filter((t) => !t.isTrashed);
      const fresh = terminals.filter((t) => !before.has(t.id) || bgIds.has(t.id));
      for (const t of fresh) seen.set(t.id, t);
      if (!trialTerminalId) trialTerminalId = fresh.find(isAgent)?.id ?? "";
      await answerTrustDialogs(fresh, log);
      if (APPROVE_DIALOGS) {
        const dialog = page.locator('[role="dialog"], [role="alertdialog"]').last();
        if (await dialog.isVisible().catch(() => false)) {
          log(`approving dialog: ${(await dialog.innerText()).replace(/\s+/g, " ").slice(0, 200)}`);
          await dialog.getByRole("button").last().click();
        }
      }
      for (const t of fresh.filter(isAgent)) {
        const recent = (await getTerminalTextById(page, t.id).catch(() => "")).trimEnd().split("\n").slice(-15).join("\n");
        if (PERMISSION_PROMPT.test(recent) && !TRUST_DIALOG.test(recent) && !promptsSeen.has(t.id + recent)) {
          promptsSeen.add(t.id + recent);
          log(`permission prompt in ${t.id}`, { screen: recent });
          await page.screenshot({ path: path.join(dir, `permission-${String(shot++).padStart(2, "0")}.png`) });
        }
      }
      const fleet = fresh
        .map((t) => `${t.id.slice(-8)} ${t.launchAgentId ?? t.detectedAgentId ?? "shell"} ${t.agentState ?? "-"}`)
        .join(" | ");
      const tail = trialTerminalId
        ? (await getTerminalTextById(page, trialTerminalId).catch(() => "")).trimEnd().split("\n").slice(-3).join(" / ")
        : "";
      const busy = fresh.some((t) => t.agentState === "working");
      if (busy) sawWork = true;
      if (fleet + tail !== fingerprint) {
        fingerprint = fleet + tail;
        lastChange = Date.now();
        log("fleet", { fleet, tail: tail.slice(0, 300) });
        if (shot < 60) await page.screenshot({ path: path.join(dir, `${String(shot++).padStart(2, "0")}-progress.png`) });
      }
      const started = sawWork || Date.now() - startedAt > FIRST_WORK_MS;
      if (started && !busy && Date.now() - lastChange > QUIET_MS) {
        outcome = "settled";
        break;
      }
    }

    const terminals = await allTerminals(page);
    const mine = terminals.filter((t) => !before.has(t.id) || bgIds.has(t.id));
    for (const t of mine) {
      const text = await getTerminalTextById(page, t.id).catch(() => "");
      fs.writeFileSync(path.join(dir, `terminal-${t.launchAgentId ?? t.detectedAgentId ?? "shell"}-${t.id.slice(-8)}.txt`), text);
    }
    await page.screenshot({ path: path.join(dir, "99-final.png") });
    const done = {
      trialId,
      outcome,
      startedAt: new Date(startedAt).toISOString(),
      endedAt: stamp(),
      // Wall time minus the quiet window that proved the turn was over.
      seconds: Math.round((Date.now() - startedAt - (outcome === "settled" ? QUIET_MS : 0)) / 1000),
      trialTerminalId,
      adoptFailed,
      request: req,
      terminals: mine.map((t) => ({
        id: t.id,
        agent: t.launchAgentId ?? t.detectedAgentId,
        title: t.title,
        cwd: t.cwd,
        agentState: t.agentState,
        agentSessionId: t.agentSessionId,
        isTrashed: t.isTrashed,
        hasPty: t.hasPty,
      })),
      everSeen: [...seen.keys()],
    };
    fs.writeFileSync(path.join(dir, "done.json"), JSON.stringify(done, null, 2));
    log(`done: ${outcome} after ${done.seconds}s`);
  }
});
