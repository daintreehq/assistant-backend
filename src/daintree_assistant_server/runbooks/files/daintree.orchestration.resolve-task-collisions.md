---
id: daintree.orchestration.resolve-task-collisions
title: Resolve agents colliding on the same task
summary: Two or more agents are already doing the same or contradictory work — freeze what is at risk, gather each one's diff and understanding, give every piece one owner on evidence, stop or redirect the others, and keep the losing work intact.
whenToUse: 'Use when two or more agents are ALREADY working on the same thing, or on contradictory versions of it, and the user wants it sorted out — "claude-2 and codex-1 are both implementing the rate limiter", "two agents got contradictory briefs about the API shape", "these two are fighting over the same files", "I accidentally gave the same issue to two agents", "they keep overwriting each other''s edits". The deliverable is one owner per piece of work, a decision on which work to keep or combine made on evidence (diff, tests, fit to the brief), every other agent stopped or redirected, and the losing work kept rather than deleted. Do not use to forecast conflicts between branches with no live duplication (use daintree.worktree.compare-worktrees), when agents only need separate checkouts (use daintree.worktree.relocate-agents), to merge finished branches (use daintree.git.integrate-agent-branches), for rival entries the user asked for (use daintree.orchestration.competition), to correct one agent''s course (use daintree.agents.steer-running-agent), or for two finished branches whose behaviour clashes once merged (use daintree.git.resolve-semantic-conflict).'
risk: project
requiredTools:
  - terminal.list
  - terminal.getStatus
  - worktree.list
  - terminal.sendCommandMany
optionalTools:
  - terminal.interruptOwned
  - terminal.sendCommandOwned
---
Recency, confidence and who claims more are not evidence. Letting both carry on "to compare" is a competition only the user starts. The losing work is kept: its worktree, branch and uncommitted files stay; nothing is deleted, reset, stashed or closed.

On a host: shell steps via its command runner, or delegate.

Procedure:
1. The set: the agents the user named, else the candidates from `terminal.list` and one `terminal.getStatus` with `includeOutput:{"lines":40}`. Per agent: terminal id, worktree and branch (`worktree.list`), and its original brief from your launch record or this conversation; otherwise mark the brief unknown (its hold reply in step 4 is its understanding, not the brief). Only agents you launched or were handed receive sends; for the rest, give the user the words to type.
2. Freeze what is at risk:
   - Two agents writing in ONE worktree overwrite each other: `terminal.interruptOwned` each working one now. `requested` is not a stop: confirm each from its screen before anything is sent. A pane you do not own, or a stop you cannot confirm: tell the user which pane to stop; choose no writer while it may still be writing.
   - Separate worktrees: nothing is lost mid-turn; the hold in step 4 is read when each turn ends.
3. Evidence, yourself, per worktree: `git -C "<path>" status --short` (untracked files included), `log --oneline <base>..HEAD` and `diff --stat <base>` (committed and uncommitted together) when the base is known, else `diff --stat HEAD` and say committed work is unmeasured; then the full diff of each contested file. In a shared tree git cannot split the hunks by agent: the agents' own accounts attribute them; hunks nobody claims are listed. Run the relevant test in each worktree only where it is cheap and writes nothing tracked; `lastCheckResult` and claimed passes are claims.
4. Hold and ask, one `terminal.sendCommandMany` with `waitForReply:true, handback:true` and `waitSeconds` about 300:

```json tool=terminal.sendCommandMany
{"sends":[{"terminalId":"<agent terminal id>","command":"Hold: another agent has been working on the same task. Stop editing and change no files. Reply in prose: the task and acceptance as you understand them, what you have done, which files you changed, what is tested and passing, and what is left."}],"waitForReply":true,"handback":true,"waitSeconds":300}
```

   Never name the other agent's kind or ask either to judge the other. A `timeout` reply is still working: recheck it. Decide nothing until every hold reply is in; the hold reply is also the stop.
5. Decide per piece of work (a task can split between agents), within the user's request: "sort it out" is the authority; no second yes. Compare against the brief the user gave: fit to it, tests that actually assert it, how much is done, what it breaks. Outcomes: one owner keeps it; the owner also takes named files or commits from the other's worktree (reading there, changing nothing there; across machines, its content goes in the prompt); or the user decides. Ask the user when the briefs contradict (quote both and ask which holds) or the evidence cannot separate them.
6. Execute in one `terminal.sendCommandMany` with `notify:true, handback:true`, only once every other writer has stopped:
   - Owner: "You own <piece>. Acceptance: <criteria>. <Take <files or commits> from <path> on <branch>; read there, change nothing there.> First state in one line what you now own, then continue."
   - Each other agent: "Stop work on <piece>: another agent owns it. Change no more files for it. Your work is kept for reference: do not revert, delete or commit it." Or a distinct new piece with its acceptance.
   - One shared worktree: first save its uncommitted work outside the repo, on its machine (`git -C "<path>" diff HEAD > "${TMPDIR:-/tmp}/collision-<time>.patch"`; untracked files by name); then only the owner writes there. A redirected agent needs its own worktree first (daintree.worktree.relocate-agents). The owner is told which files the other touched; they are now its to keep or rewrite.

On notice or resume: the owner's first line names the piece and it works on it; a part it was to take appears in its diff (`git -C "<path>" diff --stat`), not only in its claim. A stopped agent that kept editing, or the collision recurring: one restated message naming what it did; a second miss goes to the user.

Stop and recovery: one hold question and one decision message per agent. Tidying the losing work later is daintree.orchestration.workspace-cleanup, at the user's request.

Report back: the agents and how they collided; each one's evidence (diff summary, tests you ran kept apart from claims); the decision per piece and who made it; the owner and the redirected or stopped agents with terminal ids; where the kept losing work lives (worktree, branch, patch); what is pending.
