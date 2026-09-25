---
id: daintree.orchestration.interview
title: Run a judge-led interview competition among agents
summary: One agent interviews several others on a topic while you relay questions and answers — optionally with the judge verifying claims against the codebase and narrowing to a top 2 — until it names a winner.
whenToUse: Use when the user asks to have one agent interview, question, or grill several other agents on a topic and expects a winner — "have one agent interview the others and pick the best", "which of these should be my pair programmer?". Covers both depths, a light Q&A interview and a deep one where the judge verifies candidates' claims against the codebase, issues challenging follow-ups, and narrows to a top 2 before the verdict. Do not use for a plain one-round poll with no interviewer (use daintree.agents.poll-pinned-agents), a scoring-only competition where entries are judged rather than questioned (use daintree.orchestration.competition), or an open-ended dialogue with no judge and no winner (use the daintree.orchestration.conversation runbook).
risk: project
requiredTools:
  - agent.listAvailable
  - agent.launch
  - terminal.sendCommandOwned
  - terminal.getStatus
  - terminal.getOutput
optionalTools:
  - agent.listPresets
  - worktree.list
  - terminal.list
  - terminal.readLastMessageOwned
  - terminal.waitUntilIdleBatch
  - forge.getPR
  - forge.getCIStatus
  - terminal.sendCommand
  - terminal.closeOwned
  - terminal.close
---
Use when: one agent (the judge) interviews several others (the candidates) on a topic and names a winner, with you relaying questions and answers.

Availability: Core, with Hybrid verification. In an agent pane, `notify` on each launch and send brings every answer and judge turn back as it lands. An API-key client has no `notify`: close each exchange with one bounded `terminal.waitUntilIdleBatch` and tell the user what remains pending for their continuation. The sidebar assistant uses `terminal.sendCommand` and `terminal.close`. Forge or CI evidence the judge needs comes from you, through your own permitted tools (`gh`) or the app's reads (`forge.getPR`, `forge.getCIStatus`, which ask the user first at core tier).

You are the conductor, not a participant. Never answer the judge's questions, add your own assessment of a candidate, or override the verdict. If it looks wrong, report that as an observation.

Procedure:
1. Scope and record: topic, judge, candidates, the verification depth and the stopping rule. PLAIN: the judge asks, candidates answer, the judge decides. VERIFIED ("verify", "make them prove it", "top 2", "finalists"): the judge also checks candidates' concrete claims against the project itself, challenges what the evidence does not support, and narrows to a top 2 before the verdict. Default to plain and say which you chose. Agree a finite question-round budget (default three) with the judge's brief. With no judge or candidates named, choose from `agent.listAvailable` and say so.
2. Participants: named open agents as they are (yours or handed over), otherwise fresh agents resolved to real `agentId`s, all in one worktree. Everyone is read-only: "verification" means reading and searching, not edits. Ask each to report any file it changed.
3. Checkpoint (the foundation's), adding: roles and terminal ids, depth, the round budget, a shared rubric and evidence requirement, the question sets (id, exact wording, which candidates it targets), per candidate the question set it was last sent and whether that answer is pending, with its `submissionToken`, the answers received per set, the judge's pending step, any shortlist, and the verdict.
4. Launch, same-kind agents one at a time (the next launch of an `agentId` only after the previous call returned). The judge's `prompt`: topic, rubric, "prepare your first questions; you will get the candidates' answers relayed; ask follow-ups; end with a winner and reasoning", plus the verified-depth duties. Each candidate's `prompt`: topic and "make your case; answer the interviewer's questions thoroughly; do not contact other agents":

```json tool=agent.launch
{"agentId":"<id from agent.listAvailable>","name":"Interview judge: <topic>","worktreeId":"<resolved worktree id>","prompt":"You are the interviewer on <topic>. Candidates: <labels>. Rubric: <rubric>. Write your first question set. Answers will be relayed to you; then ask follow-ups or give a verdict. <verified: check each concrete claim by reading the project, challenge unsupported ones, narrow to a top 2, then pick one>. Read-only: change no files.","notify":true,"handback":true}
```

   Record ids and end your turn.
5. Relay a question set. The first comes from the judge's first turn; if it wants the candidates' pitches first, relay those as in step 6. Send each candidate the judge's exact questions, condensed only without changing substance or difficulty. Every candidate gets the same set unless the judge addressed a question to one of them. Send only to a candidate at its prompt:

```json tool=terminal.sendCommandOwned
{"terminalId":"<candidate terminal id>","command":"Question set <q>. <the judge's questions>. Answer here, with evidence where you make a claim.","notify":true,"handback":true}
```

   Record the set id and token per candidate, then end your turn.
6. Relay answers to the judge together, once every eligible candidate has answered set q or is recorded missing, so the judge compares like with like: condensed per candidate, labelled, with the judge's question beside each answer, plus "follow-ups, or is a decision ready?". Label candidates the same way throughout; if the judge already knows who they are, the interview is attributed, so say so in the report. Forge or CI facts the brief needs are fetched by you before the round and given to the judge as facts.

On notice or resume:
1. Follow the foundation's resume procedure, for each terminal the notice names.
2. Correlate every answer to the question set it was sent with: it counts for set q only if that candidate's set-q prompt is pending and it settled after that send (`terminal.getStatus` with its `submissionToken`). An answer for an older set is filed under that set, never under the current one.
3. Follow-ups arriving while some candidates are still answering the previous set (from the judge or the user): queue each candidate's follow-up and send it only after that candidate's current answer has landed. Never stack a new set on a working candidate.
4. Read answers and the judge's turns in their own words (`terminal.readLastMessageOwned` for Claude Code, otherwise `terminal.getOutput`). A blocking prompt is read literally: a permission, trust or login question is the user's, never answered with a blind "Continue".
5. On a verified interview, expect evidence-based challenges and a shortlist: relay each challenge only to the candidate it names, then run one more round. A top 2 needs two eligible candidates; never invent a second finalist.
6. Stop at the judge's verdict or the round budget. With no verdict at the budget, report the unresolved criteria rather than looping. If an absent candidate or missing verification could change the outcome, call the result provisional.

Stop and recovery: the round budget bounds the run. Never resend a question set to a candidate already pending; check its token. Nobody is closed unless the user asks: closing the losers leaves the winner and the judge open, and uses `terminal.closeOwned` on the exact recorded ids, confirmed from `closedIds`. On cancellation keep a tombstone so a late answer relays nothing.

Report back: the winner and the judge's reasoning in its own terms (and the top-2 shortlist on a verified run), how many question rounds it took, candidates missing or late, the evidence the judge actually checked versus what candidates claimed, any file changes reported, and which terminals were closed.
