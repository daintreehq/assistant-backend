# Claude Code

> **Dated and optional.** This is an inventory of one Claude Code environment on
> 2026-09-25, not a contract. No runbook requires anything on this page. Every runbook
> relies on the common contract instead: the `daintree.foundation` runbook (returned
> under `supporting_runbooks` with every search) and
> [`DAINTREE_RUNBOOK_CLIENT.md`](DAINTREE_RUNBOOK_CLIENT.md). Where they disagree with
> this page, they win. The host features below are alternatives a Claude Code user may
> choose. None of them stands in for a Daintree notice that failed to arrive.

Measured on 2026-09-25 against Claude Code 2.1.282 (Opus 5.5), from a live session's
tool list, `claude --help`, and a few test calls. The surface is not fixed. It depends on
the version, the permission mode, and which MCP servers, skills, plugins and hooks are
installed. Some tools are **deferred**: listed by name and loaded on demand. The Daintree
section was checked the same day against the app's captured MCP surfaces
(`tests/fixtures/daintree_mcp/`, app commit `cab0d52531` on `feature/mcp-cleanup`) and
that commit's source. Sections say when something is specific to this machine rather
than to Claude Code.

## Short answers

- **Following a Daintree runbook takes two MCP connections and nothing else from this
  page.** `daintree-runbooks` supplies the instructions. The app's own `daintree`
  connection supplies the actions and the notices. In a Daintree pane whose project has
  Daintree MCP switched on, Daintree itself re-activates Claude Code when a child it
  asked about stops working.
- **Loops: yes.** `/loop` re-runs a prompt at a fixed interval, or lets the model pace
  itself (60–3600s between wake-ups). `CronCreate` schedules a prompt on a cron
  expression. `/schedule` creates cloud routines that run with no local session at all.
- **Listeners: yes, for the shell and the network, and only while the session lasts.**
  `Monitor` turns each stdout line of a script (or each WebSocket frame) into an event
  that reaches the model. The harness reports when a background command exits.
  Background subagents report when they finish. Hooks run shell commands on lifecycle
  events. None of these can see Daintree agent state.
- **Sleeping: not directly.** A foreground `sleep` is blocked. Waiting is always
  event-driven: a Daintree notice, a background `until` loop, a `Monitor`, a cron fire
  or a `/loop` wake-up. The model is idle in between, not blocked.
- **Outliving the session: only in the cloud.** Every local timer, monitor, cron job and
  background task dies with the session. Cloud routines survive, but they run in a
  remote sandbox and cannot see this machine's terminals or its Daintree app.
- **Driving other agents: yes, with limits.** Through Daintree it can launch visible
  agent panes of any installed kind, and type into the ones it launched or the user
  handed to it. Without Daintree it has its own subagents, other Claude Code sessions on
  the machine, and one-shot calls to other agent CLIs such as `codex exec`.

## Where the capabilities come from

| Layer | What it contributes | Varies by |
|---|---|---|
| Built-in tools | Files, shell, subagents, monitors, cron, messaging, worktrees, questions to the user, notifications | Version, permission mode |
| Skills | Packaged procedures invoked as `/name`: `/loop`, `/schedule`, plus whatever the user or project installs | Installation, project `.claude/skills/` |
| MCP servers | `daintree-runbooks` (`search_runbooks`, `get_runbook`); `daintree` (the app's actions and notices); Google search on this machine | Configuration (`claude mcp add`, `.mcp.json`, a pane's `--mcp-config`) |
| Hooks and settings | Shell commands the harness (not the model) runs on events; allow/deny rules | `settings.json` |
| The `claude` binary | Headless runs, background sessions, cloud sessions, all callable from the shell | Version |
| Other CLIs on the machine | `gh`, `git`, `codex`, `gemini`, `copytree` … through the shell | This machine |

## Claude Code and Daintree's MCP

Checked against the fixtures and app source at `cab0d52531`. Not exercised from this
session, which had no Daintree connection.

### Three ways to be connected

| How Claude Code runs | How the connection is made | Tier | Tools listed | Notices |
|---|---|---|---|---|
| In a Daintree agent pane | Daintree writes a per-pane `--mcp-config` with its own bearer, under the server key `daintree`, when the project's Daintree MCP setting is `core` or `full`. The setting defaults to `off`, which writes no Daintree entry. | the project setting | 68 at either tier | yes |
| As Daintree's sidebar assistant | a help-session token Daintree issues, unless the assistant's "Daintree control" setting is off | the assistant's tool set, `core` or `full` | 28 or 73 | yes |
| Connected by hand with the API key | `claude mcp add --transport http daintree "http://127.0.0.1:<port>/mcp" --header "Authorization: Bearer <api-key>"`, with the port and key from Daintree's MCP settings | `external` | 34 | no |

The old tier names are gone: `workbench` and `action` became `core`, and `system`
became `full`. Daintree reads settings stored under the old names in place. The old
`system` tier also pre-approved an agent pane's ordinary confirmations. No tier does that
now, so `full` means more tools and nothing else. A pane's tier is copied onto its bearer
at launch, so changing the project setting reaches a pane only when it is relaunched.
(That comes from reading the code. It was not observed.)

Never commit a Daintree bearer or put one in an example. The pane's bearer belongs to
Daintree.

### What an agent pane sees

- **The tier sets approvals, not the listing.** A pane is listed the same 68 tools at
  `core` and `full`. At `core`, 28 run without asking and the other 40 raise an approval
  to the user. `USER_REJECTED` means the user declined, so do not retry. At `full` all
  68 run without that approval. The confirm-gated actions (`recipe.run`,
  `worktree.deleteOwned`, `worktree.resource.teardown`) still show Daintree's own
  confirmation dialog at either tier.
- **Claude Code's permission mode does not reach Daintree's approvals.**
  `bypassPermissions` skips Claude Code's prompts, not Daintree's dialogs. Daintree never
  treats a client's own answer as a human approval.
- **Owned tools.** A pane gets `terminal.sendCommandOwned`, `terminal.closeOwned`,
  `terminal.injectOwned`, `terminal.interruptOwned`, `terminal.revealOwned`,
  `terminal.readLastMessageOwned` and `worktree.deleteOwned`. They reach only terminals
  this pane launched, worktrees it created, and terminals the user handed to it in
  Daintree's UI. A hand-over never includes close or delete. The unscoped
  `terminal.sendCommand` and `terminal.close` belong to the sidebar assistant only.
  Status reads, `terminal.rename`, `terminal.moveToWorktree` and
  `terminal.notifyWhenIdle` are not ownership-scoped.
- **Ownership follows the pane's bearer.** It survives an MCP reconnect, an idle reap
  and a restart of the MCP server. It does not survive the pane being relaunched or the
  app restarting. An API-key session's ownership ends with that session.
- **Its own location.** The pane's worktree and pane id are in the environment
  (`DAINTREE_WORKTREE_ID`, `DAINTREE_PANE_ID`, `DAINTREE_CWD`), readable through Bash.
  `agent.launch` without `worktreeId` lands in the worktree the user has selected, not
  the pane's own.
- **Tool names.** Daintree's wire name is the action id, verbatim
  (`terminal.getStatus`), and Claude Code lists it under the `mcp__daintree__` prefix.
  Claude Code does rewrite some characters: in this session, a server named
  `claude.ai Claude Docs` appears as `mcp__claude_ai_Claude_Docs__…`. How it renders the
  dots in Daintree's tool names was not observed. Call whatever the tool list shows.
- **Server instructions.** The MCP `instructions` Daintree sends at `initialize` are the
  only Daintree-written guidance an ordinary pane receives. Daintree keeps them under
  1,536 bytes because Claude Code truncates server instructions at 2 KiB.
- **Reading a Claude Code child.** `terminal.readLastMessageOwned` reads a Claude Code
  child's own transcript. For any other agent it returns `unavailable`. A permission
  prompt never appears in the transcript, so read the live screen with
  `terminal.getOutput`.

### Notices

- **Asking for one.** Pass `notify:true` on `agent.launch` or `terminal.sendCommandOwned`.
  For a child that is already working, call `terminal.notifyWhenIdle`, whose optional
  `note` (160 characters at most) is echoed back. Then end the turn.
- **When it fires.** Once only: when the target first settles out of working for two
  seconds, or when it exits or is closed.
- **What arrives.** Daintree types one line into this pane's own prompt. It begins
  `Daintree: terminal <id> …` and ends `Check it with terminal.getStatus.`. A line typed
  at Claude Code's prompt starts a turn, so **Daintree re-activates the pane itself**. No
  Claude Code feature is involved.
- **When it is typed.** Only while Claude Code waits at an empty prompt with nothing typed
  since it settled. The line is held while Claude Code is working or the user has typed
  something. It is blocked while the pane is itself stopped on an approval, a question or
  an error. One line is outstanding at a time, and notices that fire together share a
  line.
- **Lifetime.** Notices survive an MCP reconnect. They end when the pane exits or is
  relaunched, and an app restart loses them. The agent has no way to list, cancel or
  expire one. The user can drop the pending ones with Stop on the pane's chip. A pane can
  hold 32 pending notices.
- **What it replaced.** In this build notices replaced the four terminal watch tools
  (`terminal.registerWatch`, `terminal.listWatches`, `terminal.getWatchEvents`,
  `terminal.cancelWatch`) and the `paneWakeEnabled` setting. Notices need no setting
  beyond the MCP server being on.
- **API-key connections get none.** `notify` is left off their schemas and refused if
  sent anyway, and `terminal.notifyWhenIdle` is not listed.

### Bounded waits

`terminal.waitUntilIdle` and `terminal.waitUntilIdleBatch` are capped at 60 seconds for
pane and sidebar sessions, and the user cannot talk to the session while a wait is open.
The schema says an API-key session may block for up to two hours. Claude Code's own MCP
tool timeout may end such a call sooner (not tested).

### One pacing owner

Daintree's instructions for its own assistant (`help/CLAUDE.md` at `cab0d52531`) say a
loop gets exactly one pacing owner. That owner is Daintree notices, or `ScheduleWakeup`
where the pane cannot take notices. Never stack a second timer, background `sleep`,
monitor or polling script on top, because two wakers mean duplicate checks and double
actions. The same holds for a Claude Code pane following a runbook.

Host monitors cannot watch Daintree agent state anyway. The Daintree tools are MCP calls
the model makes, and a shell script has no Daintree connection. Lifting the pane's bearer
out of its config file for a script would route around the app's credential boundary.

## What a runbook needs, and what Claude Code offers besides

This table is keyed by what the migrated runbooks ask for. The middle column is the
common contract that every runbook uses. The right column is optional and lasts only as
long as the session unless it says otherwise.

| Runbook need | Common contract | Claude Code natively (optional) |
|---|---|---|
| Launch a visible agent | `agent.launch` with `name`, `worktreeId` and `prompt` | `claude --bg` starts a session the user can attach to. `Agent` subagents run inside this session. Neither is a Daintree pane, so neither satisfies a request for a visible agent. |
| Collect its answer | `notify:true`, then `terminal.getStatus`, `terminal.readLastMessageOwned` (Claude Code children) or `terminal.getOutput` | Nothing is needed in a pane, because Daintree re-activates it. A subagent's report and `SendMessage` `notify_when_idle` cover Claude's own children, not Daintree panes. |
| Continue with no notice (an API-key connection) | One bounded wait, or record the step and tell the user it needs their continuation | `ScheduleWakeup` (self-paced `/loop`) or a one-shot `CronCreate`. Each starts a turn that can take a non-blocking `terminal.getStatus`. A cron job fires only while the session is alive and idle. |
| Bounded wait | One `terminal.waitUntilIdle` or `terminal.waitUntilIdleBatch`, then a notice | None needed. Foreground `sleep` is blocked. |
| Deadline, or a child stuck in working | None in Daintree. A stuck child never sends a notice, and a deadline in a checkpoint wakes nothing. | A one-shot `CronCreate` or a `ScheduleWakeup` at the deadline, if the session will still be alive and idle then. Say so when it will not be. |
| Checkpoint location | `.daintree/runbook-runs/<run-id>.json` in the coordinator checkout, excluded from git | The session scratchpad survives compaction and a process restart, but it belongs to this session. Auto-memory holds durable facts, not run state. Use the checkout default for work that may resume in a new session. |
| Local reads, edits and tests | The agent's own tools | Read, Edit, Write, Bash, LSP |
| Forge reads and writes | `gh` through the shell. A pane is also listed `forge.*` reads (`full`; a `core` pane asks first). No forge writes are on MCP. | `gh`, and a `Monitor` over `gh pr checks` for a PR the session is watching itself |
| Scheduled or recurring work | None in Daintree | `/loop` and `CronCreate` (session-bound; recurring jobs expire after 7 days). `/schedule` cloud routines survive the session but run in a cloud sandbox with no view of local Daintree. |
| Ask the user | Daintree's own approval and confirmation dialogs for its actions | `AskUserQuestion`, `PushNotification` |
| A second opinion from a non-Claude model | A Daintree pane running that agent | `codex exec`, one-shot and stateless |

## Host capabilities in detail

### Working in a checkout

- **Read, Edit, Write**: read any file (including images, PDFs and notebooks), exact
  string replacement, whole-file writes.
- **Bash**: any shell command. Foreground calls time out at 2 minutes by default and 10
  at most. `run_in_background` detaches a command, which keeps running across turns.
  When it exits, the harness notices and starts a turn. The harness does the waking, not
  the process: a process detached any other way (`nohup … &` inside a foreground call)
  wakes nothing.
- **LSP**: language-server queries such as definitions and references (deferred).
- **NotebookEdit**: Jupyter cells (deferred).
- **Plan mode**: a read-only planning state the user approves out of.

### Delegating to subagents (`Agent`)

- **Types available here:** `general-purpose` (all tools), `Explore` (read-only search,
  returns conclusions rather than file dumps), `Plan` (read-only design), `fork` (a copy
  of the current conversation), `claude-code-guide`, and any custom agents defined in
  `.claude/agents/` or passed with `--agents`.
- **Runs in the background** by default. The parent keeps working and gets a notification
  when a subagent finishes. The subagent's final message is its report, and the user
  does not see it unless the parent relays it.
- **Several at once:** independent subagents launched in one message run concurrently.
- **Model per agent:** `haiku`, `sonnet`, `opus` or `fable`, so cheap reading can go to
  a small model.
- **Isolation:** `isolation: "worktree"` gives the subagent its own git worktree,
  removed automatically if it changed nothing. `isolation: "remote"` runs it in a cloud
  sandbox, where available.
- **Follow-up:** `SendMessage` to a subagent's name or id resumes it with its own context
  intact, so the same agent can be asked a second question.
- **Stop:** `TaskStop` ends a background subagent or command.
- **Workflows (`Workflow`):** a JavaScript orchestration script with `agent()`,
  `parallel()`, `pipeline()` and phases, which fans out many subagents deterministically
  and runs in the background. It needs explicit opt-in from the user (the keyword
  `ultracode`, or asking for a workflow), and this session's size guideline is under 10
  agents. `resumeFromRunId` replays cached steps, within the same session only.

A subagent is not a Daintree pane. Its id is not a terminal id, and its completion is not
a Daintree notice.

### Other Claude Code sessions

- **`ListAgents`** lists what the session can message: its own subagents, agent-team
  teammates, other Claude Code sessions on this machine, and (when connected) cloud and
  Remote Control sessions.
- **`SendMessage`** delivers text to any of them. A message is queued until the receiver's
  next tool round. A session in a different permission mode can hold it for its user's
  approval, and silence from a remote session is not a reply. `notify_when_idle: true`
  asks a session on this machine for one notice when it next goes idle. That notice is
  a listener, not a poll. It confirms delivery to a session, not that the model read it.
- **Background sessions from the shell:** `claude --bg` starts a full session and prints
  an id. `claude agents [--json]` lists sessions, and `claude attach <id>`, `claude logs
  <id>`, `claude stop <id>`, `claude rm <id>` and `claude respawn` manage them. `-w/--worktree`
  gives the session its own worktree, and `--tmux` a tmux pane. The user can attach and
  type into such a session, but it is not a Daintree pane.
- **Headless runs:** `claude -p "<prompt>"` answers once and exits. Options:
  `--output-format json|stream-json`, `--json-schema` for validated structured output,
  `--model`, `--tools` to narrow the toolset, `--permission-mode`, `--max-budget-usd`,
  `--no-session-persistence`. Measured here: piping a test-run tail into
  `claude -p --model haiku --tools ""` with a YES/NO question answered correctly in about
  5 seconds. **In this environment headless runs bill `ANTHROPIC_API_KEY`, not the
  claude.ai login** (the CLI warns that connectors are disabled for that reason).
- **Cloud sessions:** `--cloud` creates or attaches one, and `--remote-control` makes a
  local session reachable from claude.ai and the phone app.

### Other agent CLIs

Through the shell, Claude Code can call any agent CLI installed on the machine. This
machine has `codex` (0.157.0) and `gemini`. The user's instructions define the house way
to call Codex: `codex exec` with the prompt on stdin, a read-only sandbox, and the
transcript sent to a log file, run in the background. The CLI's usual non-interactive
mode is `gemini -p`, but that is untested here.

These calls are **one-shot and stateless**: each has to carry its own context. That is
enough to poll Codex and Gemini for independent verdicts, reviews or votes. A live,
visible conversation with another agent goes through a Daintree pane instead.

### When Claude Code is the agent being driven

Daintree launches Claude Code in a pane with the `agentId` from `agent.listAvailable`.
These facts about it matter to the runbooks that drive it:

- **Skills and commands** are invoked as `/name`, or `/plugin:name` for a plugin's
  skill. User-level ones live in `~/.claude/skills/` and `~/.claude/commands/`,
  outside any project root, so a file search of the project cannot find them. Daintree's
  `slashCommands.list` (`full`; a `core` pane asks first) lists the commands an agent CLI
  offers, including project-defined ones.
- **Its last reply** can be read with `terminal.readLastMessageOwned`. The live screen,
  including permission prompts, comes from `terminal.getOutput`.
- **Usage limits:** `daintree.agents.check-usage-limits` records that `/usage` (aliases
  `/cost`, `/stats`) is read-only and that `/usage-credits` changes billing. Its entry was
  checked against September 2026 builds, not re-checked here.

### Waiting, listening and scheduling

These features are for work Claude Code runs itself: builds, tests, logs, a PR on the
forge. None of them observes Daintree agent state, and none replaces a Daintree notice.

| Mechanism | Fires when | Lifetime | Cost while waiting | Limits |
|---|---|---|---|---|
| Background `Bash` (`run_in_background`) | The command exits, for example `until grep -q Ready log; do sleep 1; done` | Session | None | One notification per command |
| `Monitor` (command) | Each stdout line the script prints | Session; each arm expires | None until a line arrives | Default 5 min, capped at 30 min per arm; re-arm to continue. Floods are auto-stopped. Filters must match failure states too, or silence reads as "still running" |
| `Monitor` (`ws`) | Each WebSocket text frame | Session; each arm expires | None | As above |
| Background subagent | It finishes | Session | None | Report is the subagent's final message |
| `SendMessage` `notify_when_idle` | Another local session goes idle or exits | One-shot | None | This machine only |
| `CronCreate` | A 5-field cron match (local time), recurring or one-shot | Session, in memory only; recurring jobs expire after 7 days | None | Fires only while the session is idle. Recurring jobs jitter up to 10% of the period (max 15 min) |
| `/loop` | A fixed interval, or a self-chosen delay (`ScheduleWakeup`, 60–3600s) | Session | None | Re-runs the same prompt each time |
| `/schedule` (cloud routine, `RemoteTrigger`) | Cron, a manual run, or a webhook such as a GitHub event | Survives the session and the machine | Billed per run | Runs in a cloud sandbox with a repo checkout. Cannot see local terminals, local servers, the Daintree app or uncommitted work |
| Hooks | Harness lifecycle events (below) | As long as settings say | None | Run shell commands, not the model. They can block or annotate a tool call, or inject context |

Hook events configured on this machine: `SessionStart`, `SessionEnd`, `UserPromptSubmit`,
`PreToolUse`, `PostToolUse`, `PostToolUseFailure`, `PermissionRequest`, `Stop`,
`StopFailure`, `SubagentStart`, `SubagentStop`, `TeammateIdle`, `PostCompact`. That is the
set in use here, not necessarily every event the harness offers.

Patterns that work today, for the session's own jobs:

- **"Tell me when X":** a background `until` loop, which sends one notification and ends.
- **"Tell me every time X":** a `Monitor` over `tail -f … | grep -E --line-buffered
  'success|FAILED|Traceback|…'`, re-armed on expiry.
- **A small-model judgement on each check:** a `Monitor` poll loop that pipes the tail
  into `claude -p --model haiku "…Answer YES or NO"` and prints a line only on YES. The
  headless call is measured (above). The combined loop is a pattern, not a tested feature.
- **"Do Y in ten minutes":** a background `sleep 600 && <command>` runs a fixed command
  once with no model involved, then the harness reports the exit. A one-shot `CronCreate`
  instead delivers a prompt, so the model decides what to do at fire time.
- **A PR's fate:** a `Monitor` polling `gh pr view --json state` and `gh pr checks`. That
  sees CI and reviews as well as merged/closed.

### Talking to the human

- **`AskUserQuestion`**: 1–4 questions per call, each with 2–4 options. Supports
  multi-select, side-by-side previews and a free-text "Other".
- **`PushNotification`**: a desktop notification, also sent to the phone when Remote
  Control is connected. It is skipped when the user is already at the terminal.
- **Artifacts**: publish a private HTML page on claude.ai. A page can watch comments
  sent to the session and keep a small shared database.
- Ordinary replies are Markdown in the terminal.

### Isolation

- `EnterWorktree` / `ExitWorktree` move the session into a new worktree under
  `.claude/worktrees/` and back. They are used only when the user or the project asks.
- `claude -w <name>` starts a session in its own worktree.
- `Agent` with `isolation: "worktree"` isolates one subagent.
- Plain `git worktree` commands through the shell work too. A worktree the runbooks work
  in is normally one Daintree created (`worktree.createWithRecipe`), addressed by its id
  and path.

### State and memory

- **Context:** long conversations are compacted automatically, so work continues past
  the window. Details can be lost in compaction, which is why run state belongs in a
  checkpoint file.
- **Scratchpad:** a session-specific directory for temporary files. Being files, they
  survive compaction and a restart of the process.
- **Auto-memory:** one fact per file, with a `MEMORY.md` index that is loaded into every
  session for this project. It belongs to the user and Claude Code. Daintree offers no
  memory store over MCP.
- **`CLAUDE.md`:** project and user instructions loaded at start. The runbook client
  instruction in `DAINTREE_RUNBOOK_CLIENT.md` is installed in the user-level one.
- **Resume:** `claude --continue`, `claude --resume`, `--fork-session`, `--from-pr`.

### Web and research

`WebFetch` and `WebSearch` (both deferred), plus on this machine an `ask-google` MCP
server. The account also has claude.ai connectors (Gmail, Google Drive, Calendar and
docs) that have no bearing on runbooks.

### Guardrails

- **Permission modes:** `manual`, `acceptEdits`, `auto`, `plan`, `dontAsk`,
  `bypassPermissions`, set per session.
- **Allow/deny rules:** `Bash(git *)`, `Edit` and similar patterns in settings or on the
  command line.
- **Scope:** these govern Claude Code's own tools. Daintree's approvals and confirmation
  dialogs belong to the app and apply whatever mode Claude Code is in. No MCP tool issues
  a Daintree grant. A pane's "Allow for this session" is a user gesture in Daintree. Cron
  fires and `/loop` wake-ups run under whatever mode the session is in.

## What Claude Code cannot do

- **Keep anything running locally after the session ends.** Background tasks, monitors,
  cron jobs and `/loop` all stop with it. Cloud routines survive, but elsewhere.
- **Pause in the foreground.** `sleep` is blocked. All waiting is event-driven.
- **Act in Daintree without the app's connection.** With only `daintree-runbooks` it can
  read procedures and nothing more. With the `daintree` connection it can read any
  terminal in the project, but type into and close only what it launched or was handed.
- **Watch Daintree agent state from the host.** Monitors, hooks and background commands
  see the shell and the network, not Daintree's agent states.
- **Get a notice over an API-key connection.** Only a pane or the sidebar assistant has
  a prompt for Daintree to type into.
- **Hold a live conversation with a non-Claude agent outside Daintree.** Codex and
  Gemini are reached one call at a time unless they run in a Daintree pane.
- **Set a deadline that fires after the session ends,** except through a cloud routine
  that cannot see this machine.
- **Run a `Workflow` without the user's explicit opt-in**, or enter a worktree unless the
  user or the project asks.

## How this was established

- The tool list, including deferred tools whose schemas were loaded to read their exact
  limits: `Monitor`, `CronCreate`, `SendMessage`, `EnterWorktree` and others.
- `claude --help`, `claude agents --help` and `claude mcp add --help`, version 2.1.282.
- `~/.claude/settings.json` (only the names of the configured hook events) and
  `~/.claude.json` (only the names of the configured MCP servers).
- A headless `claude -p --model haiku` YES/NO judgement over piped text: correct, about
  5 seconds.
- For the Daintree section: the captured surfaces in `tests/fixtures/daintree_mcp/`
  (`pane-core`, `pane-full`, `help-core`, `help-full`, `external`) and, at app commit
  `cab0d52531`, `docs/architecture/mcp-server.md`, `help/CLAUDE.md`,
  `electron/services/McpPaneConfigService.ts`,
  `electron/ipc/handlers/terminal/lifecycle.ts` and
  `electron/services/mcp-server/terminalNotify.ts`.
- The runbook service is live at `https://assistant.daintree.org/v1/daintree/mcp`
  (`docs/MCP.md`). It is added with `claude mcp add --transport http daintree-runbooks
  https://assistant.daintree.org/v1/daintree/mcp`, and its tools appear under the
  `mcp__daintree-runbooks__` prefix.

Not tested: `--bg` sessions, cloud routines, hooks, the Daintree MCP connection, and a
notice actually reaching a Claude Code pane. Those rows come from help text, tool
schemas, the fixtures and source.
