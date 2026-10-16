---
id: daintree.hosts.report-swarm-results
title: Write the final verified report for a run on the Remote Hosts
summary: 'From this Shell''s hub, once a run on the Remote Hosts is over, build one report from the task records and the shared remote: a row per assigned unit with host, branch, exact SHA and checks, hub-verified kept apart from host-claimed, false and missing reports called out, and what each host left running or unpushed, with who decides.'
whenToUse: 'Use on the hub when a run SENT to the Remote Hosts has ENDED, or been stopped, and the person wants its FINAL ACCOUNT, trusted: "give me the final report for the run on my other machines", "collect every host''s commits and test results into one summary and what is still unresolved", "which machines really delivered, which only said so, and what did they leave running", "audit the swarm''s completion claims against the evidence". The deliverable is one report with a row for every assigned unit, the failed, expired, unreachable and silent ones too: host, project, branch, exact SHA and checks, hub-verified versus host-claimed, each false or missing report named, and the dev servers, panes, worktrees and unpushed work left behind with who decides. It changes nothing. Do not use for keeping a run moving while it works (daintree.hosts.supervise-swarm), which exchanges ever finished or what became of work after a restart (daintree.hosts.recover-and-clean-up), a Remote Host''s own assistant answering the hub (daintree.hosts.handle-hub-message, daintree.hosts.steer-for-hub), stopping or changing it (daintree.hosts.steer-swarm), one host''s result before the next step (daintree.hosts.delegate-to-host), agent usage left on the hosts (daintree.hosts.check-usage-limits), why hosts behave differently (daintree.hosts.diagnose-host-differences), combining their branches (daintree.hosts.integrate-host-branches), one agent on this machine (daintree.orchestration.verify-agent-work), dated repository activity (daintree.git.summarize-activity) or why a local run failed (daintree.agents.postmortem-run).'
risk: external
requiredTools:
  - host.listTasks
  - host.getTask
  - host.sendMessage
optionalTools:
  - host.list
  - host.listAssistants
---
A report only: no new work, push, publication or cleanup comes from it; a lane is asked to read, never to change.

1. The run's units: your rows (run and unit ids, `taskId`s), else the person's list. Records hold no text, so `host.listTasks` cannot say whose run: ask. Every unit gets a row: failed, expired, never sent and silent too.
2. Per host `host.listTasks`, every page, then `host.getTask` each unit: state, `outcomeCode`, `delivery` times, replies. `repliesOmitted` or a `replyReceivedAt` with `replies` empty: the text is gone, not that nothing came; `not_found`: purged. `completed` naming no branch, SHA or check: a missing report.
3. Check each claim yourself, by reference: the claimed full SHA descends from the base and its diff fits the acceptance, checks are the forge's on that SHA (`gh`), else the host's command and exit code. The claimed SHA on no remote branch (`git branch -r --contains`, after a fetch), or a check it called passing failing there: a false claim, quoted beside what you found. A branch since moved on: moved, with times. Anything else is unverified.
4. For gaps and leftovers, one read-only `host.sendMessage` per lane covering its units (fresh address, `host.listAssistants`), its `taskId` kept in the rows; `taskId: null` is a gap. End the turn:

```json tool=host.sendMessage surface=hub-full
{"hostId":"<hostId>","projectId":"<projectId>","slot":0,"generation":"<generation>","contextId":"<contextId>","text":"Report only; change, stop, close, push and upload nothing. Per unit <units>: repository, branch, full SHA, each check's command, exit code and failing names, unpushed commits, dirty files, and every pane, worktree, dev server (port, PID, start command) and process this run left. Detail over a reply's limit, or a report path an earlier reply named: its bounded contents, or a branch, PR or reference already authorised."}
```

On notice or resume: reconcile (foundation); step 3 on what returned. A host-local path is only a locator, never opened here. What stays out: unavailable.

Stop and recovery: one question per lane, never resent; no `host.cancelTask`. A silent lane: its `delivery` times.

Report back: a row per unit: host, project, lane, repository, branch, SHA, checks; verified, claimed, contradicted or missing, and how. Then what is left (panes, worktrees, services, unpushed work), each with its owner and the decision waiting.
