---
id: daintree.workflow.prep-branch-for-review
title: Prepare a branch for review
summary: Get a read-only review-readiness verdict, then do the real prep — commit, push, open the PR — branching on what the verdict reports.
whenToUse: Use when the user asks to prepare a branch for review, open or ready a PR, wrap up work on an issue for review, or check whether the current branch is ready to push. Do not use to judge whether an agent's work is actually correct or complete before trusting it (use daintree.orchestration.verify-agent-work), to START work on an issue rather than finish it (use daintree.workflow.start-work-on-issue), to be notified later when the PR merges (use daintree.orchestration.watch-outcome), to read an EXISTING PR's CI or review state or to comment on and close its issue (use daintree.workflow.follow-up-on-issue), to forward a reviewer's findings to an implementer agent (use daintree.agents.relay-review-feedback), to REVIEW the change rather than ready it (use daintree.review.review-change), or to fix a failing check (use daintree.ci.diagnose-and-fix).
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
Use when: the user wants a branch readied for review (committed, pushed, a PR opened or marked ready), or only asks whether it is ready.

Availability: Hybrid. Daintree gives read-only verdicts: `workflow.prepBranchForReview` (go or no-go on whether checks can start; despite its name it prepares and runs nothing) and `worktree.reviewReadiness` (commit, push and PR readiness with blockers). Both ask the user first at core tier. Staging, committing, pushing, and creating or readying the PR are done with the host's own git and forge tools (for GitHub, `gh`), within what the user asked for and always with an explicit repository. A readiness question stops at the assessment and publishes nothing.

Procedure:
1. Bind the target: the worktree the user named, not your own checkout, with its id and path (`worktree.list`). Run every git command in that path and every forge command with `--repo <owner/name>`.
2. Get the verdict:

```json tool=workflow.prepBranchForReview
{"worktreeId":"<resolved worktree id>"}
```

   Read `verdict`, `stagedCount`, `unstagedCount`, `currentBranch`, `repoState` and `detectedRunners`.
   - `ready`: the tree is clean and checks can start. Nothing has run, and unpushed commits are not considered.
   - `blocked_uncommitted_changes`: commit the intended scope (step 4).
   - `blocked_merge_conflicts`: resolve them if that is within the request, else report.
   - `blocked_repo_busy`: a merge, rebase or similar is in progress. Find out which and report; do not abort it or push through it to get a green verdict.
   - `no_runners_detected`: Daintree found no check to run. That is unverified, not passed.
   `worktree.reviewReadiness` (`worktreeId`) adds `pushReady`, `prReady`, `aheadCount`, `behindCount`, the detected `pr`, and blockers such as `unpushed-commits`, `behind-remote`, `pr-draft` and `protected-branch`. Its forge signals are `unknown` until forge data arrives, and unknown is not green. For a readiness-only request, report here and stop.
3. Verify the revision you will publish. Run the relevant checks in the worktree: the repository's own test and lint commands, or `project.runCheck` with a runner id from `detectedRunners` and the worktree path as `cwd`. Detected runners include publish and deploy scripts, so know what a command does before running it. `passed:false` is a failed check; a timeout or abort is incomplete verification. Fix failures yourself when that is within the request; if the user wants a visible agent to do it, follow daintree.edits.spawn-visible-agent, then re-verify the resulting revision.
4. Stage only this task's changes. Read `git status` and `git diff --cached` first: a commit takes everything already staged, including the user's or another task's selection. Stage the task's files by path; stage everything only when every change belongs in this commit. If the index holds changes you did not stage, ask the user: do not commit them silently or unstage their selection. Commit with a message drawn from the actual change, then check what is still uncommitted.
5. Push, even when there was nothing new to commit: a clean tree can still hold unpushed commits (`git status -sb`, or `aheadCount`). Use `git push -u <remote> <branch>` for a new branch. Never force-push, rewrite history or delete a remote branch to get past a rejection; a rejected push (behind the remote, protected branch) is reported and resolved on its own terms. After an ambiguous push, compare the remote branch head with your local commit before trying again.
6. Find an existing PR before creating one: `gh pr list --repo <r> --head <branch> --state all`, or `forge.listPRs` with `"search":"head:<branch>"` (add `bypassCache:true` after a change made outside Daintree). Match the head branch and base, not a similar title. An open PR is updated by the push. A draft the user wants ready: `gh pr ready <n> --repo <r>`. Never open a duplicate.
7. Create the PR when asked: `gh pr create --repo <r> --head <branch> --base <base> --title <title> --body-file <file>`, with `--draft` if the user wants one. Take the base from the repository's convention or the user, never an assumed `main`. Write the body from the final change and the checks you actually ran, with the issue's closing reference when the work resolves one. If the create's reply is lost, look for the PR by head branch before any retry.
8. Read back: `gh pr view <n> --repo <r> --json number,url,state,isDraft,headRefName,headRefOid,baseRefName`, or `forge.getPR`. Confirm head, base and draft or ready state, and that `headRefOid` is the commit you verified. CI on the PR is separate evidence, and a cached green may predate your push (daintree.workflow.follow-up-on-issue reads it). Neither readiness nor CI is a code review.

Report back: the readiness verdict and blockers; the checks run, on which commit, with results and limits; the files and commit included; whether the push landed; the PR number, URL, head, base and draft state as read back, or the concrete blocker; and anything left staged, uncommitted or unpushed.
