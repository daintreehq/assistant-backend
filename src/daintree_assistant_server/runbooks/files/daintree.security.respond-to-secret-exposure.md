---
id: daintree.security.respond-to-secret-exposure
title: Respond to an exposed secret
summary: Contain a leaked credential — find every place it reached without repeating it, stop agents spreading it further, have its owner rotate it first, then clean up only what the user authorizes and re-check.
whenToUse: 'Use when a credential, API key, token, password or private key has LEAKED somewhere it should not be — "I pasted my Stripe key into the prompt", "an agent committed the .env", "the CopyTree bundle had our AWS keys in it", "there''s a token in the PR description", "did that key get pushed?". The deliverable is containment: every place the secret reached (worktree files, git history on each branch, remotes, pull requests and issues, context bundles, agent transcripts, saved scrollback, logs), each marked local only, pushed or public; running agents told not to spread it; the owner told to rotate it first; cleanup only as authorized, then re-checked. The secret itself is never repeated. Do not use for a security audit of existing code (use daintree.review.audit-codebase), judging a diff on its merits (use daintree.review.review-change), saving or forgetting a remembered rule (use daintree.memory.manage-project-memory), or picking files for a context bundle (use daintree.context.curate-copytree-bundle).'
risk: external
requiredTools:
  - worktree.list
  - terminal.list
  - terminal.getStatus
  - agent.launch
optionalTools:
  - terminal.sendCommandMany
  - terminal.interruptOwned
  - terminal.sendCommandOwned
---
Never write the secret anywhere: chat, briefs, commands, checkpoints, reports. Name it by kind and variable (`STRIPE_SECRET_KEY`), at most its prefix and last four characters; never use or test it. Search by pattern, never the value: the provider's format (`sk_live_[0-9A-Za-z]{20,}`, `AKIA[0-9A-Z]{16}`, `-----BEGIN [A-Z ]*PRIVATE KEY-----`), ending in the known last four where given. A match is a candidate (placeholders, the new key).

Procedure:
1. From the user's words: kind, provider, variable, where first seen, checkouts involved (`worktree.list`). A value pasted here makes this transcript a location.
2. Rotation first, in your first reply: one pushed, in a PR or pasted to a hosted agent is compromised whatever is cleaned. The owner revokes, reissues and installs the new one, never via an agent. Ask, then go on to steps 3-4 in the same turn: rotation gates only cleanup.
3. Stop the spread: `terminal.list`, `terminal.getStatus` for agents in those checkouts. One you own committing, pushing or bundling now: `terminal.interruptOwned`, then read its screen (a request, not a stop). Tell every agent you own in one call; one that does not acknowledge is uncontained. Name the others to the user. Bundle and push nothing yourself.

```json tool=terminal.sendCommandMany
{"sends":[{"terminalId":"<owned agent terminal id>","command":"A credential (<kind, variable>) was exposed. Do not print, copy, commit, bundle or pass it to any tool; no push or PR until told. Reply only: acknowledged."}],"waitForReply":true,"handback":true,"waitSeconds":120}
```

4. Inventory: one read-only agent in the main checkout (`name` "Secret inventory", `notify:true, handback:true`); end the turn. Brief: the pattern and variable (never the value), every worktree path, and:
   - "Listing modes only (`-l`, `-c`, `--name-only`): never print a matching line; never `git show` or open a hit. `git fetch origin` first; `git log --all -G'<pattern>' --format='%H %cI %an' --name-only`; per hit `git branch -r --contains <sha>`, `git tag --contains <sha>`; `gh repo view --json visibility`.
   - `grep -rlE -e '<pattern>' --` over each worktree (ignored files too; skip `.git`, dependencies), `$TMPDIR/daintree-context/` (CopyTree bundles), `~/.claude/projects` and `~/.codex/sessions` (transcripts), `terminal-sessions` in Daintree's user-data folder (`$DAINTREE_USER_DATA`, else `~/Library/Application Support/Daintree`; saved scrollback), shell history, logs.
   - PRs and issues named or holding a hit's branch: `gh pr view <n> --json body,comments | grep -cE -e '<pattern>'` (`gh issue view` likewise); CI runs of those commits: `gh run view <id> --log` piped the same way. A failed, killed or unfinished search is unsearched, not clean.
   - Change, delete, commit or push nothing. Mark each candidate this credential or not, unprinted. Reply per location: path, ref or number; confirmed, suspected, searched with no match, or unsearched; local only / pushed (which remote branch) / public."

On notice or resume:
1. Verify exposure: each pushed hit with `git -C <path> branch -r --contains <sha>` after `git fetch`; re-run one listing per location type called clean. `git status --porcelain` changed: a boundary failure. A screen showing the value adds that terminal's scrollback and transcript.
2. Report inventory and rotation; ask only about cleanup not yet authorized:
   - Working files: the literal replaced by an env-variable or secret-store reference, never moved elsewhere; an ignore rule; commit only if asked.
   - Local-only commits: rewritten before any push.
   - Pushed history: `git filter-repo` and a force-push, never before rotation; forks, clones and PR caches keep old commits.
   - PR or issue text: `gh pr edit`/`gh issue edit`; edit history keeps the old text.
   - Bundles, transcripts, logs: named files deleted. Scrollback: only after its terminal closes (else re-saved).
   The authorized list goes to one agent in the affected worktree (`agent.launch`, `notify:true, handback:true`), never-print rule included; end the turn.
3. On its notice: re-run the listings yourself per cleaned location (every candidate explained); after a force-push, `git ls-remote origin` tips equal the rewritten local ones; `git status` for stray changes. One corrective round.

Stop and recovery: one inventory agent, one cleanup agent. No force-push, rewrite, branch deletion or forge edit without the user's word for that item and its exact command. Deleting a file never makes a pushed secret safe.

Report back: the credential by kind and variable only; each location and status, verified or claimed; rotation confirmed or pending (if pending, never "contained"); agents that acknowledged, did not, or only the user can reach; cleanup verified, declined or pending; what stays exposed regardless (forks, clones, the model provider, logs).
