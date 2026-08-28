---
id: daintree.worktree.relocate-agents
title: Move running agents into their own worktrees
summary: Give agents that are ALREADY RUNNING one isolated worktree each — create the empty worktrees, file each terminal under its own, and tell each agent to continue in its new directory.
whenToUse: Use when agents that are already running share a worktree and the user wants them separated — "move each of those into its own worktree", "create four worktrees and move the agents into them", "give each terminal its own branch", "isolate these agents so they stop colliding". The tell is that the terminals ALREADY EXIST and are mid-task; the user wants them relocated, not replaced. Do not use to create a worktree with no agent involved (use daintree.worktree.create-plain-worktree), to spawn NEW agents into fresh worktrees (use daintree.edits.spawn-visible-agent, or daintree.orchestration.fan-out-work for a cohort), to start a forge issue in a worktree (use daintree.workflow.start-work-on-issue), to create a worktree with a startup recipe (use daintree.recipe.run-or-create), or to delete worktrees (use daintree.orchestration.workspace-cleanup).
opener: "Agent relocation — refiling a terminal does not move the running agent"
risk: project
requiredTools:
  - context.snapshot
  - worktree.list
  - worktree.createWithRecipe
  - terminal.moveToWorktree
  - terminal.sendCommand
  - terminal.read
---
Use when: live agents are sharing one worktree and each needs its own, without losing the sessions they are already in.

The load-bearing fact, before anything else: **filing a terminal under a different worktree does not move the process.** The agent keeps running in the directory it launched from. What actually relocates the work is TELLING it, in its own session, to continue in the new directory. Treat the tell as the operation and the filing as bookkeeping — a run that files four terminals and stops has moved nothing.

Procedure:

1. Route elsewhere first. New agents that do not exist yet are a spawn, not a relocation — `daintree.orchestration.fan-out-work` for a cohort, `daintree.edits.spawn-visible-agent` for one. A worktree with nothing running in it is `daintree.worktree.create-plain-worktree`.

2. Inventory from live state. Read the `# Open terminals` roster already in this turn for each terminal's id, worktree and agent; call `context.snapshot` only when the roster is ambiguous or a target may be gone. Call `worktree.list` for the worktrees that already exist and their paths. Copy every id in full (foundation rule 3). Confirm which terminals the user actually means: an unmarked row may be their own shell or another agent's work, and relocating someone else's terminal interrupts it.

3. Settle every branch name and the base branch BEFORE mutating. One worktree per agent, named for what that agent is doing rather than for its index, so the roster stays readable. If the user says the names can be anything, choose them, state them once, and proceed. If the base is ambiguous, ask.

4. Create the worktrees EMPTY. One `worktree.createWithRecipe` per agent carrying only `branchName` and `baseBranch` inside `arguments`, plus a `requestKey` so a retried timeout cannot create a second worktree.

       worktree.createWithRecipe({
         "arguments": { "branchName": "<branch>", "baseBranch": "<base>" },
         "requestKey": "<stable-id-for-this-request>"
       })

   **Omit `recipeId`.** It is not an optional extra here, it is the difference between an empty worktree and a wrong one: a `recipeId` makes the call spawn that recipe's terminals, so you end up with a second set of agents beside the ones you were asked to move, and it also elevates the dispatch to confirmation-gated, so the user gets a modal for what should have been silent. Do not list or inspect recipes on this path at all.

5. End the turn on those creates and read the real values back. The `worktreeId` and `worktreePath` are minted by this reply, so nothing in the SAME reply can use them (foundation rule 9). Take `worktreePath` from the result verbatim; do not assemble a path by hand from the branch name.

6. File the terminals under their worktree. `terminal.moveToWorktree` is a TYPED wrapper: call it directly, never through `daintree.call`, which returns `USE_TYPED_WRAPPER` and does nothing.

       terminal.moveToWorktree({ "terminalIds": ["<id>", "<id>"], "worktreeId": "<worktreePath>" })

   Send the whole cohort in ONE call with `terminalIds`; use the single `terminalId` form only when moving one. **`worktreeId` takes the worktree PATH, not the id** — the exact `worktreePath` you read back in step 5. A branch name or a worktree id is never accepted. If the move is refused, report the refusal verbatim and stop there: do NOT retry it under another name, do NOT reach for raw `git worktree`, and do NOT fall back to closing the agent and respawning it, which throws away the session you were asked to preserve. Step 7 still runs for every terminal either way.

7. Tell each agent where it now lives. One `terminal.sendCommand` per terminal, with exactly the sentence Daintree itself uses for this:

       terminal.sendCommand({ "terminalId": "<id>", "command": "Please continue in the directory <worktreePath>" })

   Send it once per agent. Step 6 re-files the pane; THIS is what moves the work, so it runs for every terminal regardless of how step 6 went.

8. Do not re-send it. An agent parks at `waiting` with reason `prompt` for a moment BEFORE it picks a delivered instruction up, so a roster still reading waiting right after the send means not-yet-started, not idle-and-ignoring-you. A second send queues a second instruction into a live session and can interleave with whatever the agent was mid-way through. If you need to know it landed, read a bounded tail with `terminal.read` on the next turn instead.

9. Verify from state, not from intent. Re-read the roster or `context.snapshot`: the new worktrees exist at the returned ids, each target terminal is still ALIVE, and the original worktree is still on its own branch. Never claim a terminal is working because you sent it something; report what the state actually says, and if a status you just read contradicts a sentence you just wrote, the state wins.

10. Close nothing. The terminals you relocated are the ones you were asked to keep. `terminal.close` is only ever at the user's explicit request, never your own tidy-up — and never as a recovery move when step 6 is refused.

Confirmation: creating a worktree and a branch mutates project state and the wrapper is marked as such, so the harness prompts at the active tier. Do not add a confirmation turn of your own on top of it, and do not ask permission per terminal for the sends.

Report back: the worktrees created (id, path, branch, base), which terminal was filed under which, whether the filing step succeeded or was refused, that every agent was told once to continue in its new directory, and anything you deliberately did not do — no terminal closed, no agent respawned, no raw git.
