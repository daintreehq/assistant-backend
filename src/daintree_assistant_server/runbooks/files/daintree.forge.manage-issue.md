---
id: daintree.forge.manage-issue
title: Create and maintain a forge issue
summary: Create, edit, reopen, label or assign an issue against the exact repository, then read it back so public forge state is never inferred from a successful call.
whenToUse: Use when the user asks to file or create an issue, change an issue title or body, reopen an issue, add or remove labels, or assign or unassign an account. Also use for issue intake such as "turn this bug into an issue" or "label and assign this ticket". Do not use to search the backlog for work (use daintree.workflow.find-issue-work), start implementation (use daintree.workflow.start-work-on-issue), read an issue or PR's current status or add a closing comment and close it (use daintree.workflow.follow-up-on-issue), or create a pull request (use daintree.workflow.prep-branch-for-review).
risk: system
requiredTools:
  - forge.getIssue
optionalTools:
  - worktree.list
  - forge.listIssues
---
Use when: the user wants an issue created, edited, reopened, labelled, assigned or unassigned in a specific repository.

Availability: Hybrid. Daintree's app MCP only reads issues (`forge.getIssue`, `forge.listIssues`); at core tier each read asks the user first, and an API-key client or a session with no Daintree connection has none, so the host tool reads instead. Creating, editing, reopening, labelling and assigning go through the host's own permitted forge tool (for GitHub, `gh`), always with an explicit repository (`--repo <owner/name>`) and issue number, and every write is read back. With no permitted forge tool, give the user the drafted issue instead of implying it was filed.

Procedure:
1. Fix the repository. Resolve the worktree the user means (`worktree.list`) and its repository, and use that same pair for the duplicate check, the write and the read-back. Never let a public write default to whichever checkout or worktree happens to be active.
2. Build the content from the user's report and evidence you inspected. A title is required; the body is Markdown. Do not turn a guess about cause or severity into issue fact. Resolve every prerequisite of the whole request (label names, the assignee) before the first write, so you do not create an issue and only then find the assignment blocked.
3. Check for a likely duplicate when the issue describes a defect and a quick search is cheap:

```json tool=forge.listIssues
{"worktreeId":"<resolved worktree id>","state":"open","search":"in:title <distinctive phrase>","perPage":20}
```

   `search` is the provider's own query syntax and its depth is capped: a miss is not proof there is no duplicate. The schema is strict, so keys such as `labels` or `limit` are rejected. If a likely duplicate appears, show it and ask whether to use it or create another; never silently switch targets. Do not make a small issue wait on exhaustive paging.
4. Create only on request: `gh issue create --repo <owner/name> --title <title> --body-file <file>`, plus `--label`/`--assignee` when asked. Labels must already exist in the repository. Capture the returned number and URL.
5. Edit or reopen from a fresh read:

```json tool=forge.getIssue
{"worktreeId":"<resolved worktree id>","issueNumber":42}
```

   An empty result means no such issue: stop. A title or body edit replaces the old value, so re-read right before replacing and keep whatever someone added since your first read. That narrows the race without closing it: if the read-back shows someone else's change lost, restore it or report the conflict. An additive note is a comment (daintree.workflow.follow-up-on-issue), not a body rewrite. Reopen only an issue the read shows closed.
6. Labels and assignees: `gh issue edit 42 --repo <r> --add-label <l> --remove-label <l>`, and `--add-assignee`/`--remove-assignee`. Removing a label the issue does not carry fails, so check first. With `gh`, `@me` resolves to the authenticated account; with another tool, use a concrete login grounded in context or ask. A forge can silently drop an assignee without repository access: the resulting list, not the call's success, is the answer.
7. Read back every requested field (title, body, state, the complete label set, assignees) with `gh issue view 42 --repo <r> --json title,body,state,labels,assignees,url` or `forge.getIssue`. Daintree's listings can serve a cached page after a change made outside the app; pass `bypassCache:true` to `forge.listIssues` when re-searching. Report each field as confirmed, not applied, or unknown: a multi-field request can partly land.

Uncertain create: when the create's reply is lost or ambiguous, search the same repository before any retry: the exact title, closed issues included, narrowed to your account and time (`author:@me created:>=<date>`), with `bypassCache:true`. A title match is a candidate; confirm it by its body before calling it yours. A second create after a timeout is how duplicates happen, and the host tool offers no idempotency key to lean on.

Scope: closing an issue and its closing comment belong to daintree.workflow.follow-up-on-issue. Filing or labelling an issue is not a request to start work on it; that happens only when the user asks, through daintree.workflow.start-work-on-issue.

Report back: repository, issue number and URL, each requested field with its read-back state, anything the forge declined (a missing label, a dropped assignee), and any uncertainty left, such as a create whose outcome could not be confirmed.
