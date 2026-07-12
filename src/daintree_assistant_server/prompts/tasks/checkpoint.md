You are compacting a conversation that is about to be discarded. Capture the orchestrator's working state as a STRUCTURED CHECKPOINT — not a prose summary.
Return ONLY a single JSON object with EXACTLY these keys (use [] for any list with nothing to report, "" for an empty goal):
{
  "goal": "the current overarching objective, one sentence",
  "user_directives": ["a standing instruction or constraint the user stated, kept close to their own words"],
  "active_terminals": ["terminal-5284bfef-3d11-424c-90cb-136f24046295 — what it is running and its state"],
  "active_watchers": ["wch_xxxx — what it is watching"],
  "workflow_run_ids": ["wfr_xxxx — what it is doing"],
  "decisions": ["a decision made and the reason"],
  "failed_approaches": ["an approach that was tried and did NOT work, and why it failed"],
  "pending_tool_state": ["work started but not finished, awaiting a result"],
  "next_actions": ["the next concrete step to take"],
  "open_questions": ["something still unresolved"],
  "approvals_grants": ["grt_xxxx — an approval or automation grant in effect"],
  "preserved_ids": ["any other load-bearing identifier worth keeping"]
}
Quote, don't paraphrase: goal and user_directives must stay close to the user's OWN words. Paraphrase drifts a little more on every compaction cycle until the assistant is pursuing something the user never asked for.
user_directives captures standing instructions and constraints the user stated anywhere in the conversation ("never close terminals", "always work on a branch", "don't touch the backend") — keep each one close to the user's own words. These outlive any single task: a dropped directive means the assistant starts violating an explicit instruction after compaction. Do NOT include one-off task requests (those belong in goal / next_actions).
failed_approaches is the loop-prevention register: approaches that failed and are still RELEVANT — unresolved, or likely to be re-attempted — with the reason, so the post-compaction assistant does not burn time repeating them. Deduplicate repeats of the same failure into one entry; skip transient noise that self-resolved. A failure whose fix established something durable belongs in decisions ("X failed because Y, fixed by Z"), not here.
Never copy an archived-transcript breadcrumb line ("Full pre-compaction transcript preserved as artifact …") into any field — the runtime appends a fresh one after every compaction; a copied stale one points at superseded history.
If the conversation already begins with a prior "[checkpoint | depth N]" note, that note is ground truth from an earlier compaction: CARRY its still-relevant entries FORWARD (especially user_directives, decisions, failed_approaches, and every preserved identifier) rather than re-summarizing them — each re-summarization decays them a little further. Merge new material from the conversation after the note into the carried-forward state.
Preserve EVERY load-bearing identifier VERBATIM and IN FULL — terminal IDs (terminal-<uuid>, e.g. terminal-5284bfef-3d11-424c-90cb-136f24046295), watcher IDs (wch_*), workflow run IDs (wfr_*), agent launch IDs (agt_*), timer IDs (tmr_*), grant/approval tokens (grt_*), and branch names. Copy each id character-for-character — never shorten a terminal id to its prefix (e.g. terminal-5284bfef), because a truncated id matches no terminal. A dropped or abbreviated identifier is unrecoverable after compaction.
No prose, no markdown, no commentary outside the JSON object.
