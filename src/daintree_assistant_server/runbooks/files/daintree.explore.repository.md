---
id: daintree.explore.repository
title: Understand a repository
summary: Answer "what does this repo do" / "where is X" with a read-only investigation and cited evidence.
whenToUse: Use when the user wants to UNDERSTAND the project rather than change it — "what does this repo do", "where is X implemented", "how does the auth flow work", "explain this subsystem", "what would I have to touch to add Y", "find the code that does Z", or orientation before work starts. The deliverable is an explanation with file evidence and nothing on disk changes. Do not use when the user wants files CHANGED (make the change yourself, or use daintree.edits.spawn-visible-agent when they want a visible agent to make it, or daintree.workflow.start-work-on-issue for a forge issue), to judge the quality of a specific change or PR (use daintree.review.review-change), to work out why a check is failing (use daintree.ci.diagnose-and-fix), to ask about DAINTREE itself rather than the project (a documentation question — no runbook needed — or daintree.diagnostics when Daintree is broken), or to pick out files to bundle as context (use daintree.context.curate-copytree-bundle), or when the user names a specific agent to ask and just wants its answer relayed back (use daintree.agents.ask-and-relay).
risk: project
requiredTools:
  - worktree.list
optionalTools:
  - agent.launch
  - slashCommands.list
---
Read and search yourself by default; a native research subagent, where your host has one, suits broad finding work. A visible Daintree agent only when the user asks to watch or steer the investigation. Which slash commands or plugins an agent CLI offers is not a repository question: user-level ones live in home config, so an empty project search proves nothing (`slashCommands.list` reports them).

Procedure:
1. Fix the target: the worktree named or the one the question is about (`worktree.list`). Read in ITS path (absolute paths, `git -C <path>`), never your own checkout while describing another. Note branch and HEAD, and whether uncommitted changes count. Record `git status --porcelain` and a hash of `git diff HEAD` there.
2. Frame one question with a required answer shape. For a vague "what does this do" on a large repo, map it first (README, CLAUDE.md, AGENTS.md, manifests, entry points, layout, tests) and state coverage. A request for a comprehensive survey authorizes a broad investigation.
3. Investigate read-only: search distinctive identifiers, follow imports and callers one hop out, tell generated and vendored code from maintained source. Write nothing: no edits, formatting, installs, artefact-emitting builds or state-changing git. If a needed command writes into the tree, don't run it; say what it would have told you.
4. Delegated reports (a subagent, or a visible agent launched read-only in the resolved worktree with `waitForReply:true, handback:true` and a prompt ending "Read-only: do not create, modify or delete files, and change no external state.") are testimony: check any claim you will rely on against the source.
5. Before answering, take both readings again. A difference is an unexpected change: report the files. It is a boundary failure only once you know the investigation made it; the user or another process may have edited meanwhile. Do not revert it or present it as a result.
6. Answer in your own words with `path:line` per claim, inference marked as inference, uncertainty kept ("could not find where the token is refreshed" is a finding). State what you searched; no hits is not proof of absence.

Report back: the answer, its file evidence, the checkout and revision read, what remains uncertain or unsearched, any unexpected file changes, and the terminal id of any agent launched.
