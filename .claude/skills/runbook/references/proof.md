# What a piece of evidence proves

"It routes and it works" is the right instinct and too coarse to write a PR from. A
green routing benchmark proves nothing about behaviour; a runbook that names real
actions can still teach the wrong order; a trial where the agent called the right
action proves nothing about whether the user got a true report.

Five layers. Every artefact belongs to exactly one.

**The rule: a delivery statement may not exceed its strongest proven layer.** It is
also the honest answer to the question a reviewer is asking: *how do you know?*

---

## Layer 1: Selection

> Does search return this runbook, selected, for the requests it should own, and not
> for its neighbours', with a margin that survives rephrasing?

**Evidence:** `bench search` sweeps across six or more phrasings, before and after ·
the full `bench local` gate · `bench compare` against the baseline, every moved case
explained · recall and routing-trap cases · `bench coverage`.

**Cost:** free and deterministic.

**Cannot show:** anything about behaviour. A runbook can be found perfectly and then
tell the agent to do the wrong thing.

**Sounds like:** "The request now selects `daintree.hosts.survey-hosts` across seven
phrasings, nearest neighbour `daintree.orchestration.supervise-fleet` at 0.04."

## Layer 2: Contract

> Does the runbook name only actions its caller is actually offered, with arguments
> that caller's schema accepts?

**Evidence:** the unit suite against the captured surfaces:
`test_runbook_catalog_hygiene.py` (declared tools are real, required tools are on the
pane surface), `test_runbook_examples.py` (each tagged example validates against its
caller's schema), `test_runbook_migration.py` (no retired or invented action names),
`test_runbook_budgets.py`.

**Cost:** about five seconds.

**Cannot show:** that the action does what the runbook says it does, or that the
capture is current.

## Layer 3: Grounding

> Is every factual claim in the body true of the code it describes?

**Evidence:** for each claim (an argument's meaning, a result field, a confirmation
gate, a limit, a refusal code, a third-party CLI's command), a citation: the app's
action registration or schema at `file:line` on `origin/develop`, the MCP server code
that gates it, or a dated external source. A limitation the runbook keeps is a claim
too.

**Cost:** reading. The research phase produces most of it.

**Cannot show:** that an agent reading the runbook will follow it, or that a notice
arrives.

**Sounds like:** "`host.sendMessage` returns `task: null` with a `taskId` on a
stale-generation send (`<schema file>:<line>` on `origin/develop`), and the runbook
treats that as not sent."

## Layer 4: Execution

> Did a real agent, in a real Daintree pane, search, receive this runbook, and carry
> out its procedure with the right actions and arguments?

**Evidence:** a live trial transcript (`scripts/trial/`): the `search_runbooks` query
and what came back selected; each Daintree action with its arguments, against the
procedure's steps; the `Daintree:` notice lines that resumed the pane after a
`notify`; dialogs answered.

**Cost:** minutes per trial, and the user's Claude or Codex allowance.

**Cannot show:** that the outcome in the world was right, or that the report was true.

## Layer 5: Outcome

> Did the real-world effect happen, and did the agent tell the user something true?

**Evidence:** the scratch repository's state after the trial (the branch, the commit,
the worktree, the closed pane, the untouched files) checked by you, not read off the
agent's claim · the final reply against that state · the absence of a false
completion claim · the "Report back" items present.

**Cannot be skipped** for a runbook whose point is reporting back.

---

## Writing it down

In the PR and the final report, name the layer for each claim, plainly:

> ❌ "The hub can now survey remote hosts."
>
> ✅ "Search selects it across seven phrasings (selection), and every action it names
> is on the sidebar surface with valid example arguments (contract). Each claim about
> `host.list` cites the app source (grounding). It has not been run live: the hub is
> the sidebar assistant with Remote Hosts configured, which the pane trial harness
> cannot stand up. A live run from a Claude Code and a Codex hub is owed."

## Host callers

A Shell's help session, the relayed tool session and a delegated assistant terminal
are different callers. Validate each against the current capture. Delivery and
ownership claims need the app's contract tests and a live scenario on that path;
sidebar selection alone does not prove the receiving assistant handled its brief.
See `host-control-trials.md`. Archived inbox pairs prove only their historical model.

## The two mistakes most runs make

**Stopping at layer 1 or 2 and calling it done.** A runbook with green tests and
cases and no trial has moved a number and passed a lint. Say so.

**Quoting the agent's report as layer 5.** A trial agent saying "done, committed
abc123" is the agent's claim. Layer 5 is you checking the scratch repo for `abc123`.

## When the issue demands a layer the run cannot reach

Some issues require a live run the harness cannot stage: a sidebar-only caller, Remote
Hosts, another machine, a real account. Then:

- reach the highest layer you can (usually 3, sometimes a partial 4 with the closest
  honest pane scenario);
- write the owed run in the PR as a checklist item for the person who can run it,
  with the exact prompts and what to check;
- keep the issue open: the PR body says "Part of #N" instead of "Resolves #N", so
  merging does not close an issue whose own definition of done is unmet.
