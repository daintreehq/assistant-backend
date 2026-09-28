---
id: daintree.forge.draft-release-notes
title: Draft release notes or a changelog entry
summary: Anchor the range to the previous release tag, collect exactly the PRs and commits that landed in it, group and rewrite them for readers with breaking changes first, and write the notes where asked, tagging and publishing nothing.
whenToUse: 'Use when the user wants release notes, a changelog entry or a "what''s new" written for a version or release — "write the release notes for 2.5", "update CHANGELOG.md for this release", "what changed since the last tag?", "draft the GitHub release", "summarise the merged PRs for the announcement". The deliverable is reader-facing notes (a file, a CHANGELOG section or a draft release) with every line traceable to a merged PR or commit in an explicit tag-to-target range, plus what was left out and why. Do not use for a personal or team activity report over a period of time (use daintree.git.summarize-activity), to find whether one specific thing ever shipped (use daintree.workflow.find-issue-work), or to merge the release pull request (use daintree.forge.land-pull-request).'
risk: external
requiredTools:
  - worktree.list
  - agent.launch
optionalTools:
  - forge.getPR
  - terminal.sendCommandOwned
---
The range decides everything: notes are the commits between two refs, not "PRs merged since a date". A visible agent writes the notes; you anchor the range, brief it, and verify that every line traces to that range.

Procedure:
1. Checkout: the `isMain` row of `worktree.list` (or the release branch's worktree). Run `git fetch --quiet --tags origin` there.
2. Anchor the range:
   - Target: the ref the user named (a release branch, `main`, a tag already cut), resolved to a SHA with `git rev-parse`.
   - Start: the user's previous version, else from `git -C <path> tag --merged <target> --sort=-v:refname` the highest tag on the same release line, skipping a tag on the target itself and pre-release tags (`-rc`, `-beta`) unless the user wants them. Two plausible anchors, or none: ask.
   Record both tags and SHAs; they go at the top of the report.
3. Launch one writer with `agent.launch` in that worktree (`name` "Release notes: <version>", `notify:true, handback:true`), then end the turn. The brief carries the range (tags and SHAs), the audience (users by default), the destination (reply text, a file path, or the CHANGELOG section, uncommitted) and these rules:
   - "Collect with `git log --first-parent --format='%H%x09%s' <start sha>..<target sha>`. A `(#123)` suffix or `Merge pull request #123` is a lead: confirm each number with `gh pr view <n> --json number,title,body,labels,author,baseRefName`; not a PR, or merged into another branch: a plain commit. A commit with no number: `gh api repos/<owner>/<repo>/commits/<sha>/pulls --jq '.[].number'`, none back is a plain commit. Never use a date-filtered PR list to decide membership.
   - Classify from labels, conventional prefixes (`feat`, `fix`, `perf`, `feat!`) and bodies (`BREAKING CHANGE`, migration notes); unclear, read `gh pr diff <n>`. A revert and its original both in range: drop both; a revert of an earlier release: a fix line.
   - Breaking changes and required actions first, then features, fixes, performance, other. Rewrite titles as effects, not `chore(ci): …`; for users drop internal-only items (CI, refactors, tests, docs) and list them as excluded. Each line ends with its PR (`#123`) or short SHA, and the author if the repo does so. Follow the existing CHANGELOG's format exactly.
   - Edit only the destination file. Do not commit, push, tag, publish or bump a version. Report the counts, exclusions and anything unclassified."
   A draft release only if the user asked, added to the brief: "`gh release list --json tagName,isDraft` first: one for this tag already exists, stop and report it. Then `gh release create <tag> --draft --verify-tag --target <target sha> --notes-file <file>` (drop `--verify-tag` only if the user wants a tag not yet pushed). An error or lost reply: list again before any retry; never a second draft."

```json tool=agent.launch
{"agentId":"claude","name":"Release notes: 2.5","worktreeId":"<worktree id>","prompt":"<range tags and SHAs, audience, destination, rules above>","notify":true,"handback":true}
```

On notice or resume: a notice is the writer's stop, not acceptance. A question within the brief gets one answer by `terminal.sendCommandOwned` with `notify:true, handback:true`. Verify yourself:
- Every cited `#n` or SHA comes from the range: its number in `git -C <path> log --first-parent --format=%s <start sha>..<target sha>` or a mapping you re-ran; a doubtful one, `forge.getPR` must show `merged` and the release `baseRef`. Nothing invented, nothing from outside the range.
- Every `BREAKING`, `!:` or migration item in the range subjects is listed first.
- `git -C <path> status --porcelain` shows only the destination file; HEAD unchanged; `git ls-remote --tags origin <tag>` shows no tag the user did not have; a draft's URL from `gh release view <tag> --json url,isDraft`.
One corrective round for a gap; then report what remains.

Stop and recovery: never relaunch a writer that is still working; never retry a draft release without listing first.

Report back: the range (start tag and SHA, target ref and SHA), where the notes went (reply, file path, draft release URL), how many PRs and plain commits are included, what was excluded and why (internal, reverted, other branch), anything unclassified, what you verified versus the writer's claims, the writer's terminal id, and that nothing was tagged or published.
