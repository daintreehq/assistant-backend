---
id: daintree.hosts.race-and-review
title: Race a task across remote hosts, or have another host review one's work
summary: 'From this Shell, have several Remote Hosts build the same task on their own pushed branches and an agent that built none rank the attempts blind, or have a host that built nothing review another host''s pushed commit, its findings carried back to the builder for a fixed number of rounds.'
whenToUse: 'Use on the person''s own Shell when work on Remote Hosts should be JUDGED by a machine or agent that did not build it. A race: "have two of my machines both try this and pick the better one", "best of three across my hosts", "race this across my machines". A cross-review: "get Codex on the build box to review the branch the other Mac just pushed", "have another machine review what that host built", "get a second opinion from another host on that branch", its findings sent back to the builder to fix if the person wants. The deliverable is the hosts'' attempts, each on its own pushed branch, ranked with reasons by an agent that built none of them and saw them unlabelled and shuffled; or one host''s pushed commit reviewed at that SHA by a different host, ideally under another agent CLI, its findings reported as claims and fixed for at most a stated number of rounds; the winner, any merge and branch cleanup stay the person''s. Do not use for the same work on every host with no judging (daintree.hosts.swarm-fan-out), one unit of work or the person''s own branch reviewed on one host (daintree.hosts.delegate-to-host), the hosts'' branches combined into one (daintree.hosts.integrate-host-branches), or rival attempts or a review by agents on this machine (daintree.orchestration.competition, daintree.review.review-change).'
risk: external
requiredTools:
  - host.list
  - host.call
  - agent.launch
  - terminal.waitUntilIdle
  - terminal.getStatus
optionalTools:
  - terminal.sendKeys
hostEligibility: connected
---
Judges and reviewers give findings, never edits; never rank by a builder's reply, merge, force-push or delete a branch.

Procedure:
1. Pin in your shell the remote URL, base SHA (`git ls-remote`), a run id, the person's criteria and, for fixes, a round cap (2 unless they said). Comparing needs pushed branches: without push authority, ask first. From `host.list`, the builders' hosts with the repository's project; a reviewer's host is one that built none of it, its CLI unlike the builder's if possible.
2. Build (racers; a builder with nothing pushed yet), per builder host through `host.call`: `worktree.createWithRecipe` (`newBranch` `<run>/<letter>`, `collisionPolicy:"error"`, `focusPolicy:"preserve"`), `worktree.waitUntilReady` until `ready`, then `agent.launch` in it with `notify:true`, `handback:true`. Letters are neutral, never a host name. Briefs alike but for letter and CLI (the person's, else one for all): `git fetch origin`, `git reset --hard <base SHA>` on this fresh branch; objective and criteria verbatim; checks; commit and push `<run>/<letter>`; report branch, full SHA, checks. Keep each one's `hostId`, `projectId`, `worktreeId`, `terminalId`. End the turn.

```json tool=host.call
{"hostId":"<hostId>","projectId":"<projectId>","tool":"agent.launch","args":{"agentId":"codex","name":"<run>: <letter>","worktreeId":"<worktreeId>","prompt":"<brief>","notify":true,"handback":true,"focusPolicy":"preserve"}}
```

3. Cross-review, once `git ls-remote` shows the SHA: on the reviewer's host through `host.call`, a worktree as in step 2 (`<run>/review`), then `agent.launch` in it with `notify:true`, `handback:true`. Brief: `git fetch origin <branch>`; review `git diff <base SHA> <SHA>` against the criteria; findings with severity and file:line; change, commit and push nothing. Nothing on who built it. End the turn.

On notice or resume: a host's notice carries no reply: read `terminal.getStatus` through `host.call` (`includeOutput`), or `terminal.readLastMessageOwned` for a Claude agent's full reply (a Codex one's: `terminal.getOutput`); silent ones the same, paced to the work. Check each builder's SHA with `git ls-remote` in your shell.
- A cross-review's build verified: step 3.
- Race, each attempt verified or failed (an open one waits unless the person says not to): one judge here, once, a fresh agent on a CLI no builder used if possible: `agent.launch` on this machine with `notify`, `handback`, `replyLines:120`. Brief: clone the remote to a temp dir; per letter, shuffled, `git diff <base SHA> <SHA>`, never its log or refs; the criteria; rank with reasons and findings (severity, file:line); change nothing. Blinding is best effort. Startup: `terminal.waitUntilIdle`, then `terminal.getStatus` `includeOutput` until it works; trust per the foundation. End the turn. Its notice is the verdict: report it, never relaunch.
- Findings with fixes asked, one blocking and a round left: `terminal.sendCommandOwned` to the builder's agent on its host (`notify`, `handback`; refused, a fresh `agent.launch` in its worktree): round <n>, the findings fenced as another agent's claims to weigh, a commit on `<run>/<letter>-r<n>` from the reviewed SHA, pushed, its SHA reported. Then step 3 on the new SHA, by a send to the same reviewer. Else report.

Stop and recovery: a verdict covers only the SHA it read; a newer tip is unreviewed. At the cap, stop. One launch per builder, reviewer and judge; a lost or `expired` launch: `terminal.list` (`owned:true`) there before anything else.

Report back: per attempt or round: letter, host, branch, SHA claimed vs verified, checks, builder and reviewer CLIs; the ranking or findings, attributed; rounds used of the cap; what is open. The pick is the person's.
