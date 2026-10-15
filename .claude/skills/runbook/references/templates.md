# Templates

## The design note (`$W/design.md`, Phase 3)

```markdown
# <id>: <new | improve (body/routing) | fences | cases | foundation>

## Decision
<What this run builds and why, from the Phase 2 measurement. If it differs from the
issue's proposal, say so and why.>

## Orchestration
- Daintree's role: <what the caller reads, runs and verifies>
- Delegated work: <what goes to a visible agent, in which worktree>
- Caller(s): <agent pane | sidebar | ...>; baseline path on: <surface>

## Routing text
title: <...>
summary: <one line>
whenToUse: <positive clause in users' and agents' words; the deliverable; carve-outs by FQ id>
Neighbours and fences: <id: which way, the clause>

## Frontmatter
risk: <...>  requiredTools: [...]  optionalTools: [...]  requires: [...]

## Procedure
<sync | async>; the numbered steps; the brief to the agent (objective, output, scope,
stop, must-not); On notice or resume; Stop and recovery; Report back.
Tagged examples: <which calls>.
Size target: <bytes>; bundle with supporting runbooks: <bytes>.

## Grounding
| claim | source (file:line on origin/develop, or dated external source) |
<one row per factual claim the body makes>

## Cases
- recall: <ids, agent- and user-phrased>
- traps: <id per neighbour, both directions>
- file: <cases/NNN-....yaml>

## Corpus
run: runbook-<YYYY-MM-DD>-<slug>; rows: <counts per section>; reverse rows in: <files>

## Trial plan
<scenario, stand-ins, prompt(s), agent(s), what success looks like in the scratch
repo> | <why not trialable here, and the owed run>

## Verification Contract
- Gates: <exact commands>
- Acceptance: <2-3 checks that prove the issue is done>
```

## The PR body (Phase 9)

Write it in the user's voice (`greg-priday-writing`). No AI attribution anywhere.

```markdown
## Summary

- <what the runbook does for the user, one line>
- <the decision: new / improved / fenced, and why>

Resolves #<n>          <!-- or "Part of #<n>" when a required live run is still owed -->

## Routing

| request | before | after |
|---|---|---|
| <phrasing> | <top id score / selected> | <top id score / selected> |

bench local: recall-pass <a/b → c/d>, strict <...>, candidate_miss <0>, gate <pass | red at base, no new failures>.
Moved cases: <each one and why, or "none outside the new cases">.

## Evidence

- Selection: <sweep + gate>
- Contract: <unit suite against the captures from app <commit>>
- Grounding: <the key claims and their sources>
- Live: <trials: agent/model, verdict, what was checked in the scratch repo> | <not
  run: why, and the owed run as a checklist with prompts>

## Also in this PR

- Fences on <neighbour ids>
- Cases: <ids> in <file>
- Corpus: <n> rows in run `<run id>`, blind-verified (<kept>/<written>)

## Not done

- Retrain: <owed (`/retrain-selector <ids>`) because ... | not owed>
- <anything from the issue left for later, and why>
```

## The final report (Phase 11)

```
✅ **Issue #<n>: <title>**  (or **Local run: <task>**)

🧭 **Decision**: <new | improve | fences | cases> <id>, because <measurement>
🗂️ **Files**: <runbooks, cases, corpus, fences>
📈 **Routing**: bench local <baseline → after>, candidate_miss 0, gate <pass>; sweep <n>/<m> selected
🧪 **Live**: <trials and verdicts | not run: why | owed: what, by whom>
🔁 **Retrain**: <owed: /retrain-selector <ids> | not owed>
🔗 **PR**: <url> (<Resolves | Part of> #<n>)
   (local mode instead: 🔗 **Commits**: <shas> on <branch>, not pushed)
🛰️ **CI**: <green | failures remaining | n/a>

RUNBOOK_COMPLETE
```

Blocked:

```
⛔ **Issue #<n>: blocked**

**Blocked on**: <the unmet dependency, the missing authorisation, the required gate
that cannot be made green, or the push that would not land, with the evidence:
issue/PR state, what is missing from origin/develop or the captures, the failing
gate's or push's output>
**What would unblock it**: <the concrete change, and where>
**Measured meanwhile**: <the starting-line sweep, if it ran>
**Nothing was committed or pushed.** <or: the branch <name> holds <what>, unpushed>

RUNBOOK_BLOCKED
```

No-op (the issue is already resolved):

```
✅ **Issue #<n>: no changes needed**
📋 **Why**: <the evidence: the runbook, the case, the commit, the sweep>

RUNBOOK_COMPLETE
```

Catastrophic (repo corruption, auth, filesystem; never for a normal pipeline event):

```
❌ **Catastrophic error**
**Error**: <what>   **Phase**: <which>   **Why unrecoverable**: <why>

CATASTROPHIC_STOP
```
