---
id: daintree.hosts.race-and-review
title: Race a task across remote hosts, or have another host review one's work
summary: 'From this Shell''s hub, have several Remote Hosts build the same task on their own pushed branches and an agent that built none rank the attempts blind, or have a host that built nothing review another host''s pushed commit, its findings carried back to the builder for a fixed number of rounds.'
whenToUse: 'Use on the hub, the person''s own Shell, when work on Remote Hosts should be JUDGED by a machine or agent that did not build it. A race: "have two of my machines both try this and pick the better one", "best of three across my hosts", "race this across my machines". A cross-review: "get Codex on the build box to review the branch the other Mac just pushed", "have another machine review what that host built", "get a second opinion from another host on that branch", its findings sent back to the builder to fix if the person wants. The deliverable is the hosts'' attempts, each on its own pushed branch, ranked with reasons by an agent that built none of them and saw them unlabelled and shuffled; or one host''s pushed commit reviewed at that SHA by a different host, ideally under another agent CLI, its findings reported as claims and fixed for at most a stated number of rounds; the winner, any merge and branch cleanup stay the person''s. Do not use for the same work on every host with no judging (daintree.hosts.swarm-fan-out), one unit of work or the person''s own branch reviewed on one host (daintree.hosts.delegate-to-host), the hosts'' branches combined into one (daintree.hosts.integrate-host-branches), rival attempts or a review by agents on this machine (daintree.orchestration.competition, daintree.review.review-change), or on a host''s lane, building or reviewing what the hub sent (daintree.hosts.do-delegated-work, daintree.hosts.review-for-hub; several attempts there, daintree.hosts.run-local-cohort).'
risk: external
requiredTools:
  - host.list
  - host.listAssistants
  - host.sendMessage
  - host.getTask
  - agent.launch
optionalTools:
  - host.listTasks
---
Judges and reviewers give findings, never edits; never rank by a builder's reply, merge, force-push or delete a branch.

1. Pin the remote URL, base SHA (`git ls-remote`), run id, the person's criteria and, for fixes, a round cap (2 unless they said). Comparing needs pushed branches: without push authority, ask first. `host.listAssistants`: the builders' lanes; a reviewer's on a host that built none of it, its CLI unlike the builder's if possible.
2. Build (racers; a builder with nothing pushed yet): one `host.sendMessage` each, alike but for unit and CLI: "Follow daintree.hosts.do-delegated-work"; remote, base SHA, run id, unit a neutral letter on branch `<run>/<unit>`, never a host name; objective and criteria verbatim; CLI (the person's, else the lane's `agentId`); checks; push; the round cap; report branch, full SHA, checks. End the turn.
3. Review a SHA `git ls-remote` shows: one message to the reviewing lane: "Follow daintree.hosts.review-for-hub"; remote, branch, full SHA, criteria, run id, CLI; nothing on who built it; the builder's task, if any, in `referenceTaskIds`. One exchange per SHA. End the turn.

On notice or resume:
- Race, each attempt verified or failed (an open one waits unless the person says otherwise): launch one judge here, once, a fresh agent on a CLI no builder used if possible. Brief: clone the remote to a temp dir; per letter, in shuffled order, `git diff <base SHA> <SHA>`, never its log or refs; the criteria; rank with reasons and findings (severity, file:line); change nothing. Its notice is the verdict: report it, never relaunch. Blinding is best effort.

```json tool=agent.launch surface=hub-full
{"agentId":"<CLI>","name":"Judge <run>","prompt":"<blind brief>","notify":true,"handback":true,"replyLines":120}
```

- Findings with fixes asked, one blocking and a round left: to the builder's lane, step 2's brief with unit `<unit>-r<n>` from the reviewed SHA, the findings fenced as another machine's claims to weigh, both tasks in `referenceTaskIds`; then step 3 on its new SHA. Else report.

Stop and recovery: a verdict covers only the SHA it read; a newer tip is unreviewed. At the cap, stop.

Report back: per attempt or round: letter, host, branch, SHA claimed vs verified, checks, builder and reviewer CLIs; the ranking or findings quoted as the judge's or reviewer's; rounds used of the cap; what is open. The pick is the person's.
