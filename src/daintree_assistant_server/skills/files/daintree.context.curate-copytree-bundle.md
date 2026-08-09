---
id: daintree.context.curate-copytree-bundle
title: Curate a CopyTree context bundle
summary: Explore the worktree with the read-only fs tools, settle an explicit list of files and patterns, and generate one bounded CopyTree reference file — copying it to the clipboard only when explicitly asked.
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
  - worktree.getCurrent
  - fs.list
  - fs.read
  - fs.search
  - copyTree.generate
  - copyTree.generateAndCopyFile
---
Use when: the user wants a curated context bundle — "everything relevant to X, including supporting files and tests" — turned into one CopyTree reference file, optionally landed on their clipboard. The craft is the curation: explore the worktree for evidence, freeze an explicit selection, make ONE generate call on the chosen endpoint, and verify the budget flags before calling it done.

Procedure:

1. Resolve the target worktree and the delivery intent up front. `worktree.getCurrent({})` for the current one, or `worktree.list({})` when the request names another; keep the returned id. Classify the ask now: file only (the default), or explicitly clipboard-bound — only literal copy/clipboard language ("put it on my clipboard", "copy it so I can paste it") means the clipboard; "share", "hand off", or "for an agent later" means the file. Decide the endpoint before generating so you never send the same bundle to both delivery endpoints.

2. Turn the feature request into a search plan, not a path guess. Write down the surfaces the area likely spans — entry points, core implementation, types and schemas, configuration, callers, tests, fixtures — and the terms you will search for: the feature's name, its synonyms, and the key symbol names you expect.

3. Explore with the read-only fs tools until every selected path has evidence:
   - `fs.search({"query":"<term>","maxResults":50})` — the query is a literal, case-sensitive substring (not a regex); add `"glob":".py"` to filter by filename SUFFIX, not a glob pattern (`.py` keeps Python files; `*.py` matches nothing). Matches carry file and line. `maxResults` defaults to 50, max 500.
   - `fs.list({"path":"<dir>","depth":2})` — `path` and `depth` are the only parameters, and `depth` is bounded 1–10. Entries come back sorted; walk one level at a time rather than asking for the whole tree at once.
   - `fs.read({"path":"<candidate>","maxBytes":20000})` to confirm a candidate actually concerns the feature — `maxBytes` is bounded 1–200000 and caps how much of the file comes back.
   Chase imports and references one hop out from the core files to catch the supporting files the user asked for. Stop when new searches stop surfacing new relevant files.

4. Freeze one explicit, non-empty selection, and pick the axis from the SHAPE of the ask. Use `includePaths` (or `filter`) when the ask names individual files or an evidenced family of files: exact project-relative FILE paths and file-matching globs go there, and a directory is not a file path. Use `scopePaths` ALONE when the ask is "everything under these directories" — it takes literal project-relative paths, NEVER globs, restricts which subtrees are WALKED, and, with `includePaths` and `filter` both omitted, includes every non-ignored file inside those scopes. That is the discriminator: `scopePaths` only for "everything under these directories", exact files and file-family patterns on `includePaths`/`filter`. The host unions `includePaths` and `filter` into one worktree-wide file matcher, so neither ever restricts traversal; combining that matcher with `scopePaths` is AND, narrowing the scoped tree rather than adding files from elsewhere. Never send an empty selection array — an empty list is rejected at decode time precisely because empty would mean "no filter", which is the whole worktree: the exact accidental mega-bundle this runbook exists to prevent. Omit an unused axis rather than emptying it, and never omit all three.

5. Set the remaining options deliberately:
   - `exclude` and `always`: OMIT them to inherit the project's configured exclusions and force-includes; pass an explicit `[]` to switch that settings backfill off for this one call. Note `[]` disables only Daintree's project-settings backfill — the repository's own CopyTree configuration (its ignore and force-include layers) still applies, so do not promise a byte-exact selection. `always` force-includes files past both `exclude` and the `maxFileSize` gate.
   - Budgets: `maxTotalSize`, `maxFileCount`, and `maxFileSize` are bytes and counts; `charLimit` is a character budget across ALL included content, not per file. When a budget forces a trim, `sort` (path, size, modified, name, extension, depth) decides which files survive — set it when survival order matters.
   - `format` (xml, json, markdown, tree, ndjson, sarif) and `withLineNumbers` as the user asked.
   - `modified` (working-tree changes) and `changed: "<ref>"` only when the user scoped the bundle to recent work — and only with a ref you have verified, because a bad ref silently drops the git filter, widening the bundle to everything the rest of the selection still allows.

6. Pick the shape that matches the ask, then make ONE CopyTree call with the worktree named explicitly. For named files or an evidenced family of files, select them directly and leave `scopePaths` off:

       copyTree.generate({
         "worktreeId": "<resolved id>",
         "options": { "includePaths": ["src/feature/engine.py", "tests/test_feature*.py"], "maxTotalSize": 400000 }
       })

   THE KEY TRAP is the whole-folder ask — "bundle the terrain system", which resolves to a few directories rather than a list of files. A bare directory is not a file selector: `"includePaths": ["src/worldgen"]` matches ZERO files, because no file's path is ever equal to the directory that holds it. Do NOT put a bare directory in `includePaths` or `filter`, and do NOT put a glob like `src/worldgen/**` in `scopePaths`. Scope the directories literally and OMIT both file matchers:

       copyTree.generate({
         "worktreeId": "<resolved id>",
         "options": { "scopePaths": ["src/worldgen", "src/terrain", "src/world"], "maxTotalSize": 400000 }
       })

   Leaving `includePaths` out there is deliberate, not an oversight: with a scope set and no file matcher, every non-ignored file under those directories is included, which is exactly the folder ask. A directory glob such as `"includePaths": ["src/worldgen/**"]` also matches file paths, but it does not restrict traversal — reach for it only when folder contents must be unioned with exact files elsewhere, never as the baseline folder call.

   Only when the user explicitly asked for the clipboard, send the same shape to `copyTree.generateAndCopyFile` instead. That call hard-fails without a `worktreeId` — assistant calls get no active-worktree fallback, so name the worktree you curated against. On success the host shows a "Context copied" toast; macOS and Linux put the file itself on the clipboard, Windows its path. Never call generate first and then regenerate with generateAndCopyFile — pick the endpoint in step 1 and call it once. That rule forbids delivering the same bundle twice; the single corrected call in step 7 is a different thing, because an empty result delivered no bundle at all.

7. Check the result before reporting. The verdict lives in `stats`. `noFilesMatched` is a valid empty outcome rather than a tool error, and it is never where the turn ends — diagnose it, then make at most ONE corrected call:
   - Read the ignore layers first — `fs.read({"path":".copytreeignore","maxBytes":20000})`, then `fs.read({"path":".gitignore","maxBytes":20000})`, skipping whichever is absent. Both use gitignore syntax, both are always in force, and CopyTree exposes no option to include ignored files.
   - `noFilesMatched` is a bare boolean that names no failed selector, so reading those rules is the only way to tell the two causes apart. Check every intended path against them and split the selection: paths an ignore rule explains, and paths still eligible. Switching axes NEVER rescues an ignored path — `scopePaths`, `includePaths` and `filter` are all subject to the same rules. Drop those paths from any retry, then either re-select evidenced content outside the ignored area or report the shortfall plainly; when nothing eligible is left, report and stop.
   - For whatever stays eligible, retry once and only from a concrete diagnosis. A whole-directory ask sent as bare directories in `includePaths` or `filter` is the common one: move those literal directories to `scopePaths`, omit both file matchers, and send that. A selection that was already correctly shaped does NOT justify broadening to `scopePaths` — recheck each path against the step 3 evidence and correct the specific mismatch on the axis it already sits on. With no concrete correction to make, report the empty result rather than guessing at another call.
   - Read `stats` again after that one corrected call and stop there. A second empty result is reported as an empty result, together with what you ruled out.

   Whether the bundle came from the first call or the corrected one, read the rest of the flags before reporting: `truncated` with `truncatedCount` and `truncatedBy` says a budget cut files (maxFileCount, maxTotalSize or charLimit — whichever bit first); `budgetExceeded` means the retained set is still larger than `maxTotalSize` and can be true even without truncation; `estimatedTokens` is accurate to about ±20%. A truncated or budget-exceeded bundle is never reported as complete — narrow the selection, raise the budget deliberately, or report the shortfall plainly. The returned `filePath` is a system temp file OUTSIDE the project, so `fs.read` cannot open it — you cannot inspect the bundle after the fact, which is why the selection is settled with evidence in step 3 and the stats are read carefully here.

Confirmation: `copyTree.generateAndCopyFile` is system-tier and always confirms — it replaces whatever the user currently has on their clipboard. Confirm per the active tier before sending it. `copyTree.generate` is read-tier and needs no confirmation.

Report back: the resolved worktree, the settled selection (paths, globs, scopes) and the evidence behind it, the returned filePath, fileCount / outputBytes / estimatedTokens, every budget flag that fired (truncated, truncatedBy, budgetExceeded, noFilesMatched), any requested content an ignore rule kept out and the shortfall that leaves, and whether the bundle landed on the clipboard — never a table.
