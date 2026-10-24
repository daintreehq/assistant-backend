/* eslint-disable @typescript-eslint/no-explicit-any -- window globals are untyped */
// Remote Hosts for runbook trials: N more ISOLATED Daintree instances in Host mode,
// each behind its own private user-mode sshd on 127.0.0.1, joined to the trial's
// Shell over the system ssh. The same wiring as the app's own e2e/remote-hosts specs
// (helpers/privateSshd.ts): nothing touches ~/.ssh, the user's ssh config or Remote
// Login, and the user's own Daintree and its Remote Hosts are never read or written.
//
// Staged beside hold.spec.ts by dtrb.sh. Each host's HOME is its sshd's session HOME
// (that is where a Shell's probe looks for host.json and host.sock), so its userData is
// <HOME>/Library/Application Support/Daintree under a short /tmp root.
import fs from "node:fs";
import os from "node:os";
import path from "node:path";
import { launchApp, closeApp, type AppContext } from "../helpers/launch";
import { openAndOnboardProject } from "../helpers/project";
import { makeShortTempRoot, startPrivateSshd, SSH_ALIAS, SYSTEM_SSH, type PrivateSshd } from "../helpers/privateSshd";

export interface RigHost {
  index: number;
  /** The ssh alias and the host's display name on the Shell. */
  name: string;
  root: string;
  sshd: PrivateSshd;
  userData: string;
  project: string;
  ctx: AppContext;
  dir: string;
  mcpPort: number;
  /** Set once the Shell has added it. */
  hostId?: string;
  /** The project's id as the Shell's host.list names it. */
  remoteProjectId?: string;
}

/**
 * The user's agent sign-ins, linked into a host's HOME so agents launched there run
 * as the trial panes do (the Shell already runs with the real HOME). The keychain is
 * where Claude Code keeps its login on macOS; `security` finds it through HOME.
 * DTRB_HOST_SIGNINS=0 leaves the host's agents signed out.
 */
const SIGNINS = [".claude", ".codex", "Library/Keychains"];

function linkSignins(home: string, project: string): void {
  if ((process.env.DTRB_HOST_SIGNINS ?? "1") !== "1") return;
  for (const rel of SIGNINS) {
    const real = path.join(os.homedir(), rel);
    if (!fs.existsSync(real)) continue;
    fs.mkdirSync(path.dirname(path.join(home, rel)), { recursive: true });
    fs.symlinkSync(real, path.join(home, rel));
  }
  // ~/.claude.json is copied, not linked, with the host's project already trusted: a
  // host.call launch shows its trust prompt in a panel no host window mounts while a
  // Shell drives it, so nobody could answer it, and the real file is never written.
  const config = path.join(os.homedir(), ".claude.json");
  if (!fs.existsSync(config)) return;
  const data = JSON.parse(fs.readFileSync(config, "utf8"));
  data.projects ??= {};
  for (const dir of new Set([project, fs.realpathSync(project)])) {
    data.projects[dir] = { ...(data.projects[dir] ?? {}), hasTrustDialogAccepted: true };
  }
  fs.writeFileSync(path.join(home, ".claude.json"), JSON.stringify(data, null, 2), { mode: 0o600 });
}

export async function startHost(opts: {
  index: number;
  name: string;
  project: string;
  runDir: string;
  mcpPort: number;
  configure: (page: any, ctx: AppContext) => Promise<{ apiKey: string; projectId: string }>;
  log: (line: string) => void;
}): Promise<RigHost> {
  const root = await makeShortTempRoot(`dtrb-h${opts.index}-`);
  const sshd = await startPrivateSshd(root);
  linkSignins(sshd.home, opts.project);
  const userData = path.join(sshd.home, "Library", "Application Support", "Daintree");
  fs.mkdirSync(userData, { recursive: true, mode: 0o700 });
  const dir = path.join(opts.runDir, `host${opts.index}`);
  fs.mkdirSync(dir, { recursive: true });
  // Recorded before launch, so dtrb.sh down/purge can find a host that never came up.
  fs.writeFileSync(path.join(dir, "host.json"), JSON.stringify({ root, userData, sshdPid: sshd.pid }, null, 2));
  const ctx = await launchApp({ userDataDir: userData, env: hostEnv(sshd.home), alongside: true });
  ctx.window = await openAndOnboardProject(ctx.app, ctx.window, opts.project);
  const { apiKey } = await opts.configure(ctx.window, ctx);
  fs.writeFileSync(path.join(dir, "mcp-api-key"), apiKey, { mode: 0o600 });
  // The Settings switch's own path. No start at login: that would write a login item.
  await ctx.window.evaluate(() => (window as any).electron.hostMode.setEnabled({ enabled: true, startAtLogin: false }));
  let listening = false;
  for (let i = 0; i < 60 && !listening; i++) {
    listening = Boolean((await ctx.window.evaluate(() => (window as any).electron.hostMode.getStatus()))?.listening);
    if (!listening) await ctx.window.waitForTimeout(500);
  }
  if (!listening) throw new Error(`host ${opts.name}: Host mode never started listening`);
  opts.log(`host ${opts.name} listening (HOME ${sshd.home}, sshd :${sshd.port})`);
  return { index: opts.index, name: opts.name, root, sshd, userData, project: opts.project, ctx, dir, mcpPort: opts.mcpPort };
}

function hostEnv(home: string): Record<string, string> {
  const env: Record<string, string> = { HOME: home };
  if (process.env.DTRB_RUNBOOKS_URL) env.DAINTREE_RUNBOOKS_MCP_URL = process.env.DTRB_RUNBOOKS_URL;
  return env;
}

/**
 * One ssh_config naming every host by its alias, and an `ssh` that is the system ssh
 * with it (the Shell runs it as DAINTREE_SSH). The app's helper writes one config per
 * sshd under the alias `daintree-e2e`; each is renamed here.
 */
export function writeRigSsh(runDir: string, hosts: RigHost[]): { wrapper: string; binDir: string } {
  const config = path.join(runDir, "ssh_config");
  const text = hosts
    .map((h) => fs.readFileSync(h.sshd.configPath, "utf8").replace(`Host ${SSH_ALIAS}\n`, `Host ${h.name}\n`))
    .join("\n");
  fs.writeFileSync(config, text);
  const binDir = path.join(runDir, "bin");
  fs.mkdirSync(binDir, { recursive: true });
  const wrapper = path.join(binDir, "ssh");
  const q = (v: string) => `'${v.replace(/'/g, `'\\''`)}'`;
  fs.writeFileSync(wrapper, `#!/bin/sh\nexec ${SYSTEM_SSH} -F ${q(config)} "$@"\n`, { mode: 0o755 });
  return { wrapper, binDir };
}

/** Add every host to the Shell over IPC (as the windowless spec does) and wait for each to connect. */
export async function pairHosts(shell: any, hosts: RigHost[], log: (line: string) => void): Promise<void> {
  for (const h of hosts) {
    const added = await shell.evaluate(
      ({ name }: { name: string }) =>
        (window as any).electron.remoteHosts.add({ name, connection: { kind: "ssh", target: name } }),
      { name: h.name }
    );
    h.hostId = added?.descriptor?.id ?? added?.id;
    if (!h.hostId) throw new Error(`host ${h.name}: add returned ${JSON.stringify(added)}`);
    await shell.evaluate((hostId: string) => (window as any).electron.remoteHosts.connect({ hostId }), h.hostId);
    let row: any = null;
    for (let i = 0; i < 180; i++) {
      const list = await shell.evaluate(() => (window as any).electron.remoteHosts.list());
      row = (list as any[]).find((r) => r.descriptor?.id === h.hostId);
      if (row?.connection?.status === "connected") break;
      await shell.waitForTimeout(500);
    }
    if (row?.connection?.status !== "connected") {
      throw new Error(`host ${h.name} never connected: ${JSON.stringify(row?.connection)}`);
    }
    const projects = (await shell.evaluate(
      (hostId: string) => (window as any).electron.remoteHosts.listHostProjects({ hostId }),
      h.hostId
    )) as Array<{ id: string; path?: string; name: string }>;
    h.remoteProjectId = (projects.find((p) => p.path === h.project) ?? projects[0])?.id;
    log(`host ${h.name} connected as ${h.hostId} (assistant control ${row.descriptor.assistantControlEnabled})`);
  }
}

export function describeHost(h: RigHost, apiKeyFile: string): Record<string, unknown> {
  return {
    name: h.name,
    hostId: h.hostId ?? null,
    projectId: h.remoteProjectId ?? null,
    project: h.project,
    home: h.sshd.home,
    userDataDir: h.userData,
    appPid: h.ctx.app.process().pid,
    sshdPid: h.sshd.pid,
    sshdPort: h.sshd.port,
    mcpUrl: `http://127.0.0.1:${h.mcpPort}/mcp`,
    mcpApiKeyFile: apiKeyFile,
  };
}

export async function stopHosts(hosts: RigHost[], log: (line: string) => void): Promise<void> {
  for (const h of hosts) {
    await closeApp(h.ctx.app).catch((e: unknown) => log(`closing host ${h.name}: ${e}`));
    await h.sshd.stop().catch(() => {});
    log(`host ${h.name} closed`);
  }
}
