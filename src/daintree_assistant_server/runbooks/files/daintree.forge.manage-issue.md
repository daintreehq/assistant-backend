---
id: daintree.forge.manage-issue
title: Create and maintain a forge issue
summary: Create, edit, reopen, label or assign an issue against the exact repository, then read it back so public forge state is never inferred from a successful call.
whenToUse: Use when the user asks to file or create an issue, change an issue title or body, reopen an issue, add or remove labels, or assign or unassign an account. Also use for issue intake such as "turn this bug into an issue" or "label and assign this ticket". Do not use to search the backlog for work (use daintree.workflow.find-issue-work), start implementation (use daintree.workflow.start-work-on-issue), read an issue or PR's current status or add a closing comment and close it (use daintree.workflow.follow-up-on-issue), or create a pull request (use daintree.workflow.prep-branch-for-review).
opener: "Issue upkeep — a successful write is not the public state"
risk: system
requiredTools:
  - context.snapshot
  - worktree.getCurrent
  - forge.listIssues
  - forge.getIssue
  - daintree.call
---
Use when: the user wants to create or maintain public issue state on the configured forge.

All issue writes are real Daintree MCP actions, but the Assistant CLI has no typed forge-write wrappers. Invoke them through `daintree.call`, whose envelope is `{name, arguments, requestKey?}`. Put the forge action's fields FLAT inside `arguments`; do not wrap them in another `arguments` object. `daintree.call` is system-risk, always confirmed and never grantable, even when the underlying Daintree action is marked safe.

Procedure:

1. FIX THE REPOSITORY. Read the injected worktree context or call `worktree.getCurrent`. Carry one exact locator through every operation, preferably `worktreeId` exactly as returned. Never rely on the active-worktree default for a public write: focus can change between calls. `forge.getIssue` and `forge.listIssues` are CLI passthrough wrappers, so their forge fields go under their TOP-LEVEL `arguments` object:
   `forge.getIssue({"arguments":{"worktreeId":"<id>","issueNumber":42}})`.

2. BUILD THE PUBLIC CONTENT before writing. Require a non-empty title. Use Markdown for the optional body and include only facts supported by the user's report or inspected evidence; never turn a guess about cause or severity into issue fact. If the user asked for an edit, read the issue first because `title` and `body` REPLACE the old values wholesale. Omitted fields stay unchanged; an explicit empty body clears it. Use the follow-up runbook for an additive comment rather than overwriting the body.

   Resolve every prerequisite for the whole requested mutation before the first public write. In particular, if the user says "assign it to me", obtain their concrete forge login from trustworthy context or ask one tight question NOW. Do not create a partially fulfilled issue first and discover only afterward that assignment is blocked.

3. CHECK LIKELY DUPLICATES when the title describes an existing defect and a cheap search is useful:
   `forge.listIssues({"arguments":{"worktreeId":"<id>","state":"open","search":"in:title <distinctive phrase>","perPage":20}})`.
   `search` is provider-native, not a guaranteed substring search, and may cap result depth. A miss is evidence only that this query found no match, never proof no duplicate exists. Do not make every small issue wait on exhaustive pagination.
   If a likely duplicate does appear, show the candidate and ask whether the user wants to update/use it or create another; do not silently create or silently switch targets.

4. CREATE only on the user's request:
   `daintree.call({"name":"forge.createIssue","arguments":{"worktreeId":"<id>","title":"<title>","body":"<markdown>","labels":["bug"]},"requestKey":"<stable key for this one logical create>"})`.
   `body` and `labels` are optional. Labels must already exist; there is no assistant-visible action that lists the repository's complete label catalog, so never claim a name was validated merely because another issue used it. Reuse a `requestKey` only for an ambiguous retry of the SAME logical call with byte-equivalent arguments; a key reused with different arguments is rejected.

5. CAPTURE AND VERIFY THE CREATED ISSUE. The create result is JSON text rather than guaranteed `structuredContent`; read the returned `text` for `number` and `url`. Then call typed `forge.getIssue` with that number and the same locator. Require the returned issue's title and state to match, and verify every requested creation label and assignee. If a requested label is absent, the requested state is incomplete; add it only after reporting the discrepancy and with the write still within the user's original authority. A null result means no such issue was found, not success. If the create transport outcome is ambiguous, search/read before retrying with the same request key so a timeout cannot create a duplicate.

6. EDIT OR REOPEN from a current read:
   - Edit: `daintree.call({"name":"forge.editIssue","arguments":{"worktreeId":"<id>","issueNumber":42,"title":"<replacement>","body":"<replacement>"}})`. At least one replacement field is required.
   - Reopen: `daintree.call({"name":"forge.reopenIssue","arguments":{"worktreeId":"<id>","issueNumber":42}})`. Call it only for a read-confirmed closed issue.
   Re-read with `forge.getIssue` after either mutation. Raw success alone is not proof the remote state landed.

7. CHANGE LABELS one at a time. Add is additive and preserves the rest:
   `daintree.call({"name":"forge.addIssueLabel","arguments":{"worktreeId":"<id>","issueNumber":42,"label":"bug"}})`.
   Remove fails if the issue does not currently carry the label, so inspect the issue first:
   `daintree.call({"name":"forge.removeIssueLabel","arguments":{"worktreeId":"<id>","issueNumber":42,"label":"triage"}})`.
   Both actions return the resulting label array as JSON text, not guaranteed structured content. Re-read the issue after a batch and compare the COMPLETE set, not just the requested delta.

8. ASSIGN only a concrete forge login:
   `daintree.call({"name":"forge.assignIssue","arguments":{"worktreeId":"<id>","issueNumber":42,"username":"octocat"}})` or the same shape with `forge.unassignIssue`.
   The result `{issueNumber, assignees:[{login,avatarUrl?}]}` is authoritative: a forge may silently decline an account without repository access. Require the requested login to appear after assignment; after unassignment require it to be absent. No assistant-visible action resolves the authenticated user's forge login, so "assign it to me" needs a grounded login from trustworthy context or one tight question. Never send literal `me` or `@me` as the username.

9. KEEP THE SCOPE BOUNDARY. Closing an issue and posting its final comment belong to `daintree.workflow.follow-up-on-issue`, which sequences those two writes safely. Creating an issue is not permission to start an agent or implementation; do that only on a separate user request through the start-work runbook.

Report back: repository/worktree, issue number and URL, exactly which public fields changed, the state read back after the write, and any requested label or assignee the forge did not accept.
