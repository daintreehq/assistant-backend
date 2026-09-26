---
id: daintree.memory.manage-project-memory
title: Manage project memory
summary: Save, recall, correct, pin and forget durable project facts — without hoarding, duplicating, or storing secrets.
whenToUse: Use when the user asks you to remember something ("remember that we use pnpm", "always run the linter first"), asks what was decided before ("what did we agree about the API shape?", "do you remember why we dropped X?"), wants a saved fact corrected or removed ("forget that", "that's not true any more"), or wants to review, pin or unpin what is stored. Also when you are about to save a fact and should check for an existing one first. Do not use for state that only matters inside this session — a terminal id, a step counter, an in-flight plan (that is a task checkpoint, per daintree.foundation) — for "where were we?" across sessions (use daintree.orchestration.status-and-resume), or to look something up in the project's CODE rather than in memory (use daintree.explore.repository).
risk: local
---
The Daintree app has no memory action and no shared store; the retired Daintree Assistant's memories are unreachable. Two destinations, not interchangeable:
- Your host's memory, through its own mechanism (Claude Code: per-project auto-memory, one fact per file, a MEMORY.md index loaded each session). Not given to other agents, hosts or teammates.
- The repository's instruction files (CLAUDE.md, AGENTS.md): every agent and teammate gets the fact, as a tracked change following the repository's normal review path.

Memory holds durable facts (decisions, conventions, standing preferences), not session state or notes. Saving too much and saving a contradiction both fail silently.

Procedure:
1. Name the intent: recall, save, correct or forget. "Remember we moved to pnpm" when an entry says npm is a correction.
2. Destination, before writing:
   - No audience named: host memory. Say where it went and that others do not get it.
   - "For everyone", "shared", "project memory": say no shared Daintree store exists and offer the instruction files; propose the exact line and file, write only when the user agrees, commit only if asked. Never quietly downgrade to host-only memory, and never turn a request to remember into an unrequested AGENTS.md or CLAUDE.md edit.
   - An instruction for this task ("run the tests before you push"): do it; save only a standing rule.
   - No host memory and no agreement to edit the repository: say nothing durable was saved.
3. Recall: host memory, then instruction files and decision records. Give the fact with its source; if nothing covers it, say so, never reconstruct a plausible decision. What the CODE does is daintree.explore.repository.
4. Save: check for an existing entry first (then it is a correction). One fact per entry, readable in six months (no "the fix we just discussed", absolute dates), with provenance: stated by the user or concluded by you.
5. Correct: write the replacement, read it back, then remove the stale entry. A failed write leaves the original; a failed removal means both exist for now: say so.
6. Forget: several plausible matches, show them and let the user choose; exactly one, remove it and quote it (describe a sensitive value, never reproduce it). Finding nothing is not proof nothing exists.
7. Pinning does not exist; the nearest is a place loaded every session (the MEMORY.md index, an instruction file), which costs context every session.

Never save credentials, tokens or keys (even a prefix: decline and say why), terminal output or logs (save the conclusion), what the code already records, or a preference inferred from one remark (ask). Say when you save on your own initiative.

Report back: what was saved, corrected or forgotten, quoted (sensitive values described), where it lives and who can see it; for a recall, the fact and its source; or that no shared store exists and what you did instead.
