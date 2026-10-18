---
id: daintree.hosts.verify-project-placement
title: 'Check a host''s project is the intended repository before sending work'
summary: 'Before work goes to a Remote Host, read in each candidate project its remote URLs, full commit SHA, clean state and setup, and say which project, if any, is the intended repository, without trusting names, paths or ids.'
whenToUse: 'Use when the person wants to know whether a project on another machine, a Remote Host this Shell reaches, is really the repository meant for the work about to be sent, whatever the project is called: a name shared by projects, a renamed folder, a fork where the upstream was meant, a project the host does not list, a checkout that may be stale or dirty. The deliverable is a read-only identity check of each candidate project, read on that host: its remote URLs against ours, its full commit SHA against the one wanted, clean or dirty, setup in place. Names, paths and ids never decide. Do not use for splitting pieces across hosts by capacity (daintree.hosts.place-work), sending the work itself or readying an assistant for it (daintree.hosts.delegate-to-host), listing the hosts, projects and what runs there (daintree.hosts.survey-hosts), one open question for a host''s assistant (daintree.hosts.ask-host-assistant), why one commit behaves differently on two hosts (daintree.hosts.diagnose-host-differences), or adding a machine (daintree.hosts.connect-host).'
risk: external
requiredTools:
  - host.list
  - host.call
hostEligibility: connected
---
Read-only: nothing is fetched, reset, set up or started. A project's `name` and `projectId` are that host's own hints: equal ones prove nothing across machines, and clones and forks share names.

Procedure:
1. In your own shell pin the intended `origin` (credentials removed) and the full base SHA (`git ls-remote`), or the person's.
2. `host.list`: per host, every project in its `projects` that could be it is a candidate; several are all read, never chosen by name. `projects` holds at most 50, most used first: an unlisted project is unverified, not missing. Not `connected`, or `control:false`: that host is unverified; say why.
3. Read each candidate there. At Full, one `project.runCommand` per candidate (at most 4 at once per host):

```json tool=host.call surface=help-full
{"hostId":"<hostId>","projectId":"<projectId>","tool":"project.runCommand","args":{"projectId":"<projectId>","command":"git rev-parse --show-toplevel; git remote -v; git branch --show-current; git rev-parse HEAD; git status --porcelain | wc -l; git merge-base --is-ancestor <base SHA> HEAD; echo ancestor=$?; <setup check the task needs>","timeoutMs":60000}}
```

   At Core: one `agent.launch` there with `waitForReply:true, handback:true`, a read-only brief to run exactly those commands and paste their output verbatim, then `terminal.getOutput` on it for the raw lines: its summary is a claim. A host where no agent can run: its assistant, as the hosts foundation says, asked the same. Never read one project from another.
4. Judge only output you hold; a missing line is unknown. Redact URL credentials before relaying.
   - Same repository: a credential-free network URL equal to ours in host, port and path (case kept; scheme and `.git` ignored); `file:` and local paths prove nothing. Remote names prove nothing: a match on one other than `origin` is a fork or mirror claim for the person.
   - Revision: HEAD equal to the pinned SHA; else `ancestor=0` means HEAD contains it, `1` that HEAD lacks it (behind or diverged), `128` that the clone lacks the commit: unknown, never fetched here.
   - Dirty or not set up: findings, not failures.
   - Verdict, bound to the project and root that answered: match, match needing a sync, fork, different repository, unverified. Two matches on a host, a root other than expected, or none: the person chooses.

Stop and recovery: one read per candidate; a timed-out or refused read is unverified, read again only on the person's word. Fetch, reset, clone, setup or a start is a separate step the person authorises. A read-only agent you launched stays open until they ask.

Report back: per host and candidate: project, root, remotes and HEAD seen, clean or dirty, setup, how it was read (command, or agent and `terminalId`), verdict; choices left open.
