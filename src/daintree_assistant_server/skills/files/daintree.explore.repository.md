---
id: daintree.explore.repository
title: Understand a repository
summary: Answer "what does this repo do" / "where is X" with a read-only explore agent and cited evidence.
whenToUse: Use when the user wants to UNDERSTAND the project rather than change it — "what does this repo do", "where is X implemented", "how does the auth flow work", "explain this subsystem", "what would I have to touch to add Y", "find the code that does Z", or orientation before work starts. The deliverable is an explanation with file evidence and nothing on disk changes. Do not use when the user wants files CHANGED (use daintree.edits.spawn-visible-agent, or daintree.workflow.start-work-on-issue for a forge issue), to judge the quality of a specific change or PR (use daintree.review.review-change), to work out why a check is failing (use daintree.ci.diagnose-and-fix), to ask about DAINTREE itself rather than the project (documentation arrives retrieved — no skill needed — or daintree.diagnostics when Daintree is broken), or to pick out files to bundle as context (use daintree.context.curate-copytree-bundle), or when the user names a specific agent to ask and just wants its answer relayed back (use daintree.agents.ask-and-relay).
risk: project
requiredTools:
  - context.snapshot
  - worktree.getCurrent
  - git.getProjectPulse
  - fs.list
  - fs.read
  - fs.search
  - agentTask.spawnForEdits
  - terminal.awaitAll
  - terminal.await.async
  - terminal.summarize
  - terminal.extract
---
Use when: the user wants to understand the codebase — what it does, where something lives, how a subsystem works.

Daintree is a control plane, not a code reader. Real understanding comes from an agent that can open dozens of files in its own context; you delegate, then synthesize. Forming an architectural opinion yourself from three `fs.read` fragments is the failure this skill exists to prevent — it produces confident, wrong answers, and the user cannot tell.

The exception is LOCATION. "Where is the retry logic?" is often one `fs.search` away, and spawning an agent for it is slower and dearer than answering. Judge by whether the question needs SYNTHESIS or just a POINTER.

Procedure:

1. FIX THE TARGET. The runtime context already carries a one-line worktree summary; call `worktree.getCurrent` when you need the exact `worktreeId` to spawn into, or when the user may have switched. `git.getProjectPulse` when the branch matters to the answer — a question about "the current code" means the current branch's code.

2. DECIDE: pointer or synthesis.
   - POINTER — "where is X", "which file defines Y", "is there a Z anywhere". Do it yourself: `fs.search({"query":"<literal substring>","glob":".py"})`. Two traps in one call: the search is a LITERAL, case-sensitive substring — not a regex — so search for a distinctive identifier rather than a phrase; and `glob` is a filename SUFFIX (".py", ".ts"), not a pattern, so "*.py" matches nothing and returns a confident empty result. Read the few hits that matter with `fs.read` and answer. If the first search finds nothing your keyword is wrong: try ONE different identifier, and if that also fails, delegate rather than guessing again.
   - SYNTHESIS — "what does this do", "how does X work", "what would I touch to add Y". Delegate to an explore agent, step 3.

3. SPAWN AN EXPLORE AGENT. `agentTask.spawnForEdits({"mode":"explore","worktreeId":"<id>","title":"<short>","taskPrompt":"<the question AND the deliverable>"})`. Explore mode INSTRUCTS the agent not to touch files; it is a prompt constraint, not a sandbox, so treat any file that does change as a failure to report rather than a result to accept. Spawning still creates real terminal state and is confirmed per the active permission tier like any other spawn.
   Write the taskPrompt as a QUESTION plus a required shape, not a topic:
   - bad: "look at the auth code"
   - good: "Explain how a request is authenticated end to end: entry point, where the credential is validated, what happens on failure. Cite file:line for each claim. Say explicitly what you could not determine. Change nothing."
   Pick the agent deliberately: `agentId` is OPTIONAL and defaults to one particular agent, so omitting it silently chooses for the user. Resolve the best fit from the available-agent registry in your startup context and pass it — a name the user SPOKE is only a hint, since transcription drops letters; an unknown id is rejected with the real roster and a suggestion, so re-spawn with the right id rather than the typo.
   `acceptanceCriteria` is IGNORED in explore mode — the criteria belong in the taskPrompt.
   Give it ONE question. An agent asked four things answers the first well and the rest thinly.

4. SUPERVISE with exactly ONE mode, chosen by expected length. All `terminal.awaitAll` calls in a turn share an ENFORCED cumulative 120-second foreground budget, so in-turn waiting is for a question expected to settle in a minute or two — anything longer is `terminal.await.async`, which costs no model time and wakes you. If a wait returns `budgetExhausted`, do NOT await again: hand the stragglers to the async path and end the turn. Never attach two modes to the same terminal.

5. READ THE ANSWER, don't dump it. `terminal.extract` with a targeted question is the cheap read; `terminal.summarize` for garbled scrollback. Agents answer in prose — do not ask for JSON, and do not paste the terminal into the conversation.

6. SYNTHESIZE. Your reply is the explanation, in your own words, with the agent's file evidence carried through. Keep the uncertainty: "the agent could not find where the token is refreshed" is a finding, and silently dropping it is how a gap becomes a wrong belief.

Notes:
- NEVER report that files changed. This is a read-only path; if something did change, that is a bug worth surfacing, not a result to narrate.
- Cite evidence, not confidence. `path/to/file.py:120` beats "the codebase appears to use a middleware pattern". A claim with no file behind it should be marked as inference.
- Do not re-read what you already hold. The agent's report IS the data; re-opening the same files to "check" it burns a round and adds nothing.
- One agent is usually right for a question with one answer. Reach for several only when the question genuinely has independent parts (frontend and backend, say) — and then use the fan-out skill rather than hand-rolling it here.
- If the repo is large and the question vague ("what does this do"), narrow it with the user first rather than launching a survey that takes ten minutes and answers something else.

Report back: the answer to the question asked, the concrete file evidence behind it, what remains uncertain, and the terminal id of any agent you spawned.
