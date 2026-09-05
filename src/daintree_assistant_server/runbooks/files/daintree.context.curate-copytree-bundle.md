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
confirmation: "Curating a context bundle. The aim is one reference file covering the area asked about, with the selection settled from what the code actually shows rather than guessed at."
risk: system
requiredTools:
  - worktree.list
  - worktree.getCurrent
  - fs.list
  - fs.read
  - fs.search
  - copyTree.generate
  - copyTree.generateAndCopyFile
  - subagent.run
  - agentTask.spawnForEdits
---
Use when: the user wants a curated context bundle — "everything relevant to X, including supporting files and tests" — turned into one CopyTree reference file, optionally landed on their clipboard. The craft is the curation: explore the worktree for evidence, freeze an explicit selection, make the initial generate call on the chosen endpoint, and verify the budget flags before calling it done.

Procedure:

1. Resolve the target worktree and the delivery intent up front. `worktree.getCurrent({})` for the current one, or `worktree.list({})` when the request names another; keep the returned id. Classify the ask now: file only (the default), or explicitly clipboard-bound — only literal copy/clipboard language ("put it on my clipboard", "copy it so I can paste it") means the clipboard; "share", "hand off", or "for an agent later" means the file. Decide the endpoint before generating so you never send the same bundle to both delivery endpoints.

2. Turn the feature request into a search plan, not a path guess. Write down the surfaces the area likely spans — entry points, core implementation, types and schemas, configuration, callers, tests, fixtures — and the terms you will search for: the feature's name, its synonyms, and the key symbol names you expect.

3. For a broad scope, delegate discovery to subagent.run with a required list of evidenced paths and supporting tests; do not flood the main conversation with exploratory file reads. For a small known area, use the read-only fs tools until each selected path has evidence. These tools are rooted at the assistant project path: if the selected Daintree worktree differs, use an explore agent in that checkout to curate the paths, because selecting a worktree does not retarget fs.*:
   - `fs.search({"query":"<term>","maxResults":50})` — the query is a literal, case-sensitive substring (not a regex); add `"glob":".py"` to filter by filename SUFFIX, not a glob pattern (`.py` keeps Python files; `*.py` matches nothing). Matches carry file and line. `maxResults` defaults to 50, max 500.
   - `fs.list({"path":"<dir>","depth":2})` — `path` and `depth` are the only parameters, and `depth` is bounded 1–10. Entries come back sorted; walk one level at a time rather than asking for the whole tree at once.
   - `fs.read({"path":"<candidate>","maxBytes":20000})` to confirm a candidate actually concerns the feature — `maxBytes` is bounded 1–200000 and caps how much of the file comes back.
   Chase imports and references one hop out from the core files to catch the supporting files the user asked for. Stop when new searches stop surfacing new relevant files.

4. Freeze one explicit, non-empty selection, and pick the axis from the SHAPE of the ask. Use `includePaths` (or `filter`) when the ask names individual files or an evidenced family of files: exact project-relative FILE paths and file-matching globs go there, and a directory is not a file path. Use `scopePaths` ALONE when the ask is "everything under these directories" — it takes literal project-relative paths, NEVER globs, restricts which subtrees are WALKED, and, with `includePaths` and `filter` both omitted, includes every non-ignored file inside those scopes. The host unions `includePaths` and `filter` into one worktree-wide file matcher, so neither ever restricts traversal; combining that matcher with `scopePaths` is AND, narrowing the scoped tree rather than adding files from elsewhere. Never send an empty selection array — an empty list is rejected at decode time precisely because empty would mean "no filter", which is the whole worktree: the exact accidental mega-bundle this runbook exists to prevent. Omit an unused axis rather than emptying it, and never omit all three.

5. Set the remaining options deliberately:
   - `exclude` and `always`: OMIT them to inherit the project's configured exclusions and force-includes; pass an explicit `[]` to switch that settings backfill off for this one call. Note `[]` disables only Daintree's project-settings backfill — the repository's own CopyTree configuration (its ignore and force-include layers) still applies, so do not promise a byte-exact selection. `always` force-includes files past both `exclude` and the `maxFileSize` gate.
   - Budgets: OMIT them by default. Unset, CopyTree's own ceilings apply (100 MB, 10k files), which a curated selection rarely approaches — so a budget you invent is usually the only thing that truncates the bundle, and a truncation is then YOUR cap firing, not a signal the selection was too big. Set one only when the user named a limit. `maxTotalSize`, `maxFileCount` and `maxFileSize` are bytes and counts; `charLimit` is a character budget across ALL included content, not per file. When a budget forces a trim, `sort` (path, size, modified, name, extension, depth) decides which files survive — set it when survival order matters.
   - `format` (xml, json, markdown, tree, ndjson, sarif) and `withLineNumbers` as the user asked.
   - `modified` (working-tree changes) and `changed: "<ref>"` only when the user scoped the bundle to recent work — and only with a ref you have verified, because a bad ref silently drops the git filter, widening the bundle to everything the rest of the selection still allows.

6. Match the shape to the ask: `scopePaths` only for "everything under these directories", named files and evidenced file families on `includePaths`/`filter`. Then call the endpoint chosen in step 1, passing the worktree id and a run label. `name` labels what the bundle is FOR — it becomes the run's title in the user's copy-tree history and the completion notification. Prefer a clear 2-4 word purpose label ("auth flow context"); the count is advisory, so never pad or truncate to fit. It sits at the TOP LEVEL, a sibling of `worktreeId` and `options`, never inside `options`. Always supply one: blank or whitespace is treated as absent and falls back to a label derived from the selection ("engine.py +7 more"). BOTH endpoints announce on completion, and your label is read VERBATIM in that notice — a plain `copyTree.generate` from you reads "Agent generated context — <label>", the clipboard endpoint "Context copied — <label>" — so treat the label as user-facing copy rather than an internal tag, and never leave it to the fallback. For named files or an evidenced family of files, select them directly and leave `scopePaths` off:

       copyTree.generate({
         "worktreeId": "<resolved id>",
         "name": "auth flow context",
         "options": { "includePaths": ["src/feature/engine.py", "tests/test_feature*.py"] }
       })

   THE KEY TRAP is the whole-folder ask — "bundle the terrain system", which resolves to a few directories rather than a list of files. A bare directory is not a file selector: `"includePaths": ["src/worldgen"]` matches ZERO files, because no file's path is ever equal to the directory that holds it. Do NOT put a bare directory in `includePaths` or `filter`, and do NOT put a glob like `src/worldgen/**` in `scopePaths`. Scope the directories literally and OMIT both file matchers:

       copyTree.generate({
         "worktreeId": "<resolved id>",
         "name": "terrain system context",
         "options": { "scopePaths": ["src/worldgen", "src/terrain", "src/world"] }
       })

   Omitting `includePaths` there is deliberate, not an oversight.

   THE SECOND TRAP is the MIXED ask — whole folders PLUS named files somewhere else ("the terrain system and its tests"). Reaching for both axes at once is the wrong instinct: `scopePaths` restricts the WALK, so every file outside those subtrees is ANDed away and the bundle comes back short or empty. Put the whole selection on `includePaths`, writing each folder as a `/**` glob beside the exact paths, and leave `scopePaths` off:

       copyTree.generate({
         "worktreeId": "<resolved id>",
         "name": "terrain system context",
         "options": { "includePaths": ["src/worldgen/**", "src/terrain/**", "tests/test_terrain*.py", "docs/worldgen.md"] }
       })

   `src/worldgen/**` is a first-class file matcher, not a workaround — a folder-shaped glob is exactly what the `/**` suffix is for, and it reaches anywhere in the worktree because `includePaths` never restricts traversal. Keep bare `scopePaths` for asks that are ONLY folders, and never widen a scope to a parent directory just to make a matcher land: a bundle that quietly swaps the curated selection for its enclosing folder is a worse answer than a narrow one.

   Only when the user explicitly asked for the clipboard, send the same shape — including the top-level `name` — to `copyTree.generateAndCopyFile` instead. That call hard-fails without a `worktreeId` — assistant calls get no active-worktree fallback, so pass the id of the worktree you curated against. On success the host pins a transient "Context copied — <your label>" notice to the toolbar's Copy context button; it falls back to a toast only when that button is not visible, and a blurred window turns even that fallback into an inbox row. So never promise a toast, and never treat its absence as a failed copy — read the returned `stats` for the verdict. macOS and Linux put the file itself on the clipboard, Windows its path. Never switch endpoints, send the same request to both, or deliver a second bundle once one has landed: every attempt step 7 permits goes to the endpoint chosen in step 1 and counts against its ceiling. An empty result delivered no bundle, so the corrected call in step 7 is not a second delivery.

7. Check the result before reporting. The verdict lives in `stats`. `noFilesMatched` is a valid empty outcome rather than a tool error — never report it undiagnosed, and make at most ONE corrected call AT AN EMPTY RESULT, only when the diagnosis names a concrete correction:
   - Check `stats.unmatchedSelector` first. It is returned only SOMETIMES, and when present it names which pattern field(s) you supplied to the one unioned matcher — `filter`, `includePaths`, or `filterAndIncludePaths`. That is the field to fix, never the cause. Read your own patterns against the step 3 evidence for the concrete mismatch: a bare directory where a `/**` glob belongs, a typo or wrong case, a path that does not exist, an over-narrow glob, or — whenever `scopePaths` was sent too — patterns pointing OUTSIDE the scoped subtrees, which the AND then erases. Suspect that last one first when both axes were used; none of them is ever evidence that `**` is unsupported. When the field is ABSENT the patterns are not the whole story — the scopes, an ignore rule, your own `exclude`, a `modified`/`changed` filter or a size gate did some of the excluding — so check those before reshaping the selection. The causes coexist, so keep going rather than stopping at the first explanation.
   - Then read the ignore layers — `fs.read({"path":".copytreeignore","maxBytes":20000})`, then `fs.read({"path":".gitignore","maxBytes":20000})`, skipping whichever is absent. Both use gitignore syntax, both are always in force, and no selection axis escapes them. Check every intended path against them and split the selection: paths an ignore rule explains, and paths still eligible. Switching axes NEVER rescues an ignored path. Drop those from the retry, then either re-select evidenced content outside the ignored area or report the shortfall plainly; when nothing eligible is left, report and stop.
   - Correct what stays eligible, once. Bare directories move to `scopePaths` with both file matchers omitted — but only when directories are all that is left; when any other eligible file selector remains — an exact path OR an evidenced file-family glob — put the directory `/**` globs and every file selector together on `includePaths` and omit `scopePaths`, or the scope will AND those files away. A selection that was already correctly shaped does NOT justify broadening to `scopePaths` — recheck each path against the step 3 evidence and correct the specific mismatch on the axis it already sits on. With no concrete correction to make, report the empty result rather than guessing at another call.
   - Read `stats` again after that one corrected call. A second empty result is where you stop: report it, never fire a third call at an empty selection. THREE generate calls is the ceiling for the whole request whatever the mix of outcomes — the initial call, at most one diagnosed empty-result correction, and at most one regeneration after a populated result. Neither branch refills the other, and no diagnosis, however good, buys a fourth.

   Whether the bundle came from the first call or the corrected one, read the rest of the flags before reporting: `truncated` with `truncatedCount` and `truncatedBy` says a budget cut files (maxFileCount, maxTotalSize or charLimit — whichever bit first); `budgetExceeded` means the retained set is still larger than `maxTotalSize` and can be true even without truncation; `estimatedTokens` is accurate to about ±20%. A truncated or budget-exceeded bundle is never reported as complete — drop a cap YOU invented, narrow the selection on evidence, or report the shortfall plainly; a limit the USER named is theirs, so ask before raising it rather than quietly widening the bundle past it. Regenerating to clear a truncation is expected and correct; decide the whole fix at once (selection AND budget together) rather than probing one variable per call. The same goes for a bundle that came back populated but WRONG — missing the core files the user asked for, or padded with a directory they did not. Do not report that and work around it in prose: fix the shape and regenerate ONCE, within the ceiling above, and that regeneration is terminal whatever it returns. Regenerate only on demonstrated evidence — a flag that fired, or a concrete gap between the selection you froze in step 4 and the arguments you actually sent. A merely surprising `fileCount` is not evidence; if the bundle matches what you asked for and the user wanted something else, deliver it and say so. The returned `filePath` is a system temp file OUTSIDE the project, so `fs.read` cannot open it — you cannot inspect the bundle after the fact, which is why the selection is settled with evidence in step 3 and the stats are read carefully here.

Confirmation: `copyTree.generateAndCopyFile` is system-tier and always confirms — it replaces whatever the user currently has on their clipboard. Confirm per the active tier before sending it. `copyTree.generate` is read-tier and needs no confirmation.

Preflight the final options for secrets, generated/vendor bulk and unrelated force-includes; ignore layers are not a security guarantee. Do not include credential-bearing files merely because `always` or a glob could reach them. State whether curation was performed in the exact target checkout. The delivery result and stats verify generation, not that a consumer has received/read the bundle; keep the returned filePath distinct from an assistant artifact id.

Report back: the run label you supplied as `name` (read verbatim in the completion notice and the toolbar's recents dropdown), the resolved worktree, the settled selection (paths, globs, scopes) and the evidence behind it, the returned filePath, fileCount / outputBytes / estimatedTokens, every budget flag that fired (truncated, truncatedBy, budgetExceeded, noFilesMatched, plus unmatchedSelector when it was returned), any requested content you found an ignore rule keeping out and the shortfall that leaves, and whether the bundle landed on the clipboard — never a table.
