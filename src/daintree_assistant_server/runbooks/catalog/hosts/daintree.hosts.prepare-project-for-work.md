---
id: daintree.hosts.prepare-project-for-work
title: Bring this project to a Remote Host for work, without the dialog
summary: From this Shell, make this project present on a connected Remote Host through Daintree's own preparation (the host's matching copy or a fresh clone, and the branch's worktree when asked) with nothing pushed or opened and no setup recipe run, bring back only the choices the person must make, and continue the requested work there through the host's own project id.
whenToUse: 'Use when the person wants THIS project, the one open in this Shell, made present on a connected Remote Host so work can run there, done by you with no dialog or window: "put this repo on the other studio and start an agent there", "make sure that host has this project, then continue the task on its branch", "prepare this project on the build machine without opening a window there", "the server has never had this repo, get it there and have an agent fix the test". The deliverable is the host''s own projectId (and the branch''s worktree when asked), its existing copy found or a clone made there with nothing pushed, copied or opened and no setup recipe run, any choice only the person can settle (several copies, a fork, unpushed commits, no remote) brought back with what was observed, what stayed on this machine, and, when asked, the work started there through that address. Do not use for opening or switching to the project in a window there, or the project dialog and the push or history copy only it makes (daintree.hosts.open-project-on-host), adding a machine or a host that will not connect (daintree.hosts.connect-host), work alone for a host whose projects already hold the repository, with nothing to bring (daintree.hosts.delegate-to-host), choosing hosts for pieces of work by load or capacity (daintree.hosts.place-work), or only checking which host project is the intended repository (daintree.hosts.verify-project-placement).'
risk: external
requiredTools:
  - host.list
  - host.ensureProject
optionalTools:
  - host.call
hostEligibility: connected
---
`host.ensureProject` brings the project this window holds to a host: the copy sharing its remote, else a clone from the remote with the host's own sign-ins, and the branch's worktree when asked. It never pushes, sends history, runs a setup recipe, takes the project's drive or opens a window on either machine; a worktree it makes still runs lifecycle commands the person already approved there. Prepare by no other route: no clone, push, copy or ssh of your own, no `.env` or credentials carried; reads to verify are fine. Uncommitted and ignored files stay here. Only a project's own assistant has the tool; the global assistant says so and stops. A window there is daintree.hosts.open-project-on-host.

Procedure:
1. Source: the tool always takes this window's project. A repository or project the person named that is not this one (its remote, from your shell): stop; its own window's assistant prepares it. Branch work: the worktree here that holds the branch, its name and full SHA.
2. `host.list`: the host the person named. Not `connected`, `control:false`, or `local`: report it, stop. None named: ask.
3. Call once. `worktree:true` only for work on a branch, with `worktreePath` that worktree (default: the project folder):

```json tool=host.ensureProject
{"hostId":"<hostId>","worktree":true,"worktreePath":"<this project's worktree path>"}
```

4. Read `outcome` first (the hosts foundation), then `answer.status`:
   - `ready`: the `projectId` and `worktreePath` in `project` are the host's; `how` is `existing`, `registered` or `cloned`; `leftHere` is what stayed here (`unpushedCommits` null: not checked); `setupRecipes` are names, none ran. A worktree asked for but `worktreePath` null: `note` says why; their decision, never a launch elsewhere.
   - `question`: each `reason` and `detail` to the person, with the branch and SHA they are about and each of its `copies` (`path`, `name`, `matchedBy`); end the turn. On their answer re-read the branch and SHA: changed, ask again. Then call with the same arguments plus the answer: `copy` (a listed `path`, or `"new"`) for several copies or an id-only match; `accept` only for a question whose `accept` is set, only on their yes (`unpushed`: the host starts from the remote's commit; local commits stay here). Other reasons they settle first; a push is theirs to order separately; `remote_unreachable`: later.
   - `working`: still running; the same call again joins it.
   - `failed`: quote `note`; stop.
   - `conflict`: quote `detail` (a path not among this project's worktrees, or the host's folder not free); correct the path or ask.
   - `opted_out`, `policy_rejected`, `unsupported`: nothing changed; never get round a switch by shell, ssh or another credential.
   - `ambiguous`, `expired`, `disconnected`: unknown, not failed. Once the host is back, the same call re-finds what was made, never clones twice; never `copy:"new"` on a guess.
5. Verify: that host's `projects` in `host.list` names the `projectId` (50 at most: unlisted is unverified). A worktree: `worktree.list` through `host.call` there shows it on the branch, and `project.runCommand` there reads its HEAD against `git ls-remote` from your shell; an existing worktree comes back with its tip unchecked. A different commit: report it; never reset or launch.
6. Continue only if asked, with the request's own procedure and authority (daintree.hosts.delegate-to-host from its step 2, or daintree.hosts.place-work for a placement; `get_runbook` if absent), this `hostId` and `projectId`. Continuing the prepared branch, its worktree is the one: no new branch or worktree. Not ready by implication: a setup recipe is their separate choice. Asked again later: the same call, `how:"existing"`, never a second clone.

Report back: host, `projectId`, path and worktree, `how`, what stayed here, recipes named and not run, each question with its answer, what you verified against what the app returned, and the work started there (`terminalId`) or why not.
