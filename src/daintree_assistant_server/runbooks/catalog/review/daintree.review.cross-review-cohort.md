---
id: daintree.review.cross-review-cohort
title: Have parallel agents review each other's work
summary: After several agents finish separate units, assign each a peer's frozen work to review read-only (never its own, author unnamed), verify the findings, then relay them to each author for one round of fixes or rebuttals.
whenToUse: 'Use when a COHORT of agents that each finished their own unit should review EACH OTHER — "have the agents review each other''s PRs", "each agent reviews another one''s branch, then fixes what its reviewer found", "swap reviews between the three worktrees", "rotate the reviews so nobody checks their own work". The deliverable is one verified peer review per unit, each author''s fixes or rebuttals, and your check of the fixes. Do not use when fresh read-only reviewers judge a change and nothing is fixed (use daintree.review.review-change), for an open PR queue given one new reviewer each (use daintree.review.review-pr-queue), for one named reviewer and one named fixer (use daintree.agents.relay-review-feedback), for a writer and reviewer looping until approval (use daintree.orchestration.review-until-approved), to vote a winner among rival versions of one task (use daintree.orchestration.competition), or to confirm one agent''s claim (use daintree.orchestration.verify-agent-work).'
risk: project
requiredTools:
  - terminal.getStatus
  - terminal.sendCommandMany
optionalTools:
  - terminal.list
  - worktree.list
  - agent.launch
---
Reviewers never get their own work and are never told whose they review. Findings reach an author only after you verify them. One review round and one fix round. Every git read, test run and check below is yours; never send one to a member to run.

On a host: shell steps run there, per the hosts foundation; never by a member.

Procedure:
0. Units not built yet (the user asked for the work and the swap together): build them first per daintree.orchestration.fan-out-work, each in its own new worktree and branch (`worktree.createWithRecipe` with `{"source":{"kind":"newBranch","branchName":"<unit>","baseBranch":"<base>","collisionPolicy":"error"}}`, then `worktree.waitUntilReady`) and its own Daintree `agent.launch`, never side by side in one checkout and never a native subagent; this runbook starts when they have finished.
1. Cohort: the units the user meant, each with its author's terminal id, worktree path, base and acceptance, from your records or `terminal.list` and `worktree.list`. Read every author in one `terminal.getStatus`. `working`, or `waiting` with `waitingReason` `question` or `approval`, has not finished: leave its unit out or wait for it. `waiting` at `prompt` ended its turn; its handback and the checkout say whether the unit is done. You send to every member: a terminal you neither created nor were handed returns `RESOURCE_NOT_OWNED`; ask the user to hand it over.
2. Freeze each unit at a commit: `git -C <path> rev-parse HEAD` as its frozen SHA, with `git -C <path> status --porcelain` empty. A unit with uncommitted or untracked changes in scope: if the user's request lets its author commit, ask it to (one `terminal.sendCommandMany` to those authors, `waitForReply:true, handback:true`) and freeze after; otherwise leave it out and say why. The review is of that commit, not the live tree. A unit's base is the commit its own work started from, so `diff <base> <sha>` holds only its author's commits; a range that also holds the reviewer's own commits is self-review: fix the base, or leave the unit out.
3. Assign reviewers so nobody gets its own unit: with two, swap; with more, shuffle the order and have each review the next, preferring a different agent kind where there is a choice. Keep the map to yourself.
4. Review round: one `terminal.sendCommandMany` to all reviewers, then read each `reply`. Never say who wrote the work. A reviewer on another machine than its unit gets the diff itself, not a path.

```json tool=terminal.sendCommandMany
{"sends":[{"terminalId":"<reviewer terminal id>","command":"Peer review. Worktree <path>, base <base>, frozen at <sha>: review git -C <path> diff <base> <sha> against: <acceptance>. Criteria: <criteria>. Per finding: severity, file:line, the trigger and the fix; say nothing found rather than stay silent; name the commit you read. At most 40 lines. Read-only: do not edit that worktree or your own, and change no external state."}],"waitForReply":true,"handback":true,"waitSeconds":900}
```

   Read each item's result: one that errored is not a review in progress. A timed-out reply is pending.
5. Verify every finding against the source at the frozen state (read the hunk, run the unit's tests); drop one with no evidence and say so; mark preferences as nits. Files a reviewer changed during its review are a boundary failure to report. A unit whose HEAD moved or tree got dirty since the freeze has possibly stale findings: say so.
6. Fix round only when the user asked the authors to address the findings; otherwise report and stop. The fix round: one `terminal.sendCommandMany` to the authors, each with only its own verified findings, credited to "a peer reviewer", and "Address or rebut each finding with evidence, run the tests, commit, and reply with what you changed per finding", with `notify:true, handback:true`.

On notice or resume:
1. Per author: `git -C <path> diff <frozen sha>` plus `git -C <path> status --porcelain` for new untracked files, read against each finding, then the unit's tests run by you. A finding claimed fixed with no matching change is unaddressed.
2. A rebuttal is settled by you from the code, or goes to the user; it is never sent back to the reviewer for another round.

Stop and recovery: one review round and one fix round; no ping-pong. A reviewer that exits or fails: reassign its unit once to another member that is not the author, or launch one fresh read-only reviewer in that worktree (`agent.launch` with `waitForReply:true, handback:true`); never let an author review itself. Nothing is pushed, merged or posted to the forge unless asked.

Report back: the assignment map, de-blinded; per unit the frozen state reviewed, verified and dropped findings, the author's response, the diff since the freeze and your test results; units left unreviewed or unfixed and why; anything still running.
