---
id: daintree.review.audit-spec-coverage
title: Audit how much of a spec is implemented
summary: Number a spec's requirements, have read-only auditors classify each as implemented, partial, missing or unverified with code and test evidence, check that evidence yourself, and report coverage requirement by requirement.
whenToUse: 'Use when the user wants to know how much of a SPEC, PRD, design doc or issue''s acceptance criteria actually exists in the code — "how much of this PRD is actually built?", "check docs/billing.md against the code, requirement by requirement", "which acceptance criteria of #412 are done?", "what''s left of the spec to implement?", "is everything in the RFC covered by tests?". The deliverable is a numbered requirement list, each one implemented, partial, missing or unverified, with code and test evidence where found (what was searched where missing), that evidence checked by you; nothing is changed. Do not use to check ONE agent''s claim that it finished its task (use daintree.orchestration.verify-agent-work), for a security, performance, accessibility or dead-code audit (use daintree.review.audit-codebase), to split a spec into work units before building (use daintree.orchestration.decompose-work), to judge a diff or PR (use daintree.review.review-change), to explain how the code works (use daintree.explore.repository), or to read an issue''s PR, CI or comment thread (use daintree.workflow.follow-up-on-issue).'
risk: project
requiredTools:
  - worktree.list
  - agent.launch
optionalTools:
  - forge.getIssue
  - terminal.getStatus
  - terminal.sendCommandOwned
---
Read-only throughout: agents extract, classify and cite, you check, and nothing is built, filed or fixed. Do it yourself only when the user asks.

On a host: shell steps via its command runner, or delegate.

Procedure:
1. Target: the checkout named, else the one the spec is about (`worktree.list`): its id, path, branch, `git rev-parse HEAD`, `git status --porcelain`, and whether uncommitted changes count. Source: a file (`git hash-object <path>`, which covers an edited spec), an issue (`forge.getIssue` with `worktreeId:"<that id>"`, `issueNumber:<n>`; empty means no such issue: stop; keep its URL and the body used), or the user's text verbatim. That snapshot is what is audited.
2. Requirements: one read-only agent (`name` "Spec requirements", `worktreeId`, `waitForReply:true, handback:true, waitSeconds:600`). Brief: the spec's location (an issue's or the user's text verbatim) and "Change no files. Number its requirements R1, R2 …: each one independently checkable sentence in the spec's own words, with its section. Split compound ones; leave out background and rationale; mark an ambiguous one ambiguous, never interpret it. Prose." Check the list against the source yourself (verbatim, no section skipped) and show it in your reply: it is the contract every auditor and the report share.
3. Auditors: up to about 12 requirements is one; more, split every R-number into at most four disjoint assignments, grouping sections, each agent with a distinct `name`. Record which terminal holds which R-numbers. Launch each with the resolved `worktreeId` and `notify:true, handback:true`. The prompt: the spec's location, its requirements verbatim with numbers, the snapshot, and:
   - "Classify each. IMPLEMENTED: code that does it end to end (file:line) AND a test whose assertion checks this behaviour (test file:line, the assertion quoted). PARTIAL: some of it exists; say what is missing. MISSING: not found; list what you searched (identifiers, paths). UNVERIFIED: code appears to do it but no test asserts it, it sits behind a disabled flag, or the requirement is ambiguous.
   - A file or function name is not evidence: read the body. A test that runs the code without asserting the requirement does not count. A stub, TODO or hardcoded return is PARTIAL at most.
   - Read-only: do not create, modify or delete files. You may run existing tests that need no install; say which ran and their result.
   - Reply in prose under 5000 characters, one short entry per requirement in order: number, status, code evidence, test evidence, gap, confidence."

```json tool=agent.launch
{"agentId":"codex","name":"Spec audit: billing §2-3","worktreeId":"<worktree id>","prompt":"<spec location, numbered requirements, snapshot, rules above>","notify":true,"handback":true,"replyLines":200}
```

On notice or resume:
1. A question within the spec gets one answer (`terminal.sendCommandOwned`, `notify:true, handback:true`); what the spec means is the user's call, so an ambiguity goes in the report, not your guess. `git status --porcelain` changed beyond the snapshot: a boundary failure to report. A reply cut short or skipping requirements: one send asking for the missing entries only; still missing, they are UNVERIFIED (not examined).
2. Check the evidence before any of it goes in the report:
   - Every IMPLEMENTED: open the cited code and test yourself. A test that does not assert the requirement downgrades it to UNVERIFIED; say why.
   - Run at least one cited test per auditor yourself in the worktree path, and every test behind a requirement the user singled out. A failing assertion that disproves the requirement makes it PARTIAL or MISSING; a test blocked by its environment leaves it UNVERIFIED. Keep tests you ran apart from assertions only read.
   - Each MISSING: one negative search of your own for its most distinctive term; a hit reopens it as PARTIAL or UNVERIFIED.
   - PARTIAL is never counted as done, UNVERIFIED never as implemented.
3. End the turn while any auditor is still out. Once all are in, compare HEAD, status and the spec hash with the snapshot: evidence in anything that drifted is re-checked against the snapshot or marked UNVERIFIED and left out of the implemented count.

Stop and recovery: one extractor, at most four auditors, one clarification and one continuation each. Building what is missing is a separate request (daintree.orchestration.decompose-work or daintree.edits.spawn-visible-agent); filing it is daintree.forge.manage-issue.

Report back: the spec source and the snapshot audited; the numbered requirements, each with status, code and test evidence and gap, marked checked by you or auditor-claimed; counts per status; the downgrades you made and why; ambiguous requirements for the user; tests you ran with results; extractor and auditor terminal ids with their R-numbers.
