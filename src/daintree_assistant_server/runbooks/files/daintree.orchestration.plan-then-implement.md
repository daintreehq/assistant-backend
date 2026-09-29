---
id: daintree.orchestration.plan-then-implement
title: Plan, approve, then implement
summary: Have an agent plan without editing, get the plan approved by the user or a named reviewer agent, then have that plan implemented exactly as approved and check the result against it for drift.
whenToUse: 'Use when a PLAN or design must be written and approved BEFORE any code is changed, then built exactly as approved — "have Codex plan it and Claude build it", "show me a plan before it writes anything", "spec first, I sign off, then implement", "Claude plans, Codex approves, then Claude codes", "plan mode first". The approver is the user by default, or a reviewer agent the user names. The deliverable is the approved plan and an implementation checked against it for drift. Do not use when the plan or an opinion is all that is wanted (use daintree.agents.ask-and-relay), for one agent briefed straight into the work with no gate (use daintree.edits.spawn-visible-agent), for a multi-phase dependency graph tracked across sessions (use daintree.orchestration.execute-durable-workflow), for tests written first by a separate agent (use daintree.orchestration.test-first-split), for an interface agreed first and built by two agents in parallel (use daintree.orchestration.contract-first-split), for review rounds on code already written (use daintree.orchestration.review-until-approved), or to bring a chosen prototype up to production quality (use daintree.orchestration.productionize-prototype).'
risk: project
requiredTools:
  - agent.launch
  - terminal.sendCommandOwned
optionalTools:
  - worktree.createWithRecipe
  - worktree.waitUntilReady
  - terminal.getStatus
---
Procedure:
1. Roles. Planner: the agent the user named, else the configured one best suited. Implementer: the planner's own terminal (it keeps its context) unless the user named a different agent. Approver: the user, unless they named a reviewer agent (never the planner) or said "you decide". Worktree: the one named or current; a new one only when asked (`worktree.createWithRecipe` with a new branch, then `worktree.waitUntilReady` with its `worktreeId`, repeated while `pending` or `running`; any other state but `ready`: report it and launch nothing). Baseline there: `git rev-parse HEAD`, `git status --porcelain` and `git diff HEAD --binary | git hash-object --stdin`.
2. Plan, in one launch. Do not start the CLI in its own plan mode: its exit dialog is an approval only the user may answer.

```json tool=agent.launch
{"agentId":"<planner>","name":"Plan: <task>","worktreeId":"<worktree id>","prompt":"<task verbatim and acceptance criteria>. Plan only: change no files and run nothing that writes. Reply with the plan between BEGIN PLAN and END PLAN: approach; files to change and why; non-goals; risks; test plan; open questions, or 'none'. Under 4000 characters. Then stop and wait.","waitForReply":true,"handback":true,"waitSeconds":900,"replyLines":200}
```

   A reply marked `truncated`, or with no END PLAN, is incomplete: one send asking for it shorter, then stop and report. Re-take the baseline: any change is a boundary failure; report it and go no further without the user.
3. Gate. Keep the plan text exactly as it came back; every later step uses that text, never your summary of it.
   - The user approves (the default): show the whole plan with its open questions and end your turn. Approval is a plain go with every open question answered; a go with questions left open is not one.
   - A named reviewer agent: one read-only launch in the same worktree with `waitForReply:true, handback:true, replyLines:200`, carrying the task, criteria and plan, never who wrote it: "Review this plan against the task. Read the code; change no files. List the required changes, numbered. End with exactly `VERDICT: APPROVE` or `VERDICT: CHANGES`." On CHANGES, send its required changes verbatim to the planner (`terminal.sendCommandOwned`, same wait), then send the revised plan and what changed to the SAME reviewer terminal. A reply with no verdict line gets one clarification, then counts as not approved; praise is not a verdict. If the user also asked to see the plan, the reviewer's approval still waits for theirs.
   - "You decide": approve only a plan that covers every criterion, stays in scope and has no open questions; otherwise ask the user.
   At most two plan revisions in the whole run, deviations included; then the user decides.
4. Go, once per approved plan. Re-take the baseline first; if it moved since planning, tell the user what changed and settle the plan again before sending. The message: "Approved plan follows. Implement exactly this, in this worktree. If the plan proves wrong or incomplete, stop and report what and why before deviating. Run <tests>; reply with changed files and results." then the approved plan text, the user's answers, and any amendments as a numbered list. Send it to the planner's terminal with `terminal.sendCommandOwned`, or make it the `prompt` of a different implementer's `agent.launch` in the same worktree; either way with `notify:true, handback:true`. End your turn.

On notice or resume:
1. Waiting on the user's approval: their go (questions answered) takes the current plan to step 4. A stop to report a deviation: take it to whoever approved (user, reviewer once, or you under "you decide"), then back through the gate; a revised plan approved gets its own go. A question: answer only from the approved plan and the user's answers; anything else goes to the user.
2. A finish is a claim. Drift check: read the whole `git diff <baseline HEAD>` and every new untracked file, setting aside what the baseline already held, and map each change to a plan item; an unplanned file or behaviour is reported, never silently accepted. Run the plan's test plan and check each criterion yourself in that worktree. A failing criterion gets one corrective send within the approved plan; a fix needing a different plan goes back to the gate. A planner or reviewer approving the result is not verification.

Stop and recovery: two plan revisions, one go per approved plan, one corrective send. Nothing is implemented before approval, whatever the planner offers.

Report back: the approved plan (condensed), who approved it, answers and amendments; the reviewer's rounds if any; the implementer's terminal, worktree and baseline; the drift report (changes mapped to plan items, unplanned ones named); checks you ran versus claimed; anything pending.
