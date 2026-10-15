# Authoring: what a runbook is, and how to write one that works

_In a `/runbook` run, every command in this file runs as `. "<ENV>" && <command>`
(SKILL.md, Workspace and environment), which sets `PY`, `W`, `T`, `APP_REPO`, `APP_SHA`
and the working directory._

`docs/RUNBOOKS.md` is the authoring guide and `CLAUDE.md` holds the repo rules. Read
both before Phase 3. This file is the checklist and the traps, in the order you meet
them.

## The orchestration test (decide this first)

The agent following a runbook is **Daintree's orchestration layer** (the user's rule,
2026-09-28). Its own hands:

- read Daintree state: terminal, agent, worktree and project listings and status,
  forge reads, and git reads (log, diff, status) for verification;
- run Daintree operations: launch, send, notify, rename, move, worktrees, recipes,
  `project.runCheck`, plugin validate/reload/diagnostics, dev preview reload, a
  visible `terminal.new` for a command;
- verify what agents did: read the diff, re-run the checks, read the forge.

Everything that is **work** (editing code, resolving conflicts, rebasing, bisecting,
upgrading, migrating, reproducing, writing tests or docs, building a plugin) goes to a
visible Daintree agent in the right worktree, briefed with the runbook's know-how. The
caller does the work itself only when the user asks it to, and the runbook says so in
one line at most.

The shape that follows from this: resolve targets and preflight (Daintree state) →
pick or create the worktree → brief and launch → end the turn → on the notice, verify
independently, answer in-scope questions, at most one corrective round → report.

A proposal with no Daintree role once framed this way (a pure host-shell chore) does
not belong in the catalog. Recommend dropping it rather than padding it.

## New runbook, or improve an existing one?

The issue's "Kind of change" is a proposal. Measurement decides:

- **Improve** when the request already routes to a runbook whose deliverable matches
  and the gap is in its procedure (body) or its boundary (routing text). Most
  "improve" work is a body fix, which moves no routing.
- **New** when nothing owns the deliverable, or the owner would have to grow a second,
  different deliverable to cover it. Two procedures under one routing document are
  worse than two runbooks with a fence between them.
- **Fences only** when the runbook exists and is right, but a neighbour steals its
  traffic.
- **Cases only** when it already routes and works: pin it.

Record the decision and the measurement behind it in the design note.

## Ids and namespaces

`daintree.<namespace>.<leaf>`, kebab-case leaf, filename stem equal to the id.
Existing namespaces (`ls src/daintree_assistant_server/runbooks/files | cut -d. -f2 |
sort | uniq -c`): `orchestration`, `agents`, `git`, `worktree`, `review`, `forge`,
`workflow`, `runtime`, `release`, `ci`, `terminals`, `edits`, `debug`, `context`, and
single-runbook ones. Reuse one when the runbook shares its judgment.

A **new namespace** with several runbooks that share judgment may get a domain
foundation, `daintree.<namespace>.foundation` with `foundation: true`. The nearest
foundation is delivered automatically with every runbook in its namespace, so it is
paid on every hit there: keep it to judgment every runbook in the domain needs.

- **Size.** Like any body it must stay under 5120 bytes: the recommended-budget test
  fails above that unless the id is on its allowlist, and the allowlist (where only
  `daintree.foundation` sits, under its own 5400-byte ceiling) is a protected test.
- **Inheritance.** A domain foundation does not inherit `daintree.foundation`: a
  foundation has no implicit edge to an ancestor (`catalog.py`,
  `_direct_dependencies`). One that builds on the shared rules declares
  `requires: [daintree.foundation]`, and then every runbook in the domain carries
  both. With `daintree.foundation` at about 5.4 KB, a runbook and its domain
  foundation share what is left of the 10240-byte bundle ceiling, so plan both sizes
  together.
- **Recall case.** The foundation-alone shape (`cases.py` rejects a foundation in
  `required` beside a specific).

## Grounding: the app is the authority, never memory

Every action a runbook names, and every claim it makes about what an action does,
is checked against two sources:

1. **What the app does**: the action's registration in the Daintree app,
   `$APP_REPO/src/services/actions/definitions/*Actions.ts` (`actions.set("<id>",
   () => ({ ... }))`: `description`, `argsSchema`, `resultSchema`, `danger`, `kind`),
   with the schemas in `$APP_REPO/shared/types/`. MCP exposure, tiers and per-caller
   gating live in `$APP_REPO/electron/services/mcp-server/`. The `description` string
   is where the maintainers wrote the traps down; read it in full. Read the revision
   Setup fetched (`$APP_SHA`, the app's `origin/develop`), never whatever branch the
   checkout is on (another session may be working there):

   ```bash
   git -C "$APP_REPO" show "$APP_SHA":src/services/actions/definitions/terminalInputActions.ts
   git -C "$APP_REPO" grep -n '"terminal.sendCommandOwned"' "$APP_SHA" -- src electron shared
   ```

2. **What this repo will accept**: the captured surfaces in
   `tests/fixtures/daintree_mcp/` (`pane-core`, `pane-full`, `help-core`, `help-full`,
   `hub-full`, `lane-full`, `external`), all from one app commit (the README names
   it). `requiredTools`, `optionalTools`, tagged examples and any action-shaped name in
   a body are checked against them. `requiredTools` must be on the caller's surface:
   `pane-core`, or for `daintree.hosts.*` one of `hub-full`/`lane-full`
   (`test_required_tools_are_on_the_callers_surface`, `CALLER_POLICY`).

When the two disagree, the capture is stale. Refreshing it is a change to every
runbook's validation, so it is in scope only when the issue authorises it (see
"Capture refresh"), and otherwise the run stops as blocked on the refresh. Never
weaken a test to get past it.

Third-party behaviour (a CLI's slash commands, `gh` output, a provider's usage page)
is checked against current docs with one bundled `mcp__ask-google__ask_google` query
and stated with its date ("September 2026 builds; revalidate").

## Callers and surfaces

- **Agent pane** (`pane-*`): Claude Code or Codex in a Daintree pane. Owned tools:
  `terminal.sendCommandOwned`, `terminal.closeOwned`, `terminal.injectOwned`,
  `terminal.sendKeysOwned`. Its tier decides what runs without an approval.
- **Sidebar assistant** (`help-*`): the unscoped `terminal.sendCommand`,
  `terminal.close`, `terminal.sendKeys`. Some features are sidebar-only by design
  (cross-host messaging, for instance).
- **API-key client** (`external`): a curated subset with **no `notify`** on
  `agent.launch` or the sends and no `terminal.notifyWhenIdle`.
- **Hub and lane** (`hub-full`, `lane-full`): the sidebar assistant in cross-host
  messaging. Only these are offered `host.*` and `assistant.*` messaging tools.

A runbook's baseline path is the agent-pane path, except in `daintree.hosts`, whose
baseline runs on the hub or a lane (`CALLER_POLICY`). A sidebar-only variant is one
short line plus `optionalTools`; a pane runbook's hub branch lists `host.*` there too.
Another domain whose *baseline* cannot run in a pane is a policy question the issue
has to settle (the policy is a test, and tests change only with authorisation).

## Frontmatter

| key | rule |
|---|---|
| `id` | equals the filename stem |
| `title` | short, what it does |
| `summary` | one line, ≤ 512 bytes, what it achieves |
| `whenToUse` | ≤ 2048 bytes; positive clause in the words users and agents use, the deliverable, then carve-outs naming fully-qualified ids (`references/routing.md`) |
| `foundation` | `true` only for a domain foundation |
| `risk` | `read local ui terminal project external git system`; descriptive only |
| `requiredTools` | actions the baseline path calls; on `pane-core` |
| `optionalTools` | actions a branch, caller variant or enhancement calls; never also required |
| `requires` | other runbooks whose instructions must be read first; must exist, be public, form no cycle (the implicit foundation edge included) |

Unknown keys are rejected. Native host tools (a shell, `gh`, file edits) never go in
either tool list; name them in the step that uses them.

## The body

A selected runbook's body is returned in full with the foundation, and callers that
print the whole result pay for it twice. Every byte counts.

**Shared rules live in `daintree.foundation`** (caller differences, ownership,
launching, `waitForReply` and notices, long-run checkpoints, proving a finish). Do not
repeat them, and do not repeat what an action's own `description` or Daintree's
assistant prompt already says. Read the foundation before writing a step that touches
any of those.

Asynchronous shape (anything that ends its turn on a notice):

```markdown
<one or two lines: what this runbook does that the foundation does not>

1. Resolve and preflight (Daintree state; what to check before acting).
2. Pick or create the worktree; brief and launch (exact brief contents) with
   `notify:true, handback:true`.
3. End the turn on the notice when nothing else is left.

On notice or resume: what the reply means here; questions, dialogs, errors; how to
verify independently; one corrective round at most.

Stop and recovery: finite bounds; what is never replayed (launches, public writes).

Report back: evidence, outcome, pending work, the real continuation.
```

Synchronous shape (a rename, a status read, a `waitForReply` round): the numbered
steps and "Report back", with no empty notice or checkpoint sections.

The tests hold these:

- the body contains "Report back" (`test_runbooks_end_with_report_back`);
- a body that arranges a notice (`"notify":true` or `terminal.notifyWhenIdle`) has
  both an "On notice" and a "Stop and recovery" section
  (`test_notice_driven_runbooks_say_how_they_resume_and_stop`);
- no pipe tables in a body (the test catches only the literal `|---`; avoid tables
  altogether);
- body ≤ 5120 bytes (`test_runbook_bodies_under_recommended_budget` fails above it
  unless the id is allowlisted; 8192 is a further absolute ceiling), and the body plus
  everything `supporting_for` sends with it ≤ 10240 bytes
  (`test_a_runbook_with_its_supporting_runbooks_stays_bounded`);
- an action-shaped name (`family.action`) in a family the app or the retired CLI used
  must be a real captured action, and a `daintree.*` name must be a runbook id or
  namespace; the retired CLI families (`agentTask`, `async`, `context`, `fs`, `grant`,
  `memory`, `queue`, `scratch`, `subagent`, `timer`, `tool`, `watcher`) always fail
  (`test_runbook_migration.py`). Names in families it does not know pass unchecked,
  so ground those by hand.

The rules that make a body followable:

1. **State the whole contract.** Target scope, concrete arguments, what the result
   looks like, what success is, partial failure, retry, when to stop. Preserve
   returned ids. A launch is a panel, not a ready agent; a send is queued, not
   delivered; a notice is a stop, not a success; a timeout is not a failure.
2. **Design for the fewest calls.** One call per round: `agent.launchMany`,
   `terminal.sendCommandMany`, `terminal.closeMany`. `waitForReply:true,
   handback:true` when the next step needs the answers; `notify:true, handback:true`
   and end the turn only for long background work. Never read a terminal for a reply
   the call or the notice already carried.
3. **Stay inside the requested outcome.** No extra writes, closures or public
   messages; no second approval for work already authorised. App confirmations still
   apply.
4. **Never replay an ambiguous write.** Read the real state first.
5. **The brief to a delegated agent** carries: the objective, the output expected
   (and the exact report shape: branch, SHA, PR URL, test result), the scope and
   tools allowed, when to stop, and what it must not do (no push, force or deletion
   unless the user authorised it).
6. **Complete tagged examples** for the calls that matter, arguments only, with
   angle-bracketed runtime values:

   ````markdown
   ```json tool=agent.launch
   {"agentId":"claude","name":"Review: retry","worktreeId":"<worktree id>","prompt":"…","notify":true,"handback":true}
   ```
   ````

   Add `surface=<capture>` for an example written for another caller.
   `tests/unit/test_runbook_examples.py` validates each against that caller's
   captured schema.
7. **Report back** names what the caller verified itself, separately from what an
   agent or another machine claimed.

## Public content

The MCP endpoint is public and unauthenticated. Nothing private goes into a runbook's
routing text or body: no project names, paths, people, machine names, issue or PR
numbers, or pasted content. Generalise ("a remote host", "the shared remote"). Cases
and corpus rows live in this repo but shape a model that ships; scrub them the same
way while keeping the query's shape.

## Editing an existing runbook

- **Body**: keep house style and the size budget; remove duplication before adding a
  rule. A body change moves no routing, so Phase 5 only confirms with one gate run
  that nothing moved.
- **Routing text**: score the proposed text with `try_runbook_text` first, then write
  it, then re-sweep. Respect the two-attempt cap.
- **Frontmatter tool lists**: ground every addition in the action source and the
  captures.
- Read `git log --oneline -- <file>` and the case notes that name the runbook: the
  last edits usually record why a line exists. Do not delete a line whose reason you
  have not found.

## Neighbour fences

For every neighbour the new or changed `whenToUse` names, decide whether the
neighbour needs a carve-out back (it usually does, `references/routing.md` rule 8).
One clause, abstract, fully-qualified id. Score it with `try_runbook_text`. Commit
fences with the runbook they serve.

## Capture refresh (only when the issue authorises it)

`tests/fixtures/daintree_mcp/capture/capture.sh <app commit-ish> [branch-label]`
regenerates every capture from one app commit in a throwaway detached worktree of
`$APP_REPO` (it needs `$APP_REPO/node_modules` installed and writes nothing into the
app checkout). New caller scenarios (a configured host, an enrolled lane) are
changes to `capture/captureMcpSurfaces.test.ts`. Update the fixtures README (commit,
date, tool counts, hashes), then run the whole unit suite: every runbook's examples
are re-validated against the new schemas, and a renamed or removed action fails the
migration test across the catalog. Capture only from a commit on the app's
`origin/develop` (a merged change), never from an open branch.
