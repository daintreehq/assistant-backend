
---

# Current turn

Current objective: Fix the watcher tests
Durable workflows — client-owned execution graphs, the durable source of truth for long-running work. Trust these digests over your memory of the conversation and do NOT redo work a workflow already shows as done (no repeat reads, spawns, sends, or polls). If a digest looks stale after a tool result or a wake, call workflow.reconcile. Never invent graph state — when exact state matters, read it with the workflow tools.
- wf_123 [active] Fix watcher tests and verify branch — 2/5 nodes done; current: verify tests
  active: n_verify: Run watcher tests (ready)
  resources: terminal term_8: Claude repair watcher tests; async asy_77 done
  next: Run tests in the repair worktree
  last event: Agent reported fixes complete
- wf_124 [blocked] Prepare PR for issue 286
  blockers: waiting on user: choose branch strategy

