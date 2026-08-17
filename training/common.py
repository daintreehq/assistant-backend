"""Shared helpers for the local-selector training scripts.

Text rendering is NOT defined here. It lives in the shipped package
(``daintree_assistant_server.skills.local.documents``) and is imported below,
because the server can never import ``training/`` and therefore cannot be the
one to follow. Training follows serving, not the other way round: any drift
between how a conversation is rendered at train time and at request time
silently degrades accuracy without producing an error anywhere.
"""

from __future__ import annotations

from daintree_assistant_server.skills.local.documents import query_document


class _Msg:
    """Minimal duck-type of a ChatMessage, so training reuses the SERVING renderer.

    The training corpus stores history as plain "user: ..." strings while the
    server has typed messages. Rather than write a second renderer for the
    training side — which is exactly the drift this module exists to prevent —
    the strings are wrapped into something ``query_document`` already accepts.
    """

    __slots__ = ("role", "content")

    def __init__(self, role: str, content: str) -> None:
        self.role = role
        self.content = content


def render_training_query(query: str, history: list[str] | None = None) -> str:
    """Build the query string exactly as the server will at request time."""
    messages: list[_Msg] = []
    for line in history or []:
        role, _, text = line.partition(":")
        role = role.strip().lower()
        messages.append(
            _Msg(role if role in ("user", "assistant", "tool") else "user", text.strip() or line)
        )
    messages.append(_Msg("user", query))
    return query_document(messages)
