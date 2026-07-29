---
id: daintree.orchestration.verify-agent-work
title: Verify an agent actually did the work
summary: Test an agent's "done" against evidence — cheap git reads, a small-model verdict on the tail, an explore agent when it needs real inspection — then route on verified, unverified, or contradicted.
whenToUse: Use when the user wants a finished agent's work checked before it is trusted — "did it actually do it?", "check that the agent did what I asked", "verify this before I merge", "it says the tests pass, confirm that", "I don't trust that result" — or when an agent reports done and the next step is irreversible (commit, push, merge, close, hand off). Also to judge a claim against acceptance criteria, or when the tail seems to contradict what the agent said. Do not use to sweep the whole fleet rather than judge ONE result (daintree.orchestration.supervise-fleet), to ready a branch or open a PR (daintree.workflow.prep-branch-for-review), to forward findings to an implementer (daintree.agents.relay-review-feedback), to wait on an agent still running (daintree.orchestration.watch-outcome), or for "where were we" across sessions (daintree.orchestration.status-and-resume).
risk: terminal
requiredTools:
  - context.snapshot
  - git.getProjectPulse
  - workflow.prepBranchForReview
  - terminal.awaitAll
  - terminal.extract
  - terminal.extract.async
  - terminal.extract.json
  - agentTask.spawnForEdits
  - agentTask.superviseTerminal
  - daintree.call
  - queue.resolve
---
Use when: an agent says it is done and the next move is irreversible or trust-dependent — commit, push, open or merge a PR, close an issue, hand the worktree to another agent, or simply tell the user it is finished. This runbook answers exactly one question: did the agent actually do what was asked?

You verify by EVIDENCE and by DELEGATION, never by inspecting the work yourself. A claim in a terminal is not evidence, and a clean branch verdict is not evidence that the right thing was built.

Procedure:

1. Fix the claim and the contract first. Name, for yourself, the two or three checkable statements that would make this done — the acceptance criteria from the spawn if you set them, otherwise the user's own words. A vague check yields a vague verdict: "did it work?" is not checkable; "the new endpoint exists, the suite runs clean, nothing outside that module changed" is.

2. Prove the finish before weighing any evidence — an agent that reported done while still mid-edit will show you a half-written tree. Settle it the foundation's way (watcher-confirmed completion, or `terminal.awaitAll` plus a bounded tail read); never verify off a bare agentState.

3. Cheap deterministic checks first. They cost no model call and refute a false claim faster than any read.
   - `git.getProjectPulse {"arguments": {}}` — branch state, uncommitted changes and recent commits for the active worktree (nest a `worktreeId` inside `arguments` to target another). If the agent claims it implemented something and the pulse shows no commits and a clean tree, you already have your answer.
   - `workflow.prepBranchForReview {"arguments": {}}` — a go/no-go verdict (`ready`, `blocked_uncommitted_changes`, `blocked_merge_conflicts`, `blocked_repo_busy`, `no_runners_detected`) plus staged/unstaged counts, `currentBranch` and `detectedRunners`. It is a passthrough wrapper: `arguments` is REQUIRED (pass `{}`), and the top level accepts ONLY `arguments` and `requestKey`, so a top-level `cwd` is rejected outright. THE TRAP: it is READ-ONLY despite the name, and `ready` means only that the tree is in a committable shape — never that the agent built the right thing.
   - `daintree.call {"name": "git.getStagingStatus", "arguments": {}}` for the live working-tree picture when the pulse summary is not enough. No typed wrapper exists, so this is the raw escape hatch (system tier, confirmed) — reach for it only when the two reads above leave a real question.
   Nothing in the registry reads CI status, check runs, job logs, or PR review comments; `watcher.watchPR` sees only open/merged/draft/closed. "CI is green" is therefore never verifiable here — either an agent runs the suite in the worktree and reports, or you say plainly that it was not checked.

4. Semantic check with the SMALL model, not with your own eyes. Ask the terminal a targeted question instead of pulling scrollback into your context.
   - In-turn, when the user is waiting: `terminal.extract {"terminalIds": ["<full-id>"], "instruction": "Did it run the test suite, and what was the final result line? Quote it."}` — one question, one id. Over several ids a plain-text extract MERGES the tails into ONE answer; for a verdict per agent, batch one extract per id in a single turn (they dispatch concurrently) or use `terminal.extract.json` with an array schema keyed by terminalId.
   - Off-turn, the purpose-built path: `terminal.extract.async` polls, extracts, JUDGES, and publishes the outcome to the attention queue instead of blocking the turn:
     `{"terminalIds": ["<full-id>"], "instruction": "Summarize what the agent changed and what it reported about tests.", "verdictInstruction": "Pass only if the full suite ran and every test passed.", "title": "verify: <task>", "dedupeKey": "verify-<task>"}`
     `verdictInstruction` is a pass/fail question judged against the extracted text and drives the event's severity, so what lands in the inbox is already a verdict rather than raw output. `dedupeKey` collapses repeat checks of the same thing into one item; `ttlMs` expires a check that stops mattering.
     Supervision rule: adding `"wait": {}` turns this call into a SUPERVISOR — use that form only on an agent that has none, never stacked on a live watcher or an in-flight await. On an agent that has ALREADY settled, omit `wait`: it reads once, judges, and publishes without waiting on anything. Clear the resulting inbox item with `queue.resolve {"id": "<id>"}` once you have reported the verdict.

5. When the claim needs real inspection, DELEGATE it. If deciding requires reading code, a diff, or test output in depth, spawn a reviewer: `agentTask.spawnForEdits {"mode": "explore", "worktreeId": "<worktree path>", "title": "verify <task>", "taskPrompt": "<the acceptance criteria as the brief: check each one against the working tree and report PASS or FAIL per criterion with file evidence; change nothing>"}`. Explore mode is read-only by construction, and `acceptanceCriteria` is IGNORED for it — the criteria go in the taskPrompt. Supervise it like any other agent (ONE mode), then run its report back through step 6 as the evidence.

6. Route on what you actually have. Three outcomes, three different replies.
   - VERIFIED — the deterministic reads and the semantic check agree with the claim. Report what you checked, not just the conclusion, and OFFER the next step (commit, push, PR, close) rather than taking it.
   - UNVERIFIED — you could not confirm it: the tail was truncated, no runner exists (`no_runners_detected`), the agent exited early, the evidence is simply silent. Say so plainly, name the missing evidence and the one thing that would settle it. Do NOT propose commit, push, merge or close on unverified work, and never round "I could not check" up to "it looks fine".
   - CONTRADICTED — the evidence disagrees with the claim: a clean tree after "I implemented it", a failing result line after "tests pass", nothing touched under the module the agent named. State the specific gap — what was claimed, what the evidence shows — then relay that gap to the implementer instead of closing it yourself (daintree.agents.relay-review-feedback carries the relay mechanics). If that agent is gone, spawn a fresh edit agent with the gap as its brief and as its acceptance criteria.

## Set the contract at the spawn, not at verification time
The cheapest verification is the one arranged before the agent starts. `agentTask.spawnForEdits` and `agentTask.superviseTerminal` both take `acceptanceCriteria`, a task-specific checkable definition of done. Set it and the supervising watcher verifies completion against that contract rather than git cleanliness alone, so the event that wakes you is already a verdict. Set it on EVERY edit spawn with a concrete definition of done. For an agent already running unsupervised — one from an earlier session, or one whose watcher was retired — do not re-spawn it: adopt it with `agentTask.superviseTerminal {"terminalId": "<full-id>", "acceptanceCriteria": "...", "worktreeId": "<worktree path>"}`, where `worktreeId` scopes the post-completion git verification.

## Non-goals — where verification stops
You establish WHETHER the asked-for work landed; you never judge whether the code is good, and you never do the checking by hand. No `fs.read` of source to form an engineering opinion, no pulling a diff to eyeball it (`git.getProjectPulse` already tells you whether anything changed), no running the suite yourself, no fixing what you find. Each of those is an explore-agent brief. If the user wants an actual code review, that is a spawned reviewer plus this runbook applied to its report.

Report back: the claim you tested, the checks you ran and what each returned, the outcome as one of verified / unverified / contradicted, the specific gap if contradicted, the ids of anything still running (terminal, watcher, async verdict), and the next step you are offering — never a "done" you did not evidence.
