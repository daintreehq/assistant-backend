---
id: daintree.context.curate-copytree-bundle
title: Curate a CopyTree context bundle
summary: Explore the worktree with read-only file tools, settle an explicit list of files and patterns, and generate one bounded CopyTree reference file — copying it to the clipboard only when explicitly asked.
whenToUse: >-
  Use when the user asks for a curated CopyTree bundle, context bundle, reference file, or
  clipboard-ready code context for a feature area, subsystem, bug, review, or later
  handoff — "bundle everything relevant to X, including supporting files and tests". The
  deliverable is the FILE itself, deliberately selected: explore first, settle an explicit
  selection, then one generate call. End on copyTree.generate when only the file is
  wanted; use copyTree.generateAndCopyFile only when the user explicitly asked for the
  clipboard. Do not use when context is merely an input to launching or feeding an agent —
  select the workflow that owns the agent instead
  (daintree.workflow.start-work-on-issue for one issue,
  daintree.edits.spawn-visible-agent for one new agent,
  daintree.context.feed-running-agent for an agent that is already open, or
  daintree.orchestration.fan-out-work for injecting context across parallel agents). Nor when a
  bundle has already carried a key or token out (use daintree.security.respond-to-secret-exposure).
risk: system
requiredTools:
  - worktree.list
  - copyTree.generate
optionalTools:
  - copyTree.generateAndCopyFile
---
Curate by reading the target worktree's files yourself; generate with Daintree's CopyTree (both endpoints ask first in a core pane; an API-key client has only the clipboard one). An installed CopyTree CLI is an alternative only if the user accepts it. This ends at the file: feeding an agent belongs to the runbook that owns it.

On a host: file reads run there, per the hosts foundation.

1. Resolve the worktree id and path from `worktree.list`. Only literal clipboard language ("copy it so I can paste") means `copyTree.generateAndCopyFile`, which replaces the user's clipboard (on a host, the host's); "share" or "for an agent later" means `copyTree.generate`. Choose now; never send one bundle to both.
2. Gather evidence in the target path: entry points, implementation, types, config, callers, tests, fixtures. Confirm each candidate concerns the feature and follow references one hop out. Stop when searches stop surfacing relevant files. Note which intended paths `.copytreeignore` and `.gitignore` exclude.
3. Freeze one explicit, non-empty selection shaped by the ask:
   - Named files or a family: `includePaths` with exact worktree-relative paths and file globs. A bare directory matches nothing: `src/worldgen/**`.
   - Only whole directories: `scopePaths` alone, literal directories, no globs.
   - Folders PLUS files elsewhere: all on `includePaths` (folders as `/**`), no `scopePaths`, which is ANDed with the matchers and would drop the rest.
   Never send an empty array (it means the whole worktree); omit an unused axis. Never widen to a parent to make a matcher land.
4. Options: omit `exclude` and `always` to inherit project settings. Never let `always` or a broad glob reach credentials, `.env`, `.daintree/runbook-runs/`, private ballot maps, or vendored or generated bulk. Omit budgets unless the user named one; report what a project limit cut. `scopeIgnoresIgnoreFiles:true` lifts ignores only for scoped paths, only for content the user wants that isn't secret, vendored or generated.
5. Generate once, with explicit `worktreeId` and a 2 to 4 word `name`:

```json tool=copyTree.generate
{"worktreeId":"<resolved worktree id>","name":"terrain system context","options":{"includePaths":["src/worldgen/**","src/terrain/**","tests/test_terrain*.py","docs/worldgen.md"]}}
```

6. Read the result promptly (`filePath` is a temporary file on the generating machine, pruned by age).
   - `stats.noFilesMatched` is a valid empty result. `stats.unmatchedSelector` names the pattern field that matched nothing: find the concrete mismatch. Absent, an ignore, `exclude`, filter or size gate did it; moving a path between axes never passes an ignore rule.
   - `truncated` or `budgetExceeded` means the bundle is incomplete: drop a cap you invented, narrow on evidence, or report it; ask before raising a user-named limit. `estimatedTokens` is ±20%.
   - List the files it holds against the frozen selection: missing core files or padding is a gap; a surprising `fileCount` alone is not.

At most THREE generate calls, same endpoint: the first, one correction of an empty result, one regeneration of a truncated or wrong one, each with a concrete diagnosed fix. Whatever the last returns is reported as is. Never deliver a second bundle once one landed; regenerate to the clipboard only on the user's word.

Report back: the `name`, worktree, frozen selection and its evidence, `filePath` and its machine, `fileCount`, `outputBytes`, `estimatedTokens`, every flag that fired, what an ignore rule kept out, whether contents matched the selection, and whether it is on the clipboard.
