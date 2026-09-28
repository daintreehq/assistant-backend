---
id: daintree.git.prune-stale-branches
title: Prune merged and abandoned branches and stale PRs
summary: Inventory local and remote branches and long-idle open PRs, classify each with evidence (merged, squash-merged included; abandoned; active; must keep), and delete or close only the set the user approves, read back.
whenToUse: Use when the user wants REPOSITORY hygiene over branches and pull requests — "delete my merged branches", "clean up old remote branches", "what branches can I delete?", "prune the branch list", "close the PRs nobody has touched in three months". The deliverable is a classified inventory with the evidence for each verdict and, after approval, the deletions and closures done and read back, with everything kept and why. Do not use to tear down Daintree terminals or worktrees after a session (use daintree.orchestration.workspace-cleanup); to find stale or at-risk worktrees (use daintree.worktree.audit-worktrees); to close, merge or review-state ONE pull request (use daintree.forge.land-pull-request); or to review the open PRs (use daintree.review.review-pr-queue).
risk: git
requiredTools:
  - worktree.list
optionalTools:
  - forge.listPRs
  - forge.getPR
---
Git runs natively in the main checkout (`worktree.list`, `isMain`). No agent is needed: the reads and classification are yours, and each deletion or close is one command per item the user approved. Nothing is deleted or closed before the user has seen the list with full SHAs and said yes, except that a precise request ("delete my local branches merged into main") covers the branches step 3 classes MERGED (3b). An inventory-only request ("what can I delete?") ends at step 5 with recommendations.

Procedure:
1. `git fetch origin`; add `--prune` (drops remote-tracking refs deleted upstream) only when cleanup is in scope. Base: the user's, else `git symbolic-ref --short refs/remotes/origin/HEAD` (prints `origin/main`); freeze its SHA. Keep full refs, plain names (`origin/topic` is branch `topic`) and SHAs apart.
2. Inventory: `git for-each-ref --format='%(refname) %(objectname) %(committerdate:short) %(upstream:track)' refs/heads refs/remotes/origin`, and `worktree.list` for the branches checked out in worktrees.
3. Classify each branch; the FIRST rule that matches wins, and the report names it:
   a. KEEP, never proposed, even when merged: the default branch, develop, `release/*` and `hotfix/*` lines, protected branches (`gh api repos/<owner>/<repo>/branches --paginate -q '.[] | select(.protected) | .name'`), a branch checked out in any worktree (removing that is daintree.orchestration.workspace-cleanup), branches the user excludes.
   b. MERGED: `git merge-base --is-ancestor <sha> <base SHA>` exits 0 (merge commits and fast-forwards). `git branch -d` alone is no test: it checks the upstream or HEAD, not the base.
   c. SQUASH-MERGED: `git cherry <base SHA> <sha>` prints only `-` lines (its patches are upstream; no forge needed). With a forge, also `gh pr list --repo <r> --head <name> --base <base> --state merged --json number,headRefOid,mergeCommit`: the tip equals that `headRefOid` (a later commit is unmerged work) and its `mergeCommit` is in the base. Deleting one needs its own yes.
   d. NOT MERGED. Old age makes it an "abandoned?" candidate only; the user decides.
4. Stale PRs, when asked: open PRs not updated since the user's cutoff (none given: propose 90 days), the date after `updated:<`. Follow `nextCursor` while `hasMore`; a failed page or the search depth cap means coverage is incomplete: say so.

```json tool=forge.listPRs
{"worktreeId":"<main worktree id>","state":"open","search":"updated:<2026-06-30","sort":"updated","direction":"asc","perPage":100}
```

   Read each candidate (`forge.getPR`) for author, draft state and last activity. Someone else's PR is closed only on the user's explicit yes for that PR.
5. Propose in one message, grouped, with full SHAs: merged (with the evidence), abandoned candidates (last commit, what would be lost: `git log --oneline <base SHA>..<sha>`), stale PRs (author, last update), kept and why. One question, then act on exactly the approved set. A precise request's MERGED (3b) branches need no question: delete them (step 6) in the same turn and ask only about the rest.
6. Execute one item per command. Just before, re-read each tip, worktree use and PR state; anything changed goes back to the user.
   - Local: `git branch -d <b>` for an ancestry-merged branch. `-D` only for an individually approved squash-merged or abandoned one whose unique commits and SHA were shown.
   - Remote, only when remote deletion was asked: `git push --force-with-lease=refs/heads/<b>:<sha> origin :refs/heads/<b>`, so a branch that moved is refused.
   - PR: `gh pr close <n> --repo <r>`; a comment only with text the user approved, `--delete-branch` only if asked.
   A refusal (checked out, not fully merged, protected, stale lease) is an answer: report it, never force past it.
7. Read back: `git for-each-ref refs/heads`, `git ls-remote --heads origin <b>` (empty when deleted), `gh pr view <n> --repo <r> --json state`.

Stop and recovery: after an ambiguous result re-read the ref or PR state before retrying; never repeat a close or delete blind.

Report back: per branch and PR the verdict and its evidence; what was deleted or closed, with full restore SHAs (`git branch <b> <sha>`, remote `git push origin <sha>:refs/heads/<b>`) and read-back; what was kept and why; refusals, incomplete coverage, and anything left for the user.
