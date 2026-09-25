# Reading a transcript

A transcript records what an agent did and what it was told. It does not say
why, what state things were really in, or whether the final reply was true. The
job is to rebuild those from the record and the code without promoting a
plausible story into a finding.

## Who was driving, and what it was told

| Record | Driver | Its instructions |
|---|---|---|
| Claude Code JSONL (`~/.claude/projects/<cwd-slug>/<id>.jsonl`) | Claude Code in that directory | the directory's `CLAUDE.md` and the user's global one. **Not in the JSONL**: Claude Code loads them from disk, so read them at the session's `gitBranch` if they matter |
| Codex rollout (`~/.codex/sessions/…/rollout-*.jsonl`) | Codex in `session_meta.cwd` | the developer message and the first user item, `# AGENTS.md instructions`. `--include-injected` prints both |
| either, with cwd under `…/Daintree/help-sessions/<id>` | the Daintree help assistant | Daintree's bundled help prompts (`../daintree/scripts/help-src/`). The help-session directory is overwritten whenever the templates change, so its copy is today's prompt, not necessarily the session's |

A session that went wrong because its instructions said so is a different lesson
from one that ignored them, and neither is this repository's to fix. It gets
recorded for whoever owns those instructions.

**Was the search even available?** If `search_runbooks` never appears in the tool
use, check whether it was offered at all: Claude Code lists deferred MCP tools in
system reminders and loads them with `ToolSearch`, and Codex prints its tool set in
the injected instructions. A session without the server connected cannot be a
"should have searched" lesson. The tasks are still routing questions.

## Tool names

Runbooks name tools in dotted form (`terminal.rename`). Transcripts show them as
the client renders them:

| Seen in the transcript | In a runbook |
|---|---|
| `mcp__daintree__terminal_sendCommand` (Claude Code `tool_use`) | `terminal.sendCommand` |
| `tools.mcp__daintree__terminal_sendCommand({...})` inside a Codex `exec` cell | `terminal.sendCommand` |
| `mcp__<server>__search_runbooks` (the server name is whatever the user configured) | the search itself |

The digest's `runbook_tool()` does this mapping for its follow-through line.

Some runbook tools belonged to the retired Daintree CLI and have no Daintree MCP
counterpart, among them `context.snapshot`, the `terminal.extract*` family and
`agentTask.*`. A standard assistant never holds them. Check whether a name is a
real Daintree action before calling it missing or present:

```bash
git -C ../daintree fetch -q origin
git -C ../daintree grep -n 'id: "terminal.rename"' origin/develop -- src/services/actions/definitions
```

## Reading Claude Code records

- `assistant` messages hold `text` and `tool_use` blocks. The matching
  `tool_result` arrives in the next `user` message, keyed by `tool_use_id`.
- `<system-reminder>` and command wrappers inside user content are injected, not
  typed by the user. The digest collapses them.
- `type: system` records are the harness: `scheduled_task_fire` (a `/loop` tick),
  `stop_hook_summary`, `away_summary`. In a long supervision session they, not the
  user, start most turns. The digest renders them as `_[harness] …_`.
- Usage is per API message; the digest sums it once per message id.

## Reading Codex records

- A `developer` message is injected instructions, and a `user` message starting
  `# AGENTS.md instructions` is the project doc.
- Codex often runs tools inside a JavaScript `exec` cell. One cell can hold several
  MCP calls, and its output is whatever the cell printed. The digest pulls a search
  query out of the JS and the result documents out of the printout, and pairs them
  only when that is unambiguous. When it pairs by print order, or cannot pair at
  all, it says so in a note: read the cell in `raw.jsonl`.
- Tool results nest JSON inside JSON strings. The digest unescapes three layers.
  When a quote matters, check it against `raw.jsonl`.
- `event_msg` / `token_count` carries cumulative usage, and the digest reports the
  last. Timestamps are UTC throughout.

## Reading a search

For every `search_runbooks` call, work out:

- **The query as written.** Agents paraphrase. Is it a task description or a bag
  of keywords? Did it carry the user's intent, or only the first sentence of it?
  Compare it with the user's words, and route both.
- **What came back.** Selected against ranked: the tool tells callers to prefer
  `selected` and to treat an empty selected set as "no runbook fits". The digest
  marks both.
- **What the agent did next.** It followed a selected runbook; it took a hit that
  was not selected; it ignored the result; or it **searched again with new
  wording**. A re-search is the strongest lead there is: the first phrasing missed
  in the agent's own judgement, and both queries are cases.
- **Whether it could follow it.** The digest's follow-through line compares a
  selected runbook's `required_tools` with the tools called afterwards. "Never
  called" means either skipped or not held. The transcript says which.

## The divergence catalogue

Check the transcript for every one of these, not only the one the user noticed.

**Searched, got nothing useful, improvised.** A gap, or a miss if a runbook
exists. What the agent improvised is the draft procedure only if it worked.

**Followed the wrong runbook with confidence.** A mis-route. Read both runbooks'
`whenToUse` and say why the wrong one won.

**Followed the right runbook into a failure.** A tool rejected the arguments the
runbook taught, a step named a tool the agent did not have, a stated limit no
longer holds, a step assumed state that was not there. A body lesson: verify it
against the action definition.

**A discovery hunt.** Tool-list dumps, schema reads and trial calls before the
first real action. A runbook that names the right tools and arguments would have
skipped it. Did one exist, and was it found?

**Acting for the user without saying so.** A short send (`y`, `1`, Enter) answering
a trust, permission or destructive-action prompt, never mentioned in the reply.
Severity is safety. Does the runbook governing that step (or `daintree.foundation`)
say to ask? If it does, the lesson is the caller's; if not, it is ours.

**Polling without a bound, a recovery that did not recover, a claim the evidence
does not support.** Repeated identical waits; an interrupt reported as done while
the next screen shows the turn still running; "closed all five" with no close
result. Recompute every number the final reply states. These are procedure
lessons when a runbook governs the step, and recorded for their owner when none
does.

**What went right.** Parallel launches with ids kept, one batched wait, closing
exactly what it opened. Pin it with a case, so a later edit cannot regress it
unnoticed.

## Timing and honest wording

Put timestamps next to state claims before judging them. A status read five
seconds after a launch that says `working` is not a detection failure; three
minutes of `working` over a frozen screen is.

Write every row as what the record shows plus what the code confirms. Where the
record cannot settle a question, such as whether a keystroke landed before a
dialog cleared or why a third-party CLI hung, say so in the row. An unresolved
mechanism is still a lesson if the agent's behaviour was wrong either way.
