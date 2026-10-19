---
id: daintree.orchestration.review-until-approved
title: Iterate a change between implementer and reviewer until approved
summary: Run a bounded writer/reviewer loop — a read-only reviewer judges a frozen revision, only blocking findings go back to the implementer, the reviewer re-checks — until it approves the current revision, progress stops, or the round cap is reached.
whenToUse: 'Use when a change is to go back and forth between an implementer and a reviewer agent UNTIL the reviewer approves — "have Codex review Claude''s work and keep going until Codex is happy", "loop review and fix until there are no blocking issues", "Claude writes, Codex reviews, repeat until clean", "keep iterating till it passes review". The deliverable is an approved revision, or an honest not-approved with the stop reason, plus each round''s blocking findings and fixes and your own checks on the final revision. Do not use for one pass of findings forwarded to a fixer (use daintree.agents.relay-review-feedback), for findings with no fixing (use daintree.review.review-change), for checking whether a finished agent did what it claimed (use daintree.orchestration.verify-agent-work), for approval of a plan before any code (use daintree.orchestration.plan-then-implement), for an agent trying to break the change (use daintree.review.red-team-change), for review threads left on a PR (use daintree.forge.address-pr-review), or for rival versions of the change (use daintree.orchestration.competition).'
risk: project
requiredTools:
  - agent.launch
  - terminal.sendCommandOwned
  - terminal.getStatus
---
On a host: shell steps via its command runner, or delegate.

Procedure:
1. Rules before round 1. Implementer: a terminal you own or were handed, or one you launch with the task (`notify:true, handback:true`; round 1 starts on its notice). Reviewer: the agent the user named, else a different kind from the implementer; never the implementer reviewing itself. The base the change is measured against, resolved to a SHA; the criteria; and what blocks: correctness, security, failing or missing tests, the user's named concern; style and nits never block. Round cap: the user's, default 3. A round is counted when its review request goes out.
2. Freeze the candidate. The implementer must be stopped: `terminal.getStatus` with `terminalIds:[<implementer>]`, `includeOutput:{lines:50}`, showing it at its ordinary prompt. A question, approval, error or unknown state blocks the review: answer within scope or ask the user. The two agents never work at the same time. In its worktree take the fingerprint: `git rev-parse HEAD`, `git diff HEAD --binary | git hash-object --stdin`, and `git ls-files -o --exclude-standard | git hash-object --stdin-paths | git hash-object --stdin` for untracked files. That is revision N.
3. Review. Round 1 launches the reviewer once, read-only, in the implementer's worktree, with the standing role in `systemPrompt` (Claude and Codex only; for others put it at the top of `prompt`):

```json tool=agent.launch
{"agentId":"<reviewer>","name":"Review loop: <task>","worktreeId":"<implementer worktree id>","systemPrompt":"You review; never edit files or change state. Per finding: BLOCKING or NIT, an id (B1, B2… kept across rounds), file:line, the trigger, the fix. End with exactly APPROVED, or CHANGES REQUESTED when any BLOCKING finding stands.","prompt":"Review the change against <base sha> for: <criteria>. BLOCKING means: <blocking rule>.","waitForReply":true,"handback":true,"waitSeconds":900}
```

   Later rounds go to the same terminal with `terminal.sendCommandOwned` and the same wait: "Revision changed. The implementer says it touched <files>. Re-check blocking findings <ids>, review what changed, and report any new blocking finding." add any evidence the implementer gave against a finding. Never name who wrote the code, and never resend the whole history. `outcome:"timeout"` means still reviewing: recheck as the foundation says. Afterwards re-take the fingerprint: if it moved, the tree changed during the review and its verdict is void; report the changed paths and stop, reverting nothing, since who changed them is unknown.
4. Judge. Only an explicit APPROVED over an unchanged fingerprint counts. No verdict, both verdicts, or CHANGES REQUESTED with no BLOCKING finding: one clarification send, then NOT APPROVED. Keep the nits for the report. On approval go to step 6.
5. Fix round. Send only the blocking findings, with their ids, to the implementer with `terminal.sendCommandOwned`, `notify:true, handback:true`: "Fix these blocking findings and nothing else. For each, reply with what you changed, or evidence that it is wrong. Do not weaken, skip or delete tests to get past one." End your turn.
6. Final check: run the relevant tests yourself on the approved revision in that worktree, then re-take the fingerprint. Approval is not acceptance; a failing check is a blocking finding for the next round if the cap allows, otherwise the result is NOT APPROVED.

On notice or resume:
1. The implementer's reply is its claim. Go to step 2 for the next revision, then step 3. A question: answer only from the findings and the task's scope; anything else goes to the user.
2. A finding the implementer disputes goes to the reviewer once with that evidence; if the reviewer still holds it, the user decides, and the loop waits.
3. Beyond two rounds, checkpoint as the foundation says: round, revision, both terminal ids, open findings by id.

Stop and recovery: stop at the cap (stale verdicts and final-check failures never extend it), when the same blocking id survives two fix rounds, or on cancellation, and report the open blocking findings.

Report back: verdict APPROVED or NOT APPROVED with the stop reason; the worktree, base SHA and final fingerprint; per round the blocking findings, what the implementer changed and the reviewer's re-check; the nits collected; disputes and who settled them; your own check results; both terminal ids.
