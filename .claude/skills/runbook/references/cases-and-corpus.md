# Cases and corpus rows

_In a `/runbook` run, every command in this file runs as `. "<ENV>" && <command>`
(SKILL.md, Workspace and environment), which sets `PY`, `W`, `T`, `APP_REPO`, `APP_SHA`
and the working directory._

Two different things, kept apart on purpose:

- **Benchmark cases** (`benchmarks/runbook_selection/cases/`) are the held-out
  evaluation set and the routing gate's evidence. They measure.
- **Corpus rows** (`training/corpus/`) are training data. They change nothing until
  the next retrain, and then they decide whether the runbook is competitive.

A string must never be both. `compile_corpus` drops exact collisions between the two;
near-duplicates get through unless you screen them.

## Benchmark cases

### Shape

```yaml
cases:
  - id: hosts-survey-agent-phrased          # unique across every case file
    category: single-runbook                # an EXISTING category (below)
    note: >-
      What the case pins and the routing-text clause that decides it, quoted.
      For a trap: which neighbour must not be picked, and why the text says so.
    messages:
      - role: user
        content: Show me what's happening across my other Macs, every host and its open exchanges.
    expect:
      required: [daintree.hosts.survey-hosts]
      optional: [daintree.hosts.foundation]     # the nearest foundation, never in required
```

- `expect.required`: every runbook the turn must load. A domain foundation goes in
  `optional` beside a specific; `cases.py` rejects it in `required` unless it is the
  whole answer (the foundation-alone shape).
- `expect.optional`: runbooks that may ride along without counting as extras.
- `expect.docs`: optional in any category; it also scores the docs decision (several
  routing traps use it).
- Multi-turn cases add earlier `messages` (user/assistant). Most new-runbook cases
  are a single user message, because the served query is a bare query.

### Remote Host cases

Use current help/relay behavior, with `host_state` when eligibility matters. A
receiving assistant follows the ordinary catalog for the task in its brief. Retired
hub/lane cases belong in `archive/inbox-v0.44/cases/`; their evaluation wording stays
protected from training. Do not author new inbox protocol cases.

### Categories

Use an existing one. Every category is floored on its own, so a new one-case category
fails the gate on its first miss. Current spread (836 cases): `routing-trap` (492),
`single-runbook` (228), `host-query` (42), `docs-classification` (24), `topic-shift`,
`continuation`, `no-task`, `foundation-only`, `background-wake`, `adversarial`,
`multi-runbook`. New-runbook work needs `single-runbook` and `routing-trap`; a new
foundation needs `foundation-only`.

### What a runbook owes

`bench coverage` exits non-zero unless:

- every selectable non-foundation runbook (the hidden docs runbook is exempt) has at
  least one case that **requires** it (recall);
- every runbook whose `whenToUse` names neighbours has at least one `routing-trap`
  case that requires it **and** leaves a declared neighbour out of both `required`
  and `optional` (so picking the neighbour is an extra);
- a foundation has a case where it is the entire required set.

The gate checks one trap per runbook, but every declared neighbour is a confusion
claim. Write the full set:

1. an **agent-phrased** recall case: the outcome described the way Claude Code or
   Codex writes a `search_runbooks` query;
2. a **user-phrased** recall case: casual, as typed into a pane;
3. one **routing trap per declared neighbour** where the new runbook wins;
4. one **reverse trap per close neighbour** where the neighbour wins and the new
   runbook must not ride along (this is what proves the fence both ways).

For an improvement, add the case that would have caught the defect, and a trap for
any boundary you moved.

### Choosing the strings

- Probe each candidate with `bench search` and keep the widest margin
  (`references/routing.md`, rule 5).
- Short enough to fit the encoder's 256 tokens; scrub specifics and keep the shape.
- Neutral: no runbook ids, no "runbook", no test or trial words.
- Never a paraphrase of a corpus row. The issue's own example requests may become
  cases (scrubbed of private names); they never become corpus rows, which are written
  as different situations.
- The `note` quotes the clause on disk that decides the answer. A case that no clause
  decides is not earned yet; fix the text.

A case the shipped artifact fails is honest measurement and is allowed, provided
`bench local` stays green and `candidate_miss` stays zero. Say so in the PR.

### Where they go

- A batch of runbooks in one namespace: a new numbered file with a header comment
  listing the runbooks and what the cases pin, the way
  `cases/100-y-coordination.yaml` does. Use the next free number in the 100s.
- One runbook joining an existing topic: the topical file (`28-agent-usage-limits.yaml`
  for usage, `20-routing-traps.yaml` for a lone trap).
- A lesson from a real session: `19-from-transcripts.yaml` with a `tx-` id, through
  `/learn-from-transcript`.

Never author against, read for ideas, or edit `benchmarks/runbook_selection/holdout/`:
those rounds judge retrains, and round 6b is still sealed. Reading a sealed suite
spends it.

## Corpus rows

`training/corpus/README.md` is the format authority; `training/README.md` explains
the pipeline. The essentials:

### Where a row goes

| the row's answer | file |
|---|---|
| exactly one runbook | `training/corpus/runbooks/<that id>.yaml` |
| none, or several | `training/corpus/shared/<source>.yaml` |

A boundary row lives with the runbook that **wins** it. `compile_corpus` refuses a
runbook file holding a row that answers anything else.

### A runbook file

```yaml
runbook: daintree.hosts.survey-hosts
run: runbook-2026-10-15-hosts-survey       # provenance; prune_run removes a batch by it

positives:
  - what's going on on the other machines right now
  - query: which hosts are connected and what are they doing
    history:
      - "user: are the studios up?"
      - "assistant: Two of four are connected."

boundaries:
  - query: which of my remote machines are busy right now
    confusable_with: [daintree.orchestration.supervise-fleet]
```

When the file already exists (an improvement), append to its sections and stamp each
new example with `run:` so the batch stays separable. Sections: `positives`,
`boundaries`, `followups`, `topic_shifts`, `wakes`, `near_misses`, `docs`,
`foundation`, `foundation_boundaries`.

### What to write

- **Positives**: invented situations, not `whenToUse` paraphrases. Mix agent-written
  task descriptions (the served traffic) with users' own words, short and long,
  with and without a social lead-in.
- **Boundaries** against every declared neighbour, in this runbook's file.
- **Reverse boundaries** in each close neighbour's file: rows where the neighbour is
  right and the new runbook is `confusable_with`. Without them every fence runs one
  way only.
- **Followups / topic shifts** where the runbook is commonly a second turn.
- **Label the specific runbook alone.** Never "specific + foundation"; the backend
  pairs the foundation downstream. The loader silently strips a paired
  `daintree.foundation`, but not a domain foundation, so never pair one.
- **Project work is never `[]`.** A request to change, fix or investigate the project
  routes to the runbook that delegates it (`daintree.edits.spawn-visible-agent` when
  nothing more specific owns it). `[]` is for chatter, general knowledge and
  off-topic requests.

Volume: round 6 added about 70 rows per new runbook including reverse rows (1,286
rows for 18 runbooks). A new runbook in a crowded neighbourhood needs that order; a
body-only improvement needs none; a routing improvement needs rows for the boundary it
moved, in both directions. A new runbook must have at least one row
(`test_no_runbook_is_starved_of_examples`).

### Generating rows

Write them yourself, or with one subagent per runbook told: read only
`training/data/catalog_digest.md` (refresh it first, recipe in
`.claude/skills/retrain-selector/SKILL.md`) and the runbook files; **never read
`benchmarks/` or `tests/`, nor anything in `training/data/` but the digest**; only
real runbook ids; `[]` is a
first-class answer; return positives, boundary rows mined from the "do not use for"
clauses, reverse boundaries and followups.

### Screen, then verify blind

1. **Contamination screen.** Drop any row whose query, or any history line, is a
   difflib ratio > 0.8 against any case `messages` content in `cases/` and
   `holdout/` (compare strings programmatically; do not read the holdout yourself).
2. **Blind verification.** Strip the labels, shuffle, and give Codex the rows with
   the catalog's routing text (`training/data/catalog_digest.md`). Ask for the set
   of runbook ids each row should load (usually one, several when the request
   genuinely needs several, `[]` when none should). Keep only the rows where its set
   equals yours; rewrite or drop the rest. Call shape:

   ```bash
   codex exec -m gpt-6.1-sol -c model_reasoning_effort="high" -s read-only \
     -C "$ROOT" --ephemeral - 2>"${TMPDIR:-/tmp}/codex-label-<slug>.log" <<'CODEX_PROMPT'
   You are in a read-only sandbox: reading files and read-only commands are fine; do not write.
   Read training/data/catalog_digest.md. For each numbered request below, answer with the
   runbook ids that should load for it: usually one, several only when the request needs
   several, [] when none should. Judge from the catalog text only. Output one line per
   request: "<n> [<ids, comma-separated>] <one-line reason>".
   <numbered, shuffled, unlabelled rows>
   CODEX_PROMPT
   ```

   Launch it with `run_in_background: true` and wait with `TaskOutput`.
3. **Compile and check.**

   ```bash
   "$PY" -m training.compile_corpus          # read dropped_contaminated / dropped_conflicting_label
   "$PY" -m training.compile_corpus --check  # what the test suite checks
   ```

   Commit the YAML with the recompiled `training/data/selection.jsonl` and
   `training/data/corpus-lock.json`, in one commit. On a rebase conflict in the two
   generated files, take either side and recompile; never hand-merge them.

A bad batch comes back out with `"$PY" -m training.prune_run <run-id>` then a recompile.
