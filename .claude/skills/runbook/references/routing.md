# Routing: how search finds a runbook, and how to move it

Search is free, local and deterministic, which makes it the cheapest thing in this
pipeline to measure and the easiest thing to fool yourself about. This file is the
measured behaviour, not the intuition. `training/EXPERIMENTS.md` has the full record;
read the relevant section there before trying any idea that sounds new.

## What the selector reads

Two stages (`runbooks/local/selector.py`, `setwise.py`):

1. **Shortlist.** A fine-tuned bi-encoder embeds the query and takes the `k` (32)
   nearest runbooks by cosine.
2. **Set-wise head.** Reads the query and those `k` runbook embeddings together and
   returns a probability per candidate, a cardinality decision and admission floors.

- **Only `title`, `summary` and `whenToUse` are embedded.** A body-only edit moves no
  vector, changes no routing, and owes no routing measurement or corpus rows.
- **Neither stage has per-runbook weights.** A new file in `runbooks/files/` is
  embedded at load and is searchable at once. *Reachable is not competitive*: three
  runbooks held out of training scored 4/14 on their cases against 13/14 trained. A
  new runbook wins requests only it plausibly answers and loses contested ones to
  trained neighbours. Data plus a retrain makes it competitive.
- **The served query is a bare query** with nothing loaded, capped at 256 encoder
  tokens (`documents.fit_query` shrinks it deliberately). `bench search` reproduces
  exactly that. `bench local` scores the conversational case suite, which also
  exercises continuations, topic shifts and wakes.
- **Agents write the queries.** The caller describes its task to `search_runbooks`,
  usually as an outcome ("launch a read-only agent to ask a question and relay its
  answer"), sometimes as the user's words, sometimes badly (a pilot Haiku searched
  "runbook trial setup configuration" because the pane title said so). Measure both
  shapes.

## The tools

In a `/runbook` run, every command here runs as `. "<ENV>" && <command>`
(SKILL.md, Workspace and environment), which sets `PY`, `W` and the directory.

```bash
"$PY" -m benchmarks.runbook_selection.bench search --max-results 8 "<q1>" "<q2>"   # served path
"$PY" -m benchmarks.runbook_selection.bench search --max-results 8 --file "$W/sweep.txt"
"$PY" -m benchmarks.runbook_selection.bench lint --top 40        # most confusable pairs
"$PY" -m benchmarks.runbook_selection.bench coverage             # recall + trap per runbook
"$PY" -m benchmarks.runbook_selection.bench local --out "$W/after.json"   # THE gate
"$PY" -m benchmarks.runbook_selection.bench local --filter <case-id>      # diagnosis only
"$PY" -m benchmarks.runbook_selection.bench compare "$W/baseline.json" "$W/after.json"
"$PY" -m training.try_runbook_text --runbook <existing id> --when-to-use-file "$W/proposed.txt"
```

- `bench search` prints, per query, the ranking with scores and which are `selected`,
  plus the `catalog_revision`. Read the **margin**, not just the winner: 1.00 against
  0.98 is a coin flip the next phrasing will flip.
- `try_runbook_text` scores a proposed `whenToUse` (or `--summary-file`) for an
  **existing** runbook against the whole case suite in memory, writing nothing, and
  lists every case that changed in both directions. It refuses an id that is not
  selectable, so a brand-new runbook is measured by writing the file and re-running
  `bench search` and `bench local`.
- A filtered `bench local` run never qualifies the catalog (the quality gate fails a
  filtered run outright). It is for attribution:

| stage | meaning | fix |
|---|---|---|
| `candidate_miss` | never reached the head | routing text (vocabulary). Gated at zero. |
| `rank_miss` | reached the head, scored too low | text first, then corpus rows + retrain |
| `floor_reject` | out-scored the cut, refused by a floor | calibration; usually not yours |
| `cardinality_short` | the count head asked for fewer | data; usually a multi-runbook case |
| `cap_full` | the answer was already at the cap | not a text problem |

## The rules (each one cost a wrong turn to learn)

**1. The bi-encoder has no notion of negation.** Quoting an utterance in a runbook's
`whenToUse` makes *that* runbook match it harder, whatever the surrounding words say.
A fence that quoted the neighbour's traffic ("do not use me for 'in ten minutes spawn
a terminal and run the tests'") dropped the intended winner from 0.52 to 0.15.
Rewritten abstractly ("spawn here only when the work begins this turn; when it is
scheduled for a stated later time, use `daintree.orchestration.watch-outcome`") it
recovered. So: **example wordings go on the runbook that should WIN them; fences are
abstract and name the neighbour by fully-qualified id, never quoting its traffic.**

**2. Routing is punctuation-brittle.** A trailing full stop flipped a request from
watch-outcome 1.00 to spawn-visible-agent 0.99, and adding the clearest possible
deferral cue made it worse. One probe tells you about one string. **Sweep at least six
genuinely different phrasings** and treat a result that flips on punctuation as a data
gap, not a prose problem.

**3. Text has a ceiling.** `whenToUse` shapes the embedding; it cannot overcome what
the head learned about an established neighbour. Three revisions moved one contested
utterance from 0.01 to 0.53 and never beat the neighbour's 0.99. **Two honest text
attempts at one boundary, then it is a data problem**: write the corpus rows, record
the retrain as owed, and stop editing prose at it.

**4. Every text edit moves other cases.** Two edited documents change the shortlist
for every query. Always run the FULL `bench local` and `bench compare` against the
baseline taken before the first edit, and explain every moved case: an improvement,
a neutral reorder, or a regression you name and justify. Your own new cases passing
says nothing about what moved elsewhere.

**5. Choose a trap utterance by measurement, never by taste.** A routing trap
requires your runbook and excludes a neighbour, and it fails *strict* when the
neighbour clears its floor and rides along as an extra. Probe candidate trap
utterances with `bench search` and keep the one with the widest margin, not the one
that reads best.

**6. A social lead-in erases the task.** "How do I create worktrees?" ranked its
runbook 1.000 cold and 0.005 behind a short greeting, with cardinality collapsing to
zero. Keep case queries task-first unless the lead-in is the point of the case.

**7. Route by deliverable and ownership, not mechanism.** Waiting for an agent is a
phase inside most runbooks, not a task of its own. "Ask one agent and relay its
answer" belongs to ask-and-relay even though it waits for a notice. Write the
positive clause around what the user gets.

**8. Fences only work in the direction they are written.** A new runbook's
`whenToUse` fences against its neighbours, but their text predates it and names it
nowhere. Add a short carve-out to each established neighbour that names the new id
(measured with `try_runbook_text`), and corpus rows where the neighbour wins and the
new runbook is `confusable_with`.

## Hygiene the tests enforce on routing text

`tests/unit/test_runbook_catalog_hygiene.py`:

- every dotted `daintree.*` reference in a `whenToUse` resolves to a runbook or a
  namespace;
- a carve-out ("…, use X") should defer to a fully-qualified id, because
  `bench coverage` sees only ids. The test catches shorthand that names a known
  runbook leaf ("use the competition runbook"); other prose deferrals slip past it,
  so write ids every time;
- no two runbooks share an identical routing document;
- no runbook's positive clause (before the first carve-out), taken as its set of
  words of five letters or more, is a strict subset of another's. It is a
  copy-and-extend heuristic, not semantic containment: write the positive clause
  fresh rather than extending a neighbour's.

`tests/unit/test_runbook_budgets.py`: `whenToUse` ≤ 2048 bytes, `summary` ≤ 512 bytes.

## The YAML trap

Frontmatter is `extra="forbid"`, and a plain YAML scalar cannot contain `": "`.
Writing `...including one that spawns an agent: a delayed spawn...` fails to load
with `mapping values are not allowed here`. Most of the catalog single-quotes
`whenToUse` for exactly this reason (an apostrophe inside is doubled, `''`); do the
same for any long scalar. Load the catalog after every frontmatter edit:

```bash
"$PY" -c "import sys; sys.path.insert(0,'src'); from daintree_assistant_server.runbooks.catalog import load_catalog; c=load_catalog(); print(len(c.runbooks), c.revision)"
```

## Baselines

Take `bench local --out "$W/baseline.json"` before the first edit, on the branch's
base. Never fake one later by reverting files with `git stash`: `stash push` on a
path you already committed saves nothing, and the paired `pop` then applies whatever
unrelated stash sits at `stash@{0}`, which in a shared checkout may be another
session's. If a baseline has to be retaken after a rebase, take it from a clean
worktree of `origin/develop` (SKILL.md, Pre-PR Check).

`bench compare` lists every case whose loaded set moved, the recall it fixed or
broke, and cases only one side has. That output, explained, is the routing verdict.

## When a retrain is owed

- A new runbook shares vocabulary with an established neighbour (the common case).
- A case you added is a `rank_miss`.
- You wrote boundary rows for a mis-route the text alone could not fix (rule 3).

It is **not** owed for a body-only change. Name it in the PR and the final report as
`/retrain-selector <ids>`. A retrain is about an hour, swings several cases by seed,
and is judged over seeds (`.claude/skills/retrain-selector`); the run only does one
itself when a red gate blocks delivery and text cannot fix it (SKILL.md, Measure).
