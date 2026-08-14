---
id: daintree.help-and-diagnostics
title: Answer "how do I…" and diagnose a broken Daintree
summary: Answer Daintree how-to questions from the live docs, and diagnose a broken link, missing tool, or bad key.
whenToUse: Use when the question is about DAINTREE ITSELF rather than the user's project — "how do I…", "what is a worktree", "why can't I…", "what does this setting do", "is my key working", "why is this tool missing", "nothing is responding", "the MCP is disconnected", a keybinding or documented behaviour, or a request for diagnostics to paste into a bug report. Also when a tool you expected is not offered this turn and you must explain why rather than guess a name. Do not use when the user wants you to DO the thing rather than explain it — "rename my tabs" is daintree.terminals.rename even though "how do I rename a tab" is here. Do not use for questions about the user's own codebase (use daintree.explore.repository), when a specific agent or spawn has gone wrong (use daintree.agents.recover-agent), to work out where a paused workflow left off (use daintree.orchestration.status-and-resume), or to investigate a red CI check (use daintree.ci.diagnose-and-fix).
risk: read
requiredTools:
  - daintree.status
  - daintree.listTools
  - tool.search
  - context.snapshot
---
Use when: the user is asking about Daintree itself — how to do something, what something means, or why something is not working.

Two DIFFERENT jobs share this skill, and the first move is telling them apart:
- A DOCS question ("how do I…", "what is…", "can Daintree…") — answer from the documentation supplied with the turn, never from memory.
- A LIVE-STATE problem ("it's not responding", "that tool is missing", "is my key ok") — read the actual state and compare it against what the docs say should happen.

Guessing is the failure mode this skill exists to prevent. Daintree's surface changes; an answer invented from a plausible-sounding memory is worse than "let me check", because the user acts on it.

Procedure:

1. CLASSIFY. Is the user asking what SHOULD happen (docs), or reporting that something ISN'T happening (state)? "How do I share a terminal?" is docs. "I shared it and nothing appeared" is state — and usually both, in that order.

2. DOCS QUESTIONS — read the documentation block, don't recall. A documentation question arrives with the matching pages already retrieved, in the "Daintree documentation" section of this turn's injected context. There is no search for you to run and nothing to fetch.
   - The retrieved sections usually answer the question outright. Quote the specific step, not a paraphrase.
   - Cite every page you used by the full `URL:` line printed with it. A bare path is not a citation the user can follow, and a URL you assembled or remembered is not one you can vouch for.
   - If the retrieved pages do not cover the question — or none arrived at all — say so plainly and answer with what you actually know, flagged as unverified. Do not fill the gap with an inference presented as fact, and do not go reading project source to reconstruct it.

3. LIVE-STATE PROBLEMS — read, don't theorise.
   - `daintree.status` reports the MCP link: endpoint, connected, transport, tool count, and the error text when it is down. It works WHILE disconnected, which is exactly when you need it.
   - `context.snapshot` for workspace state — worktrees, terminals, agents — when the complaint is about something that should be there and isn't.
   - A DISCONNECTED link explains almost every "nothing works" report at once: the control-plane tools (terminal, agent, worktree, git, forge) all stop, so say that first and give the reconnect step rather than diagnosing each missing capability separately. Note what still WORKS, because it changes what you can do next: `daintree.status` itself is designed to answer while disconnected, and documentation retrieval does not go through that link — so a how-to question can still be answered while the app is unreachable.

4. A MISSING TOOL is a real diagnosis, not a dead end. `daintree.listTools` marks each tool `callable`. A tool that exists but is `callable: false` was not offered THIS turn — say so and name what it does, rather than calling it and reporting a confusing failure. `tool.search({"query":"rename terminal"})` finds a capability by a couple of plain keywords when you know the job but not the name. If the capability genuinely does not exist, saying that IS the answer.

5. COMPARE, then remediate. The useful output is the gap: "the docs say X happens when you Y; your link is disconnected, so it cannot". Give the exact next step — a command, a setting, a reconnect — not a list of things to try.

6. DIAGNOSTICS TO PASTE. When the user wants something to attach to a bug report, give them the safe set: what `daintree.status` reported, the workspace shape from `context.snapshot`, and the exact error text. Never include the API key, in any form — not the value, not a prefix, not its length. If they ask you to echo the key to prove it is set, refuse and explain that no diagnostic here needs it.

Key questions are their own case, and `daintree.status` does NOT answer them: it reports the Daintree MCP link only — endpoint, connected, transport, tool count — which is a different connection from the model provider entirely. A connected MCP with a dead key looks perfectly healthy there. Point the user at the sign-in check the retrieved documentation describes rather than inferring one from a failed turn. Three outcomes that must not be conflated: a REJECTED key needs a new one, a key with no CREDIT needs topping up and is otherwise fine, and an UNREACHABLE provider is neither — never tell a user their key is invalid when we simply could not ask.

Notes:
- Answer the question asked. A user asking how to rename a tab does not need an explanation of the worktree model.
- The retrieved pages are what there is. If they are thin, the documentation does not cover the question — say so and stop, rather than reconstructing an answer from somewhere else.
- Never invent a settings key, flag, keybinding, or tool name. A near-miss name is worse than an admission, because the user will try it.

Report back: the answer with the full docs URL it came from, or — for a live problem — what you observed, what it should have been, and the one concrete step that fixes it.
