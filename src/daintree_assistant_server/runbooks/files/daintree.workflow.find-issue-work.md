---
id: daintree.workflow.find-issue-work
title: Find forge issue work
summary: Search issues or pull requests with a provider-native query; for new work, screen the hits against work already in flight so the shortlist only contains work nobody has started.
whenToUse: Use when the user wants to FIND issues or pull requests rather than start one — search or filter the tracker, triage the backlog, "what should I pick up next", "anything unassigned", "find me a small bug nobody's on" — or to locate past pull requests by text, "find the closed PRs about X", "which PRs touched Y", "did we ever ship Z" — or to check whether an issue is already being worked before committing to it. The deliverable is a shortlist or one chosen issue, NOT a launched agent. Do not use once the issue is chosen and the user wants it started (use daintree.workflow.start-work-on-issue); to launch a batch of issues in parallel, one agent each (use daintree.orchestration.fan-out-work); to reconstruct what was already running across earlier sessions (use daintree.orchestration.status-and-resume); or to read an existing PR's CI, review state or issue thread (use daintree.workflow.follow-up-on-issue).
risk: read
requiredTools:
  - worktree.list
  - forge.listIssues
  - forge.getIssue
optionalTools:
  - forge.listPRs
  - forge.getPR
  - terminal.list
---
Use when: the user wants issues or pull requests found, filtered or triaged into a shortlist, or wants to know whether an issue is already being worked. This book selects; it never launches anything.

Availability: Hybrid. Daintree's app MCP reads the forge (`forge.listIssues`, `forge.listPRs`, `forge.getIssue`); at core tier each read asks the user first, and an API-key client or a session with no Daintree connection has none. Without them, run the same query through the host's permitted forge tool (for GitHub, `gh issue list --repo <owner/name> --search …`). Either way, name the repository explicitly. Nothing here writes.

Procedure:
1. For new work, know what is in flight before you query (a lookup of past issues or PRs skips this and the screen in step 4). Read `worktree.list`: each row carries `branch`, `issueNumber` and `prNumber` (a cached hint; null does not prove there is no PR). Add the terminals working in those worktrees (`terminal.list`), any run checkpoints you keep, and any project tracking the user pointed you to. That is the in-flight set. It only covers what this Daintree install can see: other machines, other people and branches outside Daintree are invisible, so absence from it never proves an issue is untouched.
2. Query with the real keys. The listing schema is strict and flat: `search`, `state` (`open`, `closed`, `all`; PRs also `merged`), `sort` (`created`, `updated`), `direction`, `perPage` (1 to 100), `cursor`, `view` (`summary`, `full`), `bypassCache`, and the location selectors `worktreeId`/`worktreePath`. Anything else (`labels`, `limit`, `assignee`) is rejected, so filtering goes in `search`:

```json tool=forge.listIssues
{"worktreeId":"<resolved worktree id>","state":"open","sort":"updated","direction":"desc","search":"no:assignee -label:\"human review\"","perPage":30}
```

   `search` is a provider-native fragment appended after the generated repository, type, state and sort qualifiers, and the whole query is cut to the provider's cap (256 characters on GitHub). Keep state and order in their own keys, not in `search`, or the duplicate qualifiers eat the end of your filter. GitHub's dialect: `no:assignee`, `no:label`, `-label:X` (quote multi-word values), `label:X`, `assignee:@me`, `author:`, `involves:`, `created:>YYYY-MM-DD`, `updated:A..B`, `OR` and parentheses. `-no:assignee` is invalid; use `assignee:*`. For PRs (`forge.listPRs`): `is:draft`, `review:approved`, `review:changes_requested`, `head:<branch>`, `base:<branch>`; ask for merged ones with `"state":"merged"`, since closed includes merged. Never read CI off a listed PR row.
3. Page only as far as the shortlist needs. Send the previous page's `nextCursor` back with every other argument unchanged (repository, search, state, sort, direction, perPage, view), and never an empty cursor. Rows default to a summary without bodies that still carries `linkedPR`; fetch bodies for finalists only. Search goes through the provider's search API, which caps result depth, so a large query is a sample, not a census: say so when `hasMore` is still true or the cap was reached.
4. Ground each finalist with `forge.getIssue` (`worktreeId`, `issueNumber`); an empty result means no such issue. Screen a candidate out when its number is in the in-flight set, `assignees` is non-empty, `linkedPR` is open, or a worktree branch or open PR (`forge.listPRs` with `"search":"head:<branch>"`) clearly belongs to it. An absent `assignees` or `linkedPR` is unknown, not "nobody is on it". A closed or merged linked PR suggests the work may already be done: report it rather than proposing it. A similar title proves nothing; say "possibly in flight" and let the user decide.
5. Shortlist and stop. Rank a small list against what the user asked (size, area, label) with one line of reasoning each, apply the repository's own exclusion labels and contribution policy, and list what you excluded and on what evidence; the exclusions are half the value. Launch nothing. When the user picks, one issue is daintree.workflow.start-work-on-issue and a batch is daintree.orchestration.fan-out-work. If they already authorized choosing and starting, hand over the evidenced choice without asking the same question again.

For a historical question ("find the merged PRs about X", "did we ever ship X"), started, closed or merged work is what you are looking for, so nothing is screened out for being in flight or done: rank by relevance instead. Closed is not merged, and a title search cannot show which files changed or that a feature shipped: check the finalists individually (`forge.getPR`, or `gh pr view <n> --repo <r> --json files,mergedAt`) and say what the search could not establish.

Report back: the repository and the query you ran, verbatim so the user can adjust it; the shortlist with a reason each; the exclusions and their evidence; whether results were capped or paged short; what the in-flight screen could not see; and that nothing was started.
