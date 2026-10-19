---
id: daintree.orchestration.decompose-work
title: Decompose a spec into parallel work units
summary: Have a read-only planner agent break a spec, PRD, epic or large goal into independently buildable units with owned files, acceptance and dependencies, check them for overlap, get the split approved, then file or start them only as the user asked.
whenToUse: 'Use when a spec, PRD, epic or big goal has to be BROKEN DOWN into separately buildable pieces before anyone writes code — "break this PRD into issues", "split the epic so four agents can take a piece each", "plan the tasks for the payments rewrite and file them", "what are the chunks and their order for building this". The deliverable is an approved breakdown — units with owned files, acceptance criteria and dependencies, checked for overlap — then filed as issues or started only if asked. Do not use when the units are already defined, or a goal is to be split and set going at once with no review (use daintree.orchestration.fan-out-work), for one plan built by one implementer (use daintree.orchestration.plan-then-implement), for two sides of one feature built against an agreed interface (use daintree.orchestration.contract-first-split), for dependent phases executed and tracked over time (use daintree.orchestration.execute-durable-workflow), for filing or editing issues whose content is already settled (use daintree.forge.manage-issue), for cleaning up existing open issues (use daintree.forge.triage-issue-backlog), for trying rival options hands-on before deciding (use daintree.orchestration.spike-options), or to measure how much of a spec the code already delivers (use daintree.review.audit-spec-coverage).'
risk: external
requiredTools:
  - worktree.list
  - agent.launch
optionalTools:
  - forge.getIssue
  - forge.listIssues
  - terminal.list
  - terminal.getOutput
  - terminal.sendCommandOwned
---
You own the source, the checks and the approval gate; a read-only planner writes the breakdown. Nothing is filed, branched or launched before the user approves the split.

On a host: shell steps run there, per the hosts foundation.

Procedure:
1. Source and outcome. The spec is a file (keep its path and `git log -1 --format=%H -- <path>`; one off the planner's machine goes in verbatim), an epic or issue (`forge.getIssue`; empty: no such issue, stop), or the user's text kept verbatim. Fix the worktree it concerns (`worktree.list`). Fix the end state the user asked for: the plan only, issues filed, or agents started. Unsaid: the plan only. A count or cap they gave ("four agents") bounds the split.
2. Work in flight: `issueNumber`, `prNumber` and branches from `worktree.list`, and open issues matching the spec's distinctive terms (`forge.listIssues` with `search`). Pass them to the planner as existing work; a search miss is not proof.
3. Launch one planner (the agent named, else the configured one best suited) in that worktree, then end the turn:

```json tool=agent.launch
{"agentId":"<planner>","name":"Decompose: <feature>","worktreeId":"<worktree id>","prompt":"<source verbatim or path@commit>. <cap>. Existing work: <issues, branches>. Break this into units that can each be built and tested alone once their dependencies are merged. Per unit: title; goal in two lines; owned files or modules (existing paths, new ones marked new); checkable acceptance criteria; depends on (unit titles and why, or none); shared artifacts touched (lockfile, migrations, generated types, route or DI registries, single config); size S/M/L; non-goals. Prefer vertical slices to layers. Each shared artifact has one owning unit; others name what they need from it. List open questions instead of guessing. Read-only: create, modify or delete no files, issues or branches. Reply in prose between BEGIN UNITS and END UNITS, at most 12 units, then open questions, then stop.","notify":true,"handback":true,"replyLines":200}
```

On notice or resume:
1. The breakdown is the quoted reply. No END UNITS: read once with `terminal.getOutput` (`maxLines` 1000); still cut, one send asking for it shorter with `notify:true, handback:true`. A question: answer only from the spec and the user's words, otherwise ask the user. Files changed in the worktree (`git status --porcelain`) are a boundary failure: report them.
2. Check the split yourself; it is a proposal.
   - Overlap: compare owned files pairwise. A file, directory or shared artifact in two units means merge them, add a dependency edge, or give it one owner. Confirm existing paths with `git ls-files -- <path>`; `grep` importers of any shared type or interface a unit changes: its consumers depend on it.
   - Dependencies: every edge names a real unit, no cycles. Order into waves: wave 1 has no dependencies.
   - Coverage: every unit has a checkable criterion; every requirement in the spec lands in a unit or is named out of scope.
   - Duplicates: a unit matching an open issue or in-flight branch is marked "exists: #N", not refiled.
   Defects go back to the planner once, listed, in one send (same terminal, `notify:true, handback:true`), or you fix them and say which parts are yours.
3. Gate. Show the user the units by wave (title, owned files, acceptance, dependencies, size), what your check changed, and open questions; end the turn. Approval is a plain go with the questions answered. Edits: small ones yourself, rework through the planner, then show it again.
4. After the go, only what was asked:
   - File: the several-issues path of daintree.forge.manage-issue. File in dependency order so each body says "Depends on #N" with a real number; body: goal, owned files, acceptance, non-goals, link to the spec or epic. Read each back.
   - Start: wave 1 only. One unit is daintree.workflow.start-work-on-issue; several go to daintree.orchestration.fan-out-work as units already approved, with owned files and acceptance as their briefs. A later wave waits until its dependencies are merged: say so, launch nothing for it.

Stop and recovery: one corrective round with the planner; two user revision rounds, then the user decides. A lost launch reply is reconciled with `terminal.list`, never a second planner. A lost issue create is searched for before any retry, as manage-issue says.

Report back: source and revision, planner terminal id, the approved units by wave, what your check changed (overlaps, added edges, coverage gaps, duplicates), issue numbers and URLs with read-back or what was handed to which runbook, open questions and what is still pending.
