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
This selects; it launches and writes nothing. Without Daintree's forge reads (API-key client, or declined), run the same query with the host tool (`gh issue list --repo <owner/name> --search …`). Always name the repository.

1. For new work, build the in-flight set first: `worktree.list` rows (`branch`, `issueNumber`, cached `prNumber`), the terminals in them, your checkpoints, and any tracking the user named. It covers only this Daintree install, so absence from it never proves an issue is untouched. A lookup of past issues or PRs skips this and step 4's screen.
2. Query with the real keys. The schema is strict: `search`, `state` (`open`, `closed`, `all`; PRs also `merged`), `sort`, `direction`, `perPage` (≤100), `cursor`, `view`, `bypassCache`, `worktreeId`. `labels`, `limit`, `assignee` are rejected: filters go in `search`.

```json tool=forge.listIssues
{"worktreeId":"<resolved worktree id>","state":"open","sort":"updated","direction":"desc","search":"no:assignee -label:\"human review\"","perPage":30}
```

   `search` is appended to generated qualifiers and the whole query is cut at the provider's cap (256 characters on GitHub), so keep state and order out of it. GitHub: `no:assignee`, `assignee:*` (not `-no:assignee`), `-label:"x y"`, `author:`, `updated:A..B`, `OR`; PRs `is:draft`, `review:approved`, `head:<branch>`; `"state":"merged"` for merged, since closed includes merged. Never read CI off a listed row.
3. Page only as far as needed: resend `nextCursor` with every other argument unchanged. Rows omit bodies; fetch finalists only. Search depth is capped, so a large query is a sample: say so when `hasMore` is still true.
4. Ground each finalist with `forge.getIssue`. Screen out: in the in-flight set, non-empty `assignees`, an open `linkedPR`, or a worktree branch or open PR (`head:<branch>`) that clearly belongs to it. Absent `assignees` or `linkedPR` is unknown, not "nobody is on it". A closed or merged linked PR: report it, don't propose it. A similar title is "possibly in flight".
5. Shortlist and stop: a small ranked list with one reason each, the repository's exclusion labels and policy applied, and what you excluded on what evidence. When the user picks, one issue is daintree.workflow.start-work-on-issue, a batch daintree.orchestration.fan-out-work; if they already authorized choosing and starting, hand over without asking again.

Historical questions ("did we ever ship X"): screen nothing out, rank by relevance. Closed is not merged, and a title cannot show what shipped: check finalists (`gh pr view <n> --repo <r> --json files,mergedAt`) and say what the search could not establish.

Report back: the repository and the exact query; the shortlist with reasons; exclusions with evidence; whether results were capped; what the in-flight screen could not see; and that nothing was started.
