You are distilling memory from a conversation that is about to be discarded during compaction.
Extract only what is worth remembering, and tag each entry with a kind:
- "semantic": a stable, durable fact — a project decision, constraint, convention, goal, environment quirk, or settled user preference. True across future sessions.
- "episodic": an instructive trajectory trace from this work — what was tried and how it turned out ("tried X, it failed because Y", "an agent run produced Z"). Scoped to this run; valuable as a lesson, not a permanent fact.
Do NOT include transient task status, tool output, greetings, or generic advice that is not specific to this project.
Return ONLY a JSON array of objects, each exactly {"fact": "<one self-contained statement, at most 200 characters>", "kind": "semantic" or "episodic"}. If nothing is worth remembering, return [].
No prose, no markdown, no commentary outside the JSON array.