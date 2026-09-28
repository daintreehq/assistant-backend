---
id: daintree.memory.manage-project-memory
title: Remember durable instructions for Daintree's assistant and agents
summary: Save, recall, correct or forget a durable fact or rule where its audience reads it — Daintree's assistant instructions, commands and skills in .daintree/assistant/, AGENTS.md or CLAUDE.md for every agent, or your own host memory — with shared files written by an agent, verified, and running agents told.
whenToUse: Use when the user wants something remembered from now on ("remember that we use pnpm", "the Daintree assistant should always run the linter first"), asks what was decided before ("what did we agree about the API shape?"), wants a saved fact or rule corrected or removed ("forget that", "that's out of date"), or wants the durable instructions themselves changed — "add a /triage command to my Daintree assistant", "give the assistant a skill for our release checklist", "add a rule to AGENTS.md so agents stop editing generated files", "make CLAUDE.md and AGENTS.md agree", "prune our CLAUDE.md". Do not use for state that only matters in this run (a task checkpoint, per daintree.foundation), for "where were we?" (daintree.orchestration.status-and-resume), for what the CODE does (daintree.explore.repository), to correct one agent that is working now (daintree.agents.steer-running-agent), to run an existing command or plugin skill (daintree.agents.run-agent-command, daintree.skills.use-plugin-skill), or for a Daintree plugin (daintree.plugins.develop-project-plugin).
risk: project
optionalTools:
  - worktree.list
  - agent.launch
  - terminal.list
  - terminal.getStatus
  - terminal.sendCommandMany
---
Durable text lives where its audience reads it; the destinations do not reach each other:
- Daintree's assistant (the sidebar): `.daintree/assistant/` in the project's main checkout (git-trackable, so the team shares it once committed) or `~/.daintree/assistant/` (this user, every project). `instructions.md` (64 KB cap) is added to its prompt; `.claude/commands/<name>.md` are its slash commands (Claude only); skills are `<name>/SKILL.md` under `.agents/skills/` (Claude and Codex), `.claude/skills/` or `.codex/skills/`. Agents in terminals never see this folder.
- Every agent and teammate: the repository's AGENTS.md and CLAUDE.md, a tracked change on its normal review path.
- Only you: your host's own memory (Claude Code: per-project auto-memory, one fact per file, a MEMORY.md index).

Procedure:
1. Name the intent: recall, save, correct or forget. "Remember we moved to pnpm" when an entry says npm is a correction.
2. Recall: read host memory, AGENTS.md, CLAUDE.md, `.daintree/assistant/instructions.md` and decision records; give the fact with its source. Nothing covers it: say so, never reconstruct a plausible decision.
3. Pick the destination from the audience the user named. None named: host memory, saying who does not get it. A rule for this task only: follow it, save nothing. For a shared file, propose the exact text and file and write only on the user's yes; never turn a request to remember into an unrequested AGENTS.md or CLAUDE.md edit. Commit only if asked.
4. Host memory (your own), or a shared-file edit the user asked you yourself to make: write it (the sidebar assistant cannot write files: it delegates). Any other shared-file change goes to an agent (step 5). Check for an existing entry first; one fact per entry, readable in six months (absolute dates, no "the fix we just discussed"), with provenance.
5. Anything larger (a command, a skill, pruning or reconciling instruction files): `worktree.list`, then one `agent.launch` in the checkout that holds the file (the main checkout for `.daintree/assistant/`), `notify:true, handback:true`, and end your turn. Brief: the approved text or goal, the exact files, and the rules: touch only those files; replace a stale rule instead of adding a contradicting one; keep AGENTS.md and CLAUDE.md consistent where both carry it; a skill needs `name` and `description` frontmatter or it is skipped; project skills and commands lose `allowed-tools` and `hooks`; MCP server and hook files work only from `~/.daintree/assistant/`, behind a Daintree setting; no secrets; no commit unless asked; report the files changed.
6. On the notice: read the diff yourself (paths, frontmatter, nothing else touched); one corrective round, then the user.
7. Correct: write the replacement, read it back, then remove the stale entry; a failed removal leaves both: say so. Forget: several plausible matches, let the user choose; one, remove it and quote it (describe a sensitive value, never reproduce it). Nothing found is not proof nothing exists.
8. Reach: the assistant reads `.daintree/assistant/` only at its next launch (new session, reset or app restart; no action reloads it): tell the user. An AGENTS.md or CLAUDE.md change reaches other worktrees only once merged there: find agents it affects (`terminal.list`, `terminal.getStatus`), tell the ones you launched in one `terminal.sendCommandMany`, and name the rest for the user.

Never save credentials, tokens or keys (even a prefix: decline and say why), terminal output or logs (save the conclusion), what the code already records, or a preference inferred from one remark (ask). Say when you save on your own initiative.

Report back: what was saved, corrected or forgotten, quoted (sensitive values described); where it lives and who sees it; which agent wrote it (terminal id) and the diff you checked; when it takes effect and which agents were told; for a recall, the fact and its source.
