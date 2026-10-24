---
id: daintree.hosts.supervise-swarm
title: 'Supervise a run on the Remote Hosts and write its verified final report'
summary: 'Keep work already out on the Remote Hosts moving across turns on each host notice and a paced status read: wait quietly while hosts work, answer an agent''s question within what the person asked, bring decisions to the person, verify each finished unit by artifact, check once on a host left quiet after its peers are in, and end with one verified row per unit, nothing sent twice.'
whenToUse: 'Use once this Shell''s work is OUT on the Remote Hosts and the person wants the run followed to its end, or its final account once it has ended or been stopped: "keep an eye on the studios and tell me when they''re done", "keep the swarm going and answer what you can", a remote host asking whether it may do something, one host still working long after the others came back, the line "Daintree: on host …" for work it sent, "give me the final report on what my other machines did", "which machines really delivered, which only said so, and what did they leave running", "audit the swarm''s completion claims against the evidence". The deliverable is the run kept moving: every notice read, a question answered within the person''s request, decisions brought to the person, each finished unit verified by branch, SHA or PR, a quiet host checked once, and at the end one row per unit, the failed, unreachable and silent ones too: host, branch, exact SHA and checks, verified versus claimed, false or missing reports named, and the panes, worktrees, servers and unpushed work left behind with who decides. Do not use for sending the work in the first place (daintree.hosts.swarm-fan-out, daintree.hosts.place-work, daintree.hosts.delegate-to-host), changing its course (daintree.hosts.steer-swarm), questions Daintree Assistants ask outside this run (daintree.hosts.answer-assistant-questions), or agents on this machine (daintree.orchestration.supervise-fleet).'
risk: external
requiredTools:
  - host.list
  - host.call
hostEligibility: configured
---
The run's record is your rows plus each host's live terminals. Per unit: host, `projectId`, terminal and worktree ids, agent, branch, the brief by reference, state, when it last moved, result. A run spanning sessions keeps them in a checkpoint (`daintree.foundation`).

Procedure:
1. Rows lost: per host, `terminal.list` through `host.call`; match terminals to units by name and worktree, and ask the person where unsure. Never relaunch or resend work.
2. On a notice, a resume or the person's message, sweep every host with open rows (a notice can be missed): one `terminal.getStatus` per host project over its units' terminals, `includeOutput:{"lines":30}`. Per unit:
   - `working`: wait; nothing to the person. An old `lastOutputChangeAt` is possibly stalled, not hung.
   - `waiting` on a question: read it (`terminal.readLastMessageOwned` for Claude Code, else its screen). Within the person's request, answer once with `terminal.sendCommandOwned`, `notify:true, handback:true`; beyond it, or an approval, login or permission: the person, its words quoted.
   - At its prompt with a `lastHandback` or final message: a claim; step 4.
   - Exited (`exitCode`) or an entry `error` (closed, gone): report it. Host `disconnected`, or a call not `ok`: unknown, it may still be working; moving it is daintree.hosts.reassign-host-work, on the person's word.
   A unit run by a host's assistant is its terminal: its answer by `terminal.readLastMessageOwned`, a follow-up with its `generation` from `assistant.list`; `ASSISTANT_BUSY`: wait.
3. `RESOURCE_NOT_OWNED` on a send (your Shell session restarted, or another session launched it): nothing went; the person answers in that pane on that machine, or, on their word, that host's assistant, briefed.
4. Verify each claim yourself, by reference, in your own shell after `git fetch`: the claimed full SHA is on the named remote branch (`git branch -r --contains`), descends from the base (`git merge-base --is-ancestor`), and its diff fits the acceptance; checks are the forge's on that SHA (`gh pr checks`, `gh run list --commit`), else a command and exit code run on that host (`project.runCommand`, or `project.runCheck` at Full), kept apart from the agent's account. A failed check, with repair inside the person's request: one corrective prompt per unit to its agent with the failing command and output (their bound otherwise), then verify again; else report it. A reviewer only if asked, repository-required or a criterion needs inspection. Not pushed: `git rev-parse HEAD` and `git status --short` in its worktree by `project.runCommand`: the host's fact, not the remote's.
5. A quiet host: once its peers are in and it has run several times their time, its status screen is the check. Report what you saw ("working 40 minutes, screen unchanged for 25"), never a guess at why; a send to a working agent is read only when its turn ends.
6. End the turn while any row is open, every working unit armed: launched with `notify`, else `terminal.notifyWhenIdle` through `host.call` (rely on it only on `armed:true`). A native wakeup in your tools: set one at the quiet-host threshold. None: tell the person which hosts you wait on and what you will check when they return.

```json tool=host.call
{"hostId":"<hostId>","projectId":"<projectId>","tool":"terminal.notifyWhenIdle","args":{"terminalId":"<terminalId>","note":"supervise <run> <unit>"}}
```

Final report, once every row has settled or the person asks for the account of an ended or stopped run. Read-only: no new work, push, close or cleanup comes from it. A row for every unit, failed, unreachable, never sent and silent too: host, project, branch, full SHA, checks; verified, claimed, contradicted (quoted beside what you found) or missing (finished naming no branch, SHA or check). A branch moved on since: moved, with times. Leftovers per host: `terminal.list` and `worktree.list` through `host.call` for this run's panes, worktrees, dev servers and unpushed or dirty work, each with its owner and the decision waiting; cleanup is daintree.hosts.recover-and-clean-up, on the person's word.

On notice or resume: the notice names the host and terminal; step 2 for it, then the sweep. A notice is a stop, not a success.

Stop and recovery: one answer per question and one question per unpushed unit, never resent; nothing relaunched, moved or closed here.

Report back: a row per unit: host, state, branch and SHA claimed versus verified and how, checks, timings as observed; what waits on the person; then the leftovers with owners.
