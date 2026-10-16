---
id: daintree.hosts.postmortem-swarm
title: Post-mortem a run on the Remote Hosts from its records
summary: 'From the hub, once a run on the Remote Hosts has failed, stalled or disappointed, rebuild a timestamped timeline per unit from the exchange records, this conversation and the shared remote, mark what was never observed, check each host''s claim by artifact, name the first evidenced divergence and its causes by class, and propose fixes each with a way to reproduce it, replaying nothing.'
whenToUse: 'Use on the hub when a run on the Remote Hosts is OVER and went badly, and the person wants the post-mortem: why it took so long, stalled or did the same work twice, whether the delay was a person approving each host by hand or the machines, which hosts'' reports turned out wrong, what to change so it does not happen again. The deliverable is the evidence: a timeline per unit with unobserved moments marked, the first evidenced divergence, each cause named by class (the brief or routing, authority and approvals, the transport or app, the agent, a host''s repository setup, verification) with the times that show it, each host''s claim checked by branch and SHA, salvageable commits and leftovers listed, and fixes each with a reproducible scenario. It restarts, resends, cancels, cleans up and files nothing. Do not use for following a run still under way (daintree.hosts.supervise-swarm), catching up or cleaning up (daintree.hosts.recover-and-clean-up), finding which gate blocks messaging now (daintree.hosts.diagnose-messaging), reproducing why the same code behaves differently on two hosts (daintree.hosts.diagnose-host-differences), one agent on this machine (daintree.agents.postmortem-run) or a production incident (daintree.debug.investigate-outage).'
risk: external
requiredTools:
  - host.listTasks
  - host.getTask
optionalTools:
  - host.list
  - host.listAssistants
  - host.sendMessage
---
Evidence only: nothing is resent, restarted, cancelled, cleaned up, fixed or filed from here. An elapsed time is not a cause.

1. The run: your rows and this conversation (briefs, `taskId`s, the SHA each unit was sent), else ask the person which exchanges. Per host `host.listTasks` from cursor 0 (`reset`: some purged), every page, then `host.getTask` each unit (foundation). Records last about a week; reply text is gone after a day or a host restart. `not_found`, empty `replies` and `repliesOmitted` limit the finding; they never mean nothing happened.
2. One timeline row per unit: sent (yours), `acceptedAt`, `wakeTypedAt`, `inboxFetchedAt`, each reply's `state` and `at` (`input-required` is a question), `replyReceivedAt`, your check. Stamps are that host's clock: compare within one host, never across hosts or with yours. Null is unobserved; give gaps as durations. Past the records, attribute: "<host> said".
3. Check each claim at the SHA it names: on its remote branch (`git ls-remote`), the sent base its ancestor (`git merge-base`), the forge's checks on that SHA (`gh`). Absent, not descended from the base or failing: contradicted, quoted beside what you found. A branch that moved since proves nothing; the rest stays unverified.
4. If a gap or leftover needs the lane (Tool set Full): one read-only `host.sendMessage` per lane, `taskId` kept, asking what it waited on between two stamps, its checkout and setup then, the commit SHAs it holds, pushed or not, and each pane, worktree or process it left. End the turn; on the notice reconcile (foundation) and redo step 3. Without Full, or a lane gone or restarted, the finding is limited; start nothing.

Classify the first divergence from the plan, then each contributing factor: brief or routing; authority (a person's approval or a refusal: the records have no field for it, so the lane or the person says so, and a wait alone is not); transport or app (`outcomeCode`, `ambiguousAt`, a lane lost); agent; repository setup; verification. Per fix: instruction text or app change, and a scenario to reproduce it.

Stop and recovery: one question per lane, never resent; no `host.cancelTask`. No public issue or comment unless asked.

Report back: per unit the timeline, sources and unobserved marks; the first divergence; causes by class; claims verified versus contradicted; salvageable commits and leftovers with owners; fixes with scenarios; what is lost.
