---
id: daintree.explore.repository
title: Understand a repository
summary: Answer "what does this repo do" / "where is X" with a read-only investigation and cited evidence.
whenToUse: Use when the user wants to UNDERSTAND the project rather than change it — "what does this repo do", "where is X implemented", "how does the auth flow work", "explain this subsystem", "what would I have to touch to add Y", "find the code that does Z", or orientation before work starts. The deliverable is an explanation with file evidence and nothing on disk changes. Do not use when the user wants files CHANGED (make the change yourself, or use daintree.edits.spawn-visible-agent when they want a visible agent to make it, or daintree.workflow.start-work-on-issue for a forge issue), to judge the quality of a specific change or PR (use daintree.review.review-change), to work out why a check is failing (use daintree.ci.diagnose-and-fix), to ask about DAINTREE itself rather than the project (a documentation question — no runbook needed — or daintree.diagnostics when Daintree is broken), or to pick out files to bundle as context (use daintree.context.curate-copytree-bundle), or when the user names a specific agent to ask and just wants its answer relayed back (use daintree.agents.ask-and-relay).
risk: project
requiredTools:
  - worktree.list
optionalTools:
  - agent.listAvailable
  - agent.launch
  - terminal.getStatus
  - terminal.getOutput
  - terminal.readLastMessageOwned
  - slashCommands.list
---
Use when: the user wants to understand the codebase: what it does, where something lives, how a subsystem works, what a change would touch.

Availability: Hybrid. The default is to read and search yourself with your own file and search tools in the verified checkout. A native research subagent, where your host has one, suits broad finding work because its reads stay out of the conversation; it is not a Daintree pane. A visible Daintree agent is only for when the user asks to watch or steer the investigation in Daintree; it needs `agent.launch` and continues on a notice. For one named agent's answer relayed back, use daintree.agents.ask-and-relay instead.

Which slash commands or plugins an agent CLI offers is not a repository question: user-level ones live in home config (such as `~/.claude`), outside the project, so an empty project search proves nothing. `slashCommands.list` reports them where it is offered.

Procedure:
1. Fix the target. Use the worktree the user named, or the one the question is about, and resolve its id and path with `worktree.list`. If it is not your own working directory, read in ITS path (absolute paths, `git -C <path>`) and never answer from your own checkout while describing another. Note the branch and HEAD: "the current code" means that branch, and say whether uncommitted changes are included. Before you start, record `git status --porcelain` there and a hash of `git diff HEAD` (status alone misses a further edit to a file that was already dirty).
2. Frame one question with a required answer shape. For a vague "what does this do" on a large repo, map it first: project instructions (README, CLAUDE.md, AGENTS.md), manifests, entry points, top-level layout and tests, and state the coverage. Ask for a narrower focus only when the wanted outcome is unclear; an explicit request for a comprehensive survey is authority to investigate broadly.
3. Investigate read-only. Search for distinctive identifiers, read what the evidence points to, and follow imports and callers one hop out. Tell generated and vendored code from maintained source. Write nothing: no edits, formatting, installs, builds that emit artefacts, or git commands that change state (checkout, stash, reset). If a command you would need writes into the tree, do not run it; say what it would have told you.
4. Delegate broad finding work to a native subagent when you have one: send the question and the deliverable ("`file:line` for each claim; say what you could not find"). Independent questions can go out in parallel. Its report is testimony: check any claim you will rely on, or that looks wrong, against the source yourself.
5. A visible agent, only when asked: keep the question in the conversation (nothing is written into the project), then launch it in the resolved worktree with a question-plus-shape prompt ending "Read-only: do not create, modify or delete files, and change no external state." Then end the turn.

```json tool=agent.launch
{"agentId":"<id from agent.listAvailable>","name":"explore: <topic>","worktreeId":"<resolved worktree id>","prompt":"<question, answer shape with file:line evidence, read-only boundary>","handback":true,"notify":true}
```

6. Before answering, take both readings in the target again. A difference since step 1 is an unexpected change: report the files. It is a boundary failure only once you know the investigation made it; the user or another process may have edited meanwhile, so say what you saw, not whose it was. Do not revert it and do not present it as a result.
7. Answer in your own words with the evidence carried through: `path:line` per claim, inference marked as inference, and uncertainty kept ("could not find where the token is refreshed" is a finding). State what you searched; a search with no hits is not proof a feature is absent.

On notice or resume: follow the foundation's resume procedure. Read the agent's answer with `terminal.readLastMessageOwned` for a Claude Code pane, otherwise `terminal.getOutput`. Run step 6, spot-check the claims you will repeat, then answer per step 7 with attribution. A question from the agent within the original read-only scope may be answered once; anything wider goes to the user.

Stop and recovery: one question, one agent. An ambiguous launch is reconciled with `terminal.list`, never relaunched. An agent still working stays pending: never build an answer from partial output.

Report back: the answer to the question asked, the file evidence behind it, the checkout and revision you read, what remains uncertain or unsearched, any unexpected file changes, and the terminal id of any agent launched.
