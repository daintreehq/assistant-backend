---
id: daintree.edits.spawn-visible-agent
title: Spawn a visible agent for edits or exploration
summary: How to handle requests that need a visible agent — to change files or to explore read-only.
whenToUse: Use when the user asks for a direct visible agent to change project files (implement, refactor, fix, add tests, update docs) or to explore/investigate a project read-only, and no more specific workflow applies. Prefer a typed workflow first — daintree.workflow.start-work-on-issue for a forge issue, daintree.workflow.prep-branch-for-review for a PR, daintree.recipe.run-or-create for a workspace recipe, daintree.orchestration.fan-out-work when several independent units each need their own agent, or a poll/competition/interview/multi-agent runbook when several agents work together. This is the fallback SINGLE visible-agent path; if the agent is already running and has gone wrong use daintree.agents.recover-agent. If all the user wants is ONE read-only question answered and relayed back — the deliverable is the agent's answer, not work landing — use daintree.agents.ask-and-relay instead. If the deliverable is UNDERSTANDING the codebase rather than changing it, use daintree.explore.repository; if it is a judgement on an existing change, use daintree.review.review-change; if it is a failing test or check, use daintree.ci.diagnose-and-fix. Spawn here only when the work begins THIS turn; if it is scheduled to begin at a stated later time, use daintree.orchestration.watch-outcome instead.
risk: project
requiredTools:
  - agent.listAvailable
  - worktree.list
  - agent.launch
  - terminal.getStatus
  - terminal.getOutput
optionalTools:
  - agent.listPresets
  - worktree.createWithRecipe
  - worktree.waitUntilReady
  - terminal.list
  - terminal.readLastMessageOwned
  - terminal.sendCommandOwned
  - terminal.notifyWhenIdle
  - terminal.waitUntilIdle
  - terminal.interruptOwned
  - slashCommands.list
  - skills.search
  - skills.load
---
Use when: the user wants a visible Daintree agent to do engineering work (change files, or investigate read-only while they watch and steer) and no more specific workflow fits. When the user asked you to do the work, do it yourself; this is for work they want another visible agent to perform.

Availability: Core. Needs `agent.launch` and terminal reads. With `notify` on the launch, Daintree tells you when the agent stops. An API-key client has no `notify` and no `worktree.waitUntilReady`: use one bounded wait or tell the user the result needs their continuation, and launch only into a worktree whose `setupState` in `worktree.list` is already `ready`. `slashCommands.list`, `skills.search` and `skills.load` raise an approval at core tier. Verification uses your own shell and file tools (git, the project's tests) in the child's worktree path.

Procedure:
1. Settle the brief before launching. Keep the user's words as the task, including any command or skill they named. Decide the mode (edits or read-only) and write acceptance criteria as checkable statements, not a restatement of the task: "an unknown id returns 404", "the existing suite passes", "nothing outside `src/auth` changed". If tests genuinely cannot run here, say so in the criteria instead of leaving the subject out.
2. Resolve the agent through `agent.listAvailable`, and a preset only when asked for one.
3. Resolve the destination to an id and path from `worktree.list`. Use a named worktree, branch or path exactly; never substitute another. With none named, use the checkout the work is about, which for "here" is your own (`DAINTREE_WORKTREE_ID` in a pane, otherwise the listed worktree whose path holds your working directory), not whatever the user has selected. Several worktrees existing is not ambiguity; ask only when a named destination cannot be resolved. Record the starting revision there and an identity for its uncommitted changes (a hash of `git diff HEAD` plus the untracked file list): HEAD and a dirty flag miss a file edited again, and existing changes may be the user's.
4. A new worktree, when asked for: create it without a recipe, then wait for setup. Only `setupState:"ready"` is success; launching earlier puts the agent in an unpopulated tree.

```json tool=worktree.createWithRecipe
{"source":{"kind":"newBranch","branchName":"<valid branch name>","baseBranch":"<verified base>","collisionPolicy":"error"}}
```

```json tool=worktree.waitUntilReady
{"worktreeId":"<returned worktree id>","timeoutMs":25000}
```

   An API-key client re-reads `setupState` in `worktree.list` instead. `timedOut:true` while `pending` or `running` means call again, a few times at most; if setup is still running, report that and launch when the user resumes, not before. `failed`, `needs-approval` (the repository's setup commands wait for the user's approval, which you cannot give) and `unknown` stop here: report the `stage` and `error`. Carry the returned worktree id and path.
5. A named custom command or skill is resolved, never guessed. For the target agent and the destination checkout, find it with `slashCommands.list` (with that `agentId` and the destination path as `projectPath`), `skills.search` then `skills.load` for a plugin skill, or the agent's own command and skill files in that checkout or the user's config, read with your file tools. Keep the exact token (`/name` for Claude Code, `$name` for a Codex skill), put it first in the prompt, and follow it with the user's full brief. If it cannot be found, ask; never substitute a similar command or a recipe. Several ordered commands are separate rounds: only the first goes in the launch prompt, and each later one is sent (`terminal.sendCommandOwned` with `notify`) only after its predecessor's result is verified. Never concatenate them.
6. Write the prompt with the foundation's brief fields and the revision and uncommitted changes you recorded. For edits add: change only this worktree; do only the stated scope, and skip and report adjacent or optional work instead of doing it or asking about it, unless it blocks the task; run the relevant tests; report changed files, commands run with results and remaining risks, writing "unverified because …" rather than staying silent. A justified "no change needed" is a valid outcome. For read-only, ask for file:line evidence.
7. Record the checkpoint (task, agent, worktree id and path, acceptance, step), then launch once:

```json tool=agent.launch
{"agentId":"<id from agent.listAvailable>","name":"Fix: unknown id returns 404","worktreeId":"<resolved worktree id>","prompt":"<task, checkout, scope, mode, acceptance, reporting, boundaries>","handback":true,"notify":true}
```

   An API-key client omits `notify`:

```json tool=agent.launch surface=external
{"agentId":"<id from agent.listAvailable>","name":"Fix: unknown id returns 404","worktreeId":"<resolved worktree id>","prompt":"<task, checkout, scope, mode, acceptance, reporting, boundaries>","handback":true}
```

8. Inspect the result before anything else:
   - Returned `worktreeId` or `worktreePath` is null: unconfirmed, not wrong. Read the terminal's `worktreeId` in `terminal.list`; only a proven mismatch is handled as below.
   - Returned `worktreeId` or `worktreePath` differs from what you resolved: the agent is in the wrong checkout and its work is not the requested work. If it is working, stop its turn with `terminal.interruptOwned`; check that checkout for changes it made; report both paths, and ask before closing it. Launch again only after the stray agent is stopped, once, with a changed `name`, since an identical launch within two minutes returns the cached result.
   - `spawnStatus:"missing-cli"`: a setup panel, not a worker. Report it.
   - `launched:false`: nothing was created and nothing is pending, usually because a same-kind launch was still starting. Reconcile before one retry.
   - A lost or ambiguous reply: look for the panel with `terminal.list`; never launch a second worker.
9. `launched:true` is a panel whose process is starting, not an agent that has read its prompt. One `terminal.getStatus` with `includeOutput` on it catches a startup trust, login or permission screen: that is the user's to answer, and your prompt waits behind it.
10. With the notice requested, report the terminal id and that you will check the result when Daintree reports it stopped, then end your turn: no polling, and no `terminal.notifyWhenIdle` on the same terminal. Without one, a single `terminal.waitUntilIdle`, then report it pending if it is still working.

On notice or resume:
1. Follow the foundation's resume procedure. Read `terminal.getStatus` with `includeOutput`, then the report: `terminal.readLastMessageOwned` for a Claude Code pane, `terminal.getOutput` otherwise.
2. A question: answer it from the original request or the scope guard, once, protocol-exact (the token or key the prompt shows; `QUESTION_ID=<id> ANSWER=<answer>` only when that protocol is displayed), with `terminal.sendCommandOwned` and `notify` again. Tests that prove the requested behaviour are in scope; optional adjacent work gets "no, stay in scope". A real trade-off, new scope or an irreversible step goes to the user. The same question after one correctly delivered answer means blocked: report it, no variants.
3. A completed report is a claim. Check every acceptance criterion yourself in the child's worktree path, never your own checkout: `git status` and the diff there, and the relevant tests. A report that names no tests, results or changed files for a criterion leaves that criterion unverified, not met. Verify it yourself, or send ONE in-scope follow-up with `notify` asking for the evidence of every missing criterion at once; never report done on the agent's word. Note the revision and diff identity you checked: later edits make that check stale. For a substantive change, or before an irreversible next step (commit, push, PR, handing the worktree on), use daintree.orchestration.verify-agent-work.
4. Files changed under a read-only brief are a boundary failure: report them.
5. Report once, keeping what you checked apart from what the agent claims. Leave the terminal open unless asked to close it.

Stop and recovery: the launch `prompt` was the first turn. Never send it again, even when the agent looks idle, the handback is missing or a notice seems late; read status and screen instead. One launch per task; an ambiguous launch is reconciled, not repeated. Never relaunch healthy work to restore observation: if it is working with no notice pending, arm `terminal.notifyWhenIdle`, and use daintree.agents.recover-agent for anything worse. At most one evidence follow-up per task, covering every missing criterion; a gap still open after it goes to the user.

Report back: the agent and terminal id; worktree id, path and branch; what changed and which checks ran with results, marked as verified by you or claimed by the agent; criteria still unverified; out-of-scope work skipped; and what is still running and what will bring you back.
