---
id: daintree.workflow.find-issue-work
title: Find forge issue work
summary: Search the issue tracker with a provider-native query, then screen the hits against work already in flight so the shortlist only contains issues nobody has started.
whenToUse: Use when the user wants to FIND issues rather than start one — search or filter the tracker, triage the backlog, "what should I pick up next", "anything unassigned", "find me a small bug nobody's on" — or to check whether an issue is already being worked before committing to it. The deliverable is a shortlist or one chosen issue, NOT a launched agent. Do not use once the issue is chosen and the user wants it started (use daintree.workflow.start-work-on-issue); to launch a batch of issues in parallel, one agent each (use daintree.orchestration.fan-out-work); to reconstruct what was already running across earlier sessions (use daintree.orchestration.status-and-resume); or to read an existing PR's CI, review state or issue thread (use daintree.workflow.follow-up-on-issue).
risk: read
requiredTools:
  - context.snapshot
  - workflow.list
  - forge.listIssues
  - forge.getIssue
  - forge.listPRs
---
Use when: the user wants to find or triage issues worth picking up, not to start one.

The trap this runbook fixes: `forge.listIssues` looks like it takes the obvious filter
keys and it does NOT. Its schema is STRICT — `labels`, `limit`, `assignee` and every other
invented key are REJECTED with a validation error naming the bad key. That strictness is
deliberate (a stripped key used to return a confidently UNFILTERED page, and the caller
acted on the wrong set), but the tool's own description still lists "state, labels,
limit, …" as if they worked. Ignore that. Real filtering happens in `search`, a
provider-native query passthrough that is the most useful key on the whole forge surface
and is documented nowhere else.

Procedure:

## Step 1 — Know what is already in flight, BEFORE you query
Read the worktree and ledger rows already injected this turn (else `context.snapshot`) —
OPEN runs ride every round's turn context as one-line rows, so you usually need no call at
all. Reach for `workflow.list({"status":"active"})` (flat args, NOT nested in
`arguments`) when you want history the turn context does not carry. Each row gives
`issueNumber` / `prNumber` / `branch`. Build the set of issue numbers already being worked
— that set is what turns a raw search result into a shortlist worth reading, and it means
you never propose an issue the user already has a worktree open on.

## Step 2 — Write the query, and put it in `search`
`forge.listIssues` is a passthrough wrapper accepting ONLY `arguments` at the top level;
nest every field inside it. The accepted keys are exactly: `search`, `state`
(`open`|`closed`|`all`, default open), `perPage` (1-100, default 20), `sort`
(`created`|`updated`), `direction` (`asc`|`desc`), `cursor`, `view`, `bypassCache`, `cwd`.
Anything else errors.

```
forge.listIssues({
  "arguments": {
    "search": "is:open is:issue no:assignee -label:human-review sort:updated-desc",
    "perPage": 30
  }
})
```

`search` is a raw provider query fragment, NOT a plain-text filter — it is appended after
the generated repo/type/state qualifiers and routes through the provider's search API.
The dialect is the forge's own, and typically supports:
- `no:assignee`, `no:label`, `no:milestone` — the "nobody has claimed this" signals.
- `-label:X` to exclude; quote multi-word values, `-label:"human review"`.
- `label:X`, `assignee:@me`, `involves:@me`, `author:@me`.
- `created:>YYYY-MM-DD`, `updated:<YYYY-MM-DD`, ranges `updated:A..B`.
- `sort:updated-desc`, and explicit `OR` / `AND` / parentheses for anything compound.

Two gotchas worth remembering: `-no:assignee` is INVALID — negation does not compose with
`no:`, so use `assignee:*` when you want "has any assignee". And search routes through an
API that CAPS result depth, so a big query is a sample, not a census — say so rather than
claiming you swept the whole backlog.

There is no `search` on `forge.listPRs`. No provider on the roster can query PRs, so the
key is rejected outright rather than quietly returning an unfiltered page — filter PRs
with `state` and read the ones you need.

## Step 3 — Page only as far as you need
The result is `{ items, nextCursor, hasMore }`. To continue, pass the PREVIOUS response's
`nextCursor` back in the same nested shape; never pass an empty string (it is rejected,
because it would alias page one's cache while asking the provider for an invalid cursor).

```
forge.listIssues({"arguments": {"search": "<same query>", "cursor": "<nextCursor>"}})
```

Items default to a SUMMARY projection — bodies and raw provider payloads are dropped to
keep the page small. That is what you want for triage. Only ask for `"view": "full"` when
you actually need bodies, and prefer fetching the two or three finalists individually in
step 4 over pulling full bodies for a whole page.

## Step 4 — Ground the finalists, and screen out what is taken
`forge.getIssue({"arguments": {"issueNumber": <int>}})` returns the issue with `title`,
`body`, `labels[]`, `assignees[]`, `commentCount`, and — the field that answers "is
someone already on this" — `linkedPR: { number, state, url, ciStatus? }`. Batch these:
several forge reads emitted in ONE reply run concurrently, so fetch every finalist at
once rather than one per turn.

Screen a candidate OUT when: its number is in the step-1 in-flight set; `assignees[]` is
non-empty; or `linkedPR` exists and is still open. A closed or merged `linkedPR` is the
opposite signal — the work may be done and the issue merely left open, which is worth
reporting rather than proposing as new work. If you need to match by branch instead,
`forge.listPRs({"arguments": {"state": "open"}})` lists PRs (`head`, `number`, `url`) —
but do not treat a similar TITLE as proof anything is underway; say "possibly in flight"
and let the user decide.

## Step 5 — Shortlist and stop
Present a SMALL ranked shortlist against whatever the user actually asked for (size,
area, label), each with one line of why, and name what you excluded and on what evidence
— the exclusions are half the value. Then stop: this runbook selects, it does not launch.
Selection is not a mutation, so none of this needs confirmation. Hand off only when the
user picks: one issue to start is daintree.workflow.start-work-on-issue, a batch to run
in parallel is daintree.orchestration.fan-out-work.

Report back: the query you actually ran (verbatim, so the user can adjust it), the
shortlist with a reason each, what you excluded and why (already assigned, linked PR
open, worktree already exists), whether the search was capped or paged short of the whole
backlog, and the next step you are waiting on — never imply you launched anything.
