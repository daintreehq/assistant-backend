---
id: daintree.agents.postmortem-run
title: Post-mortem a failed agent run
summary: Work out why a finished agent run went wrong — find the first point it diverged from its brief using its transcript, diff and Daintree's record, classify the cause, sort salvageable work, and propose concrete prevention without replaying the run.
whenToUse: 'Use when an agent run is OVER and the user wants to know WHY it went wrong and how to stop it happening again — "why did that agent go off the rails?", "what went wrong with last night''s Codex run?", "post-mortem this failed attempt", "why did it rewrite half the repo when I asked for one fix?", "learn from this so it doesn''t happen again" — including a run that spanned the Remote Hosts this Shell controls (why it stalled or did the same work twice, which host''s report was wrong). The deliverable is the first point of divergence with evidence (across hosts, a timeline per unit with unobserved moments marked and each host''s claim checked by SHA), a classified cause (brief, environment, agent or orchestration), what of the work is salvageable, and prevention proposed with exact text, applied only on request. Do not use to rescue an agent that is stuck or broken now (use daintree.agents.recover-agent), to change the course of one still working (use daintree.agents.steer-running-agent), to check whether a finished agent''s claim is true (use daintree.orchestration.verify-agent-work), to reconstruct what is still in flight (use daintree.orchestration.status-and-resume), for a production incident (use daintree.debug.investigate-outage), when Daintree itself is misbehaving (use daintree.diagnostics), only to save a rule to memory (use daintree.memory.manage-project-memory), to reverse what the run changed (use daintree.git.undo-agent-changes), or to move its salvageable part onto a fresh branch (use daintree.orchestration.salvage-failed-attempt).'
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
  - host.list
  - host.call
---
Analysis only: nothing is relaunched to "see if it happens again", reverted, rewritten or filed. You gather Daintree's side, a read-only analyst reads transcript and diff, you check its claims and judge the cause: the first divergence, not the final symptom; elapsed time is not a cause.

Procedure:
1. Identify the run: its terminal id if still open (`terminal.list`) and its worktree (`worktree.list`). A panel the user points at ("the other panel") beats history: read each open agent panel but your own (`terminal.getOutput`) and take the one that matches. Open: one `terminal.getStatus`; `working`, or `waiting` on a `question` or `approval`: not over. Closed: `agentSessionHistory.list` with that `worktreeId` gives agent, title, branch, `savedAt`, `sessionId` (identity only; pruned: missing). Several candidates: ask.
2. Daintree's side, by source:
   - Brief: your launch prompt, never a paraphrase; else the analyst quotes it from the transcript.
   - An open Claude Code panel you hold: `terminal.readLastMessageOwned`, paging with `messageIndex` or `cursor`; otherwise `terminal.getOutput`.
   - Timeline: `notifications.recent`, `errors.recent`.
   - Start revision: your launch record, else unknown.
3. Launch one analyst in the run's worktree, `notify:true, handback:true`; end the turn:

```json tool=agent.launch
{"agentId":"<agent id>","name":"Post-mortem: <run name>","worktreeId":"<run worktree id>","prompt":"<read-only rule, session, transcript, brief, start revision, report shape>","focusPolicy":"preserve","notify":true,"handback":true}
```

   The prompt: "Read-only: create, modify or delete nothing and run nothing that writes." Session id and agent. Transcript: Claude Code `<sessionId>.jsonl` under `$CLAUDE_CONFIG_DIR/projects` (default `~/.claude/projects`) in the dir named for its cwd (non-alphanumerics as `-`); an open panel has no session id: quote its last screen lines to match the file's last turns; Codex `*<sessionId>*` under `$CODEX_HOME/sessions` (default `~/.codex/sessions`); not found: say so. The brief verbatim. The start revision, or "unknown: attribute to this run only changes its transcript or commits establish". A report of at most 40 lines: the earliest OBSERVABLE turn departing from the brief or reality, quoted with where it sits, and any gap before it; each change as correct, wrong or unknown with the reason; checks run or skipped. "Never quote a credential."

A run across Remote Hosts: units, briefs and sent SHAs from your records and this conversation, else ask. Per host, steps 1-3 through `host.call` (history and timeline at Full only), one analyst each. From your shell, each claim at its SHA: on the remote (`git ls-remote`), descended from its sent base (`git merge-base --is-ancestor`), its forge checks (`gh`); absent, not descended or failing: contradicted. Compare times within a host only. Nothing records delivery: a moment no record shows is unobserved.

On notice or resume:
1. Check the analyst's claims: each quote where it says, each diff hunk real, each label fitting the diff; drop what fails. Missing brief or earlier turns: say the first divergence cannot be established.
2. Classify the cause, with evidence: brief (ambiguous goal, no acceptance criteria or scope); environment (setup not ready, wrong worktree, missing dependency, usage limit, a host's repo setup); agent (ignored an instruction, looped, invented an API, claimed unchecked success); orchestration (no notice armed, a question answered beyond scope, two agents in one worktree, a replayed prompt, a host refusal or non-`ok` `host.call`); tooling (Daintree or the CLI misbehaved; happening now: daintree.diagnostics). Blame the agent only when the brief was clear and the environment sound.
3. Salvage from the diff: correct, wrong, unknown; a check you run writes nothing beyond build output there, else the change stays unverified. Revert nothing unasked.
4. Prevention: one to three changes with exact text, each with a scenario reproducing it: a brief line, an AGENTS.md (Codex) or CLAUDE.md (Claude Code) rule, a test, a memory entry (through daintree.memory.manage-project-memory), each applied only on the user's yes.

Stop and recovery: one analyst (per host, across hosts), at most one follow-up send (`terminal.sendCommandOwned`, `notify:true, handback:true`); one that fails or finds no transcript is reported, never relaunched. Close its panel only on request.

Report back: the run (agent, terminal or session id, worktree, branch, host); each analyst's terminal id; the timeline to the first divergence with quoted evidence, sources and unobserved moments; the cause class and why; claims verified or contradicted; changes as salvageable, wrong or unknown, and how checked; prevention with exact text; what could not be read (pruned session, missing transcript).
