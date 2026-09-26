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
  daintree.edits.spawn-visible-agent for one new agent, or
  daintree.orchestration.fan-out-work for injecting context across parallel agents).
risk: system
requiredTools:
  - worktree.list
  - copyTree.generate
optionalTools:
  - copyTree.generateAndCopyFile
---
Use when: the user wants a curated context bundle ("everything relevant to X, including supporting files and tests") written as one CopyTree reference file, and put on their clipboard only if they asked. The craft is the curation: gather evidence, freeze an explicit selection, generate once, and read the result before calling it done.

Availability: Hybrid, with generation Full-dependent. Curate with your own file and search tools in the target worktree's path. Generation is Daintree's CopyTree: `copyTree.generate` writes the bundle to a file, and `copyTree.generateAndCopyFile` also replaces whatever the user has on the clipboard. Both are on the pane surface and raise an approval in a core pane; an API-key client is offered only the clipboard one. If the endpoint you need is not offered, say so. An installed CopyTree CLI is an alternative only when the user accepts it: it does not land in Daintree's CopyTree history or clipboard flow, so never substitute it silently. This runbook ends at the file: `terminal.injectOwned` injects the active worktree's prepared context, not this selection, and giving context to an agent belongs to the runbook that owns that agent.

Procedure:
1. Resolve the target and the delivery intent. Take the worktree id and path from `worktree.list` (the one the user named, or the current one via `actions.getContext`). Only literal copy or clipboard language ("put it on my clipboard", "copy it so I can paste it") means the clipboard; "share", "hand off" or "for an agent later" means the file. Choose the endpoint now and never send the same bundle to both.
2. Plan the search: the surfaces the area spans (entry points, core implementation, types and schemas, configuration, callers, tests, fixtures) and the terms (the feature's name, synonyms, key symbols).
3. Gather evidence in the target path, not your own checkout. Search, read enough of each candidate to confirm it concerns the feature, and follow imports and references one hop out for the supporting files. For a broad area a native subagent can return the evidenced paths. Stop when new searches stop surfacing relevant files. Read `.copytreeignore` and `.gitignore` now and note which intended paths they exclude.
4. Freeze one explicit, non-empty selection, on the axis the SHAPE of the ask calls for:
   - Named files or an evidenced family of files: `includePaths` (or `filter`) with exact worktree-relative file paths and file globs. A bare directory matches no file there: write `src/worldgen/**`, not `src/worldgen`.
   - "Everything under these directories", and nothing else: `scopePaths` alone, with literal directory paths, never globs. With both file matchers omitted it includes every non-ignored file in those subtrees.
   - Folders PLUS files elsewhere (the mixed ask): everything on `includePaths`, each folder as a `/**` glob beside the exact paths, and no `scopePaths`. `scopePaths` restricts the walk and is ANDed with the file matchers, so combining them silently drops every file outside the scopes.
   Never send an empty array (it is rejected, because empty would mean the whole worktree); omit an unused axis instead. Never widen to a parent directory just to make a matcher land.
5. Set the rest deliberately:
   - `exclude` and `always`: omit them to inherit the project's settings. An explicit `[]` switches off only that backfill; the repository's own ignore layers still apply. `always` force-includes past ignores, exclusions and `maxFileSize`: never let it, or a broad glob, reach credentials, `.env` files, coordinator checkpoints (`.daintree/runbook-runs/`) or private ballot maps, or vendored or generated bulk.
   - Budgets (`maxTotalSize`, `maxFileCount`, `maxFileSize`, `charLimit`): omit unless the user named a limit, or your invented cap becomes the only thing that truncates the bundle. A limit the project or Daintree applies stays: report what it cut rather than switching it off. When a named budget forces a trim, `sort` decides what survives.
   - `format` and `withLineNumbers` as asked. `modified`, or `changed` with a ref, only when the bundle is scoped to recent work, and only with a ref you verified.
   - An intended path an ignore rule blocks: `scopePaths` with `scopeIgnoresIgnoreFiles:true` lifts only the rules blocking entry into those scoped paths. Use it only when the user wants that content and it is not secret, vendored or generated; otherwise leave the path out and report the shortfall.
6. Generate once. Pass `worktreeId` explicitly (the clipboard endpoint fails without a named worktree, and `copyTree.generate` would otherwise take the active one) and a top-level `name`: a 2 to 4 word purpose label ("auth flow context") shown in the user's CopyTree history and completion notification. Named files:

```json tool=copyTree.generate
{"worktreeId":"<resolved worktree id>","name":"auth flow context","options":{"includePaths":["src/feature/engine.py","tests/test_feature*.py"]}}
```

   Folders only:

```json tool=copyTree.generate
{"worktreeId":"<resolved worktree id>","name":"terrain system context","options":{"scopePaths":["src/worldgen","src/terrain"]}}
```

   Folders plus files elsewhere:

```json tool=copyTree.generate
{"worktreeId":"<resolved worktree id>","name":"terrain system context","options":{"includePaths":["src/worldgen/**","src/terrain/**","tests/test_terrain*.py","docs/worldgen.md"]}}
```

   Send the same shape to `copyTree.generateAndCopyFile` only when the user asked for the clipboard.
7. Read the result before reporting. `filePath` is a temporary file outside the project that is pruned by age: read it promptly with your own tools (`includeContent:true` on `copyTree.generate` also returns a bounded head).
   - `stats.noFilesMatched` is a valid empty result, not a tool error. `stats.unmatchedSelector`, when present, names which of your pattern fields matched nothing: find the concrete mismatch (a bare directory, a typo or wrong case, a missing path, patterns outside `scopePaths`). When it is absent, the scopes, an ignore rule, your `exclude`, a git filter or a size gate did the excluding. Moving a path between `includePaths` and `scopePaths` never gets it past an ignore rule, and a correctly shaped selection does not justify broadening.
   - `truncated` (with `truncatedCount` and `truncatedBy`) or `budgetExceeded` means the bundle is incomplete. Drop a cap you invented, narrow on evidence, or report the shortfall; a limit the user named is theirs, so ask before raising it. `estimatedTokens` is accurate to about ±20%.
   - A populated bundle can still be wrong. List the files it actually holds and compare them with the selection you froze: missing core files or unwanted padding is a demonstrated gap. A merely surprising `fileCount` is not.

Retry ceiling: at most THREE generate calls per request, all to the endpoint chosen in step 1: the first, at most one correction of an empty result, and at most one regeneration of a populated but truncated or wrong one. Correct only when the diagnosis names a concrete change, and decide the whole fix (selection and budget together) before regenerating. A second empty result, or whatever the regeneration returns, is reported as it stands. Never deliver a second bundle once one has landed; an empty result delivered nothing. A clipboard regeneration replaces what the user may already have pasted from: regenerate to the clipboard only on their word, while a file regeneration just writes a new file.

Report back: the `name` label, the worktree, the frozen selection (paths, globs, scopes) and the evidence behind it, the returned `filePath`, `fileCount`, `outputBytes` and `estimatedTokens`, every flag that fired (`noFilesMatched`, `unmatchedSelector`, `truncated`, `truncatedBy`, `budgetExceeded`), anything an ignore rule kept out, whether the bundle's contents matched the selection, and whether it is on the clipboard. The result proves the bundle was written, not that anyone has read it.
