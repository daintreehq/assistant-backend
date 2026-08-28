# Writing a runbook opener

Every runbook a user asks for by name carries an `opener` in its frontmatter: the
one-line caption shown the instant they hit Enter, before the model has produced
a byte.

**It is a receipt, not a sentence.** Six to twelve words:

```
Failure diagnosis — the exact error matters more than the visible symptom
```

This document is the authoring reference. It exists because the right register is
counter-intuitive, and two earlier revisions of this catalog got it wrong in two
opposite directions before landing here.

---

## What it is, mechanically

When the local classifier loads a runbook for the first time in a conversation
and scores it at or above `config.RUNBOOK_OPENER_THRESHOLD` (0.90), the server
streams that runbook's `opener` as a `preamble` SSE event, immediately after
`meta` and before the executor has opened. Shown at most **once per runbook per
conversation** — the announced set rides the signed state token.

It replaces a stage that paid a second model on a second host to write this text
per turn (`services/fast_response.py`, now off).

## What the user actually needs in that second

They have just handed over control. Their questions, in order, are: *did that
register? is it moving? and did it read my request as the kind of thing I meant?*

They are not in "read an essay" mode. They will not learn anything about their own
domain in the second before an answer arrives.

So the opener makes one small, honest promise — the only one a fixed string can
keep. It cannot prove the system understood *this particular request*; it can
prove the request was **routed, and into which mode**. That is genuinely useful,
and it is *actionable*: a caption that names the wrong mode is legibly wrong, and
the user can interrupt immediately instead of waiting out thirty seconds of the
wrong work. The 0.90 threshold is not certainty (see `RUNBOOK_OPENER_THRESHOLD`),
which is exactly why the caption should be short enough to check at a glance.

## The shape

```
<task mode> — <one stable boundary, distinction, or invariant>
```

- **Noun-led.** `Failure diagnosis`, `Fleet triage`, `Change review`. Never a
  first-person verb.
- **Task mode in the first two or three words**, so a partial glance still
  succeeds.
- **The predicate names a boundary, not a step.** What this mode is *for*, what
  it deliberately is *not*, or the one distinction that decides the work.
- **Sentence case, no trailing period.** It is a label, not a sentence.
- **Six to twelve words. Eighty characters hard.**

### Consistency is a feature — do not fight it

Thirty-seven differently-shaped "insights" read as canned text pretending to be
bespoke. Thirty-seven identically-shaped captions read as deliberate product UI.
A heavy user starting ten conversations a day *recognises* a caption rather than
re-reading it, and that recognition is the whole point. Write to the template.

## Two rules that are not style preferences

**It may not announce, because the executor announces anyway.** The base prompt already
forbids it — `main/base/40-communication.md` says an opening round gets no orienting
clause and names `I'll start by` among the banned openings — and the executor opens with
one regardless, measured. That is the situation to write for: the rule is in the prompt,
it is not obeyed, and three FURTHER attempts to prompt it out (a conditional base partial,
a per-turn tail reminder, an edit at the rule's own source) were built, measured and
reverted for changing nothing. So an opener that also announces is a visible duplicate of
the line rendering directly beneath it — the CLI joins the two into one bubble — and a
reply meant to feel fast reads as padded. Nominalising the activity is what says *what is
happening* without competing for that register.

**It may not be prose, because there is no time to read prose.** An earlier
revision authored these as two-or-three-sentence comprehension paragraphs, in the
register the retired stage's own prompt taught. Measured over the catalog: 62.6
words and 350 characters on average — roughly fifteen seconds of reading dropped
into a one-second window. The answer then arrives *mid-paragraph*, and the reader
is interrupted by the thing they were waiting for.

## Worked examples

| Runbook | Opener |
|---|---|
| `ci.diagnose-and-fix` | `Failure diagnosis — the exact error matters more than the visible symptom` |
| `orchestration.supervise-fleet` | `Fleet triage — working, waiting, finished, failed, or unwatched` |
| `workflow.find-issue-work` | `Forge search — useful candidates, not just a page of matches` |
| `explore.repository` | `Repository orientation — explanations tied to code, not guesses` |
| `review.review-change` | `Change review — defects and regressions, separate from style preferences` |
| `worktree.create-plain-worktree` | `Plain worktree — branch only, with no recipe or agent attached` |
| `terminals.rename` | `Terminal titles — the work in each tab, not the tool` |
| `orchestration.workspace-cleanup` | `Workspace teardown — quiet is not the same as safe to remove` |

Two failure modes, both real, drawn from the revisions this replaced:

- ✗ `I'll take a look at the failing check and work out what broke.` — announces,
  duplicating the model's own opening line, and carries no information.
- ✗ `A red check names a failure without containing one: the exact command and its
  output live somewhere else, and a fix written from the name alone is a guess
  that can bury the cause while making the symptom go away…` — a lecture. True,
  well written, and unreadable in the window it occupies.

## Hard rules

1. **Six to twelve words**, at most `OPENER_MAX_CHARS` (80) characters, on ONE
   line.
2. **Noun-led.** No first person anywhere — not `I`, `I'll`, `I'm`, `I've`,
   `we'll`, `we're`, `let's`. Checked mechanically, and the shared
   `validate_preamble` rejects an announcing opening on top of that.
3. **No steps, sequences, or shape-of-the-result.** If it would make sense as a
   numbered list, it is a plan — start again.
4. **Nothing in it may be contradicted.** The executor never sees the opener,
   knows far more than you did, and will take whatever approach it judges best. A
   boundary survives any approach; a commitment to one does not.
5. **Present tense.** Never `has been`, `were updated`, `was changed`. Nothing has
   been opened, checked, found, fixed or verified.
6. **Invent nothing.** No file names, paths, commands, identifiers, counts or
   digits.
7. **No internal machinery.** Never the words *runbook*, *skill*, *procedure*,
   *classifier*, *selector*, *catalog*, *prompt*, or any model or provider name.
   The product's own user-facing nouns — agents, terminals, worktrees, watchers,
   branches, issues, pull requests — ARE fine, because they are what the user
   said.
8. **Plain text.** No heading, list, code fence, markup, link, or trailing
   question. No exclamation marks, no emoji. An em dash as the separator is the
   house style.
9. **Distinct from every other opener**, and distinct in its TASK MODE — two
   runbooks whose captions both open `Agent work —` defeat the purpose.

## Which runbooks get one

Every runbook the classifier can score. `test_every_user_facing_runbook_has_an_opener`
fails a runbook that ships without one: a runbook with no opener is silently
exempt from the whole mechanism, and the only symptom is the product going quiet
for that one kind of request.

Two structural exemptions, and they are exemptions rather than choices:

- **A domain foundation** (`foundation: true`) is paired in by admission without
  ever being ranked, so it carries no score and can never be shown.
- **The server-pinned documentation runbook** (`config.DOCS_RUNBOOK_ID`) is hidden
  from the selector entirely, for the same reason.

`tests/unit/test_runbook_openers.py` asserts the converse too — neither may carry
an opener it could never use.

## Mechanics

A double-quoted YAML scalar on one line, in the frontmatter after `whenToUse:`:

```yaml
opener: "Failure diagnosis — the exact error matters more than the visible symptom"
```

Apostrophes are safe inside double quotes; do not use a double quote inside the
text. Em dashes are fine.

Editing an opener changes the runbook's content hash and therefore
`catalog_revision`, which is correct: a client holding state from before the edit
should re-select.

## How to check your work

```bash
pytest tests/unit/test_prompt_budgets.py -k opener   # house style
pytest tests/unit/test_runbook_openers.py            # the gate itself
```

Then read them all together in one column, which is the only way to see whether
they hold a consistent shape:

```bash
python -c "
from daintree_assistant_server.runbooks.catalog import load_catalog
for r in sorted(load_catalog().runbooks, key=lambda x: x.id):
    if r.opener: print(f'{r.opener}')
"
```
