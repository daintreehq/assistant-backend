---
id: daintree.hosts.foundation
title: Work on another machine
summary: The base judgment for work on a Remote Host this Shell controls — act there through host.call, keep its references with it, run its checks there, drive it yourself or brief its assistant, and read refusals and uncertain outcomes for what they are.
whenToUse: 'Use when the work, or part of it, is on ANOTHER MACHINE — a Remote Host this Shell controls: its projects, worktrees, terminals, agents, checks or assistant; doing something there, asking its assistant, or how controlling a host from here works and how to switch it on. It is the DOMAIN FOUNDATION for every daintree.hosts.* runbook, and comes with every runbook while a host is connected: co-load it with whichever one fits, and load it ALONE only when none does. Do not use for a worktree''s cloud devbox or remote resource (daintree.worktree.manage-resource), or Daintree itself misbehaving on this machine (daintree.diagnostics).'
foundation: true
risk: external
optionalTools:
  - host.list
  - host.call
requires:
  - daintree.foundation
hostEligibility: any
---
A Remote Host runs its own Daintree. `host.list` gives the hosts you may reach and, while one is connected with control on, its projects; `host.call` runs one of its own tools in one of them, under its Tool set, ownership and confirmations:

```json tool=host.call
{"hostId":"<hostId>","projectId":"<projectId>","tool":"terminal.getStatus","args":{"terminalIds":["<terminalId>"]}}
```

- **The work's machine.** Settle per step where it acts. A host's projects, worktrees, terminals, agents and checks are reached only through `host.call`; your own tools and shell act on this machine. Your shell is for what both share: the remote (`git ls-remote`, a fetch) and the forge (`gh`).
- **Host references.** Paths, ids and ports in a host's results mean nothing here: keep each with its `hostId` and `projectId`, used only in a call to that host. Hand work between machines by shared reference: repository, branch, full SHA, PR URL.
- **Checks there.** At Tool set Full, `project.runCheck` or `project.runCommand` (no TTY, never a server) through `host.call`; at Core, or for a check needing judgment, an agent there runs it.
- **Drive or delegate.** Drive a host yourself by default: launch, brief and verify its agents as you would here, each call through `host.call`. Delegate to its assistant only when that assistant's own judgment is the point, or a large batch it should split among its agents.
- **Delegating.** `assistant.list` there: one at its prompt (`agentState` `waiting` at a `prompt`), else one `assistant.start` (`ready:false` still runs: never start another). Send with `terminal.sendCommandOwned`: its `terminalId` and `generation`, `handback:true`, `notify:true`, the brief by reference; end the turn. `ASSISTANT_BUSY` (mid-turn, a draft in its prompt, or stopped on a question for the person there): wait or tell the person, never type over it; `ASSISTANT_RESTARTED`: list again. Its answer, by `terminal.readLastMessageOwned`, is a claim to verify.
- **Notices.** A host's notice names the host and may never come: pair it with `terminal.getStatus` through `host.call`, paced to the work.

Outcomes: `ok` means the call ran, not that the tool succeeded: read `result.isError` and its text, where the host's own refusals arrive (`DRIVEN_ELSEWHERE`: another machine drives that project; report it, never take over). `ambiguous`: it may have run; resend only under its `callId`, which never runs twice. `expired`: check what it would have changed first; never replay an uncertain write. `disconnected`: unreachable; resend once it is back, under the `callId` if it gave one. `opted_out` (assistant control off for that host in Settings, Hosts), `policy_rejected` (that host's "Let other machines' assistants work here" off; an identity refusal says try again) and `unsupported` (its MCP server off, or an older Daintree): quote `detail`; a setting is the person's to change. `rate_limited`: wait `retryAfterMs`. `not_found`: list again.

Report back: per host, what ran there and how you know, its ids, what is pending and what brings it back.
