---
id: daintree.hosts.integrate-host-branches
title: Have one host combine what several hosts built
summary: 'From this Shell''s hub, choose which Remote Host combines the work several hosts finished, send its assistant the inputs in order with what each must satisfy, and verify on the shared remote that the result holds exactly those inputs.'
whenToUse: 'Use on the hub, the person''s own Shell, when work that SEVERAL Remote Hosts finished has to end up on ONE branch and one machine is to do the combining: "integrate the branches the remote hosts pushed into one verified branch", "the hosts finished their units; combine them on the designated integration machine", "coordinate one host integrating pinned artifacts from the other lanes", "both my other Macs pushed their parts, get the build box to put them together and tell me if the tests pass". The deliverable is the hub''s side of it: each host''s result checked on the shared remote, one machine chosen and briefed with the inputs in order and what each must still satisfy, and the combined branch it reports verified to contain exactly those inputs, with a source that moved on or a clash brought back to the person to decide. Do not use for one unit of work on one host (daintree.hosts.delegate-to-host), the same work sent to every host (daintree.hosts.swarm-fan-out), combining branches or worktrees on this machine (daintree.git.integrate-agent-branches), or on the host chosen to do the merging (daintree.hosts.integrate-for-hub).'
risk: external
requiredTools:
  - host.list
  - host.listAssistants
  - host.sendMessage
  - host.getTask
optionalTools:
  - host.listTasks
---
Git: a scratch clone of the shared remote (`git clone --filter=blob:none --no-checkout <remote>`), `git fetch --prune` before each check.

1. Inputs: the remote URL and full base SHA the units used; each source's branch, full SHA and acceptance, from its verified reply (`host.getTask`) or the person; the order (the person's, else what others build on first). Base and each pin must be on the remote (`git branch -r --contains <SHA>`); unpushed or missing is no input yet. Keep each source's tip: past its pin, the pin goes in and newer commits only on the person's word.
2. Integrator: the host and project the person named, else an `available` lane in a project named for the repository (`host.listAssistants`; the lane checks its remote); two fit: ask. Name a new integration branch.
3. One `host.sendMessage`: "Follow daintree.hosts.integrate-for-hub", then remote URL and base SHA; inputs in order, each branch, full SHA, acceptance; checks by command; the integration branch; the CLI (the person's, else the lane's `agentId`); push or PR authority and target, none unless the person gave it. End the turn.

On notice or resume:
- `input-required` is a decision: settle it within the person's request, else bring them the inputs and acceptance it touches, the lane's evidence and last good HEAD. Your answer's text names the integration branch.
- `completed`, pushed: `origin/<branch>` is the reported HEAD; `git log --first-parent --reverse --format='%H %P' <base>..<HEAD>` shows only merges, one per input in order, second parent its pin (one already in: `git merge-base --is-ancestor <pin> <HEAD>`); each merge's tree equals `git merge-tree --write-tree <first parent> <pin>`, a settled conflict read with `git show --remerge-diff`. Not pushed: one narrow question naming branch and HEAD for that log. Checks: the forge's on that SHA (`gh`), else each check's exit code at that SHA from the lane; otherwise unverified. Then each source's tip again: changed or gone is a finding, never merged in.

Stop and recovery: one integrator, one send. Never ask for a merge into a protected branch, a force push or a source deletion; a failed or partial integration stays as the lane left it (branch, worktree, SHAs).

Report back: integrator host, project, lane; branch, base and HEAD claimed vs verified (how); per input its pin and merge, or skipped or pending and why; checks and their evidence; moved sources; open decisions.
