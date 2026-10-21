# Codex

> **Dated and optional.** This is an inventory of Codex host surfaces as of
> 25 September 2026, not a contract. No runbook requires anything on this page. Every
> runbook relies on the common contract instead: the `daintree.foundation` runbook
> (returned under `supporting_runbooks` with every search) and
> [`DAINTREE_RUNBOOK_CLIENT.md`](DAINTREE_RUNBOOK_CLIENT.md). Where they disagree with
> this page, they win. "Codex" names several OpenAI products with different facilities,
> so each claim below says which surface it belongs to. A feature of one surface is no
> evidence for another.

Checked on 25 September 2026 against the Codex CLI 0.157.0 installed on this machine
(`codex --help`, `codex exec --help`, `codex mcp add --help`, `codex features list`) and
OpenAI's published documentation (source notes). The Daintree section was checked
against the app's captured MCP surfaces (`tests/fixtures/daintree_mcp/`, app commit
`cab0d52531` on `feature/mcp-cleanup`) and that commit's source. Nothing Daintree-related
was exercised live from Codex.

## Short answers

- **Daintree notices reach Codex only when it is Daintree's sidebar assistant.** In this
  app build Daintree gives a Codex agent pane no Daintree connection. Codex reaches the
  app either as the sidebar assistant, which gets notices, or as an API-key client the
  user configures, which does not. Over an API-key connection, a notice-driven runbook
  takes the foundation's no-notice branch. That means one bounded wait, a host wake
  verified to resume the session, or a recorded step the user continues.
- **Loops and sleeping (Codex CLI): within an active turn only.** Codex can loop over
  tool calls and sleep while the turn is active. Nothing continues it after the turn ends.
- **Listeners and schedules: not in Codex CLI.** Scheduled and event-triggered tasks are
  features of the ChatGPT apps. A process that Codex CLI leaves running does not wake it.
- **Driving other agents.** Codex CLI has `codex exec` one-shots and multi-agent
  subagents inside a session. Through Daintree's MCP it has `agent.launch`, which opens
  visible panes.

## Which Codex

| Surface | What it is | What matters for Daintree runbooks |
|---|---|---|
| Codex CLI (`codex`, `codex exec`) | Local TUI and headless runs on this machine | This is what runs in a terminal, including a Daintree pane. MCP servers come from `~/.codex/config.toml` or `-c` overrides, and instructions from `AGENTS.md`. It has no scheduled tasks. |
| Codex IDE extension | Codex inside an editor | Not examined. OpenAI's documentation says it lacks the scheduled-task management interface. |
| ChatGPT / Codex desktop app (`codex app` launches it) | Desktop client | Scheduled tasks where enabled, and a local-project task needs the computer and the app running. Also connectors (apps), and browser or computer use where the host exposes them. |
| ChatGPT web and mobile | Hosted | Scheduled tasks. On eligible plans, supported Gmail, Slack and GitHub events can start a task. These run away from this machine and cannot see its Daintree app. |
| Codex Cloud (`codex cloud`, experimental in the CLI) | Remote tasks with a repository checkout | Cannot see local terminals, the local Daintree app or uncommitted work. |

## Codex and Daintree's MCP

This section covers app build `cab0d52531`.

| How Codex runs | Daintree connection | Tier and tools | Notices |
|---|---|---|---|
| In a Daintree agent pane | None from Daintree. In this build Daintree writes a pane connection only for Claude Code panes. | none | no |
| As Daintree's sidebar assistant | Daintree launches Codex with its own `-c mcp_servers.daintree.*` settings and a help-session token in `DAINTREE_MCP_TOKEN`, unless the assistant's "Daintree control" setting is off. Codex then loads Daintree's `help/AGENTS.md`. | `core` (28 tools) or `full` (73), with the unscoped `terminal.sendCommand` and `terminal.close` | yes, typed into the assistant lane's terminal |
| With the API key, configured by the user | `codex mcp add daintree --url "http://127.0.0.1:<port>/mcp" --bearer-token-env-var DAINTREE_MCP_TOKEN`, or an `http_headers` entry in `config.toml`, with the port and key from Daintree's MCP settings | `external`, 34 tools | no |

- **A Codex pane is still a normal child.** A Claude Code pane or the sidebar assistant
  can launch it with `agent.launch`, prompt it with the send tool, and read it with
  `terminal.getStatus` and `terminal.getOutput`. `terminal.readLastMessageOwned` returns
  `unavailable` (`provider-mismatch`) for Codex, so read the screen instead.
- **The credential decides the caller, not the terminal.** If the user's own Codex
  config has an API-key `daintree` entry, a Codex pane that loads it is an API-key client
  like any other: `external` tier, no notices. (Inferred from how Daintree resolves
  tiers. Not observed.)
- **What the API-key surface lacks.** `agent.launch` and `terminal.sendCommandOwned` have
  no `notify` in their schemas, and sending it anyway is refused (`NOTIFY_NOT_ELIGIBLE`)
  with nothing sent. `terminal.notifyWhenIdle`, `terminal.rename`,
  `terminal.moveToWorktree`, `worktree.waitUntilReady` and the `forge.*` reads are not
  listed. Git, forge and file work belong to Codex's own shell.
- **What it has.** The owned tools: `terminal.sendCommandOwned`, `terminal.closeOwned`,
  `terminal.injectOwned`, `terminal.interruptOwned`, `terminal.revealOwned`,
  `terminal.readLastMessageOwned` and `worktree.deleteOwned`. They reach only what this
  session launched or created. Ownership lasts as long as the MCP session, so a new
  session owns nothing.
- **A connection bound to a project.** Daintree's "Copy config for this project" adds a
  `Daintree-Workspace-Id` header, which keeps the client on that project whatever the
  user focuses. It also withholds the confirm-gated `recipe.run` and
  `worktree.deleteOwned`.
- **Waits.** `terminal.waitUntilIdle` and `terminal.waitUntilIdleBatch` may block for up
  to two hours for an API-key session, against 60 seconds in Daintree's own sessions.
  Codex's own MCP tool timeout may end the call sooner (not tested).
- **Approvals.** Confirm-gated actions need Daintree's own dialog. Codex's approval and
  sandbox settings, `--dangerously-bypass-approvals-and-sandbox` included, have no say
  over it.
- **Tool names.** Daintree's wire names are the action ids (`terminal.getStatus`). Codex
  prefixes MCP tools with the server name, since the `non_prefixed_mcp_tool_names`
  feature is off in 0.157.0. How it renders the dots was not observed. Call what Codex
  lists.

## What a runbook needs, and what Codex offers besides

The middle column is the common contract every runbook uses. The other two are optional
and belong to the named surface.

| Runbook need | Common contract | Codex CLI (optional) | Other OpenAI surfaces (optional) |
|---|---|---|---|
| Launch a visible agent | `agent.launch` with `name`, `worktreeId` and `prompt` | Multi-agent subagents and `codex exec` runs are not Daintree panes, and never satisfy a request for a visible agent | none |
| Collect its answer | A notice (sidebar assistant only), or one bounded wait, then `terminal.getStatus` and `terminal.getOutput` | Nothing that wakes the session | none |
| Continue later with no notice | Record the step and tell the user it needs their continuation | `codex resume` lets the user reopen the session. `codex queue --thread <id> --message <text>` queues a message for an existing session (from help text only; untested) | A scheduled task starts a new task at a set time. It does not resume a CLI session, and a cloud run cannot see local Daintree |
| Bounded wait | One `terminal.waitUntilIdle` or `terminal.waitUntilIdleBatch` | The sleep tool delays the active turn only | none |
| Deadline, or a child stuck in working | None in Daintree. A stuck child never sends a notice, and a deadline in a checkpoint wakes nothing | None | A desktop-app scheduled task, if the machine and app are running and the task can reach Daintree (unverified) |
| Checkpoint location | `.daintree/runbook-runs/<run-id>.json` in the coordinator checkout, excluded from git | Use the checkout default. The `memories` feature is off by default here and is not run state | none |
| Forge writes | `gh` through the shell | `gh` | The GitHub connector, a separate surface with its own permissions |
| Local reads, edits and tests | The agent's own tools | Shell, patch edits, the sandbox mode in force | none |

## Codex CLI in detail

Codex CLI 0.157.0 on this machine.

| Capability | What it is |
|---|---|
| Local work | Read, search and patch files, and run builds, tests and linters, within the sandbox mode (`read-only`, `workspace-write`, `danger-full-access`) and approval settings in force |
| Headless runs | `codex exec`: the prompt comes from an argument or stdin (`-`). Options include `-C` for the working directory, `-s` for the sandbox, `-m` for the model, `-i` to attach an image, `--worktree` for a new managed git worktree, `--output-schema`, `--json`, `-o` for the last message, `--ephemeral` and `--skip-git-repo-check`. Each run is one-shot and carries its own context |
| Sessions | `codex resume` and `codex fork` (also under `codex exec`). `codex agents` browses sessions on the shared local app-server daemon. `codex queue` queues a message for an existing session |
| Review | `codex review` and `codex exec review` |
| MCP | `codex mcp add <name> --url <url>` for streamable HTTP, with an optional `--bearer-token-env-var`, or a stdio command |
| Instructions and reuse | `AGENTS.md`, skills, plugins. A skill supplies instructions and a connected tool supplies data or actions |
| Enabled features (`codex features list`) | `multi_agent`, `goals`, `hooks` and `sleep_tool`, among others. `memories` is off |

### Loops, sleeping and listeners

| Mechanism | Surface | What it does | What it does not do |
|---|---|---|---|
| Active-turn loop | CLI | Inspects state, calls tools, decides and stops on a condition | Anything after the turn ends |
| Sleep tool | CLI | Delays the active turn | Subscribe to an event |
| Running command | CLI | Launches and polls a process while the turn is active | Tell Codex anything once the turn has ended. A process that keeps running does not start a turn |
| Goal mode | CLI and other Codex surfaces that support it | Continues a measurable objective across turns | Wake on an outside event, or stand in for a Daintree notice |
| Hooks | CLI | Run a command, or call an already-connected MCP tool, on Codex lifecycle events such as tool use or session start | Start a model turn, or watch Daintree |
| Scheduled task | ChatGPT / Codex desktop app or web, where enabled. Not the CLI or IDE extension | Starts a task at a time or interval. A local-project task needs the computer and app running | Listen in real time. The schedule is no proof its action succeeded |
| Event-triggered task | ChatGPT web and mobile, on eligible plans | Starts a task on supported Gmail, Slack and GitHub events | Listen to Daintree terminals, PRs or CI in general, or run in the CLI |

**Direct answer:** Codex CLI can run a bounded loop and sleep during an active turn, and
it can write listener software. A schedule or event trigger needs a surface that
provides one, and then it starts a fresh task rather than resuming the CLI session. Any
unattended step needs a mechanism that really re-activates the agent, a way to observe
state on that later turn, and a stopping rule. Without all three, record the step and
say that it needs the user.

### When Codex is the agent being driven

- Daintree launches Codex in a pane with the `agentId` from `agent.listAvailable`. That
  pane has no Daintree connection of its own (above).
- **Usage:** `daintree.agents.check-usage-limits` records `/status` as the read-only view
  and warns never to use `/usage`, a menu that can redeem a banked usage-limit reset. Its
  entry was not re-checked here.
- **Reading it:** use `terminal.getOutput`, since `terminal.readLastMessageOwned` supports
  Claude Code only.
- **Instructions:** Codex reads `AGENTS.md`. The runbook client instruction in
  `DAINTREE_RUNBOOK_CLIENT.md` is installed there or in a profile.

## Source notes

- Project: [common contract](../../src/daintree_assistant_server/runbooks/catalog/core/daintree.foundation.md),
  [host setup](DAINTREE_RUNBOOK_CLIENT.md), [runbook MCP contract](../MCP.md),
  [runbook authoring](../RUNBOOKS.md),
  [captured Daintree MCP surfaces](../../tests/fixtures/daintree_mcp/README.md).
- Daintree app at `cab0d52531`: `docs/architecture/mcp-server.md`,
  `electron/ipc/handlers/terminal/lifecycle.ts`, `electron/services/HelpSessionService.ts`,
  `electron/services/mcp-server/terminalNotify.ts`, `help/AGENTS.md`.
- Codex CLI 0.157.0: `codex --help`, `codex exec --help`, `codex mcp add --help`,
  `codex queue --help`, `codex agents --help`, `codex features list`.
- Official OpenAI documentation, as cited by the earlier revision of this page and not
  re-opened for this one: [MCP](https://learn.chatgpt.com/docs/extend/mcp),
  [long-running work](https://learn.chatgpt.com/docs/long-running-work),
  [scheduled tasks](https://learn.chatgpt.com/docs/automations),
  [hooks](https://learn.chatgpt.com/docs/hooks),
  [notifications](https://learn.chatgpt.com/docs/notifications),
  [plugins](https://learn.chatgpt.com/docs/plugins).
