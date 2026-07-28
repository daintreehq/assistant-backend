---
id: daintree.terminals.rename
title: Rename terminal tabs
summary: Give terminals/agents clear tab titles — the fast, direct route (no tool hunting).
whenToUse: Use whenever the user wants to rename terminals/tabs, retitle agents, or "figure out what each of these terminals is doing and give them a title" — especially when several agents share the same name (e.g. all "Claude") and need to be told apart. The deliverable is new TITLES. Do not use when the user wants a report on what the agents are doing rather than a retitle (use daintree.orchestration.supervise-fleet), or a plain list of what terminals exist (use daintree.foundation).
risk: ui
tags:
  - daintree
  - terminal
  - rename
  - title
  - tab
  - agent
requiredTools:
  - context.snapshot
  - terminal.extract.json
  - terminal.rename
---
Use when: the user wants terminals/agents/tabs renamed or titled — e.g. "figure out what each of these is doing and give them a proper title", "rename these", "title the agents".

The tool is terminal.rename({ terminalId, name }). It is always callable by name — DO NOT use tool.search or daintree.listTools to find it, and DO NOT route it through daintree.call. Hunting for it is the slow path; this is the fast one.

Always pass BOTH terminalId AND a non-empty name: the typed wrapper marks both REQUIRED with additionalProperties:false, so a call missing either is schema-rejected outright — there is no focused-tab default and no interactive-dialog fallback to land in. Be explicit on every call.

Most tool calls in one response are dispatched ONE AFTER ANOTHER (only no-wait terminal.extract/.json batches run concurrently) — and for THIS job the fast path is still FEWER model calls, not a big batch: one question ("title each terminal") spans the whole cohort, so read every terminal in ONE terminal.extract.json over all the ids (a single small-model call), not one extract or summarize per terminal.

Procedure:
1. GET THE IDS. If you don't already have the terminal ids + current titles, call context.snapshot (it embeds the terminal list). If the user pointed at a subset ("these three"), keep only those ids.
2. UNDERSTAND — in ONE call. You need one answer PER terminal across a cohort, which is exactly what `terminal.extract.json` is for; a plain-TEXT `terminal.extract` over several terminalIds MERGES them into ONE answer instead of returning one per terminal. Call it over ALL the ids at once (it accepts up to 16 terminalIds) with an array schema keyed by terminalId:
   `terminal.extract.json({"terminalIds":[...],"instruction":"For EACH terminal, a short tab title (max ~6 words) describing what it is doing.","jsonSchema":"{\"type\":\"object\",\"properties\":{\"titles\":{\"type\":\"array\",\"items\":{\"type\":\"object\",\"properties\":{\"terminalId\":{\"type\":\"string\"},\"title\":{\"type\":\"string\"}},\"required\":[\"terminalId\",\"title\"]}}},\"required\":[\"titles\"]}"})`
   That is a single model read of the whole cohort, attributed per terminal. Skip this entirely for any terminal whose job you already know. The extract result is a LEAF — read it, don't re-extract or summarize it.
3. RENAME. Emit one terminal.rename per id: terminal.rename({ "terminalId": "<id>", "name": "<short title>" }). Keep names short and scannable, leading with the kind of work. terminal.rename is UI-only (no confirmation) and each call is a cheap local UI op, so renaming the whole set is quick even though the calls run in sequence.

Then report a one-line "what it's doing" for each terminal next to its new title.

Fast-path shape (three agents all titled "Claude"):
  context.snapshot()                               // → ids t1, t2, t3 + current titles
  terminal.extract.json({ terminalIds: ["t1","t2","t3"], ... })   // ONE call, one entry each — step 2's schema
  // then, from that one result:
  terminal.rename({ terminalId: "t1", name: "bug: allowlist gap in WorkspaceHostProcess" })
  terminal.rename({ terminalId: "t2", name: "merge pipeline: PRs #10779-84" })
  terminal.rename({ terminalId: "t3", name: "debug: simple-git editor guard" })

Notes:
- This skill is for terminals you did NOT name at spawn: pre-existing tabs, agents launched outside your control, or a fleet whose titles already collide. If YOU just spawned the terminal, you ALREADY named it via the spawn `title` (the tab reads `<Agent>: <title>`) — do not rename it again; set the right, distinct title at spawn instead. Renaming an agent you just spawned-and-titled is wasted effort.
- If the user told you what an EXISTING terminal is doing, skip the extract and rename it directly.
- Renaming is independent per terminal, so one failed rename never blocks the others — report which ones changed.

Report back: which terminals were renamed (old → new), and any rename that failed and was left unchanged.
