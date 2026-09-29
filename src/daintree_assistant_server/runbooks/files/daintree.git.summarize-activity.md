---
id: daintree.git.summarize-activity
title: Summarize activity for a standup or weekly update
summary: Write a dated account of what a person or the team shipped, reviewed and still has in progress over a stated window, from git, the forge and Daintree's worktrees and commit pulse, every item cited.
whenToUse: 'Use when the user wants a written REPORT of work over a period of time — "write my standup", "what did I ship this week?", "weekly update for the team", "summarise the last sprint", "what did the agents actually land today?", "how active has the repo been lately, what is our commit streak?". The deliverable is a summary for a stated window and timezone, split into shipped, in review, in progress and blocked, each line citing a commit, PR or issue, with trend figures when asked; nothing is posted anywhere. Do not use to pick up or reconstruct in-flight Daintree runs from earlier sessions (use daintree.orchestration.status-and-resume), for release notes or a changelog for a version (use daintree.forge.draft-release-notes), to triage the agents running this minute (use daintree.orchestration.supervise-fleet), to check one PR''s CI or review state (use daintree.workflow.follow-up-on-issue), to find past PRs about a topic (use daintree.workflow.find-issue-work), to classify worktrees for cleanup (use daintree.worktree.audit-worktrees), or for a running demo of what was built (use daintree.workflow.prepare-feature-demo).'
risk: terminal
requiredTools:
  - worktree.list
  - terminal.list
  - agent.launch
optionalTools:
  - git.getProjectPulse
  - forge.getPR
  - terminal.sendCommandOwned
---
Only merged PRs and commits on the base branch are shipped. An agent's handback, a closed-unmerged PR or a pushed branch is not. Nothing is posted to chat, the forge or a file unless the user asks. You read Daintree's state and verify; one visible agent compiles the git and forge history.

Procedure:
1. Fix the window and subject before reading anything, reading what you can rather than asking. Window: exact start and end in the user's local timezone and its offset (the machine's, `date +%Z%z`, unless the user names another; "yesterday" on a Monday is Friday only if the user works that way; ask once only if the window itself is unclear). Subject: "me" is the forge login (`gh api user --jq .login`) plus the git author emails (`git config user.email`, and any others the user names); an unreadable login leaves the forge sections unread, reported as a gap, and never blocks the git history; "the team" is everyone, grouped by author. "The agents" is the branches of the worktrees their terminals ran in: git and the forge never name an agent, so credit one only where its worktree links it.
2. Daintree state, yourself: `worktree.list` (the `isMain` row's `id` and `path`; per row `branch`, `prNumber`, `status`) and `terminal.list` rows with an `agentId`, grouped by `worktreeId`. In progress: a worktree with commits in the window (`git -C <path> log <base ref>..HEAD --since=<start> --format='%h %s'`), uncommitted work (`status:"active"`), an open PR or a working agent. Any other branch is parked: list it only if asked. Trend figures only when asked (streak, how active): `git.getProjectPulse` on the main worktree walks that branch's history, every author, over `rangeDays` 60, 120 or 180; label that scope.
3. Launch one compiler with `agent.launch` in the main worktree (`notify:true, handback:true`), then end the turn. Brief: window with offsets, subject logins and emails, base branch, and:
   - "Shipped PRs: `gh pr list --state merged --base <base> --limit 500 --search 'author:<login> merged:<start>..<end>' --json number,title,author,mergedAt`, keeping only rows whose `mergedAt` is inside the window; at the limit, say the list is incomplete.
   - Direct commits, which are shipped: `git log origin/<base> --first-parent --no-merges --since=<start> --until=<end> --author=<email> --format='%h %ad %s' --date=iso-local` after `git fetch --quiet origin`. A commit mapping to a PR (`(#n)` suffix, or `gh api repos/<owner>/<repo>/commits/<sha>/pulls --jq '.[].number'`) counts once under that PR; only an unmapped one is a direct commit.
   - Reviews given: candidates from `gh search prs --reviewed-by=<login> --updated='>=<start>'`, kept only where `gh api repos/<owner>/<repo>/pulls/<n>/reviews` shows a review by that login `submitted_at` inside the window; not their own PRs.
   - Open PRs by the subject with draft state, review decision and checks; blocked only on evidence (changes requested, failing checks, a conflict). Issues opened and closed in the window only if asked.
   - Every line cites a PR number or short SHA you read; never invent one. Read-only: write no file, post nothing. Report per section, with what you could not read."

```json tool=agent.launch
{"agentId":"claude","name":"Activity: <window>","worktreeId":"<main worktree id>","prompt":"<window, subject, base, rules above>","notify":true,"handback":true}
```

On notice or resume: a notice is the compiler's stop, not acceptance. A question within the brief gets one answer by `terminal.sendCommandOwned` with `notify:true, handback:true`. Verify: each shipped PR by `forge.getPR` (`merged`, the right `baseRef`, `mergedAt` inside the window; past about 20, check a sample and say so), each direct SHA by `git merge-base --is-ancestor <sha> origin/<base>` and its date. Drop what fails and say so. Then write the summary: window and timezone first; shipped (verified PRs and direct commits), in review, in progress (your step 2 reading of the worktrees, not the compiler's), blocked; one line per item with its citation; grouped for the audience (by theme for a team, by day for a personal log); an empty section says so.

Stop and recovery: one corrective round for a gap; never relaunch a compiler that is still working.

Report back: the summary; the window, timezone and whose activity; sources read and any gap (reviews unreadable, list at its limit, pulse scope); what you verified versus the compiler's claims; and that nothing was posted.
