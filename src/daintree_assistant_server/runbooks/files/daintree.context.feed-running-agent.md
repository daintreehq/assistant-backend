---
id: daintree.context.feed-running-agent
title: Give a running agent codebase context
summary: Get a chosen slice of the codebase into an agent that is already open — by pointing it at the files, by a curated CopyTree bundle it reads, or by Daintree's whole-worktree injection only when asked and only after checking which worktree it would inject.
whenToUse: 'Use when an agent that ALREADY EXISTS should be given codebase context: "inject the context into that Claude", "give the Codex agent the whole auth module as context", "load the spec and the related code into the implementer", "paste the worktree context into terminal 3", "feed it the relevant files before it continues". The deliverable is that agent holding the named context, confirmed from its reply or screen, followed by the user''s instruction if they gave one. Do not use to produce a bundle FILE for the user or the clipboard (daintree.context.curate-copytree-bundle), when the context is part of launching a NEW agent (daintree.edits.spawn-visible-agent, daintree.orchestration.fan-out-work for several, daintree.workflow.start-work-on-issue for an issue), to correct what the agent is doing (daintree.agents.steer-running-agent), or to hand its task to a different agent (daintree.agents.hand-off-task).'
risk: terminal
requiredTools:
  - terminal.list
  - terminal.getStatus
  - terminal.sendCommandOwned
optionalTools:
  - worktree.list
  - terminal.notifyWhenIdle
  - copyTree.generate
  - actions.getContext
  - terminal.injectOwned
---
Agents read files themselves, and every byte pasted into a prompt costs their context window, so the cheapest route that carries the context wins. `terminal.injectOwned` is the blunt one: it writes the whole of the user's ACTIVE worktree (the one selected in the app, not necessarily the agent's) as raw text into the agent's input, cannot be scoped, returns before anything is written, and reports nothing back.

Procedure:
1. Target: a terminal you own or were handed (`terminal.list` with `owned:true`; its row gives `worktreeId`, and `worktree.list` the path). Not yours: give the user the paths and instruction, or ask for a hand-over. One `terminal.getStatus` with `includeOutput:{"lines":30}`. `working`: arm `terminal.notifyWhenIdle` (note "feed context: <what>") and end your turn; never inject into a busy agent (it stays pending and writes later, unseen). Asking a question: that comes first (daintree.agents.answer-waiting-agent).
2. Settle what is wanted: the named files, or a feature area named by its directories (a quick `git -C "<path>" ls-files` finds them; the agent reads onward from there). Keep credentials, `.env` files and generated or vendored bulk out.
3. Route, and say which:
   - Paths (the default when the files are in the agent's own checkout and the user did not ask for a bundle or the whole worktree): go to step 4 with the list.
   - Bundle (the user asked for a CopyTree or context bundle, the files live in another worktree, the set is large and scattered, or "inject" names only part of the code): one `copyTree.generate` with the source worktree's explicit `worktreeId` and a `name`. Select with a non-empty list (an empty one is rejected): `scopePaths` for whole directories as literal paths, `includePaths` for files and globs.

```json tool=copyTree.generate
{"worktreeId":"<source worktree id>","name":"auth module context","options":{"scopePaths":["src/auth","tests/auth"],"includePaths":["docs/auth.md"]}}
```

     Check the result: `stats.noFilesMatched` or `unmatchedSelector` means fix the selection once, then stop and report what is missing; `stats.truncated` means files were dropped or cut; `stats.budgetExceeded` means the kept bundle is over the size budget; a `fileCount` short of what you selected means something is missing. Name any gap in the agent's instruction. `filePath` is temporary and pruned by age: send it now.
   - Injection, only when the user asked for the WHOLE worktree injected: read `actions.getContext`; `activeWorktreeId` is what gets injected (`worktree.getCurrent` from a pane names the pane's own worktree, not this). None active: the call silently does nothing, so do not call it. A different worktree from the agent's: do not `worktree.setActive` (it moves the user's view); tell the user and offer the bundle route, or ask them to select that worktree. An instruction must follow it: use the bundle route instead, since nothing tells you when the injection finished writing. Otherwise call once with `{"terminalId":"<agent terminal id>"}`, then one `terminal.getStatus` with `includeOutput`, and report what you saw as the stage reached, not as delivered. Never inject again (a second dump doubles it); a failure shows in the app's injection error display, so ask the user what it showed.
4. One send carries the pointer and the instruction together:

```json tool=terminal.sendCommandOwned
{"terminalId":"<agent terminal id>","command":"Context for your task: read <paths, or the bundle at filePath> now, before continuing. They cover <why they matter>. <Gaps: files the bundle dropped.> Then <user's instruction, or: reply in two lines naming what you read; change nothing>.","waitForReply":true,"handback":true,"waitSeconds":180}
```

   With a real instruction that takes a while, pass `notify:true, handback:true` instead of `waitForReply` and end the turn.
5. Delivered means its reply names or uses what it read; "done" alone is not evidence.

On notice or resume: the armed notice means it stopped; recheck its state and continue at step 2. A reply to your send: step 5.

Stop and recovery: one delivery per request; at most one corrected `copyTree.generate`; never a second injection. Never switch the user's active worktree.

Report back: the route and why; the agent and terminal id; the worktree; the files or bundle `filePath` with `fileCount`, `estimatedTokens` and any flag that fired; how delivery was confirmed, or the injection stage observed and that it is unverified; the instruction sent.
