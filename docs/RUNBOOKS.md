# Writing runbooks

A runbook is a procedure that Claude Code or Codex follows to do one kind of work in
Daintree. Agents find them through the [runbook MCP server](MCP.md): they describe a
task, get the best-matching runbooks back with the foundation, and follow the one
that fits. A runbook is only useful if search **finds** it for the right request,
and an agent that reads it can **carry it out** correctly.

The catalog lives in [`src/daintree_assistant_server/runbooks/files`](../src/daintree_assistant_server/runbooks/files),
one Markdown file per runbook. Growing it is the main work in this repository.

## Who reads a runbook

The reader is an **engineering agent** with its own shell, file tools and judgment,
connected to two separate things:

- **This server**, which supplies the procedure and nothing else.
- **The Daintree app's MCP**, which supplies the actions: `agent.launch`,
  `terminal.getStatus`, `terminal.getOutput`, `terminal.sendCommandOwned`,
  `terminal.notifyWhenIdle`, `worktree.createWithRecipe`, and so on.

The reader follows a runbook as **Daintree's orchestration layer**, and a runbook is
built around a Daintree workflow. Its own hands read Daintree state, run Daintree
operations (launch, send, notify, worktrees, recipes, checks, previews) and verify what
agents did. The work itself (editing code, resolving conflicts, rebasing, bisecting,
upgrading, migrating, reproducing, writing) goes to a visible agent in the right
worktree. The runbook's know-how for that kind of work goes into the agent's brief and
into the orchestrator's verification. It does the work directly only when the user asks
it to. So a runbook never says "you have no shell", and it never invents an app action or
argument. It never assumes a background supervisor,
timer, queue or scratch store: those belonged to the retired Daintree Assistant
CLI, and `tests/unit/test_runbook_migration.py` fails any runbook that names one.

## The app contract

`tests/fixtures/daintree_mcp/` holds the tool surfaces the Daintree app actually
lists to each kind of caller, captured from one app build. Its README gives the
commit and how to refresh it. It is the authority on which actions exist and what
they accept. Read an action's `description` there before teaching it; the
maintainers wrote the traps into it.

- `pane-core` / `pane-full`: Claude Code or Codex in a Daintree agent pane. A pane
  is listed the whole pane surface; its tier decides only what runs without asking,
  the rest raises an approval. Owned tools: `terminal.sendCommandOwned`,
  `terminal.closeOwned`, `terminal.injectOwned`.
- `help-core` / `help-full`: Daintree's sidebar assistant, which gets the unscoped
  `terminal.sendCommand` and `terminal.close`, and alone is offered `host.list` and
  `host.call` (direct control of a Remote Host) and `assistant.list`/`assistant.start`.
- `relayed-core` / `relayed-full`: what a Remote Host offers inside `host.call`, at the
  calling assistant's Tool set: the owned terminal tools, agents, worktrees,
  `assistant.*`, and at Full the checks and the command runner (`project.runCheck`,
  `project.runCommand`). Never a `host.*` tool: calls do not chain.
- `external`: an API-key client. It gets a curated subset, and **no `notify`** on
  `agent.launch` or the send, and no `terminal.notifyWhenIdle`.

**Which caller a runbook is checked against** is decided by its id namespace,
never by its tool lists (`CALLER_POLICY` in `tests/daintree_mcp_surfaces.py`).
Every runbook is followed from an agent pane, so its `requiredTools` must be on
`pane-core`, except `daintree.hosts.*`: sidebar-only by design, its whole
baseline must be on ONE of `help-core` or `help-full`. A pane runbook with a host
branch lists `host.list`/`host.call` in `optionalTools`, never `requiredTools`, and
tags that branch's examples `surface=help-core`. A `host.call` example is validated
twice: the call against the help surface, and its `tool` and `args` against the
relayed surface of the same tier, so a Core example cannot run the Full-only command
runner on a host.

**Two catalogs.** v0.44 clients drive their hosts through an inbox (hub, lanes,
tasks), and later builds control them directly, so the server serves each its own
catalog ([MCP.md](MCP.md#host-model-and-host-state)). `runbooks/files/` is the control
catalog, the one you edit. `runbooks/inbox/` holds the v0.44 copies, frozen, of every
hosts runbook and of each shared runbook whose text differs between the models; its
surfaces are frozen beside it in `tests/fixtures/daintree_mcp/inbox/`. Every other
runbook is shared. Before changing a shared runbook in a way a v0.44 client cannot
follow (a tool v0.44 lacks, `host.call`), copy it into `inbox/` first;
`tests/unit/test_catalogs.py` pins that directory and refuses a copy identical to its
shared runbook.

A caller with a host connected gets `daintree.hosts.foundation` with every runbook,
so a pane runbook's host branch is a sentence or two ("on a host, each step through
`host.call` there"), never a second copy of the hosts foundation.

The capture proves what is listed, not what happens when an action is called. Notice
delivery in particular is only proven by a live trial in a Daintree pane.

## Being found: the routing text

Search ranks a runbook by its **title, `summary` and `whenToUse`**, never the body.

- **`whenToUse` is the primary signal.** Open with when it applies, in the words a
  user or agent would use ("rename my terminals", "ask one agent and bring back its
  answer"), and state the deliverable.
- **Fence off the neighbours.** Follow with when *not* to use it, naming the right
  runbook by fully-qualified id: "Do not use for X (use `daintree.other.runbook`)".
  The coverage tooling parses these ids, and a reference that resolves to nothing
  fails the hygiene test. `bench lint` lists the most confusable pairs.
- **Route by deliverable and ownership, not by mechanism.** Waiting for an agent is
  usually a phase inside a runbook, not a task of its own. "Ask one agent and relay
  its answer" belongs to ask-and-relay even though it waits for a notice.
- **`summary`** is one line saying what the runbook achieves.
- Changing routing text re-embeds the runbook. Measure it with `bench local`; a
  body-only edit needs no routing measurement and no retrain.

## Being followable: the body

A selected runbook's body is returned in full with the foundation, and callers that
print the whole result pay for it twice, so every byte counts. **Put shared rules in
`daintree.foundation`**: caller differences, ownership, launching, reading replies
from the notice's quote, long-run checkpoints, proving a finish. Do not repeat them,
and do not repeat what Daintree's own assistant prompt or a tool's description
already says. Put what is specific to this task beside the step it affects: ballot
blinding, PR head binding, setup readiness, question protocols.

**Design for the fewest calls.** One call per round: `agent.launchMany` for one
prompt to several agents, `terminal.sendCommandMany` for a different message per
terminal, `terminal.closeMany` for teardown. When the next step needs the
answers (a vote, a poll, a relay), pass `waitForReply:true, handback:true` and
read each `reply` from the result. Only long background work passes
`notify:true, handback:true` and ends the turn. Never read a terminal for a reply
the call or notice already carried.

The served document already carries `when_to_use` and the tool lists, so a body
has no `Use when:` or `Availability:` line; one short line for a real caller
difference at most. A runbook that ends its turn on a notice follows this shape:

```markdown
On a host: shell steps run there, per the hosts foundation; own shell: `gh -R <repo>`.

Procedure:
1. Resolve and preflight.
2. Dispatch the smallest authorized action (brief, `name`, `worktreeId`, `notify`).
3. End the turn on the notice when nothing else is left.

On notice or resume: what the reply means here; questions, dialogs, errors.

Stop and recovery: <finite bounds; what is never replayed>.

Report back: <evidence, outcome, pending work, the real continuation>.
```

A synchronous runbook (rename, a status read, a `waitForReply` round) is shorter.
It gets no empty notice or checkpoint sections.

1. **State the whole contract.** Target scope, concrete arguments, what the result
   looks like, what success looks like, partial failure and retry, and when to stop.
   Preserve returned ids. A launch is a panel, not a ready agent; a send is queued,
   not delivered; a notice is a stop, not a success; a timeout is not a failure.
2. **Stay inside the requested outcome.** A runbook does not authorize extra writes,
   terminal closure or public messages, and it should not demand a second approval
   for work already authorized. App confirmations and approvals still apply.
3. **Make asynchronous work re-entrant.** Arrange a real continuation (a `notify`
   on the launch or send, or `terminal.notifyWhenIdle`), end the turn, and on the
   next activation recover state without repeating a launch or a public write. A
   short run keeps its state in the conversation; only a long or multi-session run
   writes a checkpoint, and never into the user's repository from the sidebar. The
   no-notice path is `waitForReply`, or an honest "this needs your continuation".
4. **Never replay an ambiguous write.** Read the real state first: the listing, the
   submission receipt, the forge.
5. **Give complete examples** in a tagged fence. Put the action id in the fence
   metadata, only its arguments in the JSON, and angle-bracketed runtime
   substitutions for ids:

   ````markdown
   ```json tool=agent.launch
   {"agentId":"claude","name":"Review: retry","worktreeId":"<worktree id>","prompt":"…","notify":true}
   ```
   ````

   Add `surface=external` (or another capture name) for an example written for that
   caller; `tests/unit/test_runbook_examples.py` validates each example against its
   caller's captured schema, so an external example carrying `notify` fails.
6. **Say what happens on a host.** The Shell's assistant can follow any runbook
   against a Remote Host, where a worktree path, a process or a transcript exists only
   on that machine. Every runbook outside `daintree.hosts.*` carries one top-level
   `On a host:` line before its procedure: shell steps (git, tests, files, processes)
   run on the work's machine, per the hosts foundation (`project.runCheck` or
   `project.runCommand` through `host.call`, else an agent there), which a control
   caller with a host connected gets with every runbook; the forge (`gh`, naming the repository)
   stays in your own shell, since pushed state is the same everywhere; an
   app-tool-only runbook calls its tools on the work's machine. A shared runbook is
   also served to v0.44, which lacks `host.call` and `project.runCommand`, so its line
   points at the hosts foundation instead of naming them. Never write "delegate" for
   these steps: it means handing work to the host's assistant, not the default. Beside
   a step, say only what that line leaves wrong: a path handed to an agent on another
   machine is not shared (put the content in the prompt); a fetch or push that moves a
   checkout runs where that checkout is (reading the remote, `git ls-remote`, is your
   shell's). `tests/unit/test_runbook_host_execution.py` fails a body that acts on a
   machine without that line, or whose line lacks the route its commands need (it
   does not read the line's sense: write it affirmatively).
7. **Stay under the size budgets** (`tests/unit/test_runbook_budgets.py`). Remove
   duplication before adding a rule.

## Frontmatter

| key | required | notes |
|---|---|---|
| `id` | yes | dotted, e.g. `daintree.terminals.rename`; must equal the filename stem |
| `title` | yes | |
| `summary` | yes | one line |
| `whenToUse` | yes | the routing text above |
| `foundation` | no | `true` for a domain's base-judgment runbook |
| `risk` | no | `read local ui terminal project external git system`, descriptive only, not a permission |
| `requiredTools` | no | app action ids the baseline path calls; must be on the caller's surface (`pane-core`; `help-core` or `help-full` for `daintree.hosts.*`) |
| `optionalTools` | no | app action ids a conditional branch, caller variant or enhancement calls |
| `requires` | no | other runbook ids whose instructions must be read first |
| `hostEligibility` | `daintree.hosts.*` only, and required there | the caller host state it fits: `connected`, `configured` or `any` (`lane` only in the frozen inbox catalog) ([MCP.md](MCP.md#host-model-and-host-state)); search leaves it out for a caller whose reported state does not fit |

Unknown keys are rejected (`runbooks/models.py`). A tool may not be both required and
optional. The nearest domain foundation is delivered automatically and needs no
`requires` entry. `requires` targets must exist, must be public, and must not form a
cycle, including through the implicit foundation edge; a violation fails readiness.
Native host tools (a shell, `gh`, file edits) never go in either tool list. Name them
in the step that uses them.

## Adding one

1. Create `runbooks/files/<id>.md`.
2. Add a case under `benchmarks/runbook_selection/cases/` that **requires** it, and a
   routing trap against a declared neighbour.
3. Verify:

```bash
.venv/bin/python -m pytest tests/unit -q
.venv/bin/python -m benchmarks.runbook_selection.bench coverage   # every runbook has a case
.venv/bin/python -m benchmarks.runbook_selection.bench local      # routing quality gate
```

Passing the routing gate says search finds the runbook. Passing the example and
migration tests says it names real actions with valid arguments. Neither says an
agent executes it well, or that a Daintree notice actually wakes the pane. Only a
live trial in a Daintree pane shows that, so don't claim it from offline checks.

`/runbook <issue or description>` (`.claude/skills/runbook`) runs this whole process
end to end, for a new runbook or an improvement: the baseline and measurement, grounding
in the app's action source, cases and blind-verified corpus rows, and a live trial in an
isolated Daintree (its kit is in the skill's `scripts/trial/`). Given an issue it ends in
a pull request into `develop`; given a description it commits on the current branch.

## Learning from real sessions

A transcript of an agent doing the work is the best evidence. It shows the query
the agent actually sent, what came back, and whether it could follow the procedure.
`/learn-from-transcript <path>` (`.claude/skills/learn-from-transcript`) turns one
Claude Code or Codex session into measured catalog changes. Beyond routing, read the
session for:

- whether it called real surfaced actions with correct arguments;
- whether it arranged a real continuation before ending its turn, and whether that
  continuation arrived;
- whether it told questions, errors and completion apart, and avoided duplicate
  actions on resume;
- whether it verified the outcome and stayed in scope.

Cases it adds live in `benchmarks/runbook_selection/cases/19-from-transcripts.yaml`
with `tx-` ids.
