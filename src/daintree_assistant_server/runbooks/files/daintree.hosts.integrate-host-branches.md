---
id: daintree.hosts.integrate-host-branches
title: Have one host combine what several hosts built
summary: 'From this Shell, choose which Remote Host combines the work several hosts finished, send it the inputs in order with what each must satisfy, and verify on the shared remote that the result holds exactly those inputs.'
whenToUse: 'Use from the person''s own Shell when work that SEVERAL Remote Hosts finished has to end up on ONE branch and one machine is to do the combining: "integrate the branches the remote hosts pushed into one verified branch", "the hosts finished their units; combine them on the designated integration machine", "coordinate one host integrating pinned artifacts from the other hosts", "both my other Macs pushed their parts, get the build box to put them together and tell me if the tests pass". The deliverable is this Shell''s side of it: each host''s result checked on the shared remote, one machine chosen and briefed with the inputs in order and what each must still satisfy, and the combined branch it reports verified to contain exactly those inputs, with a source that moved on or a clash brought back to the person to decide. Do not use for one unit of work on one host (daintree.hosts.delegate-to-host), the same work sent to every host (daintree.hosts.swarm-fan-out), or combining branches or worktrees on this machine (daintree.git.integrate-agent-branches).'
risk: external
requiredTools:
  - host.list
  - host.call
hostEligibility: connected
---
You pin the inputs, pick the machine and verify on the shared remote; on that machine the merge runs as daintree.git.integrate-agent-branches teaches (`get_runbook` if absent), each of its Daintree calls through `host.call` there. Git in your own shell: a scratch clone of the remote (`git clone --filter=blob:none --no-checkout <remote>`), `git fetch --prune` before each check.

Procedure:
1. Inputs: the remote URL and full base SHA the units used; each source's branch, full SHA and acceptance, from its verified result or the person; the order (the person's, else what others build on first). Base and each pin must be on the remote (`git branch -r --contains <SHA>`); unpushed or missing is no input yet. Keep each source's tip; past its pin, newer commits join only on the person's word.
2. Integrator: the host and project the person named, else a connected host whose `host.list` projects hold that repository; two fit: ask. Name a new branch `integrate/<name>`. Push or PR authority and target: none unless the person gave it.
3. Drive it there by integrate-agent-branches' steps 3-4: on the integrator, the integration worktree from the base SHA, then its ONE merge agent with that runbook's merge brief verbatim, plus: "First `git fetch origin` and confirm `origin` is <remote>; the sources are these SHAs on it, in order: <branch, SHA, acceptance>…; checks: <commands>."

```json tool=host.call
{"hostId":"<hostId>","projectId":"<projectId>","tool":"worktree.createWithRecipe","args":{"source":{"kind":"newBranch","branchName":"integrate/<name>","baseBranch":"<base SHA>","collisionPolicy":"error"}}}
```

   Delegate it to that host's assistant instead only when the person asks for its judgment there: the same inputs, "Follow daintree.git.integrate-agent-branches", as the hosts foundation says. End the turn.

On notice or resume:
- A held-back conflict or a question outside the brief is a decision: settle it within the person's request, else bring them the inputs and acceptance it touches, the agent's evidence and its last good HEAD.
- Its report in, with push authority: one send there, `notify:true, handback:true`: "Push `integrate/<name>` to origin, nothing else; reply with the pushed SHA."
- The report is a claim. Pushed: `origin/integrate/<name>` is the reported HEAD; `git log --first-parent --reverse --format='%H %P' <base>..<HEAD>` shows only merges. Walk pins in order from `<cur>`=`<base>`: skip one `git merge-base --is-ancestor <pin> <cur>` passes; else the next merge's parents are `<cur> <pin>`, and it becomes `<cur>`; end at HEAD. A clean merge's tree equals `git merge-tree --write-tree <first parent> <pin>`; a settled conflict: `git show --remerge-diff <merge>`. Unpushed: that log there by `project.runCommand` (`cwd` the integration worktree) at Tool set Full, else asked of the agent, a claim. Checks: the forge's on that SHA (`gh`), else `project.runCheck` or `project.runCommand` there at that HEAD, else the agent's exit codes as claims, else unverified. Then each source's tip again: changed or gone is a finding.
- A source readied later: one send to the same agent with its pinned SHA, `notify:true, handback:true`.

Stop and recovery: one integrator, one merge agent; before any launch, `terminal.list` there. Never a merge into a protected branch, a force push or a source deletion; a failed or partial integration stays as the agent left it (branch, worktree, SHAs).

Report back: integrator (host, project, worktree, terminal; driven or delegated); branch, base, HEAD claimed vs verified and how; per input its pin and merge, skipped or pending, why; checks with evidence; moved sources; open decisions.
