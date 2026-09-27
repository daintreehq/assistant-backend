---
id: daintree.forge.manage-issue
title: Create and maintain a forge issue
summary: Create, edit, reopen, label or assign an issue against the exact repository, then read it back so public forge state is never inferred from a successful call.
whenToUse: 'Use when the user asks to file, log or create an issue ("log this as an issue", "log it for later", "turn this bug into an issue"), change an issue title or body, reopen an issue, add or remove labels, or assign or unassign an account, as in "label and assign this ticket". Logging an issue for later is filing it now, not a later wake (not daintree.orchestration.watch-outcome). Do not use to search the backlog for work (use daintree.workflow.find-issue-work), start implementation (use daintree.workflow.start-work-on-issue), read an issue or PR''s current status, ask why an issue is still open, or add a closing comment and close it (use daintree.workflow.follow-up-on-issue), or create a pull request (use daintree.workflow.prep-branch-for-review).'
risk: system
requiredTools:
  - forge.getIssue
optionalTools:
  - worktree.list
  - forge.listIssues
---
Daintree only reads issues. Writes go through the host tool (`gh`) with `--repo <owner/name>` and the number, and are read back. With no permitted forge tool, give the user the drafted issue; never imply it was filed.

1. Fix the repository from the worktree the user means (`worktree.list`) and use it for the duplicate check, the write and the read-back. A public write never defaults to whatever checkout is active.
2. Build the content from the user's report and evidence you inspected; a guess about cause or severity is not issue fact. Resolve every prerequisite (label names exist, the assignee) before the first write.
3. For a defect, check for a likely duplicate:

```json tool=forge.listIssues
{"worktreeId":"<resolved worktree id>","state":"open","search":"in:title <distinctive phrase>","perPage":20}
```

   Search depth is capped, so a miss is not proof. A likely duplicate: show it and ask; never silently switch targets.
4. Create only on request: `gh issue create --repo <r> --title <t> --body-file <f>` (`--label`, `--assignee` when asked). Capture number and URL.
5. Edit or reopen from a fresh `forge.getIssue` (empty: no such issue, stop). An edit replaces the old value: re-read right before, keep what others added, and if the read-back shows their change lost, restore it or report the conflict. An additive note is a comment (daintree.workflow.follow-up-on-issue). Reopen only what the read shows closed.
6. `gh issue edit N --repo <r> --add-label/--remove-label/--add-assignee/--remove-assignee`. Removing an absent label fails: check first. `@me` works with `gh`; elsewhere use a grounded login. A forge can silently drop an assignee: the resulting list is the answer.
7. Read back every requested field (`gh issue view N --repo <r> --json title,body,state,labels,assignees,url`; `bypassCache:true` when re-listing). Report each as confirmed, not applied or unknown.

Uncertain create: a lost or ambiguous create reply is searched for in the same repository before any retry: the exact title, closed included, `author:@me created:>=<date>`, `bypassCache:true`; confirm a match by its body. The host tool has no idempotency key.

Closing and its comment are daintree.workflow.follow-up-on-issue; filing an issue is not a request to start it (daintree.workflow.start-work-on-issue).

Report back: repository, issue number and URL, each requested field with its read-back state, anything the forge declined, and any unconfirmed outcome.
