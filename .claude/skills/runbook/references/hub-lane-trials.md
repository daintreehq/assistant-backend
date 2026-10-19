# Hub and lane trials: a Shell's hub and a Remote Host's lanes

**The inbox model only.** Hubs, lanes and the inbox are how v0.44 drove Remote Hosts,
and this protocol and `owed-trials.yaml` describe trials of the frozen inbox catalog
(`runbooks/inbox/`), read against its capture in `tests/fixtures/daintree_mcp/inbox/`.
Later builds control a host directly (`host.call`) and delegate to its assistant as to
any agent; the control catalog's hosts runbooks are followed by the Shell's assistant
alone and owe trials under a protocol of their own, not yet written: a Shell assistant
driving a staged host through `host.call`, its notices relayed back, and a delegation
to that host's assistant read back with `terminal.readLastMessageOwned`.

`trials.md` stages one caller, an agent pane. Hosts runbooks are followed by two others:
the **hub** (the sidebar assistant in a Shell's own project) and a **lane** (a host
project's sidebar assistant, enrolled for that Shell). This file is how those trials
are staged, run, recorded and owed. Every app fact below is cited from the app at the
revision the hosts runbooks were grounded on, `0436363469` (`$APP_SHA`); re-check the
citations before relying on them at a newer pin.

**Status, plainly.** No hub or lane trial has run through this protocol yet. The
staged path below is built on the app's own two-instance test (transport, Host mode,
adding a host); its messaging leg (a hub and a lane enrolled across two instances on
one Mac) is not covered by any app test and has not been exercised. Its first run is a
stage check, before any paid prompt. Every owed run is an entry in
`owed-trials.yaml`; `dtrb.sh plan check` validates them offline.

## The roles

| role | what it is | where its CLI runs, and whose sign-in and environment | surface |
|---|---|---|---|
| hub | the Shell's sidebar assistant in a Shell project, with "Accept messages from other machines" on (a local enrollment: `AssistantMessagingPolicy.enroll`/`getHub`) | the Shell | `hub-full` |
| lane | a host project's sidebar assistant, opened in a Shell window showing that host's project and enrolled from there (or started by the hub with `host.startAssistant`) | **the host**: `help:provision-session` is a host channel (`electron/ipc/channelLocality.ts`), so the lane's CLI, its sign-in and `DAINTREE_RUNBOOKS_MCP_URL` come from the host's process | `lane-full` |
| worker | an agent pane a lane launches in a host worktree | the host | (the lane's tools) |

- Tools are listed from session start on reachability alone, and Codex never re-reads
  them (`tests/fixtures/daintree_mcp/README.md`). So **start each sidebar session only
  after its machine is reachable and its Tool set is chosen**: the hub after the host
  shows Connected, a lane after its host is listening; restart the conversation after
  changing either.
- One local enrollment is the hub; a second refuses (`hub-taken`), and every host call
  from any other session is `NOT_ELIGIBLE` (`HubMessagingRuntime.settle`). Enrollment
  lives in memory and ends with the run: **a Shell restart or a new hub conversation
  ends the hub's enrollment**, so turn the switch on again in the new session before
  the next prompt. A lane's enrollment waits for the same Shell and comes back with
  it (`docs/architecture/remote-hosts.md`, Assistant exchanges).
- Lanes are enrolled from the Shell's window on the host. A machine's own windows
  show the switch only once that machine has Remote Hosts of its own, and there it
  makes the assistant that machine's hub (`isAvailable` in
  `electron/ipc/handlers/assistantMessaging.ts`); so a staged host's own window has no
  messaging controls, and the pause is the Shell's.
- All assistants in one project share one CLI. A hub and its lanes may differ.

## Isolation, on either path

- Never attach automation to the person's working Daintree, its user data, its MCP port
  (45454 by default) or its projects. Staged instances get their own `--user-data-dir`
  and MCP port; real machines are driven by the person's own hands.
- A scratch project and a scratch bare remote per run. Hosts share remotes, never
  working trees: each machine clones the remote, and the hub verifies against it.
- A run id in every branch the trial creates (`run/<run>/...`), so teardown can name
  exactly what the run owns.
- Per-role transcripts: help sessions run in `<userData>/help-sessions/<hash>`, so a
  role's Claude Code transcripts are under `~/.claude/projects/<that path encoded>-*`,
  and a Codex rollout's `session_meta` cwd names its role. `dtrb.sh plan layout`
  prints them. Read each with `summarise.py --transcript <file>`.
- Teardown removes only what the run owns: processes whose argv carries the stage
  root, the stage root, the run's branches and worktrees. Never kill by process name.
  Transcripts stay.

## Staged path: a Shell and one or two hosts on one Mac

macOS only: a Linux host's socket is per user under `/run/user/<uid>`, shared by every
instance (`electron/remote/host/hostSocketPath.ts`). The shape is the app's own
`e2e/remote-hosts/remote-host-ssh.spec.ts` with `e2e/helpers/privateSshd.ts`: a
private user-mode `sshd` on 127.0.0.1 whose sessions get `SetEnv HOME=<stage home>`, a
Host instance whose user data sits exactly where a Shell probing that HOME looks, and a
Shell instance whose `ssh` is a wrapper adding `-F <private ssh_config>`.

```bash
"$T/dtrb.sh" plan layout <run> --hosts 1    # paths, ports, env, links, transcript globs; checked, nothing created
mkdir /tmp/dsw-<run>                        # the root: new, never an existing folder
"$T/dtrb.sh" build "$APP_SHA"               # as in trials.md; the app build both instances run
"$T/dtrb.sh" runbooks serve                 # background task; its URL goes to both instances
```

**Launching an instance** (not yet exercised). From the kit's build (`dtrb.sh build`,
at `~/.cache/daintree-runbook-e2e/app`, here `$APP`): `env <vars>
"$APP/node_modules/.bin/electron" --user-data-dir=<dir> "$APP"`, which is what the
app's launch helper hands Electron less its test-only flags (`e2e/helpers/launch.ts`),
each instance in its own terminal and without an enclosing agent session's
`CLAUDECODE` and `CLAUDE_CODE_*` variables. No two-instance holder is written yet; the
first staged run writes one, as its own HARNESS commit.
Before any assistant opens in an instance, set Settings → MCP server → Server port to
the layout's port: the first sidebar session switches the server on, on 45454 or the
next free port when none is set (`ensureMcpServerReady` in `HelpSessionService.ts`,
`listenWithRetry` in `electron/services/mcp-server/httpLifecycle.ts`).

1. **sshd per host** (`privateSshd.ts`): ed25519 host and user keys under the stage
   root, `ListenAddress 127.0.0.1`, a free port, `AuthorizedKeysFile` in the stage,
   `UsePAM no`, `PasswordAuthentication no`, `PermitUserRC no`,
   `AllowStreamLocalForwarding yes`, `SetEnv HOME=<hN home>`; a client `ssh_config`
   with one alias, its own `UserKnownHostsFile`, `BatchMode yes`; and the wrapper
   `exec /usr/bin/ssh -F <ssh_config> "$@"`. Nothing touches `~/.ssh` or Remote Login.
   Never point the Shell at the system sshd or at `localhost`: that session's HOME is
   the real one, and the probe would find the person's own Daintree if it is a host.
2. **Host instance.** `--user-data-dir=<hN home>/Library/Application Support/Daintree`
   (`remoteHostSocketLocation`, `MAC_APP_DIR_NAME`; the socket path must fit 103 bytes,
   which `plan layout` checks) and **`HOME=<hN home>`**, as the app's spec runs its
   Host. Never the real HOME: switching Host mode on with start at login off *removes*
   the build's start-at-login file under HOME (`HostModeService.setEnabled` calls
   `removeStartAtLogin`; the LaunchAgent path is `<HOME>/Library/LaunchAgents/<label>.plist`
   in `startAtLogin.ts`), which could be the person's own development host's. Lanes run
   on the host's sign-ins ("Each runs on <host>'s own agent sign-in",
   `src/components/Settings/Hosts/HostDetail.tsx`), so give the staged HOME the
   person's: links from `<hN home>/.claude`, `.claude.json` and `.codex` to the real
   ones (the app's own sign-in check reads them under HOME, `checkAuth` in
   `electron/services/CliAvailabilityService.ts`), a `.gitconfig` with a scratch identity and signing off, and a
   `.zprofile` that restores the launching terminal's PATH (`createIsolatedHome` and
   `pathRestoreProfile` in `e2e/helpers/launch.ts` write the same kind of home);
   `plan layout` prints the env and links. Whether Claude Code reads its sign-in
   through those links on a staged HOME is unverified: the stage check settles it, and
   a CLI that reads as signed out stops the staged run (HARNESS), never a fresh
   sign-in inside the stage. Also `DAINTREE_RUNBOOKS_MCP_URL` set to the served URL
   (`resolveRunbooksMcpUrl`, `electron/services/helpSessionRunbooks.ts`). Open its
   project. Host mode on **with start at login off**: `hostMode.setEnabled({enabled:
   true, startAtLogin: false})`, or the "Allow this machine to be a host" switch with
   "Start at login" left off; until `getStatus().listening`. Never
   `--enable-host-mode`: it turns start at login on (`enableFromSetup`).
3. **Shell instance.** Its own user data, `DAINTREE_SSH=<wrapper>`, the same runbook
   URL. Open the hub's project. Settings → Hosts → **Add host** with the
   alias; **Check host** must read SSH Works, Same build and Host mode Listening;
   **Continue**, then **Skip** the switch-on step (it would start the installed app at
   login on this Mac); **Add host**; the row reads Connected. On the host's page turn
   on "Allow assistant messaging with this host", and "Let the assistant start
   assistants on this host" only when the trial needs starts (both are off by default
   and reset when the connection changes).
4. **Assistant settings** (Settings → Daintree Assistant; host-owned, so set per
   instance, and the lane's are the host's): **Tool set** Full (sends, cancels and
   starts are Full only), **Model** as the cost rules below say, and **Skip permission
   prompts** on where nobody will answer them. Leave **Daintree confirmations** as the
   trial needs: an unattended confirmation is meant to time out
   (`CONFIRMATION_TIMEOUT`).
5. **Lane.** Cmd/Ctrl-click the host's row in the Shell's host menu (a window serves
   one host; the hub's window stays on the Shell's project), pick the host project,
   open the assistant, choose the lane CLI, then More actions → "Accept messages from
   other machines". Or leave it to the hub: `host.startAssistant` needs the start
   consent and a Shell window already on that host (`HostAssistantStarter`).
6. **Hub.** In the Shell's own window open the assistant, choose the hub CLI, then More
   actions → "Accept messages from other machines".
7. **Stage check, before any paid prompt.** The host row reads Connected with
   messaging on; both menus show the switch checked; each sidebar opened to its CLI's
   composer, signed in, with no sign-in screen; in a host terminal `command -v node
   git claude codex` resolves and `git config user.name` is the scratch identity; `ps`
   shows exactly the processes the layout owns; nothing from the stage listens on
   45454. On the first prompt, the
   hub's first search must carry the revision `dtrb.sh plan revision` prints. A failure
   here is HARNESS: record it, never reword a runbook for it.
8. **Run** the entry: `"$T/dtrb.sh" plan show <id>` prints the runsheet.
9. **Evidence.** Each role's transcript; the Shell's own record of every exchange
   (Settings → Hosts overview, `remoteHosts.listAssistantExchanges`); the bare remote
   (`git --git-dir <root>/origin.git log --all --oneline`); the host project's
   worktrees and branches; screenshots of each window.
10. **Teardown.** Quit the Shell first (it closes its ControlMasters on quit,
    `stopRemoteHosts`), then each host, then each sshd by its pid; confirm no process
    carries the stage root; `rm -rf` the root (it removes the sign-in links, never
    what they point at); stop the runbook server.

**What the staged path proves.** That a real hub and real lanes, on real CLIs, through
the app's real messaging over a real local ssh, find and follow the runbooks, with
notices and enrollment as shipped (layers 4 and 5 for that scenario). **What it does
not:** a network hop, latency or link loss; a Linux, headless or logged-out host and
start at login; sleep and power; hosts signed in differently (a "Codex only" host
cannot be faked when every instance shares the person's sign-ins); a port held on the
Shell and another on a host (one Mac has one loopback: two listeners cannot hold one
port, so port-ownership cases are fleet-only); and the app lists real remote
machines among its own unverified items (`docs/architecture/remote-hosts.md`,
"Unverified on real hardware"). Those need the fleet path.

## Fleet path: the person's own machines, person-assisted

For every `path: fleet` entry, and to confirm a staged result on real hardware.

- **Setup by the person.** Hosts added and Connected (`daintree.hosts.connect-host`
  walks it); the consents the entry needs, and only those; a scratch project cloned on
  every machine from one scratch remote they can all reach; a fresh lane in a slot
  nobody is using, never an assistant the person is working with.
- **Catalog under test.** A lane reads runbooks from the URL its host's Daintree was
  started with. Either run after the branch is released, or the person starts each
  machine's Daintree from a shell with `DAINTREE_RUNBOOKS_MCP_URL` pointing at a
  runbook server that machine can reach (an `ssh -R` forward to the served port is
  enough). Either way each role's first search result must carry the revision `dtrb.sh
  plan revision` prints; a different one makes the trial ROUTING-void (a live check
  once ran against a catalog from before the runbook landed).
- **Running.** The person types each prompt into the hub; the operator records. No
  automation attaches to any of the person's apps. One entry at a time; no fleet-wide
  sweeps.
- **Checks.** Read-only commands the person runs on each machine (`git -C <project>
  log -1`, `status --porcelain`, `worktree list`, `lsof -nP -iTCP -sTCP:LISTEN`), and
  the shared remote. Copy each role's transcript off its machine for the record.
- **Teardown.** Only the run's branches, worktrees, panes and the scratch project; the
  consents back to how the person had them.

## CLI pairs and cost

| hub | lane | covers |
|---|---|---|
| Claude Code | Codex | Claude Code as hub, Codex as lane |
| Codex | Claude Code | Codex as hub, Claude Code as lane |
| Claude Code | Claude Code | same-CLI baseline |
| Codex | Codex | Codex continuation on both sides |

A register entry runs at most two trials (one per combo), and its combos must run
both CLIs in the role its runbook is written for, so the first two rows are the
default pair: both hubs and both lanes in two trials. A third trial only confirms a
body fix. One stage at a time. The assistant model is set per instance (Settings →
Assistant): Haiku for Claude Code, the issue's model for Codex; prompts that make
anyone launch agents ask for model haiku. Stop at the first usage-limit banner and
record the rest as owed. Default runs on real accounts or machines are always
explicit and recorded; nothing sweeps the fleet on its own.

## Paired role evidence

A hosts scenario is one piece of work seen from both ends: the person's request on the
hub, the hub's brief, the lane's own search for the work the brief names, and the
runbook that comes back. Evidence is kept per end, never as one "it works".

| layer | hub end | lane end | where |
|---|---|---|---|
| selection | the request selects the hub runbook | the lane's search selects the receiving runbook | `cases/1142-hosts-paired-roles.yaml`; `bench pairs` (per end: what loaded, the stage that lost a miss), and `bench local` as the gate |
| wrong role | no lane runbook loads on the hub | no hub baseline (`host.*`) loads on a lane | `forbid`, derived from the catalog per role plus explicit; a forbidden load fails the case |
| contract | baseline and bundle offered by `hub-full`, a lane's by `lane-full`, nothing hidden in `optionalTools` | same | `tests/unit/test_paired_roles.py` against the captures |
| delivery | foundation and hosts foundation in full once per session, named after | the same, in its own session | the same test, over HTTP |
| grounding | the app's rule that sends the hub to search (below) | the app's pointer for a woken lane (below) | citations at the pinned SHA |
| execution, outcome | register entry `paired-role-entry` (`discovery: entry`) | same | owed; a live hub and lane |

**Writing a pair.** Both queries are worded as the app tells the assistant to search: one
sentence of 8 to 15 words in the person's voice, specifics left out. Neither names a
runbook id, and a lane case carries the hub's emitted brief beside it (`brief`):
Objective with its deliverable, Scope, Report (the output) and Stop (when to ask back),
small, by reference, no runbook pasted in; the query must be drawn from the brief's
objective. `validate_pairs` refuses a scenario without both ends, a successful selection
at each, an emitted brief and a wrong-role trap, and loading any case file runs it.
Every scenario carries the same family of traps: the person asking on the machine itself
(a local runbook owns it and neither end's runbook may load), a question rather than
work, the lane's bare wake line (only the envelope may load: no deliverable is known
yet), and a role the caller does not hold (a lane asked to relay on, a hub reading a
host's message). A miss is recorded, not fixed here: where a pair fails on the shipped
selector it is retrain-owed, with the stage `bench pairs` printed; routing text is not
bent to pass it. A hub runbook that does not itself name the lane runbook its brief
should lead to is pinned in `HUB_NAMES_NO_RECEIVER` (`test_paired_roles.py`) with the
reason, and the test fails when the pointer lands.

**Entry discovery.** Register entries normally end their first prompt with the search
line, which proves an assistant follows a procedure it was told to look up. A role-entry
run (`discovery: entry`) types an ordinary request on the hub and lets the lane be
woken by the app's own wake line, with no search instruction anywhere; `plan check`
refuses the line in such an entry. Record each role's first search (query and result) or
that there was none; a lane that never searched is an APP finding, never a pass.

**What the app does at the pinned SHA (`0436363469`).**
- Hub: `buildRunbooksAddendum` (`electron/services/helpSessionRunbooks.ts:53`, written into
  a session's prompt only while "Follow runbooks" is on, `HelpSessionService.ts:3390`)
  makes `search_runbooks` the first call on any request to do something, with a
  sentence of 8 to 15 words in the user's voice. An ordinary request on the hub is
  therefore a discovery entry point as it stands.
- Lane: `buildMessagingAddendum` (`helpSessionRunbooks.ts:106`, written where a wake can
  reach the lane, `HelpSessionService.ts:3416`) quotes the wake line
  (`terminalNotify.ts:635`), says to read it with `assistant.getInbox`, and calls the
  lane its hub's "submanager" and the enrolled hub the manager. **It does not tell the
  lane to search for the work after reading the message.** The generic rule does not
  fill the gap: its first daintree call is the inbox read, before any task is known,
  and it excludes searching for a notice, which a wake line reads as. The test pinning
  the addendum says it is "pointing at runbooks for the procedures"
  (`__tests__/helpSessionRunbooks.test.ts:138`); the text has no such pointer. The
  addendum measures 1746 of its 1750-byte cap.
- **The app-owned change, kept owed (not made here, and no procedure moves into the
  prompt):** one sentence in the Swarm paragraph of `buildMessagingAddendum`, after the
  hub's message is read: search runbooks for the work it asks, once, as the rule
  above describes, before acting ("Once you have read a hub's message, search runbooks
  for the work it asks before acting." is 87 bytes plus a separating space), with the cap
  raised to fit or the paragraph trimmed by as much. Until it lands and a live pair shows the lane
  searching on its own, the lane end of discovery is unproven; the offline evidence
  above shows only that a lane which does search is routed correctly.

## Recording and the register

Record each trial as `trials.md` says (`$W/trials/<id>-<hub>-<lane>.md`), per role:
the search query and what came back selected with its `catalog_revision`, the actions
with their arguments, the `Daintree:` notice lines, and each independent check with
its output. Judge and classify as `trials.md` does; claim no layer the evidence does
not reach (`proof.md`).

`owed-trials.yaml` is the register: one entry per owed run, with its caller, path,
hosts, needs, combos, setup, the exact prompts (`{host_a}`, `{project}`, `{remote}`,
`{branch}`, `{sha}`, `{run}`, `{port}` and the like are filled from the stage or the
fleet), what each role must be seen to do (`selected` means received in full, by
search or `get_runbook`), the outcome checks (layer 5: the world and the app's own
records) and, apart, execution checks read from transcripts (layer 4). A run that passes sets
`status: passed` with one line of `evidence` (the record's date and verdict, no
private paths); a partial one says what is still owed. A `/runbook` run that cannot
trial a hosts runbook adds or updates its entry there as well as in its workspace.

```bash
"$T/dtrb.sh" plan check       # the register against the catalog, the captures and these rules
"$T/dtrb.sh" plan coverage    # landed daintree.hosts.* runbooks with no entry
"$T/dtrb.sh" plan show <id>   # one entry as a runsheet
"$T/dtrb.sh" plan revision    # the catalog revision every search must carry
```

## Failure and recovery scenarios

`failure-scenarios.yaml` holds reusable fault runs: a lost send or start reply, a
duplicate wake, a restart, a dropped link, a second Shell, a full slot set, a
confirmation that times out, and the rest of the plan's nine families. An issue names
the scenarios its acceptance needs by id (`dtrb.sh plan scenarios --issue N`) instead
of copying the matrix. Each scenario carries its family and observations, the
invariants its outcome checks must show (no duplicate write, no displaced lane, no
false stop/pass/completion claim, no unrelated resource touched), the issues it
serves, the fault and when it is injected, what the app returns under it, citations
of the app at `app_sha`, and a run: a register entry that already stages the fault,
or its own in the register's shape, with outcome checks kept apart from transcript
checks.

Three kinds of evidence, never mixed:

| kind | how | what it shows |
|---|---|---|
| contract | `plan check`, offline, every run | the runbooks exist; each role is offered what a step expects of it; each injected condition is a value the captured output schema declares (or a cited app error code). Nothing about behaviour. `plan grounding` also reads every citation at `app_sha` (needs the app checkout). |
| staged | the staged path above | a real hub and lanes meet the fault on one Mac (layers 4 and 5 for that scenario). `staged: owed` until a record says `passed: <date> <verdict>`. |
| real host | the fleet path | what one Mac cannot stand in for: a network hop, sleep, two loopbacks, a second Shell. `real_host: owed` the same way; `n/a: <why>` where the staged run settles it. |

`app_tests` cites the app's own tests of the behaviour, chiefly the in-process
harness that runs the real link (`electron/remote/__tests__/assistantExchanges.harness.test.ts`).
They are evidence of what the app does, never that an agent follows a runbook. The app
has no test of a lost start reply, of a hub or lane action meeting
`CONFIRMATION_TIMEOUT` (`host.*` and `assistant.*` never raise a confirmation; a lane's
pane and worktree actions do), or of message text lost when a host restarts, and no e2e
spec drives messaging between instances: those scenarios rest on their own runs.

**Injecting.** Only through the stage's own processes and the person's own switches:
a ControlMaster is found by pid under the stage root, never by name. The app types at
most one wake per exchange (`AssistantExchangeStore.ts:368-387`), so a duplicate wake
is the operator typing the lane's wake line again. An injection that misses its window
(the send settled before the cut) is HARNESS: record it and repeat that trial once.

**Cost.** Run `minimum_staged` first: six scenarios, two trials each, both CLIs as hub
and as lane in mixed pairs, covering the faults the program names first. After that a
runbook change re-runs only the scenarios that exercise it (`plan scenarios --runbook
<id>`), an issue only those it names, and evidence is shared between issues only when
the run exercised and independently checked each issue's procedure. The usage-limit
stop above applies.

```bash
"$T/dtrb.sh" plan scenarios --minimum       # the bounded subset to run first
"$T/dtrb.sh" plan scenarios --issue 110     # an issue's scenarios and what each still owes
"$T/dtrb.sh" plan show lost-send-reply      # one scenario as a runsheet
"$T/dtrb.sh" plan grounding                 # every citation read at app_sha
```

`plan coverage` also lists any observation or family owner issue no scenario covers.
A passed scenario run sets `staged:` or `real_host:` to `passed: <date> <verdict>`; a
new fault gets a scenario here, not a paragraph in an issue.
