---
id: daintree.orchestration.productionize-prototype
title: Take a chosen prototype to production quality
summary: List the shortcuts in a prototype that worked, agree a plan with the user (rebuild clean or harden in place), have an agent build it properly on its own branch, then re-audit every shortcut and run the checks yourself.
whenToUse: 'Use when a spike, prototype or proof of concept WORKED, was chosen, and must now be made production quality — "we picked the Drizzle spike, now build it properly", "take the prototype on spike/cache and make it production ready", "harden the proof of concept before we ship", "the POC works, now do it for real". The deliverable is a numbered list of the prototype''s shortcuts (mocks, hardcoded values, missing error handling, persistence, auth, tests, docs, TODO markers), a plan the user approved that says rebuild from the base or harden in place and why, the work done by an agent on a proper branch, and every shortcut re-audited and the checks re-run by you. Do not use to try several options or design variants, pick one and ship it as built (use daintree.orchestration.spike-options), for a plan approved and built with no prototype behind it (use daintree.orchestration.plan-then-implement), to rescue parts of an attempt that FAILED (use daintree.orchestration.salvage-failed-attempt), for an ordinary change with no prototype (use daintree.edits.spawn-visible-agent), or for findings on the prototype''s diff and nothing built (use daintree.review.review-change).'
risk: project
requiredTools:
  - worktree.list
  - agent.launch
  - worktree.createWithRecipe
  - worktree.waitUntilReady
optionalTools:
  - terminal.getStatus
  - terminal.sendCommandOwned
---
The prototype is evidence, not a base to trust, and its author's view of it is advocacy. Nothing is pushed, merged or deleted unasked. Every phase ends the turn on a notice: know which phase it closes.

On a host: shell steps run there, per the hosts foundation.

Procedure:
1. Pin the prototype: its branch and worktree (`worktree.list`), head SHA, `git status --porcelain` (uncommitted work: the user commits it or rules it out), its base, base SHA, `git merge-base <base> <branch>`. An agent still working in it (`terminal.getStatus`): wait or ask; never audit a moving head.
2. Shortcut inventory: one fresh read-only reviewer, never the prototype's author, in the prototype's worktree (`notify:true, handback:true, replyLines:200`). Prompt: "Read-only: change no files. Read `git diff <merge-base>...<head sha>`. Number every shortcut, file:line and what production needs: mocks, stubs or fake data outside tests; hardcoded URLs, ids, credentials or limits; missing error handling, validation or timeouts; state that should persist (storage, migrations); skipped auth or permission checks; untested behaviour; missing docs; TODO, FIXME and HACK markers; dependencies added and whether they are fit to keep. Then what is sound, and the user scenario it proves works. Then recommend rebuilding from the current base or hardening in place, with reasons. Prose."
3. On its notice, check the list: `git -C <path> diff <merge-base>...<head sha> | grep -nE '^\+.*(TODO|FIXME|HACK|XXX)'` and the manifest diff's added dependencies. Add what it missed and say so.
4. Gate. Show the user the numbered shortcuts (S1 …), the behaviour that must survive (the working scenario and its check), the approach and why, and the checks that define done. Rebuild clean from the current base when the prototype is tangled, untested or on a stale base (it is read, never copied wholesale); harden in place on a new branch from its head when most of it is sound. End the turn. Approval is a plain go. Freeze two lists: required S-items, and deferred ones, never built, audited or corrected without new approval.
5. After the go: re-read both SHAs; one moved since approval goes back to the user. A new worktree, never the prototype's own unless the user says so: `newBranch` off the base (rebuild; a remote base adds `fromRemote:true`) or off the prototype branch (harden). Pass the returned `worktreeId` to `worktree.waitUntilReady` (`timedOut:true`: call again, about ten calls at most) and the launch; only `setupState:"ready"` goes on, else report it. Its `git rev-parse HEAD` must equal the approved source SHA.

```json tool=worktree.createWithRecipe
{"source":{"kind":"newBranch","branchName":"feat/<feature>","baseBranch":"<base or prototype branch>","collisionPolicy":"error"},"focusPolicy":"preserve"}
```

6. One builder there (`name` "Build: <feature>", `notify:true, handback:true`). Brief: the required S-list, each item an acceptance criterion; the behaviour that must survive and its check; the approach; the prototype as `<branch>@<sha>`, to read with `git show` and `git diff`, never merge; "Follow this repo's existing patterns for config, errors, persistence and tests. No mocks outside tests; no TODO left for an S-item; one you cannot do, stop and report why. Do not remove working behaviour to clear an item. Run the project's checks. Commit on this branch; do not push or open a PR. Reply per S-item: what changed and which test covers it."

On notice or resume:
1. A question within the approved plan gets one answer (`terminal.sendCommandOwned`, `notify:true, handback:true`); scope and trade-offs go to the user.
2. Builder done, in its worktree: `git status --porcelain`, `git log --oneline <base>..HEAD`, the project's checks and the survival check run by you. Then one fresh read-only reviewer there (not the builder, not shown anyone's verdict), given the required S-list and the new diff, asked FIXED or NOT FIXED per item with file:line, `notify:true, handback:true`.
3. Reviewer done: open the cited code and test of every FIXED yourself, else it is reviewer-claimed; re-run the marker grep on the new diff.
4. NOT FIXED required items or failing checks: one corrective send to the builder with evidence (`notify:true, handback:true`). On its notice re-run those checks and items yourself; then report.

Stop and recovery: one reviewer per audit, one builder, one corrective round. The prototype's branch and worktree stay as they are: cleanup is daintree.orchestration.workspace-cleanup and a PR is daintree.workflow.prep-branch-for-review, each only on request.

Report back: the prototype's branch and SHA; each required S-item verified by you, reviewer-claimed or not fixed; deferred items; the survival check; the approach; the new branch, worktree and terminal ids; checks you ran with results; what remains before shipping.
