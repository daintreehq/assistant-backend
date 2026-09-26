---
id: daintree.edits.spawn-visible-agent
title: Spawn a visible agent for edits or exploration
summary: How to handle requests that need a visible agent — to change files or to explore read-only.
whenToUse: Use when the user asks for a direct visible agent to change project files (implement, refactor, fix, add tests, update docs) or to explore/investigate a project read-only, and no more specific workflow applies. Prefer a typed workflow first — daintree.workflow.start-work-on-issue for a forge issue, daintree.workflow.prep-branch-for-review for a PR, daintree.recipe.run-or-create for a workspace recipe, daintree.orchestration.fan-out-work when several independent units each need their own agent, or a poll/competition/interview/multi-agent runbook when several agents work together. This is the fallback SINGLE visible-agent path; if the agent is already running and has gone wrong use daintree.agents.recover-agent, and if an agent that is already running is to be moved to a new or another worktree, with or without a task there, use daintree.worktree.relocate-agents. If all the user wants is ONE read-only question answered and relayed back — the deliverable is the agent's answer, not work landing — use daintree.agents.ask-and-relay instead. If the deliverable is UNDERSTANDING the codebase rather than changing it, use daintree.explore.repository; if it is a judgement on an existing change, use daintree.review.review-change; if it is a failing test or check, use daintree.ci.diagnose-and-fix. Spawn here only when the work begins THIS turn; if it is scheduled to begin at a stated later time, use daintree.orchestration.watch-outcome instead.
risk: project
requiredTools:
  - worktree.list
  - agent.launch
optionalTools:
  - agent.listPresets
  - worktree.createWithRecipe
  - worktree.waitUntilReady
  - terminal.list
  - terminal.getStatus
  - terminal.sendCommandOwned
  - terminal.notifyWhenIdle
  - terminal.interruptOwned
  - slashCommands.list
  - skills.search
  - skills.load
---
If the user asked you to do the work, do it yourself; this is for work they want a visible agent to perform.

Procedure:
1. Brief. Keep the user's words as the task, including any command or skill they named. Decide edits or read-only. Write acceptance criteria as checkable statements ("an unknown id returns 404", "nothing outside `src/auth` changed"), not a restatement; if tests cannot run here, say so in the criteria.
2. Destination: resolve an id and path from `worktree.list`. Use a named worktree, branch or path exactly. With none named, use the checkout the work is about; "here" is your own (`DAINTREE_WORKTREE_ID` in a pane), not whatever the user has selected. Several worktrees is not ambiguity. Record the starting revision and a hash of `git diff HEAD` plus untracked files: existing changes may be the user's.
3. A new worktree, when asked: `worktree.createWithRecipe` without a recipe, then `worktree.waitUntilReady` (an API-key client re-reads `setupState` in `worktree.list`). Only `setupState:"ready"` admits the launch. `timedOut` while running: call again a few times, then report and launch when the user resumes. `failed`, `needs-approval` or `unknown`: stop and report `stage` and `error`.
4. A named command or skill is resolved for that agent in the destination (`slashCommands.list` with `agentId` and `projectPath`, `skills.search` then `skills.load`, or its command files), never guessed. Put the exact token (`/name`, or `$name` for a Codex skill) first in the prompt, then the full brief. Not found: ask; never substitute a similar command or a recipe. Several ordered commands are separate rounds: each later one is sent only after its predecessor is verified. Never concatenate them.
5. Prompt adds: the revision and existing changes; for edits, change only this worktree, do only the stated scope and report skipped adjacent work, run the relevant tests, and report changed files, commands with results and "unverified because …" gaps. "No change needed" is a valid outcome. Read-only asks for `file:line` evidence.
6. Launch once:

```json tool=agent.launch
{"agentId":"<built-in id>","name":"Fix: unknown id returns 404","worktreeId":"<resolved worktree id>","prompt":"<task, checkout, scope, mode, acceptance, reporting>","handback":true,"notify":true}
```

7. Inspect the result:
   - `worktreeId` or `worktreePath` null: unconfirmed, not wrong. Read it from `terminal.list`.
   - Different from what you resolved: the agent is in the wrong checkout and its work is not the requested work. Interrupt it if working (`terminal.interruptOwned`), check that checkout for its changes, report both paths and ask before closing. Relaunch only after it stops, with a changed `name`.
   - `missing-cli` or `launched:false`: report or reconcile per the foundation.
8. Report the terminal id and end your turn.

On notice or resume:
1. A question: answer once from the request or scope guard, protocol-exact (the token or key the prompt shows), with `notify` again. Tests proving the requested behaviour are in scope; optional adjacent work gets "no, stay in scope". A trade-off, new scope or an irreversible step goes to the user. The same question after a delivered answer means blocked: report it.
2. A report is a claim. Check each criterion yourself in the child's worktree path (`git status`, the diff, the relevant tests). A criterion with no named test, result or changed file is unverified, not met: verify it, or send ONE follow-up asking for all missing evidence at once. Note the revision you checked. Before commit, push, PR or hand-over of a substantive change, use daintree.orchestration.verify-agent-work.
3. Files changed under a read-only brief are a boundary failure: report them.

Stop and recovery: one launch per task. Never relaunch healthy work: a working agent with no notice pending gets `terminal.notifyWhenIdle`; anything worse is daintree.agents.recover-agent. One evidence follow-up; a gap still open goes to the user.

Report back: agent and terminal id; worktree id, path and branch; what changed and which checks ran, marked verified by you or claimed by the agent; criteria unverified; skipped out-of-scope work; what is still running.
