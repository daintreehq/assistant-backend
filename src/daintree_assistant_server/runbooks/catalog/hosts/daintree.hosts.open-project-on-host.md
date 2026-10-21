---
id: daintree.hosts.open-project-on-host
title: Open this project on a Remote Host
summary: From this Shell, open the Daintree project-placement dialog so the person gets this project onto a Remote Host, either the host's existing copy of the repository or a fresh clone, after briefing what git carries and what stays here; the person approves every step, and you report where it opened.
whenToUse: 'Use when the person wants THIS project, the one open in this Shell, put on a Remote Host they already added: "open this project on the build box or server", "get my repo onto my other Mac", "does that machine already have a copy of this repo, or clone it there", "what reaches the host if I open this project there". The deliverable is Daintree''s project dialog opened for the person to approve (matching copy, clone folder, push, setup), the host''s project opened, and the answer to where it opened and what stayed behind. Do not use for adding the machine itself or a host that will not connect (daintree.hosts.connect-host), work sent to, or assistants readied on, a host that already has the project (daintree.hosts.delegate-to-host), choosing which host should take pieces of work (daintree.hosts.place-work), a local worktree (daintree.worktree.create-plain-worktree), or catching a branch up with its base (daintree.git.sync-with-base).'
risk: external
requiredTools:
  - actions.getContext
  - host.list
  - project.openOnHost
optionalTools:
  - host.call
hostEligibility: configured
---
Needs Tool set = Full. Opened by an agent, the dialog pushes, clones and opens nothing until the person clicks it. You brief and open it; you run no git and touch no host file.

1. Brief first. A host's copy matches by a shared remote; a shared project id alone is only a flagged candidate. Committed history is all that travels: uncommitted and ignored files (`.env`), installed dependencies and credentials stay here, and the host clones with its own sign-ins. Unpushed commits go only through the dialog's push choice, pushed from this machine; with no remote a copy of the committed history is sent. A clone is a new project there: new id, none of this project's settings, trust or approvals. Leave "No setup recipe" unless they ask; the host asks again before any runs. Opening tries to take the project's drive on the host; another driver, or someone at the host's screen, leaves this view read-only.
2. `actions.getContext`: this window's `projectId`. `host.list`: the target's `hostId`; its `projects` (only while connected with control on) may already name a copy, a hint the dialog confirms by remote. The person can also open the dialog from the command palette ("Open project on host…"), which may open an existing copy unasked and move this window.
3. Open it once:

```json tool=project.openOnHost surface=help-full
{"hostId":"<host id>","projectId":"<projectId>","newWindow":true}
```

   `newWindow:true` keeps this window and conversation here. Add `navigateOnly:true` if they want the host's copy exactly as it is (you cannot see what it holds): no branch, push or worktree. The result is only `{requestId}`: the dialog is open, nothing decided.
4. End the turn; they choose and tell you. Several copies, a fork, several remotes to clone from or "Same project id, different remotes" are theirs to pick.

Stop and recovery: "Couldn't confirm the push" or "dropped off mid-clone" is unknown, not failed. They check the remote's branch or the host's project list before anything repeats; reopening the dialog re-matches a clone that has a remote. Never a second push or clone on a guess, or a second call for the same request.

Verify: what they see (host, project, branch, folder), then `host.list`: that host's `projects` naming it, with its own `projectId` (a clone's is new); its folder from `workspace.list` through `host.call` in that project. Unseen stays pending.

Report back: host, copy used or clone, folder, branch handed over, pushed or not, what stayed here, recipe, window, and what you checked against what they said.
