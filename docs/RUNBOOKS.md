# Writing runbooks

A runbook is a step-by-step procedure an agent can follow to do one kind of work in
Daintree. Agents find them through the [runbook-search MCP server](MCP.md): they
describe a task, get the best-matching runbooks back, and follow the one that fits.
A runbook is only as useful as two things — whether search **finds** it for the
right request, and whether an agent that reads it can **carry it out** correctly.

The catalog lives in [`src/daintree_assistant_server/runbooks/files`](../src/daintree_assistant_server/runbooks/files),
one Markdown file per runbook. Growing it is the main work in this repository.

## Being found: the routing text

Search ranks a runbook by its **title, `summary` and `whenToUse`** — never the body.

- **`whenToUse` is the primary signal.** Open with when it applies, in the words a
  user or agent would actually use ("rename my terminals", "figure out what each
  agent is doing"), not internal jargon.
- **Fence off the neighbours.** Follow with when *not* to use it, naming the right
  runbook by fully-qualified id: "Do not use for X (use `daintree.other.runbook`)".
  Never a bare leaf or prose — the coverage tooling parses these ids, and a
  reference that resolves to nothing fails the hygiene test. `bench lint` lists the
  most confusable pairs, which are the fences worth writing first.
- **`summary`** is one line saying what the runbook achieves.
- Changing routing text re-embeds the runbook. Measure it with `bench local`.

A new runbook is searchable as soon as it is added. It competes best against
established neighbours after a selector retrain (`.claude/skills/retrain-selector`),
so a runbook that shares vocabulary with an existing one needs training data too.

## Being followable: the body

The body is returned to the agent in full, so every byte is paid in its context.
House style: a when-to-use line, a numbered procedure, and a "Report back" line.

1. **State the whole contract.** Target scope, concrete arguments, what the result
   looks like, what success looks like, how partial failure and retry work, and when
   to stop. Preserve returned ids. A successful tool envelope can contain a failed
   check; a missing result section is not an empty workspace; a wait is not
   verification.
2. **Stay inside the requested outcome.** A runbook does not authorize extra writes,
   terminal closure or public messages — and it should not demand a second approval
   for work already authorized.
3. **Separate tool rounds from turns.** A dependent call waits for its producer's
   result. End the turn after establishing async supervision, not merely because a
   spawn returned an id.
4. **Recover state before replaying a write.** Session scratch, workflow rows and
   step checkpoints are different records; read the real state first.
5. **Keep shared judgment in the foundation.** A domain's foundation runbook
   (`foundation: true`, e.g. `daintree.foundation`) carries rules every specific in
   that namespace relies on. Put failure handling next to the step it affects.
6. **Give complete tool examples** in a tagged fence — the tool name in the fence
   metadata, only its arguments in the JSON:

   ````markdown
   ```json tool=forge.getPR
   {"worktreeId":"<resolved worktree id>","prNumber":42}
   ```
   ````

   `tests/unit/test_runbook_examples.py` validates these against the captured CLI
   schemas.
7. **Stay under the size budgets** (`tests/unit/test_runbook_budgets.py`). Remove
   duplication before adding a rule.

Runbooks call Daintree CLI tools. The pinned CLI source in `cli/` is the authority on
which tools exist and what they accept; `tests/fixtures/cli_tool_inventory.json` is
its captured schema (refresh recipe in [`tests/fixtures/README.md`](../tests/fixtures/README.md)).
Read the tool's handler before teaching an operation, and for a forwarded Daintree
action read the action itself, not a similarly named wrapper.

## Frontmatter

| key | required | notes |
|---|---|---|
| `id` | yes | dotted, e.g. `daintree.terminals.rename`; must equal the filename stem |
| `title` | yes | |
| `summary` | yes | one line |
| `whenToUse` | yes | the routing text above |
| `foundation` | no | `true` for a domain's base-judgment runbook |
| `risk` | no | `read local ui terminal project external git system` — descriptive only |
| `requiredTools` | no | the CLI tools the steps use, dotted |

Unknown keys are rejected (`runbooks/models.py`).

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

Passing the routing gate says search finds the runbook; it does not say an agent
executes it well. Only a live trial against a connected Daintree instance shows
that — don't claim it from offline checks.

## Learning from real sessions

A transcript of an agent doing the work is the best evidence of both halves: the
query it actually sent, what came back, and whether it could follow the procedure.
`/learn-from-transcript <path>` (`.claude/skills/learn-from-transcript`) turns one
Claude Code or Codex session into measured catalog changes. Cases it adds live in
`benchmarks/runbook_selection/cases/19-from-transcripts.yaml` with `tx-` ids.
