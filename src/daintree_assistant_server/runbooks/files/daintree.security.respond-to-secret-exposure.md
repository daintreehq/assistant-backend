---
id: daintree.security.respond-to-secret-exposure
title: Respond to an exposed secret
summary: Contain a leaked credential — find every place it reached, here and on the Remote Hosts' assistants, without repeating it, stop agents spreading it, have its owner rotate it first, then clean up only what the user authorizes and re-check.
whenToUse: 'Use when a credential, API key, token, password or private key has LEAKED somewhere it should not be — "I pasted my Stripe key into the prompt", "an agent committed the .env", "the CopyTree bundle had our AWS keys in it", "there''s a token in the PR description", "did that key get pushed?", "a password went out in the task I sent my other machines". The deliverable is containment: every place the secret reached (worktree files, git history on each branch, remotes, pull requests and issues, context bundles, agent transcripts, saved scrollback, logs, and on the hub the Remote Hosts'' assistants that received a message carrying it), each marked local only, pushed or public; running agents, local or on other hosts, told not to spread it; the owner told to rotate it first, each host''s own credentials separately; cleanup only as authorized, then re-checked. A host that does not acknowledge stays unresolved. The secret itself is never repeated. On a Remote Host it is that project''s share, when the hub''s message reports an exposure. Do not use for stopping or changing a run when nothing leaked (daintree.hosts.steer-swarm), a security audit of existing code (use daintree.review.audit-codebase), judging a diff on its merits (use daintree.review.review-change), saving or forgetting a remembered rule (use daintree.memory.manage-project-memory), or picking files for a context bundle (use daintree.context.curate-copytree-bundle).'
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
  - host.listTasks
  - host.getTask
  - host.cancelTask
  - host.sendMessage
  - assistant.reply
---
Never write the secret anywhere: chat, briefs, commands, checkpoints, reports, host messages. Name it by kind and variable (`STRIPE_SECRET_KEY`), at most its prefix and last four characters; never use or test it. Search by pattern, never the value: the provider's format (`sk_live_[0-9A-Za-z]{20,}`), ending in the known last four.

Procedure:
1. From the user's words: kind, variable, checkouts (`worktree.list`); on the hub, the sent tasks it may have ridden. A value pasted here makes this transcript a location.
2. Rotation first, in your first reply: one pushed, in a PR, pasted to a hosted agent or sent to another machine is compromised whatever is cleaned. The owner revokes, reissues and installs the new one, never via an agent; each host's keys are separate. Ask, then go on to steps 3-4 in the same turn: rotation gates only cleanup.
3. Stop the spread: `terminal.list`, `terminal.getStatus` for agents in those checkouts. One you own committing, pushing or bundling now: `terminal.interruptOwned`, then `terminal.getStatus` without output (a request, not a stop). Tell every agent you own in one call: "A credential (<kind, variable>) was exposed: do not print, copy, commit, bundle or pass it to any tool; no push or PR until told. Reply only: acknowledged." Name the others to the user.
   Hub: recipients are your own sends: `host.listTasks` to `hasMore:false` (no text); `host.getTask` returns reply text, so read only containment tasks. `host.cancelTask` withdraws one only if it returns `canceled`; it erases no copy. One `host.sendMessage` per lane per phase, no value: that line without its reply clause, the original's `hostTaskId` and branch or PR, and "Follow daintree.security.respond-to-secret-exposure for this project's worktrees and sessions only: read-only inventory; one `assistant.reply` with each location, status and acknowledgement." Addressing: `get_runbook` daintree.hosts.steer-swarm. Read no host file; ask for no environment.
4. Inventory: one read-only agent in the main checkout; end the turn. Brief: the pattern and variable (never the value), every worktree path, and:
   - "Listing modes only (`-l`, `-c`, `--name-only`): never print a matching line, `git show` or open a hit. `git fetch origin`; `git log --all -G'<pattern>' --format=%H --name-only`; per hit `git branch -r --contains <sha>`, `git tag --contains <sha>`; `gh repo view --json visibility`.
   - `grep -rlE -e '<pattern>' --` over each worktree (ignored files too, not `.git`), `$TMPDIR/daintree-context*/`, `~/.claude/projects`, `~/.codex/sessions`, `terminal-sessions` in Daintree's user-data folder (`$DAINTREE_USER_DATA`, else `~/Library/Application Support/Daintree`), shell history, logs.
   - PRs and issues named or holding a hit's branch, and their CI runs: `gh pr view <n> --json body,comments | grep -cE -e '<pattern>'` (`gh issue view`, `gh run view <id> --log` likewise). A failed search is unsearched.
   - Change nothing; mark each candidate this credential or not. Reply per location: path, ref or number; confirmed, suspected, no match or unsearched; local only / pushed (which branch) / public."

On notice or resume:
1. Verify: each pushed hit with `git -C <path> branch -r --contains <sha>` after `git fetch`; re-run one listing per location type called clean. `git status --porcelain` changed: a boundary failure. A screen showing it adds that terminal's scrollback and transcript. Hub: `host.getTask` per containment task; a silent or `disconnected` host is unresolved.
2. Report inventory and rotation; ask only about cleanup not yet authorized: working files (literal replaced by an env or secret-store reference; ignore rule; commit only if asked), local-only commits (rewritten before any push), pushed history (`git filter-repo` and a force-push, never before rotation; forks and clones keep old commits), PR or issue text (`gh pr edit`/`gh issue edit`; history stays), named bundles, transcripts, logs deleted, scrollback (only after its terminal closes). The authorized list goes to one agent in the affected worktree (`agent.launch`), never-print rule included; end the turn. A host's goes to its lane in a new message.
3. On its notice: re-run the listings per cleaned location and `git status`; after a force-push, `git ls-remote origin` tips equal the rewritten local ones. One corrective round.

Stop and recovery: one inventory agent, one cleanup agent, one message per lane per phase. No force-push, rewrite, branch deletion or forge edit without the user's word for that item and its command.

Report back: the credential by kind and variable only; each location and status, verified or claimed; rotation confirmed or pending (never "contained" while pending); agents and hosts that acknowledged, did not, or only the user can reach; cleanup verified, declined or pending; what stays exposed.
