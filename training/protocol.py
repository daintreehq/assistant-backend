"""Keep the retired inbox protocol out of the current selector's lessons."""

from __future__ import annotations

import re
from collections.abc import Sequence

_RETIRED_API = re.compile(
    r"assistant\.(?:getInbox|reply|sendMessage)\b|"
    r"host\.(?:sendMessage|getTask|listTasks|cancelTask|startAssistant|listAssistants|readQuota)\b|"
    r"\b(?:inboxFetchedAt|wakeTypedAt|lane_unavailable)\b|delegated exchange update|"
    r"\bassistant inbox\b",
    re.IGNORECASE,
)
_INBOX_ROLES = re.compile(
    r"\blanes?\b|\bexchanges?\b|\benroll(?:ed|ing|ment)?\b", re.IGNORECASE
)
_HOST_CONTEXT = re.compile(r"\b(?:remote|hosts?|hub|inbox)\b", re.IGNORECASE)


def retired_inbox_reason(
    query: str, history: Sequence[str], runbook_ids: Sequence[str]
) -> str | None:
    text = " ".join((query, *history))
    if _RETIRED_API.search(text):
        return "retired inbox API or delivery event"
    host_context = any(r.startswith("daintree.hosts.") for r in runbook_ids)
    if (host_context or _HOST_CONTEXT.search(text)) and _INBOX_ROLES.search(text):
        return "retired host lane, enrollment or exchange model"
    if re.search(r"\bhub\b", text, re.IGNORECASE) and re.search(
        r"\b(?:our|its|the) inbox\b", text, re.IGNORECASE
    ):
        return "retired hub assistant inbox"
    return None
