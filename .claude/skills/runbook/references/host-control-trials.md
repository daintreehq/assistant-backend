# Current Remote Host trials

The current catalog uses `host.list` and `host.call`. It has no inbox, exchange ledger or hub/lane role pair. Historical trial plans in `archive/inbox-v0.44/trials/` describe that retired system and must not be run against current Daintree.

For a host procedure, validate its outer calls against `help-core` or `help-full` and its inner calls against the matching `relayed-*` surface. A delegated assistant is an agent terminal: resolve it through `assistant.list`, start at most once through `assistant.start`, and preserve its terminal id and generation when sending. Its brief must be executable with the ordinary catalog on that machine.

Offline evidence comes from the current captured schemas, tagged examples, host eligibility and dependency tests, routing cases, and the app's host-control contract tests. These prove the protocol and selection, not live notice delivery or successful task execution.

A live trial needs an isolated Shell and host, a scratch repository, a pinned base commit and an identified caller tier. Exercise the requested path and one relevant refusal or uncertain-write recovery. Record outer and inner outcomes, call ids, terminal ids, assistant generations, submissions, notices actually received, and artifact checks performed by the operator. A Core trial cannot use the Full check runner (`project.runCheck`); `project.runCommand` runs at either tier. Never replay an ambiguous launch or send.

Only run live app trials when authorized. When the environment is unavailable, record the precise scenario and expected evidence in the work's `trials/owed.md`; a passing pane trial or an archived inbox scenario does not close that obligation.

## The live host rig (macOS)

`scripts/trial/dtrb.sh up --hosts N` (N = 1 to 3) stands that environment up on one Mac:
the Shell plus N more isolated Daintree instances in Host mode, all from the one build
`dtrb.sh build` made (app `origin/develop` by default). It is the app's own Remote Hosts
e2e wiring (`e2e/helpers/privateSshd.ts`, `e2e/remote-hosts/*.spec.ts`), held open:

- Each host gets a private user-mode `sshd` on 127.0.0.1 with its own keys and a session
  HOME under `/tmp/dtrb-h<N>-*`; its userData is `<HOME>/Library/Application Support/Daintree`,
  where a Shell's probe looks. The Shell reaches them through an `ssh` wrapper
  (`DAINTREE_SSH`) with a private config naming `devbox-1`, `devbox-2`, ... Nothing reads
  or writes `~/.ssh`, Remote Login, the user's Daintree or its Remote Hosts.
- Each host opens its own clone of the scratch repo's bare origin: Shell and hosts start
  at the same base commit. Each has its own MCP server (Shell port +51, +52, ...).
  `DTRB_HOST_WITHOUT_PROJECT=N` gives host N an unrelated repo (`notes`, no origin)
  instead, so it starts without the project: the case for preparing a project there.
- Host agents run on the user's sign-ins (`~/.claude`, `~/.codex` and the keychain are
  linked into the host HOME; `~/.claude.json` is a private copy with the host project
  already trusted). `DTRB_HOST_SIGNINS=0` leaves them signed out.
- The holder adds every host to the Shell over IPC, waits for `connected`, then starts the
  Shell's own Daintree Assistant in the sidebar lane (`help.launchAgent`, Claude by
  default, `DTRB_HELP_AGENT`) at `DTRB_HELP_TIER` (default `core`), pointed at the local
  runbook server through `DAINTREE_RUNBOOKS_MCP_URL`.

The caller tiers it gives you:

| call | caller | surface |
|---|---|---|
| `dtrb.sh mcp --as help <tool> '<json>'` | the Shell's assistant, by its bearer | `help-core` / `help-full`: `host.list`, `host.call`, ... |
| `dtrb.sh mcp <tool> '<json>'` | an API-key client on the Shell | `external` (no `host.*`) |
| `dtrb.sh mcp --on devbox-1 <tool> '<json>'` | an API-key client on that host | `external`, for operator checks there |
| `dtrb.sh trial --assistant "prompt"` | the Shell's assistant, prompted as the person would | the real caller a host runbook is written for |
| `dtrb.sh snap` | the operator | screenshot and terminal dump of every instance's own window |

`--as help` works only while the assistant's PTY runs: a provisioned bearer no terminal
took is refused `NOT_ELIGIBLE` (`localRunOf` in the app's `electron/remote/assistant/install.ts`).
`dtrb.sh reset` keeps the assistant open and resets only the Shell's repo; `down` then `up`
for fresh hosts.

**Checking a host's side.** An agent a `host.call` launched runs in the host's process but is
not mounted in the host window's grid while the Shell drives the project, and the host's
API-key `terminal.list` and `terminal.getStatus` do not list it. Check it through the relay
(`terminal.getOutput`, `terminal.readLastMessageOwned` via `host.call`), with `dtrb.sh snap`
(the host window's own terminal store names it, with its cwd), and by its transcript and the
host's clone on disk.

At app `2c6d1fb3` the relay is no better: through `host.call`, `terminal.list` comes back
empty and `terminal.getStatus` answers `"error":"Terminal not found"` (source `renderer`) for
every agent a `host.call` launched, grid or background, while `terminal.getOutput`,
`terminal.closeMany` and the `Daintree:` done-marker notice all work. A host runbook's status
loop therefore sees nothing; judge a trial on what the caller does with "not found" (it must
never relaunch), not on whether it reads a state. A `terminal.closeMany` that returned the id
left a Claude agent's process running on the host.

**Judging a rig trial.** Hosts share this Mac's disk and process table, so the caller can
read a host's transcripts, `ps` or `kill` its processes from its own shell: none of that
works against a real host, so count it as a finding, not as the procedure working.
Confirmations a host raises (`worktree.deleteOwned` there) open in that host's window,
which nobody watches: they time out (`CONFIRMATION_TIMEOUT`), and the right outcome is a
caller that reports them pending rather than one that works around them. `DTRB_HELP_AGENT=codex`
runs a Codex assistant that `trial --assistant` types into, but Codex takes its bearer
in its launch arguments rather than a lane file, so `mcp --as help` has no bearer:
use a Claude assistant for operator calls as the assistant, `--on HOST` for host checks.

**What the rig cannot show.** Every host is this Mac: `host.list` reports one machine's
load for all of them, so a placement choice between hosts (#170) can be exercised for
mechanics but not for a real capacity difference. Hosts are macOS only, reached over
loopback ssh, never a Linux box or a real network drop. Agent listings for remote hosts
come back `coverage: "unsupported"` at app `d3451414d5`. The global Assistant is a caller
since app #14465 (below). Search-driven trials need the
selector model locally (`training/fetch_selector.sh`); without it the assistant reports the
runbook server unreachable and acts unguided.

**The global Assistant as caller** (app `62205c1f` on). `DTRB_GLOBAL_AGENT=claude|codex
dtrb.sh up ...` also starts the installation's global Assistant, with that agent set as the
isolated app's own assistant agent (`agentDefaults.set({assistantAgentId})`, in the run's
user data only), through `help.ensureGlobalAssistant`, which is what its panel calls.
`dtrb.sh trial --global "prompt"` types into its PTY as the person would. It has no panel, so
the rig settles on its PTY's output activity and its screen is not captured: the evidence is
its CLI transcript (Codex: the `~/.codex/sessions` rollout whose cwd is under the run's
`userdata/help-sessions/`; Claude Code: `~/.claude/projects/`). `dtrb.sh restart` quits the
Shell and reopens it on the same user data, as a person restarting Daintree: hosts and
their agents keep running and are reconnected, and the global Assistant starts again,
resuming its conversation. Its checkpoints are under
`userdata/assistant-run-checkpoints/global/`; copy them before and after the restart to
inspect them independently. At `62205c1f` a Claude Code global Assistant cannot start: the
Claude lane's readiness probe needs `actions.getContext` in a listing that carries only the
three root tools (daintreehq/daintree#14483). Codex starts.
