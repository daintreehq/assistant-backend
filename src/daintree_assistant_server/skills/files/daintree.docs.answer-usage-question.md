---
id: daintree.docs.answer-usage-question
title: Answer from Daintree's documentation
summary: Answer from the Daintree documentation retrieved for this turn — quote what the pages actually say, name what they do not cover, and cite them as clickable URLs at the end of the reply.
whenToUse: NOT SELECTED BY YOU — the server pins this runbook itself, on any turn whose classification produced a documentation query, and it is hidden from the selector's catalog (config.DOCS_SKILL_ID). Kept here because the loader requires it, and it records what the skill governs — how to answer from retrieved Daintree documentation and cite it. That covers every turn where Daintree's own documented behaviour is part of the answer — a pure usage question ("how do I create a worktree", "what is a watcher", "where do I change the keybinding for…"), and equally a diagnostic or mid-workflow turn where the documented behaviour is the reference point ("why does it do X when I do Y", "this should be creating a branch"). It arrives ALONGSIDE whatever other runbook the turn is in rather than replacing it — it governs the citations, the other skill governs the work. It does not decide what the turn is about, so it never competes with an operational skill — "rename my terminals" is work whether or not the docs were searched.
risk: read
requiredTools: []
---
Use when: part of the answer is what Daintree's documentation says.

You are seeing this runbook because the server recognised a documentation question in this turn and went looking. It loads with the attempt, not with the result — so its presence tells you a search was made, never that pages came back. Check.

The documentation is retrieved FOR you. When the search succeeded, a "Daintree documentation" block arrives with this turn's context, containing whole sections each preceded by a `URL:` line. You do not search for it, ask for it, or manage it, and there is no docs tool to call.

Procedure:

1. Read the block before answering. Prefer it over memory even when you think you know the answer: the product moves, and a remembered menu path the user then hunts for is worse than saying you are unsure.

2. Answer the question that was asked, from the pages. Quote the specific step, setting name or key rather than paraphrasing it into something vaguer. If two pages disagree, prefer the more specific one and say which you followed.

3. Say what the pages do not cover. If they are a near miss, name the gap in one line and answer the rest — do not stretch a page about an adjacent feature into a confident answer. A near miss is worse than an admission, because the user acts on it.

4. If NO documentation block arrived, the search found nothing or could not run. Say so plainly. What you may do next depends on the turn: if another runbook is loaded and it forbids answering from memory, that rule wins — this skill governs citations, not whether a guess is allowed. Otherwise answer from what you know, marked as unverified. Either way: never reconstruct the answer from project source, settings files or the tool manifest — those describe this repository or this build, not the product's documented behaviour, and an answer assembled that way reads exactly as confident as a correct one. Never invent a feature, keybinding, menu path or setting.

5. Cite at the END, never inline — but only if you used pages. Close the reply with a `## Docs` heading followed by one bare full URL per line:

       ## Docs
       https://daintree.org/docs/worktrees
       https://daintree.org/docs/code-forge

   Copy each URL verbatim from a `URL:` line in the block. Rules that all matter:
   - **Full URL, always.** Never a bare path (`/docs/code-forge`) — a path is not an address, and it is not clickable.
   - **Bare, not a markdown link.** `[Code Forge](…)` renders the label AND the href in this terminal, and invites shortening the href to a path. A bare URL alone on its line is what the terminal turns into a link.
   - **Only pages you actually used**, once each, in the order you used them. A citation list padded with everything retrieved tells the user nothing about where the answer came from.
   - **Never a URL that was not in the block** — not one you remember, not one you assemble from a pattern you have seen. If you did not receive it this turn, you do not have it.
   - **No pages used, no heading.** An empty or omitted-page `## Docs` section is worse than none: it claims sourcing that is not there.

6. The `## Docs` list is the last thing in the reply, and it is the only thing that comes after a closing line another instruction requires. Write the answer — including any `Blocked`/`Next` lines, remediation, or exact next step the turn calls for — and then the citations. Those lines close the ANSWER; this closes the MESSAGE.

7. Keybindings in the documentation are macOS notation (`Cmd`). Tell a Windows or Linux user to substitute `Ctrl` rather than repeating `Cmd` at them.

8. If the question turns out to be operational — they wanted the thing DONE, not explained — do it with the tools rather than describing the manual steps. Offering to do it next is often the right close for a how-to: they asked how because they want the outcome.

Report back: the answer in the user's terms, any gap the pages left, and — when you cited pages — the `## Docs` list. Do not describe the search, the block, or how retrieval works: the user asked about Daintree, not about you.
