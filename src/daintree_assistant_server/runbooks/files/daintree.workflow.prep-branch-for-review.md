---
id: daintree.workflow.prep-branch-for-review
title: Prepare a branch for review
summary: Get a read-only review-readiness verdict, then do the real prep — commit, push, open the PR — branching on what the verdict reports.
whenToUse: Use when the user asks to prepare a branch for review, open or ready a PR, wrap up work on an issue for review, or check whether the current branch is ready to push. Do not use to judge whether an agent's work is actually correct or complete before trusting it (use daintree.orchestration.verify-agent-work), to START work on an issue rather than finish it (use daintree.workflow.start-work-on-issue), to be notified later when the PR merges (use daintree.orchestration.watch-outcome), to read an EXISTING PR's CI or review state or to comment on and close its issue (use daintree.workflow.follow-up-on-issue), to forward a reviewer's findings to an implementer agent (use daintree.agents.relay-review-feedback), to REVIEW the change rather than ready it (use daintree.review.review-change), to fix a failing check (use daintree.ci.diagnose-and-fix), only to run the checks and report without committing (use daintree.ci.run-local-checks), to catch a branch that is behind or conflicted up with its base (use daintree.git.sync-with-base), to cut one large change into stacked PRs (use daintree.git.split-into-stacked-prs), to park half-done work at the end of the day with no review wanted (use daintree.orchestration.park-work), or for a go/no-go on a whole release candidate (use daintree.release.assess-readiness).
risk: external
requiredTools:
  - worktree.list
  - workflow.prepBranchForReview
optionalTools:
  - worktree.reviewReadiness
  - project.runCheck
  - forge.listPRs
  - forge.getPR
---
Daintree only gives read-only verdicts (both ask first at core tier); staging, committing and pushing use git in the checkout, PR writes `gh`, always with `--repo <owner/name>`. Do only the stages asked for: a readiness question stops at the assessment, a commit-only request stops after the commit, a push-only request after the push.

On a host: shell steps via its command runner and checks via its `project.runCheck`, else delegate; own shell: `gh -R <repo>`.

1. Bind the target worktree the user named (`worktree.list`), not your own checkout; run every git command in its path.
2. `workflow.prepBranchForReview` (`worktreeId`) prepares and runs nothing; it reports whether checks can start. Without it, assess with git (`git status -sb`, merge or rebase in progress); if the user declined its approval, ask before assessing another way.
   - `ready`: clean tree. Nothing has run, and unpushed commits are not considered.
   - `blocked_uncommitted_changes`: commit the intended scope (step 4).
   - `blocked_merge_conflicts`: resolve if within the request (daintree.git.sync-with-base), else report.
   - `blocked_repo_busy`: find out which operation and report; never abort it to get green.
   - `no_runners_detected`: unverified, not passed.
   `worktree.reviewReadiness` adds `pushReady`, `prReady`, ahead/behind counts, the PR and blockers; its forge signals are `unknown` until forge data arrives, and unknown is not green. Readiness-only: report and stop.
3. Verify the revision you will publish: the repository's test and lint commands, or `project.runCheck` with a runner from `detectedRunners` and the worktree as `cwd` (runners include publish and deploy scripts: know what one does first). `passed:false` fails; a timeout is incomplete. A failure is fixed by an agent, not by you unless the user asked: the branch's own agent (a send with `notify:true, handback:true`), else daintree.edits.spawn-visible-agent; then re-verify yourself.
4. Stage only this task's changes. Read `git status` and `git diff --cached` first: a commit takes everything already staged. Stage by path; split a mixed file by hunk (`git apply --cached`) or ask. Changes staged by someone else: ask; never commit them silently or unstage them. Commit with a message from the actual change. If a hook changed files, re-run the checks.
5. Push from the checkout's machine, even with nothing new to commit (check ahead count); `git push -u <remote> <branch>` for a new branch. Never force-push, rewrite history or delete a remote branch to get past a rejection: report it. After an ambiguous push, compare the remote head with your commit before retrying.
6. Find an existing PR first (`gh pr list --repo <r> --head <branch> --state all`, or `forge.listPRs` `"search":"head:<branch>"` with `bypassCache:true`). Match head and base, not title. An open PR is updated by the push; a closed or merged one on a reused branch is history. A draft to ready: `gh pr ready <n> --repo <r>`. Never open a duplicate.
7. Create when asked: `gh pr create --repo <r> --head <branch> --base <base> --title <t> --body-file <f>` (`--draft` if wanted). Base from convention or the user, never an assumed `main`. The body reflects the final change and checks actually run. `Closes #N` only when the PR fully resolves the issue and the link is expected, else `#N`. A lost reply: look up by head branch before any retry.
8. Read back `gh pr view <n> --repo <r> --json number,url,state,isDraft,headRefName,headRefOid,baseRefName`: `headRefOid` must be the commit you verified. PR CI is separate evidence (daintree.workflow.follow-up-on-issue); neither readiness nor CI is a code review.

Report back: the verdict and blockers; checks run, on which commit, with results; files and commit included; whether the push landed; the PR number, URL, head, base and draft state as read back, or the blocker; anything left staged, uncommitted or unpushed.
