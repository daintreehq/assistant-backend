---
id: daintree.diagnostics
title: Diagnose a broken or misbehaving Daintree
summary: Work out why Daintree itself is not doing what it should — a dead MCP link, a tool that is not offered, a key that will not sign in — and give the one step that fixes it.
whenToUse: Use when the question is about DAINTREE'S OWN HEALTH — either something is misbehaving and the user wants to know why ("nothing is responding", "the MCP is disconnected", "I did X and nothing happened", "why can't I…", "why is this tool missing"), or they want their SIGN-IN confirmed before they rely on it ("is my key working", "am I actually signed in", "will this burn credit if it is not"). Nothing has to be broken yet for the sign-in case — `daintree.status` reports the MCP link and does NOT answer it, which is exactly why it needs a runbook. Also when they want diagnostics to paste into a bug report. A plain "is Daintree connected?" with nothing reported wrong is a single status read, not this — that is daintree.foundation. Also when a tool you expected is not offered this turn and you must explain why rather than guess a name. Do NOT use for a plain how-to, concept, setting or keybinding question about Daintree ("how do I create a worktree", "what is a watcher", "where do I change X") — the matching documentation is retrieved for you and arrives with the turn already, so answering needs no skill and loading one only spends tokens. Do not use merely because an ordinary working request names Daintree — "rename my tabs" is daintree.terminals.rename — but DO use it when that operation has failed or the capability is missing, including an imperative "it's broken, get it working again", where it may co-load with the operational skill. Do not use for questions about the user's own codebase (use daintree.explore.repository), when a specific agent or spawn has gone wrong (use daintree.agents.recover-agent), to work out where a paused workflow left off (use daintree.orchestration.status-and-resume), or to investigate a red CI check (use daintree.ci.diagnose-and-fix).
risk: read
requiredTools:
  - daintree.status
  - daintree.listTools
  - tool.search
  - context.snapshot
---
Use when: something in Daintree is not working and the user wants to know why.

This skill is for LIVE STATE, not for documentation. A plain "how do I…" question arrives with the matching pages already retrieved and needs no runbook at all — if you are reading this on such a turn, just answer from the documentation block. What you are here for is the other case: the user expected something to happen and it did not.

Guessing is the failure mode this skill exists to prevent. Daintree's surface changes, and a diagnosis invented from a plausible-sounding memory is worse than "let me check", because the user acts on it.

Procedure:

1. READ THE STATE, don't theorise.
   - `daintree.status` reports the MCP link: endpoint, connected, transport, tool count, and the error text when it is down. It works WHILE disconnected, which is exactly when you need it.
   - `context.snapshot` for workspace state — worktrees, terminals, agents — when the complaint is about something that should be there and isn't.
   - A DISCONNECTED link explains almost every "nothing works" report at once: the control-plane tools (terminal, agent, worktree, git, forge) all stop, so say that first and give the reconnect step rather than diagnosing each missing capability separately. Note what still WORKS, because it changes what you can do next: `daintree.status` itself is designed to answer while disconnected, and documentation retrieval does not go through that link — so a how-to question can still be answered while the app is unreachable.

2. A MISSING TOOL is a real diagnosis, not a dead end. `daintree.listTools` marks each tool `callable`. A tool that exists but is `callable: false` was not offered THIS turn — say so and name what it does, rather than calling it and reporting a confusing failure. `tool.search({"query":"rename terminal"})` finds a capability by a couple of plain keywords when you know the job but not the name. If the capability genuinely does not exist, saying that IS the answer.

3. COMPARE against what SHOULD happen, then remediate. When the turn also carries a "Daintree documentation" block, that is the other half of the diagnosis: the useful output is the gap — "the docs say X happens when you Y; your link is disconnected, so it cannot". Cite any page you lean on by the full `URL:` line printed with it. If no documentation arrived, diagnose from the state alone. This is the one place that OVERRIDES the general permission to answer from memory with an unverified flag: a remembered "what should happen" is what turns a diagnosis into a wrong instruction the user then acts on, so say the docs did not cover it and report what you actually observed instead — and do not reconstruct the expected behaviour from project source either. Finish with the exact next step — a command, a setting, a reconnect — not a list of things to try.

4. DIAGNOSTICS TO PASTE. When the user wants something to attach to a bug report, give them the safe set: what `daintree.status` reported, the workspace shape from `context.snapshot`, and the exact error text. Never include the API key, in any form — not the value, not a prefix, not its length. If they ask you to echo the key to prove it is set, refuse and explain that no diagnostic here needs it.

Key questions are their own case, and `daintree.status` does NOT answer them: it reports the Daintree MCP link only — endpoint, connected, transport, tool count — which is a different connection from the model provider entirely. A connected MCP with a dead key looks perfectly healthy there. Point the user at the sign-in check the documentation describes rather than inferring one from a failed turn. Three outcomes that must not be conflated: a REJECTED key needs a new one, a key with no CREDIT needs topping up and is otherwise fine, and an UNREACHABLE provider is neither — never tell a user their key is invalid when we simply could not ask.

Notes:
- Answer the question asked. A user whose terminals stopped responding does not need an explanation of the worktree model.
- Never invent a settings key, flag, keybinding, or tool name. A near-miss name is worse than an admission, because the user will try it.

Report back: what you observed, what it should have been, and the one concrete step that fixes it.
