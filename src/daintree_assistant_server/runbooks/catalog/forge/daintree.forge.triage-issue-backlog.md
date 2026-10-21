---
id: daintree.forge.triage-issue-backlog
title: Triage an issue backlog
summary: Sweep a bounded set of open issues, group duplicates, check which are still valid against the code, propose labels, priority and closures as one plan, and apply the rows the user's request or approval covers, each read back.
whenToUse: 'Use when the user wants the issue tracker itself CLEANED UP rather than a next task picked — "triage the open issues", "find the duplicate issues and close them", "label everything untriaged", "which of these bugs are already fixed?", "prioritise the backlog", "go through the needs-triage issues". The deliverable is one triage plan over a bounded set (duplicate groups, still valid, already fixed with evidence, needs info, proposed labels and priority) and the labels, comments and closures the user''s request or approval covers, applied and read back. Do not use to pick something to work on next (use daintree.workflow.find-issue-work); to file new issues, or edit, label or assign one (use daintree.forge.manage-issue); to comment on and close ONE issue (use daintree.workflow.follow-up-on-issue); to clean up branches or stale pull requests (use daintree.git.prune-stale-branches); to start agents implementing issues (use daintree.orchestration.fan-out-work); or to re-plan which running agents work on what around something urgent (use daintree.orchestration.reprioritize-work).'
risk: external
requiredTools:
  - worktree.list
  - forge.listIssues
  - agent.launch
---
Writes go through `gh` with `--repo <owner/name>` and are read back, and only for rows the user's request or approval covers. Checking issues against the code is read-only agents' work; you list, read, plan, verify and apply. Without Daintree's forge reads (API-key client, or declined), list with `gh issue list --repo <r> --search … --limit 50 --json number,title,labels,author,createdAt`.

On a host: shell steps run there, per the hosts foundation; own shell: `gh -R <repo>`.

Procedure:
1. Bound the set. Repository from the worktree the user means (`worktree.list`). Their filter; else all open issues, oldest first, at most about 50 per pass (`no:label` only when they said untriaged or unlabeled). Summary rows; bodies come in step 2:

```json tool=forge.listIssues
{"worktreeId":"<main worktree id>","state":"open","sort":"created","direction":"asc","perPage":50}
```

   Record the exact query. `hasMore` still true: the rest is a later pass; say so. A truncated result: narrow and retry. Read the repository's real labels (`gh label list --repo <r> --limit 200`) and conventions (CONTRIBUTING, issue templates, an existing priority scheme). Never invent a label or priority name; project-board fields only when asked.
2. Read each candidate with its comments (`gh issue view <n> --repo <r> --comments`). A maintainer's reply ("won't fix", "duplicate of", "needs a repro") outranks your guess, and a later comment can supersede the body.
3. Duplicates: group by symptom, error text, stack frame or area, then confirm each pair by reading both. Similar titles are not duplicates. Check closed issues too (`"state":"closed"`, `"search":"in:title <phrase>"`). The canonical issue is the one with the clearest reproduction or the most discussion, usually the older.
4. Still valid? One read-only agent per slice of about ten bugs (never one per issue) in the default-branch worktree: separate `agent.launch` calls with distinct `name`s, `notify:true, handback:true`, the prompt carrying the issues' numbers and text and "For each: still present / fixed by <commit or PR> / cannot tell, with `file:line` evidence. A phrase-search hit is not a fix: name the change that addresses the reported behaviour. Read-only: do not create, modify or delete files, and change no external state."

5. One plan, a row per issue: number, title, verdict (duplicate of #N, fixed by X, still valid, needs info, out of scope), evidence, proposed labels and priority, action (label, comment, close, leave). Show it. Apply now the rows the user's request already covers ("close the duplicates" covers confirmed duplicate rows); the rest, and any ambiguous row, wait for their yes, as edited. Never assign people unless asked.
6. Apply one issue at a time:
   - Labels: `gh issue edit <n> --repo <r> --add-label a,b --remove-label c`.
   - Needs info: `gh issue comment <n> --repo <r> --body-file <f>` with the specific question.
   - Duplicate: `gh issue close <n> --repo <r> --duplicate-of <m>` where your `gh` has that flag, else `--reason "not planned" --comment "Duplicate of #<m>"`.
   - Fixed: `gh issue close <n> --repo <r> --reason completed --comment "Fixed by <PR or commit>"`.
   Read back each: `gh issue view <n> --repo <r> --json state,stateReason,labels,comments --jq '{state,stateReason,labels:[.labels[].name],last:.comments[-1].body}'`. A lost or ambiguous reply is settled by that read, never by repeating the write. A permission error stops the writes: report what was done and what remains; never route around it.

On notice or resume: map the terminal to its slice. Its verdicts are claims. "Fixed" enters the plan only when you confirm the commit or merged PR is on the default branch (`git merge-base --is-ancestor <sha> origin/<default>`) and its diff addresses the report; otherwise "cannot tell". A slice not back yet leaves those rows pending, never guessed. Continue at step 5 once every slice is in or recorded missing.

Stop and recovery: one recap request per cut reply, then those issues are "cannot tell". Writes never repeat: an interrupted apply resumes from the read-back of each covered row.

Report back: repository, the exact query and what it left unexamined; the plan; each applied row with its read-back state and comment; rows declined, skipped, pending or failed; which verdicts agents claimed and which you checked.
