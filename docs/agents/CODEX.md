# Codex

Capability inventory, observed and checked on 25 September 2026. It is intended to inform later runbook design for Codex and other agents. A capability can be built into this session, supplied by a connected tool or app, or implemented in external software. Those are different promises. Access also depends on the host, permissions and the user's requested scope.

## Core work I can perform

| Capability | What that means in this session |
|---|---|
| Understand and plan | Interpret natural language, compare options, break a goal into steps, track constraints, and explain a decision. I can revise the plan when new evidence or user steering arrives. |
| Read and investigate | Search and read repository files, docs, logs and test output; trace code paths; compare checkouts; inspect schemas and handlers. I can read the sibling worktree without changing it. |
| Write and change artifacts | Edit code, Markdown, configuration and other local files with patches or shell tools; create scripts and documents; inspect the resulting diff. This session's write scope is the user's requested worktree. |
| Run and verify | Execute shell commands, builds, tests and linters; inspect Git status and diffs; debug failures and re-run a focused check. I can distinguish a tool call succeeding from the requested outcome succeeding. |
| Research | Search and open web pages, check current official documentation, cite sources, and state when evidence is missing. |
| Use connected systems | Discover and call available MCP, plugin and app tools. This session offers GitHub and other app connectors, but no Daintree control-plane tools. Each action remains subject to the tool's permission and the user's scope. |
| Use a browser or computer | Where the host exposes it, inspect and operate visible UI, including browser tabs and native apps. UI state must be observed; I cannot assume a click succeeded. |
| Work with media and formats | Inspect images and, when useful, generate bitmap images. Installed workflows also support documents, PDFs, presentations, spreadsheets and websites; each depends on its tools and artifact type. |
| Sequence and coordinate | Make dependent calls in order, batch independent calls, keep a working state in the chat, and accept mid-turn corrections. Subagents are possible where the host and task allow delegation. |
| Resume from recorded state | Re-read the conversation and available files or connected records, reconcile them with current state, and continue. I do not have an inherent durable Daintree workflow ledger; persistence needs a file, connected service or host feature. |
| Communicate | Give progress updates, ask for missing information, and report what was observed, changed, tested or left unresolved. I can keep a user in the loop while work runs. |
| Notify | Reply in this chat and use host notification features where configured. A finished turn does not by itself create a future message or event subscription. |
| Use reusable guidance | Follow `AGENTS.md` and installed skills, and use connected MCP servers and plugins. A skill supplies instructions; a connected tool supplies data or actions. |

These are task capabilities, not blanket authorization. I can inspect an available tool's contract and use it for the requested outcome. I cannot invoke a tool that the host has not connected, infer unobserved state, or treat a runbook as permission to widen scope.

## Loops, sleeping and listeners

| Mechanism | What Codex can do | What it does **not** establish |
|---|---|---|
| Active-turn loop | Repeatedly inspect state, call tools, decide what to do next and stop on a condition. I can wait briefly with a sleep/wait tool or poll a running command while this turn remains active. | No durable watch after the turn ends. Sleeping only delays the active turn; it does not subscribe to an event. |
| Running command | Launch and poll a process, including a long-running script, while the process and host are available. | A process continuing in the OS does not itself send a new observation to the model or guarantee a later Codex turn. |
| Goal mode | Continue a measurable objective across turns in supported Codex surfaces, subject to the same access and approvals. | A Daintree terminal watcher, event subscription or permission grant. |
| Scheduled task | In the ChatGPT desktop app or web, where enabled, create a time-based task; an in-chat schedule can revisit context at minute-based intervals. A local-project task requires the computer and desktop app to be running. Codex CLI and the IDE extension do not provide the Scheduled management interface. | A real-time listener. Each run must observe fresh state, and a schedule is not evidence that its action succeeded. |
| Event-triggered scheduled task | On eligible ChatGPT web/mobile plans, supported Gmail, Slack and GitHub events can start a task. | A general Daintree terminal/PR/CI listener, or an event trigger in Codex CLI/IDE/desktop. Availability depends on the connected app and workspace. |
| Codex hook | A configured lifecycle hook can run a command or call an already-connected MCP tool on supported Codex events, such as tool use or session start. | A continuously running model or a subscription to arbitrary Daintree events; hook coverage and timing are limited to the configured Codex lifecycle. |
| Daintree watcher/async/timer | Codex could select the [watch-outcome runbook](../../src/daintree_assistant_server/runbooks/files/daintree.orchestration.watch-outcome.md) and use these mechanisms **if their Daintree tools are connected**. | These facilities are not provided by `search_runbooks`, nor are they present in this session. They have different wake, action and note semantics; a timer ID alone proves only setup. |

**Direct answer:** I can construct a bounded loop and sleep during active work. I can write listener software; a schedule or event trigger requires a host that provides one. This session has no native Daintree watcher or durable callback that wakes me when a Daintree terminal changes. Any unattended workflow needs a separately running mechanism, an observation tool on the later turn, and a stopping rule.

## Daintree-specific availability

The [runbook-search worktree](../../../feature-runbook-search-endpoint/docs/MCP.md) defines one read-only MCP tool, `search_runbooks`. It returns ranked, full runbooks with `selected`, `score`, `foundation` and `required_tools`. It does not execute steps or supply the Daintree CLI actions named in them. The catalog is the Markdown files in [`runbooks/files`](../../src/daintree_assistant_server/runbooks/files); the current authoring guidance is in [`RUNBOOKS.md`](../RUNBOOKS.md).

I inspected that service's docs and implementation in the sibling worktree, but `search_runbooks` is not connected in this session, so I did not make a live MCP call. The Daintree CLI wrappers such as `context.snapshot`, `terminal.await.async`, `watcher.terminal.create`, `timer.schedule`, `grant.create`, `workflow.*` and `agentTask.spawnForEdits` are also absent here. If a future host connects them, Codex can use the corresponding capabilities after inspecting their live contracts. The [runbook audit](../RUNBOOK_AUDIT.md) describes the current Daintree watcher, timer, async and grant distinctions.

## Source notes

- Project: [new MCP contract](../../../feature-runbook-search-endpoint/docs/MCP.md), [runbook authoring](../RUNBOOKS.md), [catalog audit](../RUNBOOK_AUDIT.md), [watch-outcome runbook](../../src/daintree_assistant_server/runbooks/files/daintree.orchestration.watch-outcome.md).
- Official OpenAI documentation: [MCP](https://learn.chatgpt.com/docs/extend/mcp), [long-running work](https://learn.chatgpt.com/docs/long-running-work), [scheduled tasks](https://learn.chatgpt.com/docs/automations), [hooks](https://learn.chatgpt.com/docs/hooks), [notifications](https://learn.chatgpt.com/docs/notifications), [plugins](https://learn.chatgpt.com/docs/plugins).
