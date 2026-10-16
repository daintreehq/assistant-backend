---
id: daintree.git.undo-agent-changes
title: Undo what an agent changed
summary: Reverse exactly what one agent changed — its commits and its uncommitted edits — while the user's own and other agents' work stays intact, proven by a before/after diff, with ambiguous hunks put to the user.
whenToUse: 'Use when an agent changed things the user wants taken back while everything else stays — "undo what that agent just did but keep my changes", "revert codex''s edits to the config, leave the rest", "roll back the last agent''s commits on this branch", "back out whatever claude touched in the API layer". The deliverable is exactly that agent''s attributable commits and uncommitted edits reversed in place, the user''s staged, earlier and unrelated work intact, proven by a before/after diff, and every hunk of uncertain authorship listed for a decision. Do not use to learn why the run went wrong (use daintree.agents.postmortem-run), to keep the useful part of a failed attempt in a fresh branch (use daintree.orchestration.salvage-failed-attempt), to get back work that was lost (use daintree.git.recover-lost-work), to stop or redirect an agent without undoing anything (use daintree.agents.steer-running-agent), to copy commits onto another branch (use daintree.git.cherry-pick-changes), to delete the whole worktree (use daintree.orchestration.workspace-cleanup), to take back a run sent to Remote Hosts, from the hub (use daintree.hosts.undo-swarm-changes), or to roll a deployed service back (use daintree.release.rollback-deployment).'
risk: git
requiredTools:
  - terminal.list
  - terminal.getStatus
  - worktree.list
  - agent.launch
optionalTools:
  - terminal.interruptOwned
  - terminal.waitUntilIdle
  - terminal.readLastMessageOwned
  - terminal.getOutput
  - agentSessionHistory.list
  - actions.getContext
  - project.detectRunners
  - project.runCheck
---
You attribute and verify with git reads; a fresh visible agent in that worktree does the reversing. The trap is a whole-tree undo (`reset --hard`, `checkout -- .`, `restore .`, `clean`, the shared `stash`): it wipes the user's work too.

Procedure:
1. The agent: its terminal (`terminal.list`; unclear: ask), worktree id and path (`worktree.list`); a closed one from `agentSessionHistory.list` (`worktreeId`, `savedAt`). Still `working` (`terminal.getStatus`): stop it first (`terminal.interruptOwned` if yours, then `terminal.waitUntilIdle` and its screen shows it stopped; not yours: ask the user to press Escape in its pane). Another agent working in that tree: ask before going on.
2. Baseline, your reads with `git -C "<path>"`: H1 = `rev-parse HEAD`; R0 = `ls-remote origin refs/heads/<branch>`; `status --porcelain`, `diff --stat`, `diff --cached --stat`.
3. Attribute on evidence, naming each source; author, time and file names only find candidates (agents commit as the user).
   - Start S, its HEAD at launch: your launch record, its brief or report, or `reflog --date=iso` around its launch. Unknown: ask; never guess a range.
   - Its commits: `log --format='%H %P %an %cI %s' <S>..HEAD`; the agent's when the user picked it, its report lists it, or nothing else worked there after S. A merge (two parents) needs its mainline (`-m 1`, the branch side) confirmed.
   - Its uncommitted edits: read its screen (`terminal.getOutput`; `terminal.readLastMessageOwned` if you hold it); hunks matching edits it shows succeeding. Dirty before S, or matched by nothing: unknown, never assumed the user's.
   - Unknown or mixed (a hunk both touched, a commit mixing both): list it, leave it unchanged pending a decision.
4. Mode, from the user's words: revert commits by default. Dropping them from history only on the user's ask: the worker first writes the rewrite plan (commits removed, descendants kept, a backup ref) and runs it after the user approves; commits already on the remote also need their word to force-push. Never silently swap a revert for a drop. Say if a later commit builds on a reverted one.
5. Proceed with what the request names; ask only about unknown items, changed scope or a rewrite.
6. Launch a fresh agent there (never the one that made it), `notify:true, handback:true`, and end the turn:

```json tool=agent.launch
{"agentId":"claude","name":"Undo: <agent> changes","worktreeId":"<worktree id>","prompt":"<brief>","notify":true,"handback":true}
```

   The brief: path, branch, H1, the undo set by SHA and hunk, the mode, the check commands, and verbatim and whole (a shortened one lost user work): "Use `git -C <path>` throughout. First save outside the repo `git diff --binary`, `git diff --cached --binary` and copies of listed untracked files. Reverse only the listed items. Commits newest first: `git revert --no-edit <sha>` (a merge: `-m <mainline>` as given). It refuses over uncommitted edits: lift them with the saved patches (`git apply -R` unstaged, `git update-index -q --refresh`, `git apply -R --index` staged), revert, restore (`git apply --index` staged, then `git apply` unstaged); one fails: stop and report. A conflict: keep later work, undo only what <sha> changed (restore its deletions, reverse its edits), `git revert --continue`; never restart it. Uncommitted hunks: reverse exactly those in their own layer (`git apply -R` of their patch; staged too: `git apply -R --index`). Never `git reset --hard`, `checkout -- .`, `restore .`, `stash` or `clean`; touch no unlisted file or hunk. No push. Run the checks. Report each command, revert SHA, conflict and what you kept, the save location, final HEAD, any open operation, check results."

On notice or resume:
1. An authorship or scope question goes to the user.
2. Verify yourself: `git status` shows no operation in progress; each revert is the inverse of its original (`git show <revert>` against `git show <sha>`); `git diff --name-only <H1> HEAD` stays inside the reverted commits' files; `git diff` and `git diff --cached` differ from the worker's saved patches only by the listed hunks, by content; untracked files as before; the checks re-run (`project.detectRunners`, then `project.runCheck` with `projectId` from `actions.getContext`, that `runnerId`, `cwd` the path; read `passed`, `exitCode`, `timedOut`); the remote ref still R0.

Stop and recovery: one corrective round, then the user. Never replay a revert. The saved patches and H1 are the rescue point, restored only on the user's word.

Report back: the agent and worktree; S and its source; what was reversed (revert SHAs, hunks), what was kept as the user's or another agent's, unknown items awaiting a decision; the undo agent's terminal id, final HEAD, save location, any open operation; before/after evidence, check results; R0 before and after.
