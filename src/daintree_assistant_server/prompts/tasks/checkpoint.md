You are compacting a conversation that is about to be discarded. Capture the orchestrator's working state as a STRUCTURED CHECKPOINT — not a prose summary.
Return ONLY a single JSON object with EXACTLY these keys (use [] for any list with nothing to report, "" for an empty goal):
{
  "goal": "the current overarching objective, one sentence",
  "active_terminals": ["terminal-5284bfef-3d11-424c-90cb-136f24046295 — what it is running and its state"],
  "active_watchers": ["wch_xxxx — what it is watching"],
  "workflow_run_ids": ["wfr_xxxx — what it is doing"],
  "decisions": ["a decision made and the reason"],
  "pending_tool_state": ["work started but not finished, awaiting a result"],
  "next_actions": ["the next concrete step to take"],
  "open_questions": ["something still unresolved"],
  "approvals_grants": ["grt_xxxx — an approval or automation grant in effect"],
  "preserved_ids": ["any other load-bearing identifier worth keeping"]
}
Preserve EVERY load-bearing identifier VERBATIM and IN FULL — terminal IDs (terminal-<uuid>, e.g. terminal-5284bfef-3d11-424c-90cb-136f24046295), watcher IDs (wch_*), workflow run IDs (wfr_*), agent launch IDs (agt_*), timer IDs (tmr_*), grant/approval tokens (grt_*), and branch names. Copy each id character-for-character — never shorten a terminal id to its prefix (e.g. terminal-5284bfef), because a truncated id matches no terminal. A dropped or abbreviated identifier is unrecoverable after compaction.
No prose, no markdown, no commentary outside the JSON object.
