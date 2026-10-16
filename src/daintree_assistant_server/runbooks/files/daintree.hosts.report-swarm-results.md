---
id: daintree.hosts.report-swarm-results
title: Write the final verified report for a run on the Remote Hosts
summary: 'From this Shell''s hub, once a run on the Remote Hosts is over, build one report from the task records and the shared remote: a row per assigned unit with host, branch, exact SHA and checks, what the hub verified kept apart from what each host claimed, false and missing reports called out, and what each host left running or unpushed, with who decides.'
whenToUse: 'Use on the hub when a run SENT to the Remote Hosts has ENDED, or been stopped, and the person wants its FINAL ACCOUNT, trusted: "give me the final report for the run on my other machines", "collect every host''s commits and test results into one summary and what is still unresolved", "which machines really delivered, which only said so, and what did they leave running", "audit the swarm''s completion claims against the evidence". The deliverable is one report with a row for every assigned unit, the failed, expired, unreachable and silent ones too: host, project, branch, exact SHA and checks, hub-verified versus host-claimed, each false or missing report named, and the dev servers, panes, worktrees and unpushed work left behind with who decides. It starts, pushes, publishes and cleans up nothing. Do not use for keeping a run moving while it works (daintree.hosts.supervise-swarm), which exchanges ever finished or what became of work after a restart (daintree.hosts.recover-and-clean-up), a Remote Host''s own assistant answering the hub (daintree.hosts.handle-hub-message, daintree.hosts.steer-for-hub), stopping or changing it (daintree.hosts.steer-swarm), one host''s result before the next step (daintree.hosts.delegate-to-host), agent usage left on the hosts (daintree.hosts.check-usage-limits), why hosts behave differently (daintree.hosts.diagnose-host-differences), combining their branches (daintree.hosts.integrate-host-branches), one agent on this machine (daintree.orchestration.verify-agent-work), dated repository activity (daintree.git.summarize-activity) or why a local run failed (daintree.agents.postmortem-run).'
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

1. The run's units: your rows (run and unit ids, `taskId`s), else the person's list. `host.listTasks` shows exchanges, not whose run: records hold no text, so ask. Every unit gets a row: failed, expired, never sent and silent too.
2. Per host `host.listTasks` from its cursor, every page, then `host.getTask` each unit: state, `outcomeCode`, `delivery` times, replies. `repliesOmitted` or a `replyReceivedAt` with `replies` empty: the text is gone, not that nothing came; `not_found`: purged. `completed` naming no branch, SHA or check: a missing report.
3. Check each claim yourself, by reference: `git ls-remote` shows the claimed full SHA on its branch, the base is its ancestor, the diff fits the acceptance, checks are the forge's on that SHA (`gh`), else the host's command and exit code. A branch absent, another SHA or a failing check it called passing: a false claim, quoted beside what you found. Anything else stays unverified, not false.
4. For gaps and leftovers, one read-only `host.sendMessage` per lane covering its units (fresh address, `host.listAssistants`), its `taskId` and purpose kept in the rows; `taskId: null` is a gap in the report. End the turn:

```json tool=host.sendMessage surface=hub-full
{"hostId":"<hostId>","projectId":"<projectId>","slot":0,"generation":"<generation>","contextId":"<contextId>","text":"Report only; change, stop, close, push and upload nothing. Per unit <units>: repository, branch, full SHA, each check's command, exit code and failing names, unpushed commits, dirty files, and every pane, worktree, dev server (port, PID, start command) and process this run left. Detail over a reply's limit: a branch, PR or reference already authorised."}
```

On notice or resume: reconcile (foundation); step 3 on what returned. A host-local report path is a locator: one more question to that lane for it, never opened here. An answer that stays out: unavailable.

Stop and recovery: one question per lane, never resent; no `host.cancelTask`. A silent lane is reported with its `delivery` times.

Report back: a row per unit: host, project, lane, repository, branch, SHA, checks; verified, claimed, contradicted or missing, and how. Then what is left (panes, worktrees, services, unpushed work), each with its owner and the decision waiting.
