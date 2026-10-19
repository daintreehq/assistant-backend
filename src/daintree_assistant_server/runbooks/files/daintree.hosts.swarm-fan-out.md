---
id: daintree.hosts.swarm-fan-out
title: Send the same work to every remote host
summary: 'From this Shell, give an agent on every chosen Remote Host the same brief at once, each on its own run branch, end the turn, and bring back one table with a row per host, each result verified by artifact and any host still open named as open.'
whenToUse: 'Use when the person wants the SAME WORK done on EVERY Remote Host this Shell reaches, or on the several they name, one copy per machine: "run this on all my machines", "have each machine try this", "have every host build the parser on its own branch and show me what each got", a fix, feature or experiment replicated across their other Macs or servers at once. The deliverable is one table with a row per host (project, agent CLI, branch, commit SHA, checks, pane), each result verified on the shared remote or by a narrow follow-up question, and any host still working or not reached shown as such, never folded into one overall success. Do not use for one unit on one host, a hand-over between two, or readying an assistant on each host with no work sent (daintree.hosts.delegate-to-host), agent usage left on each host (daintree.agents.check-usage-limits), one question to one host''s assistant (daintree.hosts.ask-host-assistant), what became of work already sent (daintree.hosts.recover-and-clean-up) or stopping or changing it (daintree.hosts.steer-swarm), independent units for agents on this machine (daintree.orchestration.fan-out-work), or the hosts'' finished branches combined into one (daintree.hosts.integrate-host-branches); one migration carried through several repositories, daintree.hosts.migrate-across-repositories; a list of different pieces, one host each, is daintree.hosts.place-work.'
risk: external
requiredTools:
  - host.list
  - host.call
hostEligibility: connected
---
One brief to every chosen host at once, each on its own branch; one table back. Different pieces per host, a split suite or an environment matrix is not this: say so.

Procedure:
1. Rows: the hosts the person named, else every one in `host.list`; each keeps a row. Not `connected`, or `control` off: that row says so, no retry. Per host, the project for the work's repository from its `projects`; none: a gap, never a clone; two fit: ask.
2. Pin in your shell: the remote URL, the base SHA (`git ls-remote`), a fresh run id; per host a unit id naming host and project, and its branch `<run>/<unit>`, absent from the remote.
3. Through `host.call`, every host's worktree first, then each one's readiness, then each launch, waiting on no agent: `worktree.createWithRecipe` (`newBranch` `<run>/<unit>`, `collisionPolicy:"error"`, `focusPolicy:"preserve"`, no recipe); `worktree.waitUntilReady` until `ready` (`failed` or `needs-approval` is that row's finding); `agent.launch` in it. CLI: the person's for that host, else one for all; `spawnStatus:"missing-cli"` is that row's finding, never a silent swap. The prompt is the same but for unit, branch and CLI:
   - Objective: the person's words, with checkable acceptance.
   - Start: `git fetch origin`; `origin` must be <remote>, else stop and say so; `git reset --hard <base SHA>` on this fresh branch.
   - Scope: this worktree only; checks by exact command, or none; commit; push the branch or open a PR only if the person said.
   - Report: branch, full SHA, each check's command and exit code; stop and ask on a mismatch or doubt.

```json tool=host.call
{"hostId":"<hostId>","projectId":"<projectId>","tool":"agent.launch","args":{"agentId":"claude","name":"<run>: <unit>","worktreeId":"<worktreeId>","prompt":"<brief>","notify":true,"handback":true,"focusPolicy":"preserve"}}
```

   Keep each row's `hostId`, `projectId`, `worktreeId`, `worktreePath` and `terminalId`. End the turn once every reachable host has its launch.

On notice or resume: a host's notice carries no reply. Per host with open rows, one `terminal.getStatus` through `host.call` for its terminals, `includeOutput`: the `lastHandback` for its prompt is the claim (`terminal.readLastMessageOwned` for a Claude agent's exact text; a Codex one's: `terminal.getOutput`). Rows with no notice get the same read, paced to the work. A question inside the person's scope: answer it with `terminal.sendCommandOwned` there (`notify`, `handback`); else ask them.
- Verify each claim. Pushed: in your shell, `git ls-remote` shows the claimed SHA on its branch; fetch it and `git diff --stat <base SHA> <SHA>`. Not pushed: at Full, `project.runCommand` in its `worktreePath` (`git log -1 --stat`, `git status --porcelain`, each check as given); at Core, one narrow follow-up send asking for exactly that, its answer marked reported.
- End the turn while any row is working. One still working once the rest are in: open, never failed.

Stop and recovery: one launch per host. A lost or `expired` launch: `terminal.list` (`owned:true`) there before anything else; never relaunch on a guess. A host that drops keeps its agent running: read it again when `host.list` shows it back. Close panels (`terminal.closeOwned`) or delete worktrees (`worktree.deleteOwned`) there only when asked; after this assistant restarts those are refused `RESOURCE_NOT_OWNED`: the person, or that host's own assistant, closes them.

Report back: one table, a row per chosen host: project, CLI, branch, SHA claimed vs verified and how, checks you ran vs reported, terminal id. Side by side, no winner. "All done" only when every row is verified.
