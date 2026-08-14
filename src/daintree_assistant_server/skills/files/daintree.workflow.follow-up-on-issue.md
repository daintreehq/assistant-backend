---
id: daintree.workflow.follow-up-on-issue
title: Follow up on a forge issue
summary: Read where an issue actually stands right now — its PR, the CI roll-up, the review decision, the comment thread — then relay what is blocking it, or comment and close it out.
whenToUse: Use for a one-off READ of an issue or PR's current forge state and the writes that close it out — "is that PR green?", "did CI pass?", "what's blocking it?", "did anyone reply on that issue?", "what did the reviewer say?", "comment on it and close it". Covers checking CI or check status, reading a review decision, reading an issue's comment thread, forwarding forge-sourced blockers to the agent still working, and posting a closing comment. Do not use to leave something watching and wake the user on a LATER PR transition (use daintree.orchestration.watch-outcome); to commit, push, open or ready a PR (use daintree.workflow.prep-branch-for-review); to search or triage the backlog for new work (use daintree.workflow.find-issue-work); to start work on an issue (use daintree.workflow.start-work-on-issue); or to forward a review that is already sitting in a reviewer agent's terminal rather than on the forge (use daintree.agents.relay-review-feedback). Do not use to DIAGNOSE or FIX a failing check — reading that a check is red is here, working out why and getting it green is daintree.ci.diagnose-and-fix.
risk: external
requiredTools:
  - context.snapshot
  - forge.getIssue
  - forge.getPR
  - forge.listPRs
  - daintree.call
  - agentTask.spawnForEdits
  - terminal.awaitAll
  - terminal.summarize
  - terminal.sendCommand
---
Use when: the user wants to know where an issue or its PR stands on the forge right now,
or wants it commented on and closed.

CI, review decisions and issue threads ARE readable — that changed, and older habits
assume otherwise. `forge.getCIStatus` returns a check roll-up, `forge.getPR` carries
`reviewDecision`, and `forge.listIssueComments` reads a thread back. You no longer need an
agent to answer "is it green". What an agent is still for is DETAIL none of them carry:
check names, job logs, failure output, the text of a review left on the PR.

Procedure:

## Step 1 — Fix which issue and which PR
Read the injected worktree/ledger rows (else `context.snapshot`) — a row carries
`issueNumber`, `prNumber` and `prUrl` together, which is the cheapest way to tie the two.
Otherwise `forge.getIssue({"arguments": {"issueNumber": <int>}})` MAY carry `linkedPR:
{ number, state, url, ciStatus? }` — the key is optional, and its absence means no linked
PR was reported, not that none exists. If neither ties them and the user named only one,
ask rather than guessing — reporting on the wrong PR is worse than a clarifying question.
`forge.listPRs({"arguments": {"state": "open"}})` lists open PRs carrying `number`, `url`
and `headRef` (the source branch — the field is `headRef`, not `head`) if you must match on
branch. (`forge.listPRs` accepts no `search` key at all; it is rejected outright.)

## Step 2 — Read the review decision, then the CI verdict
Two different calls, and they are not interchangeable.

`forge.getPR` is the TYPED exception on this surface: its args go at the TOP level, not
nested in `arguments`. It is a free read needing no confirmation, and several typed forge
reads emitted in ONE reply run concurrently.

```
forge.getPR({"prNumber": <int>})
```

It returns `state`, `isDraft`, `merged`, `baseRef`, `headRef`, and may carry
`commentCount` and — the field to read here — `reviewDecision`: `APPROVED`,
`CHANGES_REQUESTED`, `REVIEW_REQUIRED`, or `null`. A `null` is a real answer meaning
reviews are not gated, and reads as approved; only an ABSENT key means "not reported". The
whole result is nullable — a PR that does not exist comes back empty rather than failing,
so treat empty as absence, not as an error.

Do NOT read CI off this call. `ciStatus` is declared on the PR shape but a provider need
not populate it on a single-PR fetch, and the common one does not — it is absent here, and
absent is not `success`. The CI verdict has its own action, reached through the escape
hatch:

```
daintree.call({"name": "forge.getCIStatus", "arguments": {"prNumber": <int>}})
```

`daintree.call` is the highest-risk tool there is — system tier, ALWAYS confirmed, and it
does not batch with the typed reads above (it runs on its own). So this is one deliberate
confirmed call, not part of a free parallel sweep: read the PR first, and only reach for
the CI roll-up when the CI answer is actually what the user asked for.

-> `{ "ciStatus": { "state", "total", "passed", "failed", "pending",
"requiredChecksPassing"? } | null }`. Read `state` for the answer — it is one of
`success`, `failure`, `pending`, `neutral`, `unknown`. `neutral` means no required checks
are configured, and `unknown` means the provider reported states that do not map; neither
is a pass. The counts are COUNTS — this roll-up carries no check NAMES, so it can tell you
three checks failed but never which ones. Three traps, all of which produce a confidently
wrong "it's green":
- The counts cover REQUIRED checks only, and a `total` of 0 also appears when the
  required-check list could not be read in full. Zero total is never evidence that
  nothing gates the merge.
- Values are provider-cached and can lag by around a minute. A single `success` is not a
  settled verdict — re-read before you act on it, and never report a just-pushed commit
  as green off one call.
- `success` is about checks, not correctness. It does not mean the work is right.

## Step 3 — Read the thread when the question is "what did anyone say"
`forge.getIssue` reports `commentCount` but no comment text. For the text:

```
daintree.call({"name": "forge.listIssueComments",
               "arguments": {"issueNumber": <int>, "perPage": 50}})
```

-> `{ items: [{ id, body, url, createdAt, author? }], nextCursor, hasMore, totalCount? }`.
Comments arrive OLDEST FIRST, so the newest reply is on the LAST page — page with
`cursor` to the end before claiming you have read the latest, or you will report a stale
answer with total confidence. An empty page genuinely means nobody has commented; a
missing issue or a provider that cannot read threads FAILS instead, so silence here is
never ambiguous.

## Step 4 — Know exactly where the forge stops, and escalate for the rest
Be precise about what you can and cannot get, because the gap is where confident wrong
answers come from:
- The forge gives you the review DECISION, the CI roll-up STATE and its counts, and issue
  comment BODIES.
- It does NOT give you check names, check output, job logs, or the bodies of reviews and
  inline review comments already left on a PR. No READ action returns any of those (the
  review-WRITE actions echo back the body you just posted, which is not the same thing).
  A `failure` roll-up says THAT checks failed, never which or why.

So when the user wants the cause, or the actual text of a review left on the PR, that is
an agent's job: spawn one with `agentTask.spawnForEdits` `mode:"explore"`, picking a
best-fit id from the agent registry in your startup context (never an invented or
user-transcribed name). Pick ONE supervision mode — either drive it in-turn, waiting with
`terminal.awaitAll({"terminalIds": ["<id>"]})` before reading it with `terminal.summarize`
(a snapshot read with no wait of its own, so reading first just captures an empty pane),
or attach a watcher and end the turn. Never both. Report the roll-up you already have
while it works rather than making the user wait for everything.

## Step 5 — Relay blockers to whoever is still working
If an implementer agent is still running on this issue, send it the ACTIONABLE content
with `terminal.sendCommand` ({"terminalId": "<full id>", "command": "..."}). A bare
`CHANGES_REQUESTED` is a state, not feedback: relaying it alone tells the agent nothing it
can act on. Send what you actually hold — the review decision plus the specific asks from
the issue thread, or the failure counts — and be explicit that the detail is not yours to
give when it isn't. If the substance lives in PR review bodies or check output, get it via
step 4 FIRST and relay that; do not dress up a roll-up as feedback. Feedback already
sitting in a reviewer AGENT's terminal is a different job — that is
daintree.agents.relay-review-feedback.

## Step 6 — Comment and close (the writes — confirmed)
Forge writes have no typed wrapper; each goes through the escape hatch, so each is an
externally visible forge mutation invoked through the system-risk `daintree.call` — always
confirmed, and one call at a time. Only close on the user's explicit ask: a green
CI and an approval are evidence, not authority, and neither is permission to close
anything.

```
daintree.call({"name": "forge.addIssueComment",
               "arguments": {"issueNumber": <int>, "body": "<what shipped, with the PR url>"}})
```

`body` must be non-empty. Comments are public, notify every watcher, and CANNOT be edited
or withdrawn through this surface — treat posting as final and get it right the first
time. Then:

```
daintree.call({"name": "forge.closeIssue",
               "arguments": {"issueNumber": <int>, "stateReason": "completed"}})
```

`stateReason` is `completed` | `not_planned` | `duplicate` (default `completed`) — state
one, since it is what distinguishes finished work from something declined. Comment first,
then close: if the comment fails, do NOT close silently, and if the close fails after the
comment landed, say so plainly rather than reporting a clean finish. Re-read the issue
before writing if any time has passed — do not add a second closing comment to an issue
someone already closed.

Report back: the issue and PR numbers with the PR url, the CI verdict as the roll-up
actually stated it (`state` plus the passed/failed/pending counts, and any caveat — a zero
total, a `neutral`, a value that may be a minute stale), the review decision, anything
notable from the thread and whether you paged to the newest comment, what you relayed and
to which terminal, and for any write, exactly what landed and what did not — never report
"closed out" when only the comment posted.
