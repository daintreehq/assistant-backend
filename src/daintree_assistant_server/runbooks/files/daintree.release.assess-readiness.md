---
id: daintree.release.assess-readiness
title: Assess whether a release candidate is ready to ship
summary: Pin the exact release candidate, have read-only checkers examine scope, migrations and rollback, and config and docs, read CI on that SHA and the open blockers yourself, and give a go, no-go or go-with-risks verdict with evidence per area, tagging and deploying nothing.
whenToUse: 'Use when the user wants a go/no-go on shipping a release — "are we ready to ship 2.5?", "is the release candidate good to go?", "what''s blocking the release?", "sanity-check v3.1 before we cut it", "give me a readiness verdict for the RC". The deliverable is a verdict (go, no-go, or go with named risks) for one EXACT candidate pinned to a tag or SHA, never a moving branch, with evidence per area: planned scope merged, CI green on that SHA, migrations and whether they reverse, config, env and flag changes, notes and docs, open blockers, a rollback path; what could not be established stays unknown. Nothing is merged, tagged or deployed. Do not use for readying one branch or opening its PR (use daintree.workflow.prep-branch-for-review), merging a PR (use daintree.forge.land-pull-request), writing the release notes (use daintree.forge.draft-release-notes), running the tests with no verdict on shipping (use daintree.ci.run-local-checks; across remote hosts'' environments, daintree.hosts.run-validation-matrix), or actually shipping it to an environment (use daintree.release.deploy-and-verify).'
risk: read
requiredTools:
  - worktree.list
  - agent.launch
  - forge.listIssues
  - forge.listPRs
optionalTools:
  - terminal.getStatus
  - terminal.sendCommandOwned
---
The verdict is advice; the user decides. Nothing here merges, tags, deploys or writes to the forge.

On a host: shell steps run there, per the hosts foundation; own shell: `gh` naming the repo.

Procedure:
1. Pin the candidate from the remote, never a local branch: `git ls-remote origin refs/tags/<tag> 'refs/tags/<tag>^{}'` (a `^{}` line is the commit) or `refs/heads/<branch>`; a SHA the user gave is the candidate. A branch moves: say "candidate = origin/<branch> at <sha>, read at <time>" and judge only that SHA. Previous release = what last shipped on this line: the user's word, release or deploy history, else the highest non-prerelease tag below the candidate, never one on the candidate SHA. `<prev>..<sha>` is what is new. Neither defensible: ask once; range-dependent areas stay unknown.
2. Scope source: the milestone, issue list or plan the user named, with its must-haves; none: scope "unknown".
3. Launch three read-only checkers in the main checkout (`worktree.list`, `isMain`): separate `agent.launch` calls, `name` "Readiness: scope", "Readiness: migrations", "Readiness: config & docs", each `notify:true, handback:true`, prompt: repo, candidate SHA, previous tag and SHA, its lens, and: "`git fetch --tags origin` if the SHA is missing, then read the candidate only through git at <sha> (`git show <sha>:<path>`, `git diff <prev>..<sha>`). Otherwise read-only: change no files, branches, tags, issues or PRs; run no migrations, deploys or publish scripts. Cite a commit, PR or file:line per claim; say unknown where you could not establish it. Prose, at most 40 lines." Lenses:
   - Scope (given the scope source verbatim): each planned item in or out. In means its PR's merge commit is an ancestor (`git merge-base --is-ancestor <commit> <sha>`) AND its code is still at `<sha>`: not reverted or disabled later in the range. Also unplanned PRs in the range.
   - Migrations and rollback: each migration or schema change: reversible? drops, renames or rewrites data? does `<prev>`'s code still run on the new schema? Then the rollback path (documented, or a redeploy of `<prev>`): does it exist and still apply?
   - Config and docs: new or renamed env vars, secrets, config keys and flags with defaults, bumps needing an ops change; do the notes or CHANGELOG cover the range and the setup docs name the new config? A target environment named, with read-only access: compare its applied config, flags and migrations with the candidate's needs; else operational readiness unknown.
4. While they run, read yourself:
   - CI on the SHA, not on a PR. Required checks: the base branch's protection (`gh api repos/<o>/<r>/branches/<base>/protection/required_status_checks`), else the release workflows; no forge remote or unreadable: CI unknown. Results: `gh api --paginate repos/<o>/<r>/commits/<sha>/check-runs --jq '.check_runs[]|[.name,.status,.conclusion]'` plus `.../commits/<sha>/statuses`. A required check pending, skipped or missing is not green; no classic statuses is no failure. Never `forge.getCIStatus` (per PR, no commit).
   - Open blockers: `forge.listIssues` and `forge.listPRs` with the main `worktreeId`, `state:"open"`, `perPage:100`, `bypassCache:true` and a provider-native `search` (GitHub: `label:"<blocker label>"` or `milestone:"<milestone>"`); follow `nextCursor`. A listing error is incomplete, not zero.

```json tool=agent.launch
{"agentId":"claude","name":"Readiness: migrations","worktreeId":"<main worktree id>","prompt":"<repo, SHAs, lens, read-only and citation rules>","notify":true,"handback":true}
```

On notice or resume: track each lens (pending, done, blocked) by terminal id. Check each load-bearing claim yourself (a PR's ancestry, a migration at `<sha>` and its down path, a config key at the cited line); the rest stays the checker's claim. A question within the brief: one answer by `terminal.sendCommandOwned`, `notify:true, handback:true`. The verdict waits for all three; wanted sooner, it is provisional, missing lenses named.

Verdict: no-go on a required check failing or absent on the SHA, an open blocker, a must-have out of scope, an irreversible migration without a rollback plan the user accepted, or a required config change nobody has made. Go with risks: partial findings, each named. An unknown required area (CI, blockers, a must-have) blocks go: the verdict is "not established", naming it. Go only when every area has evidence.

Stop and recovery: one follow-up per checker. A tip that moved after pinning is a new candidate.

Report back: candidate ref and SHA, previous release, verdict, then every area, unknown ones too (scope, CI, blockers, migrations and rollback, config and docs): pass, fail, risk or unknown, with evidence and links, your reads apart from checker claims; what would flip a no-go; checker terminal ids; that nothing was merged, tagged or deployed.
