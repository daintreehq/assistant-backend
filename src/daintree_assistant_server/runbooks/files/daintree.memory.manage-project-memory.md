---
id: daintree.memory.manage-project-memory
title: Manage project memory
summary: Save, recall, correct, pin and forget durable project facts — without hoarding, duplicating, or storing secrets.
whenToUse: Use when the user asks you to remember something ("remember that we use pnpm", "always run the linter first"), asks what was decided before ("what did we agree about the API shape?", "do you remember why we dropped X?"), wants a saved fact corrected or removed ("forget that", "that's not true any more"), or wants to review, pin or unpin what is stored. Also when you are about to save a fact and should check for an existing one first. Do not use for state that only matters inside this session — a terminal id, a step counter, an in-flight plan (that is a task checkpoint, per daintree.foundation) — for "where were we?" across sessions (use daintree.orchestration.status-and-resume), or to look something up in the project's CODE rather than in memory (use daintree.explore.repository).
risk: local
---
Use when: the user wants something remembered, recalled, corrected or forgotten.

Availability: host-native. The Daintree app MCP has no memory action, and no shared Daintree memory store is available to you; memories the retired Daintree Assistant saved are not reachable from here. What you can write lives in one of two places, and they are not interchangeable:
- Your host's own memory, where it has one, used through the host's own mechanism (a memory tool, or files in the layout it documents). Claude Code keeps per-project auto-memory: one fact per file, with a MEMORY.md index loaded into each session. It is not shared: other agents, other hosts and the user's teammates are not given it, though anything with access to those files can read them.
- The repository's instruction files (CLAUDE.md, AGENTS.md and the like). Every agent that reads them, and every teammate, gets the fact, as a change to tracked files that follows the repository's normal review and commit path.

Memory holds durable facts: decisions, conventions, standing preferences. It is not session state (that is a checkpoint, per the foundation) and not a notepad. Two failures are silent: saving too much (noise crowds out what matters and costs context every session), and saving a contradiction (two disagreeing entries, and whichever loads first wins).

Procedure:
1. Name the intent: recall, save, correct or forget. "Remember that we moved to pnpm" when an entry says npm is a correction, not a save.
2. Establish the destination before writing anything:
   - "Remember this" with no audience named: your host's memory. Say where it went, and that other agents and teammates are not given it.
   - "For everyone", "for all the agents", "shared" or "project memory": there is no shared Daintree store here. Say so, and offer the repository's instruction files as the shared option. That is a repository change: propose the exact line and file, write it only when the user agrees, and commit only if asked. Never quietly downgrade a shared request to host-only memory, and never turn a request to remember into an unrequested AGENTS.md or CLAUDE.md edit.
   - "Remember to run the tests before you push" inside one task is an instruction for now: do it, and save it only if they mean a standing rule.
   - No host memory and no agreement to edit the repository: report that nothing durable was saved.
3. Recall: check your host's memory (much of it is already loaded), then the repository's instruction files and any decision records the project keeps. Give the fact with its source (which file, and who stated it where that is recorded). If nothing covers it, say so; never reconstruct a decision from what seems plausible. What the CODE does is daintree.explore.repository.
4. Save: check first for an existing entry on the subject; if there is one, this is a correction. Write ONE fact per entry, readable in six months: no "the fix we just discussed", no bare pronouns, absolute dates. Record provenance: stated by the user, or concluded by you. Never launder your own inference into a user directive.
5. Correct without losing the original: write the replacement first, read it back, and only then remove the stale entry. If the replacement write fails, leave the original untouched and say the correction did not happen. If removing the stale entry fails, say both versions exist for now. Keep whatever the old entry carried that still applies, such as where it lives and how it is indexed.
6. Forget: when more than one entry plausibly matches, show the candidates and let the user choose; do not pick. When exactly one matches, remove it and quote what was removed, except a credential or other sensitive value, which you describe without reproducing. A search that finds nothing is not proof that nothing exists.
7. Pinning does not exist here. The nearest thing is a place loaded every session (the MEMORY.md index, or an instruction file), which costs context every session: keep it for standing directives.

Never save:
- Credentials, tokens, keys or anything that looks like one, in any form, including a prefix. Decline and say why.
- Terminal output, logs or file dumps: save the conclusion, not the evidence.
- Anything the code already records.
- A preference inferred from one offhand remark: ask whether it is a standing rule.
When you save on your own initiative, say so: silent writes are how a store fills with things nobody agreed to.

Report back: what was saved, corrected or forgotten, quoting the exact text (a sensitive value described, never reproduced), where it lives (host memory or which file) and who can see it; for a recall, the fact and its source; or that no shared store exists and what you did instead.
