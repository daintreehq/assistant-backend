---
id: daintree.hosts.verify-project-placement
title: 'Check a host''s project is the intended repository before sending work'
summary: 'Before work goes to a Remote Host, ask the assistant in each candidate project for its remote URLs, full commit SHA, clean state and setup, and say which project, if any, is the intended repository, without trusting names, paths or ids.'
whenToUse: 'Use on the hub when the person wants to know whether a project on another machine, a Remote Host this Shell reaches, is really the repository meant for the work about to be sent, whatever the project is called: a name shared by projects, a renamed folder, a fork where the upstream was meant, a project the host does not list, a checkout that may be stale or dirty. The deliverable is a read-only identity check of each candidate project, from the assistant working in it: its remote URLs against ours, its full commit SHA against the one wanted, clean or dirty, setup in place. Names, paths and ids never decide. Do not use for splitting pieces across hosts by capacity (daintree.hosts.place-work), sending the work itself (daintree.hosts.delegate-to-host), one open question for a host''s assistant (daintree.hosts.ask-host-assistant), readying assistants (daintree.hosts.start-swarm), why one commit behaves differently on two hosts (daintree.hosts.diagnose-host-differences), or adding a machine (daintree.hosts.connect-host).'
risk: external
requiredTools:
  - host.list
  - host.listAssistants
  - host.sendMessage
  - host.getTask
optionalTools:
  - host.listTasks
  - host.startAssistant
---
Read-only. A host's project `name`, `path` and `projectId` are its own hints: equal ones prove nothing across machines, clones and forks inherit an id, and a repeated id lists once, hiding a clone.

1. Pin here the intended `origin` (credentials removed) and the full base SHA (`git ls-remote`), or the person's.
2. Per host, `host.listAssistants`. Every project that could be it is a candidate; several are asked, never chosen by name. `projectsOmitted` above 0: that many unseen, with no page to ask for; an unlisted project is unverified, not missing. A candidate with no `available` lane: unverified; the foundation's step 2, or the person. Never ask a lane in another project to look.
3. One `host.sendMessage` per candidate project, to one available lane at its listed `slot`, no `waitMs`; end the turn:

```json tool=host.sendMessage surface=hub-full
{"hostId":"<hostId>","projectId":"<projectId>","slot":0,"generation":"<generation>","text":"Question only: change nothing, fetch nothing, start no other work. For this project report its root folder, every remote with its URL (credentials removed), current branch, full HEAD SHA, whether <base SHA> is an ancestor of HEAD, tracked and untracked change counts, and whether <setup the task needs> is in place. Say what you cannot tell."}
```

On notice or resume: judge only `completed`; unsent, open or empty is unverified, a missing field unknown. The answer is the lane's claim, attributed; redact URL credentials before relaying.
- Same repository: a credential-free network URL equal to ours in host, port and path (case kept; scheme and `.git` ignored); `file:` and local paths prove nothing. Remote names prove nothing: a match on one other than `origin` is a fork or mirror claim for the person.
- Revision: its SHA equal to the pinned one; else `git merge-base --is-ancestor` if your clone holds it, else its ancestry answer, claimed (`git ls-remote` lists tips only; an unlisted SHA is unknown).
- Dirty or unset-up: findings, not failures.
- Verdict, bound to the project, slot and root that answered: match, match needing a sync, fork, different repository, unverified. A different root for one project, two matches on a host, or none: the person chooses.

Stop and recovery: fetch, reset, clone, setup or a start is a separate step the person authorises.

Report back: per host: project, slot, root, verdict with the remote and SHA seen, choices left open.
