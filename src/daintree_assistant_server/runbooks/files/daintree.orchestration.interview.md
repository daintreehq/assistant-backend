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
optionalTools:
  - worktree.list
  - terminal.list
  - terminal.getStatus
  - terminal.waitUntilIdleBatch
  - forge.getPR
  - forge.getCIStatus
  - terminal.closeOwned
---
Use when: one agent (the judge) interviews several others (the candidates) on a topic and names a winner, with you relaying questions and answers.

Availability: Core. An API-key client has no `notify`: close each exchange with one `terminal.waitUntilIdleBatch`. Forge or CI facts the judge needs come from you (`gh`, or `forge.getPR` and `forge.getCIStatus`, which ask the user first at core tier), given to the judge as facts.

You conduct: never answer the judge's questions, assess a candidate or override the verdict; doubts are observations.

Procedure:
1. Scope: topic, judge, candidates, depth and a question-round budget (default three, judge relays and follow-ups included). PLAIN: the judge asks, candidates answer, the judge decides. VERIFIED ("make them prove it", "top 2"): the judge also checks candidates' concrete claims by reading the project, challenges unsupported ones, and narrows to a top 2. Default to plain and say which. With none named, choose from `agent.listAvailable` and say so.
2. Participants: named open agents as they are (yours or handed over), otherwise fresh launches, all read-only in one worktree. Track per candidate its terminal id, the question set it was last sent, whether that answer is pending, and its answers per set; plus the judge's pending step and any shortlist.
3. Launch the judge and every candidate in this turn, then end it. Candidates get the topic and "make your case in at most 20 lines; answer the interviewer's questions; change no files".

```json tool=agent.launch
{"agentId":"<id from agent.listAvailable>","name":"Interview judge: <topic>","worktreeId":"<worktree id>","prompt":"You interview candidates <labels> on <topic>. Rubric: <rubric>. Write your first question set, at most 10 lines. Answers will be relayed to you; then ask follow-ups or give a verdict with reasons. <verified: check each concrete claim by reading the project, challenge unsupported ones, narrow to a top 2, then pick one>. Read-only: change no files.","focusPolicy":"preserve","notify":true,"handback":true}
```

4. Question set: send every candidate the judge's exact questions (the same set unless the judge addressed one), in one turn, then end it.

```json tool=terminal.sendCommandOwned
{"terminalId":"<candidate terminal id>","command":"Question set <q>. <the judge's questions>. Answer here in at most 30 lines, with evidence for each claim.","notify":true,"handback":true}
```

5. Judge relay, its own pending step: once every eligible candidate has answered set q or is recorded missing, send the judge all answers together, labelled consistently, each beside its question, plus "follow-ups, or is a decision ready?", with `notify`. If the judge knows who the candidates are, the interview is attributed; say so.

On notice or resume:
1. Each quote is that terminal's answer or judge turn. An answer counts for set q only from a candidate whose set-q prompt is pending; an answer for an older set is filed under that set.
2. A follow-up for a candidate still answering is queued until its current answer lands; never stack sets on a working candidate. On a verified run, relay each challenge only to the candidate it names.
3. A top 2 needs two eligible candidates; never invent a finalist.
4. Stop at the judge's verdict or the budget. With no verdict at the budget, report the unresolved criteria. If an absent candidate or missing verification could change the outcome, the result is provisional and says which.

Stop and recovery: the budget bounds the run. Never resend a pending set. Close only on request: "close the losers" keeps the winner and the judge, confirmed from `closedIds`. On cancellation relay nothing more.

Report back: the winner and the judge's reasoning in its own terms (and the top 2 on a verified run); rounds taken; missing or late candidates; evidence the judge checked versus what candidates claimed; any file changes reported; terminals closed.
