# Claude Code

What Claude Code can do as an agent that pulls Daintree runbooks from the
`search_runbooks` MCP server. It is written for the next step: refining the runbooks so
they apply to agents that are not Daintree's own assistant. Every runbook in the catalog
is written against the Daintree CLI's tools (`terminal.sendCommand`, `watcher.*`,
`timer.schedule`, `grant.create` …). Claude Code has none of those. This document says
what it has instead.

Measured on 2026-09-25 against Claude Code 2.1.282 (Opus 5.5), from a live session's
tool list, `claude --help`, and a few test calls. The surface is not fixed. It depends on
the version, the permission mode, and which MCP servers, skills, plugins and hooks are
installed. Some tools are **deferred**: listed by name and loaded on demand. Sections say
when something is specific to this machine rather than to Claude Code.

## Short answers

- **Loops: yes.** `/loop` re-runs a prompt at a fixed interval, or lets the model pace
  itself (60–3600s between wake-ups). `CronCreate` schedules a prompt on a cron
  expression. `/schedule` creates cloud routines that run with no local session at all.
- **Listeners: yes, but they last only as long as the session.** `Monitor` turns each
  stdout line of a script (or each WebSocket frame) into an event that reaches the
  model. A background command sends a notification when it exits. Background subagents
  report when they finish. Another local Claude session can be asked to send one notice
  when it goes idle. Hooks run shell commands on lifecycle events.
- **Sleeping: not directly.** A foreground `sleep` is blocked. Waiting is always
  event-driven: a background `until` loop, a `Monitor`, a cron fire or a `/loop`
  wake-up. The model is idle in between, not blocked.
- **Outliving the session: only in the cloud.** Every local timer, monitor, cron job and
  background task dies with the session. Cloud routines survive, but they run in a
  remote sandbox and cannot see this machine's terminals.
- **Driving other agents: yes, with limits.** It can spawn and message its own
  subagents, message other Claude Code sessions on the machine, start background Claude
  sessions, and call other agent CLIs such as `codex exec` one-shot. It cannot type into
  an arbitrary terminal, such as a Codex TUI in a Daintree pane, unless Daintree's own
  MCP server is connected, and even then only into terminals it opened itself.

## Where the capabilities come from

| Layer | What it contributes | Varies by |
|---|---|---|
| Built-in tools | Files, shell, subagents, monitors, cron, messaging, worktrees, questions to the user, notifications | Version, permission mode |
| Skills | Packaged procedures invoked as `/name`: `/loop`, `/schedule`, plus whatever the user or project installs | Installation, project `.claude/skills/` |
| MCP servers | Any tool surface: `search_runbooks`, Daintree's control plane, Google search on this machine | Configuration (`claude mcp add`, `.mcp.json`) |
| Hooks and settings | Shell commands the harness (not the model) runs on events; allow/deny rules | `settings.json` |
| The `claude` binary | Headless runs, background sessions, cloud sessions, all callable from the shell | Version |
| Other CLIs on the machine | `gh`, `git`, `codex`, `gemini`, `copytree` … through the shell | This machine |

## Capabilities

### Working in a checkout

- **Read, Edit, Write**: read any file (including images, PDFs and notebooks), exact
  string replacement, whole-file writes.
- **Bash**: any shell command. Foreground calls time out at 2 minutes by default and 10
  at most. `run_in_background` detaches a command, which keeps running across turns and
  re-invokes the model when it exits.
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

### Other Claude Code sessions

- **`ListAgents`** lists what the session can message: its own subagents, agent-team
  teammates, other Claude Code sessions on this machine, and (when connected) cloud and
  Remote Control sessions.
- **`SendMessage`** delivers text to any of them. A message is queued until the receiver's
  next tool round. A session in a different permission mode can hold it for its user's
  approval, and silence from a remote session is not a reply. `notify_when_idle: true`
  asks a session on this machine for one notice when it next goes idle. That notice is
  a listener, not a poll.
- **Background sessions from the shell:** `claude --bg` starts a full session and prints
  an id. `claude agents [--json]` lists sessions, and `claude attach <id>`, `claude logs
  <id>`, `claude stop <id>`, `claude rm <id>` and `claude respawn` manage them. `-w/--worktree`
  gives the session its own worktree, and `--tmux` a tmux pane. This is the closest thing
  to "spawn a visible agent" without Daintree: the user can attach and type into it.
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

These are **one-shot and stateless**: each call has to carry its own context. This is
enough to poll Codex and Gemini for independent verdicts, reviews or votes, and to run
competitions, debates and interviews with them. It cannot take part in a live
conversation with a Codex or Gemini TUI running in some other terminal.

### When Claude Code is the agent being driven

Several runbooks already treat Claude Code as an agent Daintree launches in a pane.
These are the facts about it that they depend on:

- **Skills and commands** are invoked as `/name`, or `/plugin:name` for a plugin's
  skill. User-level ones live in `~/.claude/skills/` and `~/.claude/commands/`,
  outside any project root, so a file search of the project cannot find them. That is
  why the runbooks look them up through Daintree's capability catalog instead.
- **Usage limits:** `daintree.agents.check-usage-limits` records that `/usage` (aliases
  `/cost`, `/stats`) is read-only and that `/usage-credits` changes billing. Its entry was
  checked against September 2026 builds, not re-checked here.

### Waiting, listening and scheduling

The core question for the watch, supervise and timer runbooks.

| Mechanism | Fires when | Lifetime | Cost while waiting | Limits |
|---|---|---|---|---|
| Background `Bash` (`run_in_background`) | The command exits, for example `until grep -q Ready log; do sleep 1; done` | Session | None | One notification per command |
| `Monitor` (command) | Each stdout line the script prints | Session; each arm expires | None until a line arrives | Default 5 min, capped at 30 min per arm; re-arm to continue. Floods are auto-stopped. Filters must match failure states too, or silence reads as "still running" |
| `Monitor` (`ws`) | Each WebSocket text frame | Session; each arm expires | None | As above |
| Background subagent | It finishes | Session | None | Report is the subagent's final message |
| `SendMessage` `notify_when_idle` | Another local session goes idle or exits | One-shot | None | This machine only |
| `CronCreate` | A 5-field cron match (local time), recurring or one-shot | Session, in memory only; recurring jobs expire after 7 days | None | Fires only while the session is idle. Recurring jobs jitter up to 10% of the period (max 15 min) |
| `/loop` | A fixed interval, or a self-chosen delay (`ScheduleWakeup`, 60–3600s) | Session | None | Re-runs the same prompt each time |
| `/schedule` (cloud routine, `RemoteTrigger`) | Cron, a manual run, or a webhook such as a GitHub event | Survives the session and the machine | Billed per run | Runs in a cloud sandbox with a repo checkout. Cannot see local terminals, local servers or uncommitted work |
| Hooks | Harness lifecycle events (below) | As long as settings say | None | Run shell commands, not the model. They can block or annotate a tool call, or inject context |

Hook events configured on this machine: `SessionStart`, `SessionEnd`, `UserPromptSubmit`,
`PreToolUse`, `PostToolUse`, `PostToolUseFailure`, `PermissionRequest`, `Stop`,
`StopFailure`, `SubagentStart`, `SubagentStop`, `TeammateIdle`, `PostCompact`. That is the
set in use here, not necessarily every event the harness offers.

Patterns that work today:

- **"Tell me when X":** a background `until` loop, which sends one notification and ends.
- **"Tell me every time X":** a `Monitor` over `tail -f … | grep -E --line-buffered
  'success|FAILED|Traceback|…'`, re-armed on expiry.
- **A small-model judgement on each check:** a `Monitor` poll loop that pipes the tail
  into `claude -p --model haiku "…Answer YES or NO"` and prints a line only on YES. The
  headless call is measured (above). The combined loop is a pattern, not a tested feature.
- **"Do Y in ten minutes":** a background `sleep 600 && <command>` runs a fixed command
  once with no model involved, then notifies on exit. A one-shot `CronCreate` instead
  delivers a prompt, so the model decides what to do at fire time.
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
- Plain `git worktree` commands through the shell work too.

### State and memory

- **Context:** long conversations are compacted automatically, so work continues past
  the window. Details can be lost in compaction, which is the reason for the next item.
- **Scratchpad:** a session-specific directory for temporary files. Being files, they
  survive compaction and a restart of the process.
- **Auto-memory:** one fact per file, with a `MEMORY.md` index that is loaded into every
  session for this project. It belongs to the user and Claude Code, and nothing shares it
  with Daintree's project memory.
- **`CLAUDE.md`:** project and user instructions loaded at start.
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
- **What this lacks:** a narrow grant with a time limit and a use count, like Daintree's
  `grant.create`. Cron fires and `/loop` wake-ups run under whatever mode the session is
  in.

## With Daintree's MCP server connected

Daintree runs a local MCP server (loopback, bearer-authenticated) that exposes its
action system. Claude Code can reach it two ways, both confirmed in the Daintree source
(`docs/architecture/mcp-server.md`, `electron/services/McpPaneConfigService.ts`) but not
exercised in this session, where no Daintree server is connected.

- **Launched by Daintree:** in a project whose Daintree MCP tier is not `off`, Daintree
  writes a per-pane `--mcp-config` with its own bearer. The session gets the project's
  tier (`workbench`, `action` or `system`), and the tools appear as
  `mcp__daintree__<action>`.
- **Connected by hand:** `claude mcp add --transport http daintree
  "http://127.0.0.1:<port>/mcp" --header "Authorization: Bearer <api-key>"`, using the
  values in Settings → MCP server. An API-key client gets the fixed `external` roster:
  `actions.getContext`, `actions.search`, `actions.getSchema`, `mcp.surface`,
  `agent.launch`, `agent.listAvailable`, `agent.listPresets`, `workspace.list`,
  `fleet.getRunStatus`, `recipe.list`, `recipe.run`, `skills.search`, `skills.load`,
  `terminal.list`, `terminal.getOutput`, `terminal.getStatus`,
  `terminal.sendCommandOwned`, `terminal.injectOwned`, `terminal.new`,
  `terminal.closeOwned`, `terminal.revealOwned`, `terminal.interruptOwned`,
  `terminal.readLastMessageOwned`, `terminal.waitUntilIdle`,
  `terminal.waitUntilIdleBatch`.

Two limits apply either way:

- **Input goes only to terminals the session opened itself**
  (`terminal.sendCommandOwned`, `terminal.injectOwned`). An external agent cannot type
  into the user's shell or into an agent somebody else launched.
- **Confirm-gated actions** need Daintree's own dialog. A client scoped to a background
  workspace has them refused up front.

With the server connected, Claude Code can launch real Daintree agent panes, read their
output and wait on them. What stays Daintree-only is the CLI assistant's own layer:
watchers, timers, grants, the attention queue, scratch stores and the workflow ledger.

## Daintree runbook primitives, and what Claude Code has instead

Input for the refinement, not a plan for it. The counts are how many of the 41 runbooks
declare the tool in `requiredTools`.

| Daintree CLI primitive | Runbooks | Claude Code equivalent | What differs |
|---|---|---|---|
| `context.snapshot`, `worktree.list`, `git.getProjectPulse` | 38, 10, 8 | `git`, `gh` through the shell; `claude agents --json` for its own sessions; with Daintree MCP: `terminal.list`, `workspace.list`, `actions.getContext` | Without Daintree MCP, Daintree's panes are invisible |
| `agentTask.spawnForEdits` | 25 | `Agent` with `isolation: "worktree"`; `claude --bg -w`; with Daintree MCP: `agent.launch` | A subagent is not a visible pane the user can type into. A `--bg` session is attachable, not a Daintree pane |
| `subagent.run` | 3 | `Agent` with `subagent_type: "Explore"` | Nearly identical |
| `terminal.sendCommand` (relay) | 18 | `SendMessage` to a subagent or local Claude session; a fresh `codex exec` per message; with Daintree MCP: `terminal.sendCommandOwned` | Non-Claude agents are stateless one-shots. Only terminals it opened can be typed into |
| `terminal.awaitAll` (in-turn, 120s budget) | 22 | Parallel subagents plus their completion notifications | No shared sleep budget. A foreground shell call stops at 10 minutes |
| `terminal.await.async` (wake later) | 17 | Background completion notifications; `notify_when_idle`; with Daintree MCP: `terminal.waitUntilIdleBatch` | Ends with the session |
| `terminal.read`, `.extract`, `.summarize` | 17, 19, 19 | The subagent's report; reading a background task's output file; `claude logs <id>`; `Agent` with a `haiku` model or `claude -p --model haiku`; with Daintree MCP: `terminal.getOutput`, `terminal.readLastMessageOwned` | Cheap reading is a model choice, not a built-in tool |
| `watcher.terminal.create` | 5 | `Monitor` (substring and regex through `grep`; silence through a poll loop; a model judge through headless `claude -p`) | 30 min per arm, then re-arm. Dies with the session. No Daintree agent-state conditions |
| `watcher.watchPR` | 1 | `Monitor` over `gh pr view` / `gh pr checks`; a cloud routine with a GitHub webhook trigger | Sees CI and reviews too |
| `timer.schedule` `enqueue` | 4 | One-shot `CronCreate` plus `PushNotification` | No inbox note. The prompt goes to the model |
| `timer.schedule` `message` | 4 | One-shot `CronCreate`; `/loop`; `/schedule` if it must survive the session | Local fires need the session alive and idle. Cloud fires cannot see this machine |
| `timer.schedule` `call_safe_tool` | 4 | Background `sleep N && <command>` | A fixed shell command, not a tool call. Dies with the session |
| `grant.create` | 3 | Permission mode plus allow rules | No time limit, no use count, no per-actor scope |
| `scratch.*` | 7–9 | Files in the scratchpad | More durable than Daintree scratch (they survive a restart) |
| `workflow.*` ledger and graph | 1–10 | A file the runbook defines; `Workflow` runs fan-outs but is not a ledger | No built-in durable record of in-flight work |
| `queue.digest`, `queue.resolve` | 6, 14 | None; events arrive in the conversation; `PushNotification` reaches the human | No attention inbox |
| `user.askMultipleChoice` | 8 | `AskUserQuestion` | Close match; 4 options per question at most |
| `forge.*` | 1–7 each | `gh` | Broader than the wrappers |
| `fs.*`, `project.runCheck`, `project.detectRunners` | 3–4 | Read, Bash, LSP | Broader |
| `memory.*` | 1 | Auto-memory | Belongs to the user and Claude Code, not shared with Daintree |
| `copyTree.*` | 1 | The `copytree` CLI (installed on this machine) | No injection into another terminal |
| `terminal.close` | 10 | `TaskStop`; `claude stop`; with Daintree MCP: `terminal.closeOwned` | Only what it started |
| `agentSessionHistory.list` | 1 | `claude --resume`, `claude agents --json --all` | Its own sessions only |
| `recipe.*`, `terminal.rename`, `terminal.focus`, `errors.recent`, `notifications.recent`, `daintree.status`, `worktree.resource.status`, `browser.getConsoleMessages` | 1–5 | Daintree MCP where the tier exposes them; `--chrome` for a browser | Daintree-only otherwise |

## What Claude Code cannot do

- **Keep anything running locally after the session ends.** Background tasks, monitors,
  cron jobs and `/loop` all stop with it. Cloud routines survive, but elsewhere.
- **Pause in the foreground.** `sleep` is blocked. All waiting is event-driven.
- **See or type into terminals it did not start.** Without Daintree's MCP it cannot see
  Daintree panes at all. With it, it can read them but type only into its own.
- **Hold a live conversation with a non-Claude agent's session.** Codex and Gemini are
  reached one call at a time.
- **Grant narrow, expiring authority for unattended work.** Permissions are per session
  and per pattern.
- **Keep an attention inbox, a durable workflow ledger, or send receipts for a message.**
  `SendMessage` confirms delivery to a session, not that the model read it.
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
- The Daintree app's `docs/architecture/mcp-server.md`,
  `shared/config/mcpExternalTierAllowlist.ts` and `McpPaneConfigService.ts` for the
  Daintree MCP section.
- The runbook catalog and `docs/MCP.md` on the `feature/runbook-search-endpoint` branch
  (commit `43f00c8`), where `search_runbooks` is defined. That endpoint is not on `main`
  yet. Once deployed, it is added with `claude mcp add --transport http daintree-runbooks
  https://assistant.daintree.org/v1/daintree/mcp`, and the tool appears as
  `mcp__daintree-runbooks__search_runbooks`.

Not tested: `--bg` sessions, cloud routines, hooks and the Daintree MCP connection.
Those rows come from help text, tool schemas and source.
