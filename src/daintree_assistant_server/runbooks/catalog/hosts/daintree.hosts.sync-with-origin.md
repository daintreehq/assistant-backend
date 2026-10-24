---
id: daintree.hosts.sync-with-origin
title: Bring each remote host's checkout up to date with origin
summary: 'From this Shell, fetch origin and fast-forward a repository''s checkout on each chosen Remote Host (and this machine if asked) to origin''s base branch, never switching, stashing, resetting or cleaning, and report each machine''s branch and SHA against origin, holding any that cannot fast-forward.'
whenToUse: 'Use when the person wants a repository''s checkout on their other machines brought UP TO DATE with origin, once or before each batch of a run: "tell every host to pull develop", "make sure both studios are on the latest main before the next batch", "fast-forward the main checkout on each machine", "are my hosts behind origin?", "new work on the hosts must start from the latest develop". The deliverable is, per machine, whether its base checkout now matches origin''s after a fetch and fast-forward, and any machine that could not fast-forward (uncommitted changes on the base, diverged, unreachable) held with the reason, nothing switched, stashed, reset, cleaned or force-updated. Do not use for the same coding work on every host (daintree.hosts.swarm-fan-out), one task handed to one host (daintree.hosts.delegate-to-host), why two machines behave differently (daintree.hosts.diagnose-host-differences), bringing a feature branch or worktree up to date with its base (daintree.git.sync-with-base), or copying settings between machines (daintree.hosts.copy-portable-settings).'
risk: external
requiredTools:
  - host.list
  - host.call
hostEligibility: connected
---
Fast-forward only: a checkout that needs anything more is reported, never forced. The target is origin's base as your shell reads it: `git ls-remote origin refs/heads/<base>`.

Procedure:
1. Machines: the hosts the person named, else every `connected` host with `control` in `host.list`; this machine only when asked, the same command in your shell in its checkout. On each host, the project whose `git remote get-url origin` names the repository (https or ssh, `.git` or not); none: a gap, never a clone.
2. One call per host: the command runner there, as the host's user in the project's root; `timeoutMs` keeps the relay waiting past its 90-second default:

```json tool=host.call surface=help-core
{"hostId":"<hostId>","projectId":"<projectId>","tool":"project.runCommand","args":{"projectId":"<projectId>","command":"<sync command>","timeoutMs":300000}}
```

   The sync command, `<base>` filled in, fetches the base, then fast-forwards the checkout only when it is on the base with no uncommitted tracked changes, else only its local `<base>` ref (git refuses that while another worktree has it out), and prints the branch and both SHAs:

```bash
git fetch --quiet origin "+refs/heads/<base>:refs/remotes/origin/<base>" || exit $?; if [ "$(git branch --show-current)" != "<base>" ]; then git fetch --quiet origin "<base>:<base>"; elif [ -n "$(git status --porcelain --untracked-files=no)" ]; then echo "uncommitted changes on <base>"; false; else git merge --ff-only --quiet "origin/<base>"; fi; s=$?; git branch --show-current; git rev-parse "refs/heads/<base>" "refs/remotes/origin/<base>"; exit $s
```

3. Read `result.structured`: `exitCode`, `timedOut`, and the last two lines, the local and remote base SHAs. Up to date: exit 0 and both at the target. Otherwise held, with its last output lines: never switch, stash, reset, clean or force to make it pass; the fix is the person's. `ambiguous`: resend under its `callId`; `expired`: run it again (it is safe to repeat); other outcomes as the hosts foundation reads them.
4. If origin moved meanwhile (`git ls-remote` again), sync every machine once more against the new target.
5. A fast-forward installs nothing. If it moved a lockfile or manifest while agents there are running checks, say so; reinstalling under them is the person's call, at a quiet moment.

Report back: per machine its branch, its base SHA after the sync beside origin's, up to date or held and why with its last lines, and any machine not reached.
