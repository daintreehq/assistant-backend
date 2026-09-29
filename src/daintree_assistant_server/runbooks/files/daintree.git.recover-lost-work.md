---
id: daintree.git.recover-lost-work
title: Recover lost work
summary: Get back commits and files lost to a reset, a deleted branch, a removed worktree or a cleanup — stop anything that could prune them, find each item's surviving copy, restore it into a new recovery worktree, verify it, and say plainly what cannot come back.
whenToUse: 'Use when work has gone MISSING and the user wants it back — "I accidentally reset my branch, get my changes back", "the agent deleted the feature branch", "I removed the worktree and it had uncommitted work", "cleanup deleted my branch before I pushed", "my commits disappeared after the rebase". The deliverable is a recovery branch in a new worktree holding what could be found, each item with its source (reflog, an unreachable commit or blob, a remote or PR ref, the stash, an agent transcript), checked by diff and tests, and an honest list of what is gone for good. Do not use to take back changes the user does NOT want (use daintree.git.undo-agent-changes), to keep the good part of a failed attempt (use daintree.orchestration.salvage-failed-attempt), to reopen a closed agent session or catch up on where things were (use daintree.orchestration.status-and-resume), to delete merged or abandoned branches (use daintree.git.prune-stale-branches), or to find which worktrees are at risk before anything is lost (use daintree.worktree.audit-worktrees).'
risk: git
requiredTools:
  - worktree.list
  - terminal.list
  - worktree.createWithRecipe
  - worktree.waitUntilReady
  - agent.launch
optionalTools:
  - terminal.getStatus
  - terminal.getOutput
  - terminal.interruptOwned
  - terminal.sendCommandMany
  - agentSessionHistory.list
  - actions.getContext
  - project.detectRunners
  - project.runCheck
---
Lost objects live only until git prunes them, and ordinary git commands can trigger `gc --auto`: assume no safe window. Freeze, locate (your git reads), restore in new worktrees.

Procedure:
1. Freeze, this turn; no git write of yours. Worktrees share objects: `terminal.list`, `terminal.getStatus` over the repository's agents, never your own pane. One you own `working`: `terminal.interruptOwned`; its screen shows it stopped. Then to all you own (handed over included) one `terminal.sendCommandMany` with `waitForReply:true, handback:true`, `waitSeconds` 120: "Pause: run no git write (commit, reset, rebase, gc, prune) until I say. Reply 'held'." Record who replied or was interrupted; silence is not frozen. Agents not yours: tell the user.
2. What was lost (user): branch, worktree, when, what it held. Daintree: `worktree.list`; `agentSessionHistory.list` (`projectId` from `actions.getContext`, all pages): matching `cwd` or `branch`, `savedAt`, `sessionId`; `terminal.getOutput` may show `Deleted branch <b> (was <sha>)` or the reset.
3. Locate each item with `git -C <main checkout>` reads, naming the source:
   - Reset, rebase or amend: the entry before the loss in `git reflog <branch>` or `git -C <path> reflog`.
   - Deleted branch: the `was <sha>` line; a reflog of a worktree that had it; `refs/remotes/origin/<b>`; the PR head (`gh pr view --json headRefOid`).
   - Unreachable: `git fsck --unreachable --no-reflogs --no-progress`; pick commits with `git log --no-walk <sha>…`. A dropped stash has subject `WIP on …` or `On …`; `^1` is its base, `^3` (if any) its untracked files. A removed worktree's staged files are unnamed blobs: match by content (`git cat-file -p`).
   - Never staged: not in git. Partial: an agent transcript (Claude Code `~/.claude/projects/<cwd, non-alphanumerics as ->/<sessionId>.jsonl`; Codex `~/.codex/sessions`); a backup.
   List each item: source, SHA or blob, date, confidence, start point: a lost commit or tip itself, never its parent; a stash `<stash>^1`; blobs, transcript text: the branch's last known HEAD, else ask. The user confirms the set ("get my branch back" names it).
4. One recovery worktree per start point, created at it (that branch restores a lost tip and all before it), then `worktree.waitUntilReady` with the returned `worktreeId`, at most three calls, until `setupState` is `ready`; else report id, state, error; resume by rechecking, never a second create. Branch `recover/<name>` (the original only if free and asked); never move the user's.

```json tool=worktree.createWithRecipe
{"source":{"kind":"newBranch","branchName":"recover/<name>","baseBranch":"<start point SHA>","collisionPolicy":"error"}}
```

5. Only commits lost: verify; no agent, no cherry-pick. Else launch one agent there, `notify:true, handback:true`, and end the turn. Brief: the path, each item with source and start point, and verbatim: "Before any commit, pin each listed commit and stash (`git update-ref refs/recovery/<name>/<n> <sha>`) and copy each blob outside the repository. Restore here only. A stash: `git stash apply <sha>` once. A blob: `git cat-file -p <blob> > <path>`, its mode set. Transcript text: replay in order only edits whose result shows success, each Edit only where its old text is present exactly; a failed call, missing result, mismatch or truncation makes the file partial; never guess. Never touch <damaged path> or another worktree; no gc, prune, push, branch deletion. Commit each source apart, provenance in the message. Run the checks. Report each item as restored, partial or not found, its commit, the pins, copy location, checks."

On notice or resume:
1. An uncertain item, path or start point: the user.
2. Verify each item at its commit against its source: a commit is an ancestor (`git merge-base --is-ancestor`); a stash: `git diff <stash> <commit>` shows only its untracked files, matching `<stash>^3`; a blob: `git hash-object <path>` equals it; transcript files read against it, partial where evidence is missing. The pins exist (`git for-each-ref refs/recovery/`); `git status` clean; checks re-run (`project.runCheck`, `runnerId` from `project.detectRunners`, `cwd`); nothing pushed.
3. Every exit (done, declined, failed setup or launch, a stop): once evidence is pinned, one `terminal.sendCommandMany` tells the held set "Resume your previous task."; name any left held, why, and what releases it.

Stop and recovery: one corrective round, then the user. Never gc, delete the recovery branch or pins, or reset a user branch without their word.

Report back: per item the source, SHA or blob, and restored, partial or not found; the worktree id, path, branch, HEAD; the agent's terminal id, pins, copy location, open operation; checks; what searched sources did not yield, why, options not tried; agents held, interrupted, released.
