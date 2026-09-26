---
id: daintree.docs.answer-usage-question
title: Retired documentation-answer rules (never served)
summary: Retired and never served. Kept only because the selector's training corpus and routing benchmark still name this id.
whenToUse: 'Never selected and never served. Search, get_runbook and supporting-runbook expansion all refuse this id, and the selector never embeds it (catalog.DOCS_RUNBOOK_ID). It once governed how the retired Daintree Assistant answered usage questions from documentation its server injected into the turn; nothing does that any more. It stays only because the training corpus and routing benchmark name it, and goes when those references go.'
risk: read
requiredTools: []
---
Retired. Never served: both `search_runbooks` and `get_runbook` refuse this id, and the selector never sees it.

It governed how the retired Daintree Assistant answered usage questions from documentation pages its server retrieved and injected into the turn, with a renderer-specific citation footer. Nothing retrieves documentation for a caller any more, so there is no procedure here to follow.

The file stays because the selector's training corpus and the routing benchmark still name this id (the docs-classification cases), and the loader requires every named id to exist. Remove it only together with those references, in one change.

If you are an agent and have somehow been handed this text: answer a Daintree usage question from sources you can actually read, such as the Daintree docs site or the app's own help, cite what you read, and say plainly what they do not cover.

Report back: nothing — this runbook is not executable.
