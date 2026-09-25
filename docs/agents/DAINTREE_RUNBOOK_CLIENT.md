# Using Daintree runbooks from Claude Code and Codex

This page is for people wiring Claude Code or Codex up to Daintree. It says which two
MCP connections are involved, and gives one short instruction to install in the
host, so it looks up runbooks for Daintree work.

It is **not** maintainer guidance for this repository (that is the root
`CLAUDE.md`/`AGENTS.md`), and it is not meant to be pasted into every project.

## Two connections, two jobs

| connection | what it gives the agent | how it is added |
|---|---|---|
| `daintree-runbooks` | instructions: `search_runbooks`, `get_runbook` | by the user, once per host (below) |
| `daintree` | workspace actions and notices: `agent.launch`, `terminal.getOutput`, `terminal.notifyWhenIdle`, … | by Daintree itself, per agent pane (`--mcp-config`), or an API-key client the user configures |

Installing the runbook server gives an agent **no** workspace control and **no**
notices. Notices reach an agent because Daintree types a line into the prompt of
the pane that asked for them. An API-key client has no pane to type into, so it gets
no `notify` argument and no `terminal.notifyWhenIdle`.

Which hosts get the full experience (Daintree app `feature/mcp-cleanup` at
`cab0d52`, the build `tests/fixtures/daintree_mcp` was captured from):

- **Claude Code in a Daintree pane**: Daintree writes the pane's own `daintree`
  connection when the project's Daintree MCP setting is not `off`. The setting
  defaults to `off`. This connection has the full pane surface and notices.
- **Codex**: Daintree gives a Codex pane no connection of its own in this build. Codex
  reaches Daintree as the sidebar assistant, which gets notices, or as an API-key
  client the user configures, which gets none. Runbooks tell an API-key caller to use
  a bounded wait or ask the user to continue.
- **Anything else** connected with an API key: the curated `external` surface, with
  no notices.

### Adding the runbook server

```bash
# Claude Code (pick the scope you want; see `claude mcp add --help`)
claude mcp add --transport http daintree-runbooks https://assistant.daintree.org/v1/daintree/mcp

# Codex
codex mcp add daintree-runbooks --url https://assistant.daintree.org/v1/daintree/mcp
```

Check the installed client's `--help` and configuration scope before changing
someone's existing setup. Never put a Daintree bearer in a committed config or an
example. The pane connection's bearer belongs to Daintree.

## The client instruction

Install this text with the host's own instruction mechanism: a user-level
`CLAUDE.md` for Claude Code, an `AGENTS.md` or profile instructions for Codex. Get
the user's consent, and don't overwrite unrelated instructions.

```text
For Daintree workspace or agent-orchestration tasks, search the connected
Daintree runbook service with a concise, self-contained description of the
requested outcome (max_results 2-3). Follow a result marked selected; do not
execute neighbouring results merely because search returned them. Read the
supporting_runbooks (the foundation) before following a procedure, and use
get_runbook for a runbook you already know by id.

The runbook service supplies instructions, not workspace actions or permission.
Use the separate Daintree app MCP and inspect its actual surface and schemas.
Resolve exact targets from current context and listings. Respect owned-resource
restrictions and the app's confirmations. Do not invent absent tools or arguments.

Use your native engineering tools for ordinary authorized local work. Use Daintree
agent panes when the user asks for visible, named, independent or parallel agents.
For a child response that must be collected, prefer a supported Daintree notice.
Record the task and returned identities, then end your turn when no immediate work
remains.

On a notice or resume, restore the checkpoint, apply the user's latest instructions,
read fresh state and output, and verify the expected result before advancing. A
stopped agent may be asking a question or showing an error. A notice, handback or
successful send is not proof the work succeeded. Do not replay uncertain or
completed writes.

When no supported wake exists, use a bounded active-turn check or state that the
remaining work needs a later user continuation. Never promise unattended progress
from a checkpoint alone. Keep all action and cleanup within the user's scope.
```

## What a host adapter needs to settle

The runbooks are host-neutral. Each host differs only in these four things, and the
runbooks leave them to the host:

1. **How tools are called.** Daintree lists action ids as its wire names
   (`terminal.getOutput`). A host may show them under a server prefix (Claude Code
   uses `mcp__<server>__…`), and may rewrite characters such as the dots. Call the
   name the host actually lists. The action id is what a runbook names.
2. **How instructions are installed.** See above.
3. **Where a checkpoint can live.** Runbooks that span several activations keep a small
   JSON record. The default is `.daintree/runbook-runs/<run-id>.json` in a stable
   coordinator checkout, kept out of git (e.g. via `.git/info/exclude`), or a host
   scratch directory whose persistence matches the task. See the foundation runbook.
4. **Which continuation mechanisms really exist.** Daintree notices, when the pane is
   offered them. Anything else must be a verified host feature: a Claude Code
   monitor or scheduled task, or a Codex environment's own facility. The dated
   inventories in [`CLAUDE_CODE.md`](CLAUDE_CODE.md) and [`CODEX.md`](CODEX.md)
   describe what particular environments had. They are optional extras, and no
   runbook requires them. The primary path must work because Daintree reactivates
   the pane, not because a host monitor happened to cover for a broken notice.
