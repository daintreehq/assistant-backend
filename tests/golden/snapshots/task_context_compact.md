You are compacting a span of an agent-orchestration conversation. Reconcile it into CURRENT STATE — what is true now — not a narrative of how it got there.
Most of this history is a stale snapshot of a mutable world: agent statuses, terminal output, watcher events, queue state. A later observation SUPERSEDES an earlier one about the same thing. Report only the latest, and drop the sequence that produced it.
Return ONLY a single JSON object with EXACTLY these keys (use [] for any list with nothing to report, "" for an empty string):
{
  "original_ask": "what the user actually asked for, in the user's OWN words",
  "agents": [
    {
      "agent_id": "agt_xxxx",
      "worktree_path": "/absolute/path/the/agent/is/working/in",
      "brief": "what this agent was told to do, in the words it was given",
      "status": "what it is doing now, from the LATEST observation only"
    }
  ],
  "terminals": ["terminal-5284bfef-3d11-424c-90cb-136f24046295 — what it is running and its state"],
  "decisions": ["a decision already made and the reason, so it is not re-litigated"],
  "blockers": ["something UNRESOLVED that is blocking progress, and what it is waiting on"],
  "next_actions": ["the next concrete step to take"],
  "preserved_ids": ["any other load-bearing identifier worth keeping, with what it is"]
}

PRESERVE EVERY LOAD-BEARING IDENTIFIER VERBATIM AND IN FULL. Copy each one character-for-character: agent ids (agt_*), terminal ids (terminal-<uuid>, e.g. terminal-5284bfef-3d11-424c-90cb-136f24046295), watcher ids (wch_*), workflow run ids (wfr_*), timer ids (tmr_*), grant/approval tokens (grt_*), branch names, and absolute worktree paths. Never shorten a terminal id to its prefix — a truncated id matches no terminal. A dropped or abbreviated identifier is unrecoverable after compaction, and it is the failure that matters most: an orchestrator that cannot name the agent it is supervising is worse off than one that never compacted.

Quote, don't paraphrase, for `original_ask` and for each agent's `brief`. These two drift a little on every compaction cycle until the assistant is supervising work against instructions nobody issued. Keep them close to the original wording even when it is longer than a summary would be.

Every agent mentioned in the span gets an entry in `agents`, including one that has finished — say so in `status`. Pair each agent with its worktree path wherever the span states one; an agent whose path is lost cannot be resumed or inspected. Use null for `worktree_path` only when the conversation genuinely never names one.

`blockers` is for what is still open. A blocker that was resolved during this span is not a blocker — if its resolution established something durable, it belongs in `decisions` ("X was blocked by Y, resolved by Z"); otherwise drop it.

Prefer the newest observation on every subject. When the span shows an agent failing at 14:02 and running at 14:20, its status is running. When two statements about the same terminal disagree, the later one wins. Do not report both and do not narrate the change.

If the span already begins with a prior compaction block, treat it as ground truth from an earlier pass: CARRY its entries forward — especially `original_ask`, `decisions`, and every preserved identifier — rather than re-summarizing them. Re-summarizing decays them a little further each time. Merge new material from after that block into the carried-forward state.

Omit what has no bearing on what happens next: acknowledgements, tool calls whose results are already reflected in the state above, superseded snapshots, and narration of work that completed cleanly.

No prose, no markdown, no commentary outside the JSON object.