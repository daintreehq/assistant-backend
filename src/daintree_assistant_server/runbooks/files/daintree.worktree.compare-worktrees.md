---
id: daintree.worktree.compare-worktrees
title: Compare worktrees and forecast merge conflicts
summary: Read-only comparison of worktrees' branches — what each changed since its fork point, which ones touch the same files, whether they will actually conflict with each other or the base, and a suggested merge order.
whenToUse: Use when the user wants worktrees or branches COMPARED or their collisions predicted, with nothing changed — "what's the difference between the auth and billing worktrees?", "are any of my agents touching the same files?", "will these branches conflict when I merge them?", "what order should I merge these five in?", "diff worktree A against worktree B". The deliverable is a per-worktree change summary, an overlap list, a per-pair conflict forecast from a real trial merge with file names, and a suggested merge order; nothing merged, checked out or written. Do not use to actually combine the branches (use daintree.git.integrate-agent-branches) or update one from its base (use daintree.git.sync-with-base); to judge which of several rival attempts is better (use daintree.orchestration.competition); to review one change on its merits (use daintree.review.review-change); to find stale, merged or at-risk worktrees (use daintree.worktree.audit-worktrees); to separate agents sharing one checkout (use daintree.worktree.relocate-agents); to pick one owner when agents are already duplicating or contradicting each other's work (use daintree.orchestration.resolve-task-collisions); or to settle a behaviour clash already found on the combined tree (use daintree.git.resolve-semantic-conflict).
risk: read
requiredTools:
  - worktree.list
optionalTools:
  - terminal.list
---
This is reading, so it stays yours: git with `git -C <path>`, no agents launched. It changes no branch, index or working tree; `git fetch` and trial merges only add objects and remote-tracking refs, so report them, and on a strict no-write request skip both and give an overlap-only assessment. Never test a merge with `git merge`, `checkout` or `switch` in any worktree: it mutates someone's tree.

On a host: shell steps via its command runner, or delegate.

Procedure:
1. Targets: exactly the set named, including branches with no worktree (`worktree.list` gives id, path, branch for those that have one); "my agents" is the worktrees with agent terminals; "these" with no antecedent: ask. Base: the user's, the PR's base, or `git symbolic-ref --short refs/remotes/origin/HEAD` (prints `origin/main`), after an optional `git fetch origin`. Freeze every target and the base to a SHA (`git rev-parse`) and use those SHAs in every command below; state the base ref, its SHA and whether it was fetched. Worktrees on two machines share no files or objects: read each on its own machine and compare the output as content; such a pair gets a trial merge only from pushed SHAs fetched into one checkout, else it is overlap-only.
2. Per target:
   - Committed changes since it forked: `git diff --name-only <base SHA>...<SHA>`. Three dots, from the fork point; two would count the base's later changes as the branch's.
   - Uncommitted (worktrees only): `git status --porcelain`, noted with the time read. A branch diff and a trial merge never see these, so they are flagged as a risk only.
   - `terminal.list` with its `worktreeId`: an agent `working` there makes it a moving target.
3. Overlap: for each pair, the files both changed (committed and uncommitted apart). Overlap only prioritizes review: file/directory and rename conflicts happen without a shared filename, and no overlap is not proof they work together.
4. Forecast every requested pair, and each target against the base, with a trial merge:
   `git merge-tree --write-tree --name-only <SHA A> <SHA B>`
   Exit 0: merges cleanly. Exit 1: the lines after the first (a tree id), up to the first blank line, are the conflicting files; the `CONFLICT` lines below say what kind. Any other exit is a failed forecast, not a result. Needs git 2.38+ (`git version`); older, or a pair you skipped, is labelled overlap-only or untested. A shared file that merges cleanly can still clash in meaning: name it as a review point.
5. "Diff A against B": `git diff --stat <SHA A> <SHA B>` plus the notable hunks; "what B did that A did not": `git diff <SHA A>...<SHA B>`. Uncommitted content: `git diff --no-index <path A>/<file> <path B>/<file>` per file; across machines, compare the two files' content.
6. Merge order: branches others build on first; then those conflicting with nothing; among conflicting ones the smaller first. This rests on pairwise trials: conflicts that appear only once three or more are combined are untested, and the integration reforecasts against the real combined HEAD (daintree.git.integrate-agent-branches). Say which later branches will need syncing with the new base (daintree.git.sync-with-base).

Report back: base ref, SHA and freshness; per target its branch, SHA, committed files, and whether dirty (with the time) or has a working agent; the overlap list; per pair and per target against the base, clean, the conflicting files, overlap-only or untested; the suggested order with reasons, marked as pairwise evidence; any fetch or objects written, and that no branch, index or tree changed.
