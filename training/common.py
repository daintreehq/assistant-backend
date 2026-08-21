"""Shared helpers for the local-selector training scripts.

Text rendering is NOT defined here. It lives in the shipped package
(``daintree_assistant_server.skills.local.documents``) and is imported below,
because the server can never import ``training/`` and therefore cannot be the
one to follow. Training follows serving, not the other way round: any drift
between how a conversation is rendered at train time and at request time
silently degrades accuracy without producing an error anywhere.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from pathlib import Path

from daintree_assistant_server.skills.local.documents import (
    ACTIVE_PREFIX,
    fit_query,
    query_document,
)


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


def training_messages(query: str, history: list[str] | None = None) -> list[_Msg]:
    """One corpus row's history + query as the typed messages the renderer takes."""
    messages: list[_Msg] = []
    for line in history or []:
        role, _, text = line.partition(":")
        role = role.strip().lower()
        messages.append(
            _Msg(role if role in ("user", "assistant", "tool") else "user", text.strip() or line)
        )
    messages.append(_Msg("user", query))
    return messages


def render_training_query(query: str, history: list[str] | None = None) -> str:
    """Build the query string exactly as the server will at request time."""
    return query_document(training_messages(query, history))


def build_query(row: dict[str, object], *, fits: Callable[[str], bool] | None = None) -> str:
    """Render one corpus row into the exact string a ranker will embed.

    Both stages and every evaluator go through this, so the ``[loaded: …]``
    state prefix can never be applied by one and forgotten by another. It was
    previously a private helper in the classifier trainer, which meant the
    retriever and the set-wise head each had to re-derive it correctly.

    ``fits`` is the encoder's token-cap test. Supply it and this takes the same
    overflow-shrink path the server does (``documents.fit_query``); omit it and
    a row is rendered at the default budgets. Both are correct for today's
    corpus — its longest row is 155 tokens against a cap of 256, so no row
    reaches the shrink path — and the parameter exists so that stays true by
    construction rather than by nobody having generated a long row yet. Serving
    shrinks; if training did not, the two would diverge on exactly the rows
    where the difference matters.
    """
    messages = training_messages(str(row["query"]), row.get("history"))  # type: ignore[arg-type]
    active = row.get("active_ids") or []
    prefix = ACTIVE_PREFIX.format(", ".join(active)) if active else ""  # type: ignore[arg-type]
    if fits is None:
        return prefix + query_document(messages)
    return prefix + fit_query(messages, fits=fits, prefix=prefix)


def corpus_rows(path: Path) -> list[dict]:
    """Load a jsonl corpus, skipping blank lines."""
    return [
        json.loads(line) for line in path.read_text().splitlines() if line.strip()
    ]


def device() -> str:
    """MPS when the Mac has it, else CPU. Training only — serving is ONNX."""
    try:
        import torch

        if torch.backends.mps.is_available():
            return "mps"
    except Exception:  # noqa: BLE001
        pass
    return "cpu"
