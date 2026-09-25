---
id: daintree.memory.manage-project-memory
title: Manage project memory
summary: Save, recall, correct, pin and forget durable project facts — without hoarding, duplicating, or storing secrets.
whenToUse: Use when the user asks you to remember something ("remember that we use pnpm", "always run the linter first"), asks what was decided before ("what did we agree about the API shape?", "do you remember why we dropped X?"), wants a saved fact corrected or removed ("forget that", "that's not true any more"), or wants to review, pin or unpin what is stored. Also when you are about to save a fact and should check for an existing one first. Do not use for state that only matters inside this session — a terminal id, a step counter, an in-flight plan (that is the scratch store, per daintree.foundation) — for "where were we?" across sessions (use daintree.orchestration.status-and-resume), or to look something up in the project's CODE rather than in memory (use daintree.explore.repository).
risk: local
requiredTools:
  - memory.recall
  - memory.list
  - memory.save
  - memory.forget
  - memory.pin
  - memory.unpin
  - user.askMultipleChoice
---
Use when: the user wants something remembered, recalled, corrected, or forgotten.

Memory is for DURABLE facts about the project: decisions, conventions, standing preferences. It is not session state and it is not a notepad. Every turn already carries the pinned memories plus an automatic recall for the user's ask, so most of what you need is in front of you before you call anything.

The two failure modes worth naming, because both are silent:
- Saving too much. A store full of incidental remarks makes recall worse for everything else, and pinned entries cost tokens on EVERY round.
- Saving a contradiction. Two memories that disagree are worse than neither, because recall surfaces whichever ranks higher and the user cannot see the other.

Procedure:

1. NAME THE INTENT: recall, save, correct, pin/unpin, or forget. "Remember that we moved to pnpm" when a memory says npm is a CORRECT, not a save — step 4.

2. RECALL. Check the turn context first — the pinned block and this turn's automatic recall are already there. Call `memory.recall({"query":"<keywords>"})` only to dig for something they did not surface, and `memory.list({})` to audit the store (pinned first, then most recent) when the user wants to see what is saved rather than answer a question. Report the fact and its `mem_…` id when they may want to act on it.

3. SAVE — but check first. The turn's automatic recall usually already tells you whether a memory covers this subject; call `memory.recall` only when it did not. If one exists, you are correcting (step 4), not adding.
   `memory.save({"content":"Use pnpm for this project, never npm.","source":"user","kind":"semantic","category":"conventions"})`
   - `source` is who the fact came FROM — `user` when they stated it, `assistant` when you concluded it. Do not launder your own inference into a user directive.
   - `kind` is `semantic` for a standing truth ("the API returns snake_case"), or `episodic` for something that happened ("we tried X on the 3rd and reverted it").
   - Only `content` is required; the rest sharpen later recall and are worth setting.
   - ONE fact per memory. A paragraph holding three decisions cannot be corrected or forgotten independently.
   - Write it so it still makes sense in six months: no "the fix we just discussed", no bare pronouns, no relative dates.

4. CORRECT by SAVING FIRST, then forgetting. Save the replacement, wait for a real `mem_…` id back, and only then `memory.forget({"id":"<the stale id>"})`. Never the other way round: forget is a soft delete with NO undo, so a forget-then-save that fails in the middle destroys the old fact and stores nothing in its place. The two calls must be in SEPARATE tool rounds within the same user turn — a batch freezes both sets of arguments before either runs, so the forget cannot depend on the save having succeeded. Leaving both alive for one turn is a small, recoverable cost; losing the fact is not. If the forget then fails, say the store briefly holds two versions rather than reporting a clean correction.

5. CONFIRM anything destructive or ambiguous. If the user says "forget that" and more than one memory plausibly matches, do NOT pick one — `user.askMultipleChoice` with the candidate contents and let them choose. Call it alone; the turn blocks on the answer. If exactly one memory matches, forget it and say which.

6. PIN SPARINGLY. A pin puts the memory in EVERY round's context, so it is a permanent per-round token cost. Reserve it for standing directives ("never force-push this repo") and facts that must survive mid-task. Ordinary facts reach the model through recall without being pinned. `memory.unpin` when a pin has stopped earning its cost; the memory stays saved and recallable.

NEVER save:
- Credentials, tokens, API keys, or anything that looks like one — in any form, including "the key starts with sk-or". If the user pastes a secret and asks you to remember it, decline and say why.
- Terminal output, scrollback, logs, or file dumps. Save the CONCLUSION, not the evidence.
- Anything re-derivable from the code. The repository is the source of truth for what the code does; memory is for decisions the code does not record.
- A preference inferred from one offhand remark. "Ugh, tabs" while reading someone else's file is not a standing directive. If it might be, ask.

Notes:
- A user saying "remember" does not always mean the memory store. "Remember to run the tests before you push" inside one task is an instruction for now — do the thing; save it only if they mean it as a standing rule.
- Session state belongs in the scratch store, not here: memory survives restarts and is project-wide, which is exactly wrong for a step counter or a list of ids you are working through.
- When you save on your own initiative, say so in your reply. Silent memory writes are how a store fills with things nobody agreed to.

Correction must preserve intended pinning and lifetime too. Read the old entry’s pinned/category/kind/source/expiry metadata; save with the correct provenance and remaining TTL if it should still expire. If the old entry was pinned and the replacement should be, pin the returned new id successfully BEFORE forgetting the old one. Do not remove the only pinned directive after a failed replacement/pin. `memory.list` and recall are bounded, so an omitted entry is not proof of absence; inspect returned matches before forgetting and report partial failures without claiming a complete correction.

Report back: what you saved, corrected, pinned or forgot, quoting the memory content and its `mem_…` id exactly — and, for a recall, the fact plus where it came from.
