---
id: daintree.agents.postmortem-run
title: Post-mortem a failed agent run
summary: Work out why a finished agent run went wrong — find the first point it diverged from its brief using its transcript, diff and Daintree's record, classify the cause, sort salvageable work, and propose concrete prevention without replaying the run.
whenToUse: 'Use when an agent run is OVER and the user wants to know WHY it went wrong and how to stop it happening again — "why did that agent go off the rails?", "what went wrong with last night''s Codex run?", "post-mortem this failed attempt", "why did it rewrite half the repo when I asked for one fix?", "learn from this so it doesn''t happen again". The deliverable is the first point of divergence with evidence, a classified cause (brief, environment, agent or orchestration), what of the work is salvageable, and prevention proposed with exact text, applied only on request. Do not use to rescue an agent that is stuck or broken now (use daintree.agents.recover-agent), to change the course of one still working (use daintree.agents.steer-running-agent), to check whether a finished agent''s claim is true (use daintree.orchestration.verify-agent-work), to reconstruct what is still in flight (use daintree.orchestration.status-and-resume), for a production incident (use daintree.debug.investigate-outage), when Daintree itself is misbehaving (use daintree.diagnostics), only to save a rule to memory (use daintree.memory.manage-project-memory), to reverse what the run changed (use daintree.git.undo-agent-changes), or to move its salvageable part onto a fresh branch (use daintree.orchestration.salvage-failed-attempt).'
risk: terminal
requiredTools:
  - worktree.list
  - agent.launch
optionalTools:
  - terminal.list
  - terminal.getStatus
  - terminal.readLastMessageOwned
  - terminal.sendCommandOwned
  - terminal.getOutput
  - agentSessionHistory.list
  - notifications.recent
  - errors.recent
---
Analysis only: nothing is relaunched to "see if it happens again", reverted or rewritten. You gather Daintree's side, a read-only analyst agent reads the transcript and diff, and you check its claims and judge the cause. Find the first divergence, not the final symptom.

Procedure:
1. Identify the run: its terminal id if the panel is still open (`terminal.list`), and its worktree (`worktree.list`). A panel the user points at ("the other panel") beats any history entry: `terminal.getOutput` each open agent panel but your own, and take the one whose screen shows what the user describes. An open panel: one `terminal.getStatus`. `working`, or `waiting` with `waitingReason` `question` or `approval`, is not over; `waiting` at `prompt` ended its turn. A closed session: `agentSessionHistory.list` with that `worktreeId` gives agent, title, branch, `savedAt` and `sessionId`, identity only, no transcript; a pruned session is missing. Several candidates: ask.
2. Daintree's side, labelled by source:
   - The brief it was given: the launch prompt in your conversation, never a paraphrase from memory; else the analyst quotes it from the transcript.
   - An open Claude Code panel you own or were handed: `terminal.readLastMessageOwned`, paging back with `messageIndex` or `cursor`. Otherwise `terminal.getOutput`.
   - The timeline: `notifications.recent` and `errors.recent`.
   - The revision it started from: your launch record; otherwise unknown.
3. Launch one analyst in the run's worktree, `notify:true, handback:true`, and end the turn:

```json tool=agent.launch
{"agentId":"<agent id>","name":"Post-mortem: <run name>","worktreeId":"<run worktree id>","prompt":"<read-only rule, session, transcript location, brief, start revision, report shape>","focusPolicy":"preserve","notify":true,"handback":true}
```

   The prompt: "Read-only: create, modify or delete nothing and run nothing that writes." The session id and agent. Transcript: Claude Code `<sessionId>.jsonl` under `$CLAUDE_CONFIG_DIR/projects` (default `~/.claude/projects`) in the dir named for its cwd (non-alphanumerics as `-`); an open panel has no session id in Daintree: quote its last screen lines so the analyst picks the file whose last turns match; Codex `*<sessionId>*` under `$CODEX_HOME/sessions` (default `~/.codex/sessions`); not found: say so. The brief verbatim. The start revision, or "unknown: attribute to this run only changes its transcript or commits establish". A report of at most 40 lines: the earliest OBSERVABLE turn where its action departed from the brief or from reality (a wrong assumption, a skipped instruction, an ignored error) with the quote and where it sits, and any gap before it; each change it made as correct, wrong or unknown with the reason; checks it ran or skipped. "Never quote a credential."

On notice or resume:
1. Check the analyst's claims: each quote is in the transcript where it says, each diff hunk exists, each change's label fits the diff; drop what does not hold. Missing brief or earlier turns: the first divergence cannot be established; say so.
2. Classify the cause, with evidence, using the timeline and your own records:
   - brief: ambiguous goal, no acceptance criteria, missing scope or constraints;
   - environment: setup not ready, wrong worktree, missing dependency, rate or usage limit;
   - agent: ignored an explicit instruction, looped, invented an API, claimed success it never checked;
   - orchestration: no notice armed, a question answered beyond scope, two agents in one worktree, a replayed prompt;
   - tooling: Daintree or the CLI misbehaved (a fault happening now is daintree.diagnostics).
   Blame the agent only when the brief was clear and the environment sound.
3. Salvage: correct, wrong and unknown, from the diff; a check you run must write nothing beyond build output in that worktree, else the change stays unverified. Revert nothing unasked.
4. Prevention: one to three concrete changes with exact text: a line for future briefs, an AGENTS.md (Codex) or CLAUDE.md (Claude Code) rule, a test that would have caught it, a memory entry. Apply any only on the user's yes; memory goes through daintree.memory.manage-project-memory.

Stop and recovery: one analyst and at most one follow-up send to it (`terminal.sendCommandOwned`, `notify:true, handback:true`); one that fails or finds no transcript is reported, never relaunched on a guess. Close its panel only on request.

Report back: the run (agent, terminal or session id, worktree, branch); the analyst's terminal id; the timeline to the first observable divergence with quoted evidence and its source; the cause class and why; salvageable, wrong and unknown changes and how each was checked; the prevention proposals with exact text; what could not be read (a pruned session, a missing transcript, an unknown start revision).
