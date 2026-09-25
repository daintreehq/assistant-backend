---
id: daintree.docs.answer-usage-question
title: Answer from Daintree's documentation
summary: Answer from the Daintree documentation retrieved for this turn — quote what the pages actually say, name what they do not cover, and cite them as clickable URLs at the end of the reply.
whenToUse: NOT SELECTED BY YOU — the server pins this runbook itself, on any turn whose classification produced a documentation query, and it is hidden from the selector's catalog (config.DOCS_RUNBOOK_ID). Kept here because the loader requires it, and it records what the runbook governs — how to answer from retrieved Daintree documentation and cite it. That covers every turn where Daintree's own documented behaviour is part of the answer — a pure usage question ("how do I create a worktree", "what is a watcher", "where do I change the keybinding for…"), and equally a diagnostic or mid-workflow turn where the documented behaviour is the reference point ("why does it do X when I do Y", "this should be creating a branch"). It arrives ALONGSIDE whatever other runbook the turn is in rather than replacing it — it governs the citations, the other runbook governs the work. It does not decide what the turn is about, so it never competes with an operational runbook — "rename my terminals" is work whether or not the docs were searched.
risk: read
requiredTools: []
---
Retired. Never served: both `search_runbooks` and `get_runbook` refuse this id, and the selector never sees it.

It governed how the retired Daintree Assistant answered usage questions from documentation pages its server retrieved and injected into the turn, with a renderer-specific citation footer. Nothing retrieves documentation for a caller any more, so there is no procedure here to follow.

The file stays because the selector's training corpus and the routing benchmark still name this id (the docs-classification cases), and the loader requires every named id to exist. Remove it only together with those references, in one change.

If you are an agent and have somehow been handed this text: answer a Daintree usage question from sources you can actually read, such as the Daintree docs site or the app's own help, cite what you read, and say plainly what they do not cover.

Report back: nothing — this runbook is not executable.
