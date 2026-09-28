---
id: daintree.explore.repository
title: Understand a repository
summary: Answer "what does this repo do" / "where is X" by having a read-only visible agent investigate, checking its cited evidence, and answering.
whenToUse: Use when the user wants to UNDERSTAND the project rather than change it — "what does this repo do", "where is X implemented", "how does the auth flow work", "explain this subsystem", "what would I have to touch to add Y", "find the code that does Z", or orientation before work starts. The deliverable is an explanation with file evidence and nothing on disk changes; you frame the question and check the answer, a read-only visible agent does the reading unless it is a one-file or one-grep lookup. Do not use when the user wants files CHANGED (use daintree.edits.spawn-visible-agent, or daintree.workflow.start-work-on-issue for a forge issue), to judge the quality of a specific change or PR (use daintree.review.review-change), for a security, accessibility, performance or dead-code audit that returns findings (use daintree.review.audit-codebase), to work out why a check is failing (use daintree.ci.diagnose-and-fix), to ask about DAINTREE itself rather than the project (a documentation question — no runbook needed — or daintree.diagnostics when Daintree is broken), or to pick out files to bundle as context (use daintree.context.curate-copytree-bundle), or when the user asks for a particular agent's view — names the agent to ask, or says "ask an agent" — and wants its answer relayed back (use daintree.agents.ask-and-relay).
risk: project
requiredTools:
  - worktree.list
  - agent.launch
optionalTools:
  - terminal.getStatus
  - terminal.sendCommandOwned
  - slashCommands.list
---
You own the question, the checking and the answer; the reading goes to a read-only visible agent. Read directly only a trivial lookup (one named file, one grep), or when the user asks you to investigate yourself. Which slash commands or plugins an agent CLI offers is not a repository question: user-level ones live in home config, so an empty project search proves nothing (`slashCommands.list` reports them).

Procedure:
1. Fix the target: the worktree named or the one the question is about (`worktree.list`); its path, branch and HEAD, and whether uncommitted changes count. Record `git status --porcelain` and a hash of `git diff HEAD` there.
2. Frame one question with a required answer shape. A vague "what does this do" on a large repo is a map first (README, CLAUDE.md, AGENTS.md, manifests, entry points, layout, tests) with coverage stated. A request for a comprehensive survey authorizes a broad investigation: split it into at most three distinct areas, one agent each, each with a distinct `name`.
3. Launch read-only in the resolved worktree:

```json tool=agent.launch
{"agentId":"<built-in id>","name":"Explore: <topic>","worktreeId":"<resolved worktree id>","prompt":"<question, answer shape, revision, method, report, read-only line>","waitForReply":true,"handback":true,"waitSeconds":900,"replyLines":200}
```

   The prompt carries: the question verbatim and its answer shape; the checkout and revision; method (search distinctive identifiers, follow imports and callers one hop out, tell generated and vendored code from maintained source, say what was searched: no hits is not proof of absence); report (`path:line` per claim, inference marked as inference, "could not find where X happens" as a finding, at most about 80 lines); and "Read-only: do not create, modify or delete files, and change no external state: no installs, artefact-emitting builds or state-changing git." A survey likely to outrun the wait: `notify:true, handback:true` instead, and end the turn.
4. The reply is testimony. Open the cited `path:line` for every claim you will rely on and every surprising one; a citation that does not say it drops the claim, and you say so. One follow-up for gaps (`terminal.sendCommandOwned` with `waitForReply:true, handback:true`); never send the question again. A timeout: still working; recheck as the foundation says.
5. Before answering, take both readings again. A difference is an unexpected change: report the files. It is a boundary failure only once you know the investigation made it; the user or another process may have edited meanwhile. Do not revert it or present it as a result.
6. Answer in your own words, attributing the investigation to the agent and marking what you checked yourself; keep its uncertainty and what it did not search.

Report back: the answer with its file evidence, which claims you checked against the source, the checkout and revision read, what remains uncertain or unsearched, any unexpected file changes, and each agent's terminal id.
