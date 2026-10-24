---
id: daintree.orchestration.interview
title: Run a judge-led interview competition among agents
summary: One agent interviews several others on a topic while you relay questions and answers — optionally with the judge verifying claims against the codebase and narrowing to a top 2 — until it names a winner.
whenToUse: Use when the user asks to have one agent interview, question, or grill several other agents on a topic and expects a winner — "have one agent interview the others and pick the best", "which of these should be my pair programmer?". Covers both depths, a light Q&A interview and a deep one where the judge verifies candidates' claims against the codebase, issues challenging follow-ups, and narrows to a top 2 before the verdict. Do not use for a plain one-round poll with no interviewer (use daintree.agents.poll-pinned-agents), a scoring-only competition where entries are judged rather than questioned (use daintree.orchestration.competition), or an open-ended dialogue with no judge and no winner (use the daintree.orchestration.conversation runbook).
risk: project
requiredTools:
  - agent.launch
  - terminal.sendCommandMany
optionalTools:
  - agent.launchMany
  - agent.listAvailable
  - terminal.sendCommandOwned
  - terminal.getStatus
  - forge.getPR
  - forge.getCIStatus
  - terminal.closeMany
---
You conduct: never answer the judge's questions, assess a candidate or override the verdict; doubts are observations. Forge or CI facts the judge needs come from you (`gh`, or `forge.getPR` / `forge.getCIStatus`), given as facts.

On a host: call these tools on the work's machine; `gh -R <owner/repo>` from your own shell.

1. Scope: topic, judge, candidates, depth and a question-round budget (default three, follow-ups included). PLAIN: the judge asks, candidates answer, the judge decides. VERIFIED ("make them prove it", "top 2"): the judge also checks candidates' concrete claims by reading the project, challenges unsupported ones, and narrows to a top 2. Default to plain and say which. With none named, choose from `agent.listAvailable` and say so.
2. Participants: named open agents as they are, otherwise fresh launches, all read-only in one worktree. Track per candidate its terminal id, the question set last sent and its answers per set, plus the judge's pending step and any shortlist.
3. Launch together, each with `waitForReply:true, handback:true`: the candidates (one `agent.launchMany` when kinds are distinct) with the topic and "make your case in at most 20 lines; answer the interviewer's questions; change no files", and the judge:

```json tool=agent.launch
{"agentId":"<agent id>","name":"Interview judge: <topic>","prompt":"You interview candidates <labels> on <topic>. Rubric: <rubric>. Write your first question set, at most 10 lines. Answers will be relayed to you; then ask follow-ups or give a verdict with reasons. <verified: check each concrete claim by reading the project, challenge unsupported ones, narrow to a top 2, then pick one>. Read-only: change no files.","focusPolicy":"preserve","waitForReply":true,"handback":true}
```

4. Each question set: send every candidate the judge's exact questions (the same set unless the judge addressed one) in one `terminal.sendCommandMany` with `waitForReply:true, handback:true`, asking for at most 30 lines with evidence for each claim. Never send a set to a candidate still answering one. On a verified run, relay each challenge only to the candidate it names.
5. Then send the judge all answers together, labelled consistently beside each question, plus "follow-ups, or is a decision ready?", with `waitForReply:true`. If the judge knows who the candidates are, the interview is attributed; say so. An answer for an older set is filed under that set.
6. Stop at the judge's verdict or the budget. A top 2 needs two eligible candidates; never invent a finalist. With no verdict at the budget, report the unresolved criteria. If an absent candidate or missing verification could change the outcome, the result is provisional and says which.

Once the verdict is reported, close the candidates and judge you launched, per the foundation, in one `terminal.closeMany`, confirmed from `closedIds`, keeping the winner the user may work with next. No verdict, or a provisional one: close none. The user's "close the losers" instead closes only the losing candidates.

Report back: the winner and the judge's reasoning in its own terms (and the top 2 on a verified run); rounds taken; missing or late candidates; evidence the judge checked versus what candidates claimed; any file changes reported; terminals closed.
