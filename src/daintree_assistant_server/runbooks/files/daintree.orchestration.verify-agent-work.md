---
id: daintree.orchestration.verify-agent-work
title: Verify an agent actually did the work
summary: Test an agent's "done" against evidence — cheap git reads, the diff, checks you run yourself, a separate reviewer when it needs real inspection — then route on verified, unverified, or contradicted.
whenToUse: Use when the user wants a finished agent's work checked before it is trusted — "did it actually do it?", "check that the agent did what I asked", "verify this before I merge", "it says the tests pass, confirm that", "I don't trust that result" — or when an agent reports done and the next step is irreversible (commit, push, merge, close, hand off). Also to judge a claim against acceptance criteria, or when the tail seems to contradict what the agent said. Do not use to sweep the whole fleet rather than judge ONE result (daintree.orchestration.supervise-fleet), to ready a branch or open a PR (daintree.workflow.prep-branch-for-review), to forward findings to an implementer (daintree.agents.relay-review-feedback), to wait on an agent still running (daintree.orchestration.watch-outcome), for "where were we" across sessions (daintree.orchestration.status-and-resume), or to JUDGE a change on its merits rather than check a claim about it (daintree.review.review-change).
risk: terminal
requiredTools:
  - worktree.list
  - terminal.getStatus
  - terminal.getOutput
optionalTools:
  - terminal.readLastMessageOwned
  - terminal.notifyWhenIdle
  - project.detectRunners
  - project.runCheck
  - workflow.prepBranchForReview
  - worktree.reviewReadiness
  - forge.getPR
  - forge.getCIStatus
  - agent.listAvailable
  - agent.launch
---
Use when: an agent says it is done and the next move is irreversible or depends on trusting it: commit, push, open or merge a PR, close an issue, hand the worktree on, or tell the user it is finished. This answers one question: did the agent actually do what was asked?

Availability: Hybrid. You read the diff and run the checks yourself with git and your shell in the agent's worktree path; that is the default. From the app: `terminal.getStatus`, `terminal.getOutput` and `terminal.readLastMessageOwned` for the agent's state and claim, and `worktree.list` for its checkout. These optional app reads raise an approval in a core pane: `project.detectRunners` and `project.runCheck`, `workflow.prepBranchForReview` (read-only despite its name), `worktree.reviewReadiness`, and `forge.getPR` / `forge.getCIStatus` for a PR. Use a separate reviewer agent only when a criterion needs deep reading or the user asks for one.

Keep four levels of evidence apart: the agent's claim; confirmation that it stopped; results you ran against a known revision; an independent review. The author confirming its own work adds nothing.

Procedure:
1. Fix the claim, target and contract: the terminal id, the worktree id and path, the original assignment and its acceptance criteria (your launch prompt or checkpoint, otherwise the user's words), and the baseline revision recorded at launch. If no baseline was recorded, mark it unknown; never reconstruct one from a clean tree or from the author's later account. Turn the criteria into checkable statements and keep every required one: "did it work?" is not checkable, "the endpoint exists, the suite passes, nothing outside the module changed" is.
2. Confirm it stopped. Read `terminal.getStatus` with a short output tail. `working` or `directing` is not done, and a half-written tree proves nothing: if the user wants the verdict when it stops, arm `terminal.notifyWhenIdle` with a note naming this verification, and end the turn. A pending question or approval means it is not done either.
3. Read the claim: `terminal.readLastMessageOwned` for a Claude Code pane, `lastHandback` from the status, or `terminal.getOutput`. Write down exactly what it says it changed and which commands it says passed. `lastCheckResult` is parsed from its output, not an exit code.
   If the terminal exited or was closed and its output is gone, the claim is lost: say so, treat the result as unknown, and verify from the worktree alone (steps 4 to 6). Never reconstruct what it probably said.
4. Check the tree yourself, in the agent's worktree path, never your own: `git status`, and `git diff` and `git log` against the baseline, with the changed files compared to the agreed scope. A clean tree means nothing is pending, not that nothing was done: the work may be committed, or may have existed already. `workflow.prepBranchForReview` and `worktree.reviewReadiness` say whether the tree is in a committable shape (an `unknown` there is unknown), never whether the right thing was built.
5. Run the claimed checks yourself in that path, at the recorded revision. `project.runCheck` with `cwd` set to the worktree path is the app alternative (runner id from `project.detectRunners`, never a server): read `passed`, `timedOut`, `aborted`, `outputTruncated` and the returned `command`. "Tests pass" with no command and no output is unverified until you have run them. A timeout or abort is unverified, not a failure.
6. For a PR, read it with `forge.getPR` and its CI with `forge.getCIStatus`, but CI only counts for the exact head commit you are verifying: the roll-up does not name its commit, so confirm the head SHA with git or the forge CLI. PR review threads and job logs come only from the host's permitted forge CLI; if you did not read them, say so.
7. When a criterion needs deep inspection, read the diff against it yourself, or launch a separate read-only reviewer (never the author) in the same worktree. Its prompt is the criteria and the revision, and asks for PASS, FAIL or UNVERIFIED per criterion with `file:line` or check evidence.

```json tool=agent.launch
{"agentId":"<id from agent.listAvailable>","name":"verify: <task>","worktreeId":"<agent's worktree id>","prompt":"<criteria, revision, per-criterion verdict format, read-only boundary>","handback":true,"notify":true}
```

8. Route on what you actually have:
   - VERIFIED: evidence supports every required criterion for this revision. A truthful "no changes needed" is verified when you confirm the required behaviour already exists by reading it and running its check; an empty diff alone neither proves nor refutes it. Continue to the next step only if the user already authorized it; otherwise offer it.
   - UNVERIFIED: the evidence is missing, lost, partial or silent. Name what is missing and the one thing that would settle it. Criteria that pass while others remain untested leave the whole result unverified: list which is which. Never propose commit, push, merge or close on unverified work, and never round "could not check" up to "looks fine".
   - CONTRADICTED: the evidence disagrees with the claim: required behaviour demonstrably absent against the baseline, a failing result on the claimed revision, or changes outside the agreed scope. State what was claimed and what the evidence shows, then relay the gap to the implementer with daintree.agents.relay-review-feedback rather than fixing it yourself, unless the user asked you to fix it.

The cheapest verification is arranged at launch: put checkable acceptance criteria in the child's prompt and record the baseline revision in the checkpoint before it starts.

On notice or resume: follow the foundation's resume procedure. For the agent being verified, continue from step 2. For a reviewer, read its per-criterion report, check any FAIL or surprising PASS against the source, and combine it with your own results in step 8; a review is evidence alongside the checks you can run, not a replacement for them.

Stop and recovery: one reviewer per verification. An ambiguous launch is reconciled with `terminal.list`, never relaunched. Do not re-run the agent's task to see whether it works, and do not fix the gap on your own initiative.

Report back: the claim tested, each check you ran with its result and the revision, the outcome per criterion and overall (verified, unverified or contradicted), the specific gap if contradicted, anything still running with its terminal id, and the next step you are offering.
