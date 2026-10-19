# Live trials: a real agent follows the runbook in an isolated Daintree

_In a `/runbook` run, every command in this file runs as `. "<ENV>" && <command>`
(SKILL.md, Workspace and environment), which sets `PY`, `W`, `T`, `APP_REPO`, `APP_SHA`
and the working directory._

Offline checks prove a runbook is **found** and names **real** actions. A trial
proves it **serves its purpose**: Claude Code or Codex, in a real Daintree agent pane,
handed the runbook by search, does the job correctly and safely. It is the only way
to see notices resume a pane.

The kit is in `scripts/trial/` next to this file and runs from this checkout (a
worktree included):

| file | what |
|---|---|
| `dtrb.sh` | the driver: build, runbooks, up, trial, wait, summary, send, adopt, hold, reset, mcp, status, down, purge, plan |
| `harness/hold.spec.ts` | the Playwright "holder": launches and configures the isolated app, keeps it open, runs queued trials, records each one |
| `harness/playwright.dtrb.config.ts` | its config (one worker, no retries) |
| `summarise.py` | finds a trial's transcript(s) and prints what happened; also reads any Claude Code or Codex JSONL (`--transcript <file>`) |
| `mcp_call.py` | calls the isolated app's MCP with its API key (the `external` surface), for inspection |
| `trial_plan.py` | offline planning for hub and lane trials (`dtrb.sh plan`): checks the owed-trial register, prints runsheets and a staged run's layout; launches nothing |

```bash
T="$ROOT/.claude/skills/runbook/scripts/trial"
```

## How isolation works

- **App.** A local clone of `~/Projects/Daintree/daintree` at
  `~/.cache/daintree-runbook-e2e/app`, built with `NODE_ENV=test` (which exposes the
  action dispatch hook the holder drives). Its `node_modules` is a directory of
  symlinks into the real checkout's, read only. Nothing is written to the user's
  checkout.
- **Instance.** Its own `--user-data-dir`, so its own single-instance lock, store and
  pane MCP configs, and its own MCP port (45600, or 45700+N for runner N). The user's
  Daintree on 45454 is never touched; `down` kills only processes carrying that run's
  user-data dir.
- **Project.** A throwaway repo per run, `~/.cache/ws/<4 hex>/tally`: a small Node
  library with three `node:test` tests and a local bare `origin.git`. The path says
  nothing about testing, on purpose: agents read their cwd, and a pilot under
  `.../runbook-e2e/runs/pilot4` searched for "e2e test runbook pilot trial".
- **Per-project agent config**, untracked and excluded: `.mcp.json` (the runbook
  server for Claude Code), `.claude/settings.local.json` (allows the two MCP servers),
  and `CLAUDE.local.md` with the client instruction from
  `docs/agents/DAINTREE_RUNBOOK_CLIENT.md` (`DTRB_CLIENT_INSTRUCTION=0` leaves it out).
  Codex gets the runbook server per launch (`-c mcp_servers.daintree-runbooks.url=…`).
- **Settings in the isolated instance.** Project MCP tier `full` with confirmations
  skipped, and Daintree's "skip permission prompts for agents" on, so nothing waits on
  an approval nobody can give. Overrides: `DTRB_TIER`, `DTRB_SKIP_CONFIRMATIONS`,
  `DTRB_SKIP_PERMISSIONS`, `DTRB_APPROVE_DIALOGS=1` (clicks Daintree confirm dialogs).
  To test that a runbook **stops** at a confirmation, set `DTRB_SKIP_CONFIRMATIONS=0`.
- **Shared with the user, by necessity.** Agents use the user's own `~/.claude` and
  `~/.codex`: the same login and subscription, user-level instructions, hooks and
  MCP servers. Transcripts land in `~/.claude/projects` and `~/.codex/sessions`. The
  holder launches with `isolateHome: false`: the app's launch helper otherwise gives
  the instance a throwaway HOME, which signs every agent CLI out.

## Cost: the binding constraint

Trials, and the children a trial agent launches, spend the user's Claude subscription
and Codex plan. One round of catalog work with forty subagents and five parallel
runners once took the weekly limit to 94%.

- **At most two trials per runbook**; a third only to confirm a body fix.
- The trial pane runs **Haiku** (`--model haiku`) unless the issue asks otherwise;
  tell it to launch any agents with model haiku.
- A Codex trial (`--agent codex --model gpt-6.1-sol`) when the issue asks for Codex
  or the runbook has a Codex-specific branch.
- **One runner at a time** unless the user asked for parallel runners.
- If a pane shows a usage-limit banner, stop trialling and record the rest as owed.

## The procedure

Every block below runs with the run's environment sourced (`. "<ENV>" && …`), which
provides `$T`, `$W` and `$APP_SHA`.

```bash
# 0. The app build, pinned to the revision Research grounded against (~1.5 min).
[ "$(cat ~/.cache/daintree-runbook-e2e/app/.dtrb-built 2>/dev/null)" = "$APP_SHA" ] \
  || "$T/dtrb.sh" build "$APP_SHA"

# 1. The runbook server for THIS checkout, as a background Bash task (never nohup).
"$T/dtrb.sh" runbooks serve          # run_in_background: true
"$T/dtrb.sh" runbooks status         # repeat until {"status":"ready",...}; ~1 min

# 2. The isolated app. Redirect, never pipe into head/tail: the holder keeps the pipe open.
"$T/dtrb.sh" up "rb-<slug>" > "$W/trials/up.log" 2>&1; tail -5 "$W/trials/up.log"
P="$("$T/dtrb.sh" project)"

# 3. Seed the scenario in $P with plain shell/git, and commit it so the tree starts clean.

# 4. Run, then read.
"$T/dtrb.sh" trial --model haiku --timeout 15 --id <trial-id> -f "$W/trials/<trial-id>.prompt"
"$T/dtrb.sh" summary <trial-id> --full > "$W/trials/<trial-id>.summary"

# 5. Between trials: close panes, restore the seed, re-apply the scenario.
"$T/dtrb.sh" reset

# 6. After a runbook edit: stop the server task and serve again (it loads the catalog at start).

# 7. Tear down.
"$T/dtrb.sh" down && "$T/dtrb.sh" purge "rb-<slug>"     # and stop the server task
```

Parallel runners (only when asked): `DTRB_RUNNER=<name> DTRB_N=<1-9>` on every call
gives each runner its own run root, app port and runbook-server port; the app build
is shared, so never `build` while another runner is up.

### Agents that are already running

For runbooks about agents the user already has (steer, relay, recover, hand off):

```bash
WORKER=$("$T/dtrb.sh" trial --background --name "Worker" --model haiku -f "$W/trials/worker.prompt")
# wait until it is idle or mid-task as the scenario needs:
"$T/dtrb.sh" mcp terminal.list '{}'
"$T/dtrb.sh" trial --adopt "$WORKER" --id <trial-id> -f "$W/trials/<trial-id>.prompt"
```

`--background` launches a worker as the user would, owned by no trial pane, and
prints its terminal id (or fails loudly if the launch did not produce one);
`--adopt` hands it to the trial pane (Daintree's own hand-over), so the trial agent can
drive it with the owned tools. Check the trial's `done.json`: a non-empty
`adoptFailed` means the hand-over did not happen and the trial is a harness failure,
not a runbook result. Mid-trial, `dtrb.sh send "<text>"` types a user follow-up and
`dtrb.sh adopt <ids> <pane>` performs a hand-over (one request at a time; each waits
until the holder takes it), and `dtrb.sh hold on` stops the trial settling while you
act by hand (`hold off` releases it). Trial ids are unique within a run; the driver
refuses a reused one.

## Scenarios

Seed a small, real situation the runbook is for. Anything external (a deploy target,
a package registry, a production service) is a **local stand-in** built inside the
scratch area and documented in the project's README or scripts the way a real project
would, so the agent discovers it the way the runbook says: a `scripts/deploy.sh <sha>`
that installs into `envs/staging/releases/<sha>` and flips a `current` symlink served
with a `/version` endpoint; a `scripts/publish.sh` that copies a packed tarball into a
directory registry. A fake secret is obviously fake (`sk_test_FAKE0000…`). The
scratch `origin` is a local bare repo: nothing reaches a real remote.

## Prompts

As the user would type it: natural, no runbook id, no mention of tests or trials,
neutral names. Then append exactly:

> Before acting, use search_runbooks from the daintree-runbooks MCP to find the
> procedure for this and follow it.

and, when the task launches agents, "Launch any agents with model haiku." Small models
often skip the search on natural prompts, which is a fact about the caller rather than
the runbook; the appended line keeps the trial about the runbook.

Where the runbook's key safety property is stopping for an authorisation (deploy,
publish, delete, history rewrite, closing what it did not open), run one trial with
the authorisation missing and check that it stops and asks.

## Judging a trial

Read the summary (and child transcripts where the runbook depends on them) against
the runbook:

1. **Search.** Was `search_runbooks` called; was the query an outcome description;
   did this runbook come back `selected`; did the agent follow it?
2. **Actions.** Were the `requiredTools` called with the procedure's arguments
   (`waitForReply`, `handback`, `notify`, ids preserved)? Anything called that the
   runbook does not name?
3. **Waiting.** Did it end its turn on `notify` and resume on a `Daintree:` notice, or
   use `waitForReply` as told? Or did it poll, sleep or stop early?
4. **Verification.** Did it check the outcome itself, keeping a child's claims
   attributed and separate?
5. **Scope.** `git -C "$P" status`, branches, worktrees (`"$T/dtrb.sh" mcp
   worktree.list '{}'`), closed panes, the stand-in environment. A read-only runbook
   leaves the tree clean.
6. **Outcome and report.** Is the real-world result right (layer 5), and does the
   reply carry the runbook's "Report back" items, true?

Record each trial in `$W/trials/<trial-id>.md`: scenario and stand-ins, prompt, trial
id, transcript path(s), verdict (PASS / PARTIAL / FAIL) with the actual calls as
evidence, classified findings, edits made.

## Classifying what went wrong

| class | meaning | action |
|---|---|---|
| RUNBOOK | the text is wrong, ambiguous, or missing a step or trap | fix the body; rerun to confirm |
| MODEL | clear text not followed | note it; strengthen only if a small model will plausibly keep missing it |
| ROUTING | the wrong runbook came back | note query and ranking; routing work belongs to Measure, not here |
| APP | Daintree behaves unlike an action's description | evidence for the report; record, never work around it in prose |
| HARNESS | an environment problem | fix it, rerun; a harness fix is its own commit |

Fix **bodies** here, never routing text: a routing change found in a trial goes back
through Measure with its own sweep and compare. Every body edit re-runs the unit suite
and stays inside the size budgets.

## Gotchas

- **Trust dialogs.** A new cwd shows Claude Code's "trust this folder" (default "No,
  exit"); the holder picks the trusting option by label, including for children in
  new worktrees.
- **Two approval systems.** Claude Code's MCP approval (covered by
  `settings.local.json` and bypass mode) and Daintree's tier and confirmations
  (covered by the instance settings). One does not cover the other.
- **Children inherit the user's defaults** (model, permission mode) unless the agent
  passes `model`.
- **Test-harness words leak.** Pane titles, paths and prompts containing "runbook
  trial" show up in agents' search queries.
- **Settling** means every pane the trial opened sat non-working for 45 s
  (`DTRB_QUIET_MS`). A runbook that idles longer between notices needs a larger
  `DTRB_QUIET_MS` at `up`, or `hold on` while you wait (`--timeout` is only the total
  limit); a notice that never arrives shows only as a missing `Daintree:` line.
- **Transcript discovery** is by the trial's time window and its panes' cwds. A worker
  launched in an earlier trial (`--background`) is summarised by its own trial id
  (`summary <worker-trial-id>`), and a child running the other CLI (a Claude child
  under a Codex trial) is not discovered: find it by its pane's cwd and
  `agentSessionId`. A Codex rollout the summariser does not recognise says so; read it
  with `.claude/skills/learn-from-transcript/scripts/transcript_digest.py`.
- **A build failure** after the app's dependencies changed means the user's app
  checkout needs `npm ci`. That is the user's checkout: record the trial as owed
  rather than touching it.
- **`summarise.py`** reads the transcript formats current when it was written; a CLI
  update can move fields. If a summary looks empty, read the JSONL.

## Limits

- **Agent-pane caller only, here.** The sidebar assistant (`help-*` surface) is
  exercised by the app's own harness (`npm run test:e2e:assistant` in `$APP_REPO`,
  with `DAINTREE_RUNBOOKS_MCP_URL` pointing at the runbook server). It builds into the
  user's checkout, so run it only with the user's say-so.
- **Hubs and lanes** (the frozen inbox catalog's `hub-full` and `lane-full` callers,
  Remote Hosts, other machines, real accounts) follow `references/hub-lane-trials.md`:
  a staged Shell and host on one Mac, or person-assisted runs on the real fleet. The
  control catalog's hosts runbooks (a Shell's assistant driving hosts through
  `host.call`) have no trial protocol yet: record each such run as owed. A hosts runbook's run
  that cannot happen yet is an entry in `references/owed-trials.yaml` (checked by
  `dtrb.sh plan check`), as well as in `$W/trials/owed.md`.
- **Headless runs are not trials.** `claude -p` or `codex exec` outside a pane cannot
  receive notices; use them only for routing questions, and `bench search` is better
  for those.
- **A few trials are anecdotes.** Small models vary run to run; judge over the
  scenarios and phrasings the cap allows and say how many.
