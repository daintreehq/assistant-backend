---
id: daintree.edits.spawn-visible-agent
title: Spawn a visible agent for edits or exploration
summary: The default path for a code change in Daintree — brief one visible agent in the right worktree, launch it, and verify its work yourself.
whenToUse: Use for any request to CHANGE project code or files that no more specific workflow owns — "add a retry to the webhook sender", "fix the off-by-one in pagination", "add tests for X" — or asks for a visible agent to do it. The work goes to ONE visible agent in the right worktree, on any model or effort named. Prefer a typed workflow (daintree.workflow.start-work-on-issue for an issue, daintree.orchestration.fan-out-work for several units, the poll/competition/multi-agent runbooks when agents work together). This is the fallback SINGLE-agent path, for work starting THIS turn (a later start is daintree.orchestration.watch-outcome). Do not use for an agent ALREADY RUNNING that has gone wrong (daintree.agents.recover-agent), is to move worktrees (daintree.worktree.relocate-agents), or is to be given context (daintree.context.feed-running-agent), a correction (daintree.agents.steer-running-agent) or one of its CLI commands (daintree.agents.run-agent-command); for ONE read-only question relayed back (daintree.agents.ask-and-relay); for UNDERSTANDING the codebase (daintree.explore.repository), judging an existing change (daintree.review.review-change) or a failing test or check (daintree.ci.diagnose-and-fix); a plan approved before code (daintree.orchestration.plan-then-implement), tests and code by separate agents (daintree.orchestration.test-first-split) or a time-limited attempt (daintree.orchestration.timebox-attempt); for a plain shell command (daintree.terminals.run-visible-command); a dependency or framework upgrade (daintree.deps.upgrade-dependency); or to rewrite docs to match the code (daintree.edits.sync-project-docs).
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
A code change goes to a visible agent; you brief, launch and verify. Edit it yourself only when the user asks you to.

On a host: shell steps run there, per the hosts foundation.

Procedure:
1. Brief. Keep the user's words as the task, including any command or skill they named. Decide edits or read-only. Write acceptance criteria as checkable statements ("an unknown id returns 404", "nothing outside `src/auth` changed"), not a restatement; if tests cannot run here, say so in the criteria.
2. Destination: resolve an id and path from `worktree.list`. Use a named worktree, branch or path exactly. With none named, use the checkout the work is about; "here" is your own (`DAINTREE_WORKTREE_ID` in a pane), not whatever the user has selected. Several worktrees is not ambiguity. Record the starting revision and a hash of `git diff HEAD` plus untracked files: existing changes may be the user's.
3. A new worktree, when asked: `worktree.createWithRecipe` without a recipe, then `worktree.waitUntilReady` (an API-key client re-reads `setupState` in `worktree.list`). Only `setupState:"ready"` admits the launch. `timedOut` while running: call again a few times, then report and launch when the user resumes. `failed`, `needs-approval` or `unknown`: stop and report `stage` and `error`.
4. A named command or skill is resolved for that agent in the destination (`slashCommands.list` with `agentId` and `projectPath`, `skills.search` then `skills.load`, or its command files), never guessed. Put the exact token (`/name`, or `$name` for a Codex skill) first in the prompt, then the full brief. Not found: ask; never substitute a similar command or a recipe. Several ordered commands are separate rounds: each later one is sent only after its predecessor is verified. Never concatenate them.
5. Prompt adds: the revision and existing changes; for edits, change only this worktree, do only the stated scope and report skipped adjacent work, run the relevant tests, and report changed files, commands with results and "unverified because …" gaps. "No change needed" is a valid outcome. Read-only asks for `file:line` evidence.
6. Launch once. A model the user named: `model`, as `agent.listModels` spells it; a named effort: `agentLaunchFlags` (Claude Code `["--effort","<level>"]`, Codex `["-c","model_reasoning_effort=<level>"]`). A choice left to you: daintree.agents.choose-agent-model (`get_runbook` if absent).

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
2. A report is a claim, never your verification. Before reporting, run yourself in the child's worktree path `git status --porcelain`, `git diff` and the tests the criteria name, and check each criterion against what they show. A criterion with no named test, result or changed file is unverified, not met: verify it, or send ONE follow-up asking for all missing evidence at once. Note the revision you checked. Before commit, push, PR or hand-over of a substantive change, use daintree.orchestration.verify-agent-work.
3. Files changed under a read-only brief are a boundary failure: report them.

Stop and recovery: one launch per task. Never relaunch healthy work: a working agent with no notice pending gets `terminal.notifyWhenIdle`; anything worse is daintree.agents.recover-agent. One evidence follow-up; a gap still open goes to the user.

Report back: agent and terminal id; worktree id, path and branch; what changed and which checks ran, marked verified by you or claimed by the agent; criteria unverified; skipped out-of-scope work; what is still running.
