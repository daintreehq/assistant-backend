---
id: daintree.hosts.integrate-for-hub
title: 'Merge the hub''s pinned commits into one integration branch'
summary: 'As the integrator the hub chose, merge several pinned commits from other hosts'' branches one at a time, in the given order, into a new integration branch on this host, with checks after each merge and a stop at any conflict the brief does not settle.'
whenToUse: 'Use when the hub has chosen this lane as its integrator: several finished commits from other hosts'' or units'' branches, each pinned by SHA, to merge in order into one new integration branch, as in "integrate these exact commits from the other hosts", "combine the named swarm branches at their recorded SHAs" or "integrate remote artifacts in this lane''s project". The deliverable is that integration branch in its own worktree: a visible integrator agent merges each pinned commit (never a branch''s newer tip) one at a time with checks after each; a missing commit, a merge conflict the brief does not settle or an acceptance clash goes back as a question, never resolved by guessing; no source branch is rewritten. Do not use for the hub''s request envelope (daintree.hosts.handle-hub-message), a unit built from scratch (daintree.hosts.do-delegated-work), checks alone (daintree.hosts.verify-for-hub), the person''s own local merge (daintree.git.integrate-agent-branches) or the hub''s side (daintree.hosts.delegate-to-host).'
risk: git
requiredTools:
  - actions.getContext
  - worktree.list
  - terminal.list
  - worktree.createWithRecipe
  - worktree.waitUntilReady
  - agent.launch
  - assistant.reply
optionalTools:
  - terminal.sendCommand
  - terminal.getStatus
---
Message and reply: daintree.hosts.handle-hub-message.

1. From the brief: repository, base SHA, inputs in order (branch, SHA, acceptance), checks, integration branch, CLI, push or PR authority and target. Missing, short or a remote not `git remote get-url origin` here: ask back. Git: `git -C <project path>` (`actions.getContext`).
2. `git fetch --prune origin`; a pin, base too, that `git branch -r --contains <SHA>` lists nowhere: ask back for a ref holding that commit, never its tip. Keep each `git ls-remote origin refs/heads/<branch>`: a tip off its pin is a finding; the pin merges.
3. Reconcile: the integration branch's worktree (`worktree.list`), HEAD descending from the base, and its integrator (`terminal.list`) continue; the branch existing otherwise: ask back. Else create, then `worktree.waitUntilReady` on its id while `pending` or `running`, at most three times; only `ready` proceeds:

```json tool=worktree.createWithRecipe
{"source":{"kind":"newBranch","branchName":"<integration branch>","baseBranch":"<base SHA>","collisionPolicy":"error"}}
```

4. Launch the integrator there, `notify:true, handback:true`. Brief: pins, acceptance, checks, authority, and "Checks on the base, then per input in order: `git merge --no-ff --no-edit <SHA>` (already in: note it), the checks, HEAD before and after. A conflict the brief does not settle, or a check failing or not running: record files, output and SHA; `git merge --abort` if a merge is open, else `git reset --hard <HEAD before>`; stop. Never guess, touch a source branch or force; push or PR only as authorised." End the turn.

On notice or resume: worktree clean, no merge in progress; `git log --first-parent --reverse --format='%H %P' <base>..HEAD` shows the merges in input order, second parent each SHA; a noted input passes `git merge-base --is-ancestor <SHA> HEAD`; checks rerun on HEAD; `ls-remote` again: moved or deleted sources, the integration branch absent unless pushed (then HEAD). Reply `completed`: branch, HEAD, base, per input SHA, merge, checks and tip; worktree and pane. A stop or missing input: `input-required` with the evidence, last good HEAD and inputs left. The hub's answer, a new exchange: re-read the worktree, send it once (`terminal.sendCommand`, `notify`, `handback`); reply to that exchange.

Stop and recovery: one worktree, integrator and correction round; never replay a merge.

Report back: the reply as sent.
