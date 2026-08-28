# Writing a runbook confirmation

Every runbook a user asks for by name carries a `confirmation` in its
frontmatter: the two sentences shown the instant they hit Enter, before the model
has produced a byte.

```
Diagnosing a failing check. The aim is to get from the symptom to the actual
error output before proposing anything, since a check name says what failed and
not why.
```

This document is the authoring reference and the record of why the form is what
it is. **Read it before writing one.** Three earlier revisions of this catalog
got it wrong in three different directions, and the reasons are below.

---

## What it is called

The pattern has a name, and it is not "announcement".

- In **spoken-dialogue-systems and VUI design** it is an **implicit confirmation**:
  the system reflects the intent it parsed and proceeds, rather than halting to
  ask "did you mean X?" (which is an *explicit* confirmation).
  Google's Conversation Design, the Alexa Design Guide and Cathy Pearl's
  *Designing Voice User Interfaces* all use the term.
- In **Clark & Schaefer's grounding theory** (1989; Clark, *Using Language*,
  1996) it is a **demonstration** — evidence of understanding at **Level 3
  (meaning)** on the four-level ladder of joint action.

Worth being precise about, because "acknowledgement" is a real term in that same
taxonomy and it means something *weaker*: a continuer like "OK" or "uh-huh",
evidence at Levels 1–2 that the signal was received. A spinner is an
acknowledgement. This is a demonstration — it claims to have understood the
*meaning*, not merely to have heard.

That is the bar the text has to clear. It also explains the failure mode: a
demonstration that demonstrates the wrong understanding is worse than an
acknowledgement that claimed nothing.

## What it is, mechanically

When the local classifier loads a runbook for the first time in a conversation
and scores it at or above `config.RUNBOOK_CONFIRMATION_THRESHOLD` (0.95), the
server streams that runbook's `confirmation` as a `preamble` SSE event,
immediately after `meta` and before the executor has opened. Shown at most **once
per runbook per conversation** — the set rides the signed state token.

It replaces a stage that paid a second model on a second host to write this text
per turn (`services/fast_response.py`, now off).

**It is typed out, word by word.** A beat first
(`config.CONFIRMATION_LEAD_IN_SECONDS`, 0.2s) so the text reads as arriving
rather than as having been there, then the words over
`config.CONFIRMATION_PACING_SECONDS` (1.0s). Together that fills the slot
reasoning and an opening sentence used to, instead of landing as a block and
leaving the screen still.

The budget is measured, not chosen: over 24 clean trials the executor's first
output arrived at p50 1.26s / p90 1.52s, with the fastest at 0.98s. The 1.2s
total therefore sits on the median and runs past the fastest turns — deliberately,
because overrunning is free: the moment the executor has output the rest is
flushed in one event. `python -m benchmarks.executor_window.bench` re-derives the
window.

Two consequences for an author. Word count is now a DURATION as well as a reading
cost — the budget is fixed, so a 24-word confirmation types faster per word than a
35-word one, and both finish at the same moment. And the executor is the real
bound: if it produces output mid-word the rest is flushed at once, so a
confirmation is never the reason an answer is late.

## What the reader needs in that second

They have just handed over control. Their questions, in order: *did that
register? is it moving? and did it read my request as the kind of thing I meant?*

A fixed string cannot prove the system understood *this particular request*. It
can prove the request was **routed, and into which kind of work** — and that is
worth something specific: it is **actionable**. A confirmation naming the wrong
kind of work is visibly wrong, and the reader can interrupt immediately rather
than waiting out the wrong answer. Lee & See (2004) call this calibrated trust:
stating intent up front helps when the system is right, *and* when it is wrong.

The 0.95 floor is not certainty — roughly one in nine confirmations names a
runbook the benchmark did not require. The text is written to survive that.

## The form

Two sentences. **A lead, then a scope.**

```
<Gerund lead: the work>. <One sentence: what it is aiming at, and what bounds it>.
```

1. **Lead sentence — 3 to 8 words, gerund phrase.** `Diagnosing a failing
   check.` `Reviewing the change.` `Tearing down parts of the workspace.` This is
   the primary scan target; UX-writing practice puts a lead line at 2–4 words and
   readers process one in under 500ms. It names the kind of work and nothing else.
2. **Scope sentence — 12 to 28 words.** What the work is *aiming at*, plus the
   one boundary that makes it non-trivial. This is where the goal goes.

Total: **two sentences, at most 38 words and 240 characters**, on one line in the
file.

### Impersonal, not first person

Never `I'll`, `I'm`, `we'll`, `let's`. Use the gerund.

Two independent reasons, and both are load-bearing:

- **The executor already announces.** `main/base/40-communication.md` forbids an
  opening orienting clause and names `I'll start by` among the banned openings —
  and the executor opens with one anyway, measured. Three further attempts to
  prompt it out were built, measured and reverted for changing nothing. The CLI
  joins the confirmation and the answer into one bubble, so a first-person
  confirmation sits directly above the same sentence.
- **First person raises the cost of being wrong.** Research on anthropomorphic
  cues in developer tooling finds that first-person self-reference raises the
  expectation of human-grade understanding, so a failure after "I" reads as
  incompetence rather than as a boundary. Impersonal progressive phrasing aligns
  expectations with what actually happened: a classifier routed a request.

### State the goal, never the outcome

The product owner's requirement — *"clearly state what the goals are"* — is
right, and the research draws the line precisely:

- ✓ **Intent / scope framing**: `The aim is to get from the symptom to the actual
  error output before proposing anything.`
- ✗ **Asserted outcome**: `This will fix the failing test.`

Outcome framing is only safe when the system is deterministic. Under Oliver's
expectation-disconfirmation theory the penalty for a stated goal the result then
misses is *worse* than having stated no goal at all. Say what the work is
directed at; never what it will have achieved.

Nothing in the confirmation may be contradicted by whatever the executor decides
to do. It never sees this text, knows far more than the author did, and will take
whatever approach it judges best.

### Negative scope, sparingly

`not style preferences` is a useful boundary for a review. `never the rendered
page` is not — it answers a question nobody asked and plants doubt.

Negations cost more to process than affirmatives, and a non-goal only helps when
it resolves a confusion the reader plausibly has: a genuinely confusable
neighbour, or a destructive operation where the blast radius matters. Otherwise
state the boundary **affirmatively** — a well-chosen scope defines its own edges.

**This is the rule that most often makes a confirmation false.** A negation is a
claim, and a claim about what the work is *not* is exactly the kind that a
legitimate variant of the request will contradict.

## Worked examples

| Runbook | Confirmation |
|---|---|
| `ci.diagnose-and-fix` | Diagnosing a failing check. The aim is to get from the symptom to the actual error output before proposing anything, since a check name says what failed and not why. |
| `review.review-change` | Reviewing the change. The goal is defects and regressions that would matter after it lands, judged against what the change is trying to do rather than style preference. |
| `explore.repository` | Working out how something here behaves. The aim is an explanation grounded in the code itself, rather than a plausible account of how it probably works. |
| `orchestration.workspace-cleanup` | Tearing down parts of the workspace. The aim is to agree exactly what is in scope before closing anything, because a quiet terminal is not the same as a finished one. |

And the three revisions this replaced, each wrong in its own way:

- ✗ `I'll take a look at the failing check and work out what broke.` — announces,
  duplicating the executor's own opening line, and says nothing else.
- ✗ `A red check names a failure without containing one: the exact command and
  its output live somewhere else, and a fix written from the name alone is a
  guess that can bury the cause…` — a lecture. 62 words: about fifteen seconds of
  reading in a window that is often one second, so the answer arrives
  mid-paragraph.
- ✗ `Failure diagnosis — the exact error matters more than the visible symptom` —
  scannable, but too compressed to state a goal, and it reads as cryptic.

## Hard rules

1. **Exactly two sentences.** At most 38 words and `CONFIRMATION_MAX_CHARS` (240)
   characters, on ONE line in the file.
2. **Lead sentence 3–8 words, gerund.** `Diagnosing…`, `Reviewing…`,
   `Working out…`. It names the kind of work and stops.
3. **No first person anywhere** — not `I`, `I'll`, `I'm`, `we'll`, `let's`.
   Checked mechanically, and `validate_preamble` rejects an announcing opening on
   top of that.
4. **State the goal, not the outcome.** "The aim is to…", "The goal is…" is the
   house phrasing for the second sentence. Never assert what will have been
   achieved.
5. **No steps or sequences.** If the second sentence would make sense as a
   numbered list, it is a plan — rewrite it as a goal.
6. **Negative scope only where it resolves a real confusion.** Prefer an
   affirmative boundary.
7. **Present tense.** Nothing has been opened, checked, found, fixed or verified.
8. **Invent nothing.** No file names, paths, commands, identifiers, counts or
   digits.
9. **No internal machinery.** Never *runbook*, *skill*, *procedure*,
   *classifier*, *selector*, *catalog*, *prompt*, or any model or provider name.
   The product's own user-facing nouns — agents, terminals, worktrees, watchers,
   branches, issues, pull requests — ARE fine; they are what the user said.
10. **Plain prose.** No heading, list, code fence, markup, link or trailing
    question. No exclamation marks, no emoji.
11. **Distinct from every other confirmation**, and distinct in its LEAD — two
    runbooks whose first sentences match defeat the purpose.

## Which runbooks get one

Every runbook the classifier can score.
`test_every_user_facing_runbook_has_a_confirmation` fails a runbook that ships
without one: such a runbook is silently exempt from the whole mechanism, and the
only symptom is the product going quiet for that one kind of request.

Two structural exemptions, and they are exemptions rather than choices:

- **A domain foundation** (`foundation: true`) is paired in by admission without
  ever being ranked, so it carries no score and can never be shown.
- **The server-pinned documentation runbook** (`config.DOCS_RUNBOOK_ID`) is
  hidden from the selector entirely, for the same reason.

`tests/unit/test_runbook_confirmations.py` asserts the converse too — neither may
carry a confirmation it could never use.

## Mechanics

A double-quoted YAML scalar on one line, in the frontmatter after `whenToUse:`:

```yaml
confirmation: "Reviewing the change. The goal is defects and regressions that would matter after it lands, judged against what the change is trying to do rather than style preference."
```

Apostrophes are safe inside double quotes; do not use a double quote inside the
text. Em dashes are fine.

Editing a confirmation changes the runbook's content hash and therefore
`catalog_revision`, which is correct: a client holding state from before the edit
should re-select.

## How to check your work

```bash
pytest tests/unit/test_prompt_budgets.py -k confirmation   # house style
pytest tests/unit/test_runbook_confirmations.py            # the gate itself
```

Then read them all together, which is the only way to see whether they hold a
consistent shape:

```bash
python -c "
from daintree_assistant_server.runbooks.catalog import load_catalog
for r in sorted(load_catalog().runbooks, key=lambda x: x.id):
    if r.confirmation: print(f'{r.id}\n  {r.confirmation}\n')
"
```

## Sources

- Clark, H. H. & Schaefer, E. F. (1989), *Contributing to Discourse*; Clark, H. H.
  (1996), *Using Language* — grounding, the ladder of joint action, and the
  acknowledgement / demonstration / display distinction.
- Google Conversation Design; Amazon Alexa Design Guide; Pearl, C., *Designing
  Voice User Interfaces* — implicit vs. explicit confirmation.
- Nielsen, J. (1993), *Usability Engineering* — the 0.1s / 1s / 10s response-time
  limits; NN/g on visibility of system status and progressive disclosure.
- Oliver, R. L. (1977, 1980) — expectation-disconfirmation theory, and the
  asymmetric penalty for a stated goal the result misses.
- Lee, J. D. & See, K. A. (2004), *Trust in Automation* — calibrated trust.
- Podmajersky, T., *Strategic Writing for UX* — two-part status message
  construction, lead line plus boundary clause.
